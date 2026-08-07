"""Mirror gitlab.manjaro.org projects into the manjaro-contrib GitHub org."""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import state as state_mod
from .github import ORG, GitHub, SecondaryLimit
from .gitlab import MAX_AGE_DAYS, Project, is_stale, list_projects

# Throughput is bounded by GitLab's 55/min git rate budget (see state.gitlab_rate),
# not by worker count; these widths just keep the budget saturated.
LS_REMOTE_WORKERS = 8
SYNC_WORKERS = 4
CLONE_ATTEMPTS = 4
CLONE_BACKOFF = 5.0
FLUSH_EVERY = 25

# Both long phases are otherwise silent for over an hour, which is
# indistinguishable from a hang in CI logs.
PROGRESS_EVERY = 25


def _log(msg: str) -> None:
    print(msg, flush=True)


class Progress:
    """Periodic 'n/total, rate, ETA' line for a long phase."""

    def __init__(self, label: str, total: int, every: int = PROGRESS_EVERY):
        self._label = label
        self._total = total
        self._every = every
        self._done = 0
        self._start = time.monotonic()
        self._lock = threading.Lock()
        _log(f"{label}: 0/{total}")

    def tick(self, note: str = "") -> None:
        with self._lock:
            self._done += 1
            done = self._done
            elapsed = time.monotonic() - self._start
        if done % self._every and done != self._total:
            return
        rate = done / elapsed * 60 if elapsed else 0.0
        eta = (self._total - done) / (done / elapsed) if done and elapsed else 0.0
        _log(f"{self._label}: {done}/{self._total} "
             f"({rate:.0f}/min, eta {eta / 60:.0f}m){note}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_git(args: list[str]) -> None:
    subprocess.run(args, check=True, text=True, capture_output=True)


def _clone_mirror(url: str, tmpdir: str) -> None:
    for attempt in range(1, CLONE_ATTEMPTS + 1):
        state_mod.gitlab_rate.acquire()
        proc = subprocess.run(
            ["git", "clone", "--mirror", url, tmpdir],
            text=True, capture_output=True,
        )
        if proc.returncode == 0:
            return
        if attempt == CLONE_ATTEMPTS or not state_mod.is_throttled(proc.stderr):
            raise subprocess.CalledProcessError(
                proc.returncode, proc.args, proc.stdout, proc.stderr
            )
        state_mod.gitlab_rate.penalize(CLONE_BACKOFF * attempt + random.uniform(0, 5))


def _clone_and_push(p: Project, token: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        _clone_mirror(p.clone_url, tmpdir)
        _run_git([
            "git", "-C", tmpdir, "push", "--prune", "--force",
            f"https://x-access-token:{token}@github.com/{ORG}/{p.name}.git",
            "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
        ])


class Syncer:
    def __init__(self, gh: GitHub, token: str, state: dict):
        self._gh = gh
        self._token = token
        self._state = state
        self._lock = threading.Lock()
        self._since_flush = 0

    def sync(self, work: state_mod.Work) -> None:
        p = work.project
        repo = self._gh.get_repo(p.name) or self._gh.create_repo(p)

        if repo.get("archived"):
            self._gh.set_archived(p.name, False)

        if work.git:
            _clone_and_push(p, self._token)

        if p.default_branch and p.default_branch != repo.get("default_branch"):
            self._gh.edit_repo(p.name, default_branch=p.default_branch)

        if work.topics:
            self._gh.set_topics(p.name, list(p.topics))

        if p.archived:
            self._gh.set_archived(p.name, True)

        self._record(p, work.digest)

    def _record(self, p: Project, digest: str) -> None:
        with self._lock:
            state_mod.record(self._state, p, digest, _now())
            self._since_flush += 1
            if self._since_flush >= FLUSH_EVERY:
                state_mod.save(self._state)
                self._since_flush = 0

    def flush(self) -> None:
        with self._lock:
            state_mod.save(self._state)
            self._since_flush = 0


def _digest_all(projects: list[Project]) -> tuple[dict[str, str | None], list[str]]:
    digests: dict[str, str | None] = {}
    failures: list[str] = []
    progress = Progress("scanning refs", len(projects))
    with ThreadPoolExecutor(max_workers=LS_REMOTE_WORKERS) as pool:
        futures = {pool.submit(state_mod.remote_digest, p.clone_url): p for p in projects}
        for fut in as_completed(futures):
            p = futures[fut]
            try:
                digests[p.path] = fut.result()
            except Exception as e:
                failures.append(p.path)
                print(f"ls-remote failed {p.path}: {_brief(e)}", file=sys.stderr, flush=True)
            progress.tick()
    return digests, failures


def _brief(e: Exception) -> str:
    if isinstance(e, subprocess.CalledProcessError):
        return f"{' '.join(e.cmd[:3])}: {(e.stderr or '').strip()[:300]}"
    return str(e)[:300]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sync")
    parser.add_argument("--limit", type=int, default=0,
                        help="max projects needing work to process (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true",
                        help="enumerate and digest only; write and push nothing")
    parser.add_argument("--only", action="append", default=[], metavar="PATH",
                        help="restrict to a GitLab path (repeatable)")
    parser.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS,
                        metavar="N",
                        help="skip projects untouched for N days (0 = no age limit)")
    parser.add_argument("--include-stale", action="store_true",
                        help="mirror archived and long-dormant projects too")
    args = parser.parse_args(argv)

    token = os.environ.get("GH_MIRROR_TOKEN")
    if not token and not args.dry_run:
        print("GH_MIRROR_TOKEN is not set", file=sys.stderr)
        return 2

    _log("enumerating GitLab projects...")
    projects = list_projects()
    _log(f"enumerated {len(projects)} projects")

    if not args.include_stale:
        live = [p for p in projects if not is_stale(p, args.max_age_days)]
        dropped = len(projects) - len(live)
        _log(f"skipping {dropped} stale (archived or untouched "
             f"> {args.max_age_days}d); {len(live)} in scope")
        projects = live
    if args.only:
        wanted = set(args.only)
        projects = [p for p in projects if p.path in wanted]
        missing = wanted - {p.path for p in projects}
        if missing:
            print(f"unknown GitLab paths: {sorted(missing)}", file=sys.stderr)
            return 2

    state = state_mod.load()
    _log(f"{len(state.get('projects', {}))} projects in state; "
         f"GitLab allows ~{state_mod.GIT_RATE_PER_MIN} git req/min")

    quiet, to_scan = [], []
    for p in projects:
        (quiet if state_mod.can_skip_scan(p, state) else to_scan).append(p)
    if quiet:
        _log(f"{len(quiet)} unchanged since last sync (last_activity_at), "
             f"scanning {len(to_scan)}")
    digests, failed_paths = _digest_all(to_scan)

    empty = 0
    pending: list[state_mod.Work] = []
    skipped = len(quiet)
    for p in to_scan:
        if p.path not in digests:
            continue
        digest = digests[p.path]
        if digest is None:
            empty += 1
            print(f"empty {p.path}")
            continue
        work = state_mod.evaluate(p, digest, state)
        if work.any:
            pending.append(work)
        else:
            skipped += 1

    # Most-recently-active first, so a capped run syncs what actually moved
    # rather than whatever sorts early by path.
    pending.sort(key=lambda w: w.project.last_activity_at, reverse=True)

    if args.limit > 0 and len(pending) > args.limit:
        _log(f"limiting to {args.limit} of {len(pending)} projects needing work")
        pending = pending[: args.limit]

    _log(f"{len(pending)} need work, {skipped} unchanged, {empty} empty")

    if args.dry_run:
        for work in pending:
            flags = ",".join(
                f for f, on in (("git", work.git), ("topics", work.topics),
                                ("archive", work.archive)) if on
            )
            print(f"would sync {work.project.path} [{flags}]")
        print(f"synced=0 pending={len(pending)} skipped={skipped} "
              f"empty={empty} failed={len(failed_paths)} (dry-run)")
        return 1 if failed_paths else 0

    gh = GitHub(token)
    syncer = Syncer(gh, token, state)
    synced = 0
    failures = list(failed_paths)
    blocked: list[str] = []
    progress = Progress("syncing", len(pending)) if pending else None
    with ThreadPoolExecutor(max_workers=SYNC_WORKERS) as pool:
        futures = {pool.submit(syncer.sync, w): w for w in pending}
        for fut in as_completed(futures):
            work = futures[fut]
            try:
                fut.result()
                synced += 1
            except SecondaryLimit:
                blocked.append(work.project.path)
            except Exception as e:
                failures.append(work.project.path)
                print(f"failed {work.project.path}: {_brief(e)}",
                      file=sys.stderr, flush=True)
            if progress:
                progress.tick(f" failed={len(failures)}" if failures else "")
    syncer.flush()

    if blocked:
        _log(f"deferred {len(blocked)} projects: GitHub secondary rate limit on "
             f"repo creation (500/h). They are retried next run.")

    topics_only = sum(1 for w in pending if not w.git and w.topics)
    archived_changed = sum(1 for w in pending if w.archive)
    print(f"synced={synced} topics_only={topics_only} "
          f"archived_changed={archived_changed} skipped={skipped} "
          f"empty={empty} deferred={len(blocked)} failed={len(failures)}")
    return 1 if failures else 0
