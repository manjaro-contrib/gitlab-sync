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
MAX_DESCRIPTION = 350


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str):
        self._token = token
        self._create_lock = threading.Lock()
        self._last_create = 0.0

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
                if e.code in (403, 429) and attempt < MAX_ATTEMPTS:
                    delay = _throttle_delay(e)
                    if delay is not None:
                        time.sleep(delay)
                        continue
                raise GitHubError(
                    f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')[:500]}"
                ) from e
        raise GitHubError(f"{method} {path}: giving up after {MAX_ATTEMPTS} attempts")

    def get_repo(self, name: str) -> dict | None:
        return self._request("GET", f"/repos/{ORG}/{name}", ok_404=True)

    def create_repo(self, p: Project) -> dict:
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

    def edit_repo(self, name: str, **fields) -> dict:
        return self._request("PATCH", f"/repos/{ORG}/{name}", fields) or {}

    def set_archived(self, name: str, archived: bool) -> None:
        self._request("PATCH", f"/repos/{ORG}/{name}", {"archived": archived})

    def get_topics(self, name: str) -> list[str]:
        body = self._request("GET", f"/repos/{ORG}/{name}/topics") or {}
        return body.get("names", [])

    def set_topics(self, name: str, topics: list[str]) -> None:
        self._request("PUT", f"/repos/{ORG}/{name}/topics", {"names": topics})


def _throttle_delay(e: urllib.error.HTTPError) -> float | None:
    retry_after = e.headers.get("Retry-After")
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            return None
    if e.headers.get("x-ratelimit-remaining") == "0":
        reset = e.headers.get("x-ratelimit-reset")
        if reset:
            try:
                return max(0.0, float(reset) - time.time()) + 5.0
            except ValueError:
                return None
    return None
