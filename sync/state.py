"""Ref digests and the committed state file that make runs incremental."""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .gitlab import Project

STATE_PATH = Path("state/refs.json")
VERSION = 1
LS_REMOTE_TIMEOUT = 120

# gitlab.manjaro.org enforces `throttle_unauthenticated_git_http` at 60 requests
# per minute (confirmed via the ratelimit-* headers on info/refs). It is a rate
# budget, not a concurrency cap: bursts of any width pass until the budget is
# spent, after which every fetch returns HTTP 429 "remote: Retry later". A full
# 2039-project sweep therefore needs a process-wide limiter, not just backoff.
GIT_RATE_PER_MIN = 55
LS_REMOTE_ATTEMPTS = 6
LS_REMOTE_BACKOFF = 30.0


class _RateLimiter:
    """Process-wide minimum interval between anonymous GitLab git fetches."""

    def __init__(self, per_minute: int):
        self._interval = 60.0 / per_minute
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next = now + self._interval

    def penalize(self, seconds: float) -> None:
        """Push the whole pool back after a 429, so every thread backs off."""
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


gitlab_rate = _RateLimiter(GIT_RATE_PER_MIN)


def load(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return {"version": VERSION, "projects": {}}
    return json.loads(path.read_text())


def save(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def is_throttled(stderr: str) -> bool:
    return "429" in stderr or "Retry later" in stderr


def _ls_remote(url: str) -> str:
    for attempt in range(1, LS_REMOTE_ATTEMPTS + 1):
        gitlab_rate.acquire()
        proc = subprocess.run(
            ["git", "ls-remote", "--heads", "--tags", url],
            text=True, capture_output=True, timeout=LS_REMOTE_TIMEOUT,
        )
        if proc.returncode == 0:
            return proc.stdout
        if attempt == LS_REMOTE_ATTEMPTS or not is_throttled(proc.stderr):
            raise subprocess.CalledProcessError(
                proc.returncode, proc.args, proc.stdout, proc.stderr
            )
        gitlab_rate.penalize(LS_REMOTE_BACKOFF * attempt + random.uniform(0, 5))
    raise AssertionError("unreachable")


def remote_digest(url: str) -> str | None:
    lines = sorted(line for line in _ls_remote(url).splitlines() if line.strip())
    if not lines:
        return None
    return "sha256:" + hashlib.sha256("\n".join(lines).encode()).hexdigest()


@dataclass(frozen=True)
class Work:
    project: Project
    digest: str
    git: bool
    topics: bool
    archive: bool

    @property
    def any(self) -> bool:
        return self.git or self.topics or self.archive


def evaluate(p: Project, digest: str, state: dict) -> Work:
    entry = state.get("projects", {}).get(p.path)
    if entry is None:
        return Work(p, digest, git=True, topics=True, archive=True)
    return Work(
        p,
        digest,
        git=entry.get("digest") != digest,
        topics=entry.get("topics") != list(p.topics),
        archive=entry.get("archived") != p.archived,
    )


def record(state: dict, p: Project, digest: str, synced_at: str) -> None:
    state.setdefault("projects", {})[p.path] = {
        "digest": digest,
        "topics": list(p.topics),
        "archived": p.archived,
        "last_activity_at": p.last_activity_at,
        "synced_at": synced_at,
    }


def can_skip_scan(p: Project, state: dict) -> bool:
    """True when enumeration alone proves nothing can have changed.

    GitLab bumps `last_activity_at` on push, so an unchanged value means the refs
    are unchanged and the ~1.7 s `ls-remote` is pure waste. Only safe when the
    stored entry is a complete success: topics and the archived bit are mirrored
    independently of refs, and a project whose previous run failed must be
    retried even though upstream has been quiet since.
    """
    entry = state.get("projects", {}).get(p.path)
    if entry is None:
        return False
    return (
        bool(p.last_activity_at)
        and entry.get("last_activity_at") == p.last_activity_at
        and entry.get("topics") == list(p.topics)
        and entry.get("archived") == p.archived
    )
