"""Minimal GitHub REST client for the mirror org, with rate-limit handling."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from .gitlab import Project

GITHUB_API = "https://api.github.com"
ORG = "manjaro-contrib"

MAX_ATTEMPTS = 5
CREATE_INTERVAL = 1.0
SECONDARY_LIMIT_DELAY = 60.0
MAX_DESCRIPTION = 350

# Org automation stamps freshly created manjaro-contrib repos with its own topics
# (`arch`, `package`) a second or two after creation, clobbering ours. Setting
# topics once is therefore not enough on a new repo: confirm they stuck, and
# re-apply if something raced us.
TOPIC_CONFIRM_ATTEMPTS = 4
TOPIC_CONFIRM_DELAY = 4.0


class GitHubError(RuntimeError):
    pass


class SecondaryLimit(GitHubError):
    """GitHub is refusing new content creation for this token, for now."""


class GitHub:
    def __init__(self, token: str):
        self._token = token
        self._create_lock = threading.Lock()
        self._last_create = 0.0
        self._secondary_blocked = False

    def _request(self, method: str, path: str, body: dict | None = None,
                 ok_404: bool = False) -> dict | None:
        url = f"{GITHUB_API}{path}"
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self._token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                if e.code == 404 and ok_404:
                    return None
                detail = e.read().decode(errors="replace")
                if e.code in (403, 429) and attempt < MAX_ATTEMPTS:
                    delay = _throttle_delay(e, detail)
                    if delay is not None:
                        if "secondary rate limit" in detail.lower():
                            self._secondary_blocked = True
                        time.sleep(delay)
                        continue
                raise GitHubError(f"{method} {path} -> {e.code}: {detail[:500]}") from e
        raise GitHubError(f"{method} {path}: giving up after {MAX_ATTEMPTS} attempts")

    def get_repo(self, name: str) -> dict | None:
        return self._request("GET", f"/repos/{ORG}/{name}", ok_404=True)

    def create_repo(self, p: Project) -> dict:
        if self._secondary_blocked:
            raise SecondaryLimit("secondary rate limit reached earlier this run")
        body = {
            "name": p.name,
            "description": p.description[:MAX_DESCRIPTION],
            "private": False,
            "auto_init": False,
            "has_issues": False,
            "has_wiki": False,
            "has_projects": False,
        }
        self._pace_create()
        try:
            return self._request("POST", f"/orgs/{ORG}/repos", body) or {}
        except GitHubError as e:
            if " -> 422:" not in str(e):
                raise
            existing = self.get_repo(p.name)
            if existing is None:
                raise
            return existing

    def _pace_create(self) -> None:
        with self._create_lock:
            wait = CREATE_INTERVAL - (time.monotonic() - self._last_create)
            if wait > 0:
                time.sleep(wait)
            self._last_create = time.monotonic()

    def disable_actions(self, name: str) -> None:
        """Stop mirrored workflows from ever running.

        These are backups. Upstream repos carry their own `.github/workflows`,
        and GitHub would happily schedule them here -- running Manjaro's CI in a
        mirror org on every push and cron. Best effort: a repo archived moments
        later, or one the token cannot administer, must not fail the sync.
        """
        try:
            self._request("PUT", f"/repos/{ORG}/{name}/actions/permissions",
                          {"enabled": False})
        except GitHubError as e:
            print(f"note: could not disable actions on {name}: {e}", flush=True)

    def edit_repo(self, name: str, **fields) -> dict:
        return self._request("PATCH", f"/repos/{ORG}/{name}", fields) or {}

    def set_archived(self, name: str, archived: bool) -> None:
        self._request("PATCH", f"/repos/{ORG}/{name}", {"archived": archived})

    def get_topics(self, name: str) -> list[str]:
        body = self._request("GET", f"/repos/{ORG}/{name}/topics") or {}
        return body.get("names", [])

    def set_topics(self, name: str, topics: list[str]) -> None:
        """Set the topics we own, and keep any topic somebody else added.

        The endpoint replaces the whole set, and we compute only the namespace
        topics. Without this, a rewrite silently deletes markers applied by
        other tooling, such as the stale-candidate topic.
        """
        ours = set(topics)
        foreign = [t for t in self.get_topics(name) if t not in ours]
        topics = list(topics) + foreign
        want = sorted(topics)
        for attempt in range(1, TOPIC_CONFIRM_ATTEMPTS + 1):
            self._request("PUT", f"/repos/{ORG}/{name}/topics", {"names": topics})
            if attempt == TOPIC_CONFIRM_ATTEMPTS:
                return
            time.sleep(TOPIC_CONFIRM_DELAY)
            if sorted(self.get_topics(name)) == want:
                return


def _throttle_delay(e: urllib.error.HTTPError, body: str = "") -> float | None:
    retry_after = e.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            return None
    # Secondary-limit blocks frequently carry neither Retry-After nor an
    # exhausted primary quota; without this they look like hard failures.
    if "secondary rate limit" in body.lower():
        return SECONDARY_LIMIT_DELAY
    if e.headers.get("x-ratelimit-remaining") == "0":
        reset = e.headers.get("x-ratelimit-reset")
        if reset:
            try:
                return max(0.0, float(reset) - time.time()) + 5.0
            except ValueError:
                return None
    return None
