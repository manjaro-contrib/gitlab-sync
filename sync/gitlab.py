"""Enumerate the public projects to mirror from gitlab.manjaro.org."""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

GITLAB = "https://gitlab.manjaro.org"
GROUPS = [
    "applications",
    "artwork",
    "documentation",
    "manjaro-arm",
    "packages",
    "profiles-and-settings",
    "release-plan",
    "security-overlay",
    "tools",
    "web",
]

TOPIC_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")

API_TIMEOUT = 60
API_ATTEMPTS = 5
API_BACKOFF = 3.0

# Stamped on every mirror so the org can be filtered down to repos this action
# owns -- the org also holds hand-made repos that must never be touched.
MARKER_TOPIC = "gitlab-mirror"

MAX_NAME_LEN = 100
MAX_TOPIC_LEN = 50
MAX_TOPICS = 20


@dataclass(frozen=True)
class Project:
    path: str
    name: str
    description: str
    default_branch: str | None
    archived: bool
    last_activity_at: str
    topics: tuple[str, ...]

    @property
    def clone_url(self) -> str:
        return f"{GITLAB}/{self.path}.git"

    @property
    def web_url(self) -> str:
        """Where the mirror came from, shown as the GitHub homepage."""
        return f"{GITLAB}/{self.path}"


def _get_json(url: str) -> list[dict]:
    """Fetch one API page, retrying transient failures.

    Enumeration is the first phase of a multi-hour run, and gitlab.manjaro.org
    intermittently times out or 5xxs under load. Without retries a single blip
    aborts the whole sync before any work happens.
    """
    last: Exception | None = None
    for attempt in range(1, API_ATTEMPTS + 1):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
        if attempt < API_ATTEMPTS:
            time.sleep(API_BACKOFF * attempt + random.uniform(0, 2))
    raise SystemExit(f"GitLab API failed after {API_ATTEMPTS} attempts: {url}: {last}")


def _group_projects(group: str) -> list[dict]:
    out: list[dict] = []
    page = 1
    while True:
        url = (
            f"{GITLAB}/api/v4/groups/{group}/projects"
            f"?include_subgroups=true&per_page=100&page={page}"
        )
        batch = _get_json(url)
        if not batch:
            return out
        out.extend(batch)
        page += 1


def clean_description(text: str) -> str:
    """Flatten a GitLab description into something GitHub accepts.

    GitLab descriptions are multi-line markdown. GitHub rejects any control
    character in the field with 422 "description control characters are not
    allowed", which fails repository creation outright. Truncation stays in the
    GitHub client, next to the limit it enforces.
    """
    return " ".join(text.split())


def _to_project(raw: dict) -> Project:
    path = raw["path_with_namespace"]
    topics = (MARKER_TOPIC, *path.split("/")[:-1])
    p = Project(
        path=path,
        name=path.replace("/", "-"),
        description=clean_description(raw.get("description") or ""),
        default_branch=raw.get("default_branch"),
        archived=bool(raw.get("archived")),
        last_activity_at=raw.get("last_activity_at") or "",
        topics=topics,
    )
    _validate(p)
    return p


def _validate(p: Project) -> None:
    def fail(reason: str) -> None:
        raise SystemExit(f"unmappable project {p.path}: {reason}")

    if not NAME_RE.fullmatch(p.name):
        fail(f"flattened name {p.name!r} has characters GitHub rejects")
    if len(p.name) > MAX_NAME_LEN:
        fail(f"flattened name is {len(p.name)} chars, GitHub allows {MAX_NAME_LEN}")
    if len(p.topics) > MAX_TOPICS:
        fail(f"{len(p.topics)} namespace segments, GitHub allows {MAX_TOPICS} topics")
    for t in p.topics:
        if not TOPIC_RE.fullmatch(t):
            fail(f"namespace segment {t!r} is not a valid GitHub topic")
        if len(t) > MAX_TOPIC_LEN:
            fail(f"namespace segment {t!r} is {len(t)} chars, GitHub allows {MAX_TOPIC_LEN}")


MAX_AGE_DAYS = 730


def is_stale(p: Project, max_age_days: int = MAX_AGE_DAYS) -> bool:
    """True for projects the mirror deliberately leaves behind.

    Archived upstream, or untouched for over two years. Both signal a project
    nobody is maintaining, and mirroring them costs creation quota and scan time
    that the live packages need.
    """
    if p.archived:
        return True
    if not max_age_days or not p.last_activity_at:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    try:
        seen = datetime.fromisoformat(p.last_activity_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return seen < cutoff


def list_projects() -> list[Project]:
    with ThreadPoolExecutor(max_workers=12) as pool:
        batches = pool.map(_group_projects, GROUPS)

    by_path: dict[str, Project] = {}
    for batch in batches:
        for raw in batch:
            p = _to_project(raw)
            by_path[p.path] = p

    by_name: dict[str, list[str]] = {}
    for p in by_path.values():
        by_name.setdefault(p.name, []).append(p.path)
    collisions = {n: sorted(paths) for n, paths in by_name.items() if len(paths) > 1}
    if collisions:
        detail = "; ".join(f"{n} <- {paths}" for n, paths in sorted(collisions.items()))
        raise SystemExit(f"flattened name collisions: {detail}")

    return sorted(by_path.values(), key=lambda p: p.path)
