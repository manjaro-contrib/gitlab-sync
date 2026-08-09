"""Report which mirrors still reach the Manjaro repositories, and why not.

Runs `python -m sync.stale_main`. Writes nothing unless --apply is given.

SECURITY: classification runs `makepkg --printsrcinfo`, which sources the
PKGBUILD and therefore executes shell code from the source repository. Keep
this out of any job that holds a token worth stealing.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import stale
from .github import GitHub
from .gitlab import list_projects
from .main import Progress, _log
from .stale import ALL_TOPICS, Result, Verdict

# Each project costs one GitLab fetch plus one branch compare request per built
# package name. This stays well under the rate that made GitLab answer 429.
WORKERS = 6


def _retag(gh: GitHub, result: Result) -> bool:
    """Give a mirror exactly the reason topic its verdict calls for."""
    name = result.project.name
    current = gh.get_topics(name)
    keep = [t for t in current if t not in ALL_TOPICS]
    wanted = keep + ([result.topic] if result.topic else [])
    if sorted(wanted) == sorted(current):
        return False
    gh.set_topics(name, wanted)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sync.stale_main")
    parser.add_argument("--apply", action="store_true",
                        help="write the reason topics (default: report only)")
    parser.add_argument("--limit", type=int, default=0, metavar="N",
                        help="examine at most N projects (0 = all)")
    parser.add_argument("--only", action="append", default=[], metavar="PATH",
                        help="restrict to a GitLab path (repeatable)")
    parser.add_argument("--min-quiet-days", type=int, default=stale.MIN_QUIET_DAYS,
                        metavar="N", help="a candidate must be untouched this long")
    args = parser.parse_args(argv)

    token = os.environ.get("GH_MIRROR_TOKEN")
    if args.apply and not token:
        print("GH_MIRROR_TOKEN is not set, needed for --apply", file=sys.stderr)
        return 2

    _log("enumerating GitLab projects...")
    projects = list_projects()
    if args.only:
        wanted = set(args.only)
        projects = [p for p in projects if p.path in wanted]
        missing = wanted - {p.path for p in projects}
        if missing:
            print(f"unknown GitLab paths: {sorted(missing)}", file=sys.stderr)
            return 2
    else:
        projects = [p for p in projects if not p.archived]
    if args.limit:
        projects = projects[: args.limit]
    _log(f"examining {len(projects)} projects")

    results: list[Result] = []
    failures = 0
    progress = Progress("classifying", len(projects))
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(stale.classify, p): p for p in projects}
        for fut in as_completed(futures):
            project = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                failures += 1
                print(f"error {project.path}: {str(e)[:160]}",
                      file=sys.stderr, flush=True)
            progress.tick()

    counts = Counter(r.verdict.value for r in results)
    candidates = [r for r in results
                  if r.verdict is Verdict.BEHIND
                  and r.quiet_days >= args.min_quiet_days]

    if candidates:
        _log(f"\n{len(candidates)} behind stable and quiet "
             f">= {args.min_quiet_days}d:")
        for r in sorted(candidates, key=lambda r: -r.quiet_days):
            built = ", ".join(f"{n}={v}"
                              for n, v in (r.branch_versions or {}).items())
            _log(f"  {r.project.path}\n"
                 f"      repo={r.repo_version}  stable: {built}  "
                 f"quiet={r.quiet_days}d")

    tagged = 0
    if args.apply:
        gh = GitHub(token)
        for r in results:
            try:
                tagged += _retag(gh, r)
            except Exception as e:
                failures += 1
                print(f"error tagging {r.project.name}: {str(e)[:160]}",
                      file=sys.stderr, flush=True)
    else:
        _log("\nreport only: re-run with --apply to write the reason topics")

    _log("\n" + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
         + f" candidates={len(candidates)} tagged={tagged} errors={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
