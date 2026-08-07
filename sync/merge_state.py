"""Merge two versions of state/refs.json.

Concurrent runs both rewrite the whole file, so git sees a content conflict on
almost every overlapping run. The conflict is spurious: the file is a cache
keyed by GitLab project path, and two runs that touched different projects hold
complementary truths. Union the entries, keeping whichever observation is newer
per project.

Usage: python -m sync.merge_state OURS THEIRS OUT
"""

from __future__ import annotations

import json
import sys


def merge(ours: dict, theirs: dict) -> dict:
    merged = dict(theirs.get("projects", {}))
    for path, entry in ours.get("projects", {}).items():
        other = merged.get(path)
        if other is None or entry.get("synced_at", "") >= other.get("synced_at", ""):
            merged[path] = entry
    return {"version": ours.get("version") or theirs.get("version") or 1,
            "projects": merged}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    ours_path, theirs_path, out_path = argv
    with open(ours_path) as f:
        ours = json.load(f)
    with open(theirs_path) as f:
        theirs = json.load(f)
    with open(out_path, "w") as f:
        json.dump(merge(ours, theirs), f, indent=2, sort_keys=True)
        f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
