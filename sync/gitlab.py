"""Enumerate the public projects to mirror from gitlab.manjaro.org."""

from __future__ import annotations

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

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
    topics: tuple[str, ...]

    @property
    def clone_url(self) -> str:
        return f"{GITLAB}/{self.path}.git"


def _get_json(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


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


def _to_project(raw: dict) -> Project:
    path = raw["path_with_namespace"]
    topics = tuple(path.split("/")[:-1])
    p = Project(
        path=path,
        name=path.replace("/", "-"),
        description=raw.get("description") or "",
        default_branch=raw.get("default_branch"),
        archived=bool(raw.get("archived")),
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
