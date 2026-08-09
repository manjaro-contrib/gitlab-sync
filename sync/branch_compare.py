"""Read package versions from manjaristas.org branch compare.

The site renders a CSS grid, not a table, so the versions are recovered with a
regular expression per branch. There is no JSON API.
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BRANCH_COMPARE = "https://manjaristas.org/branch_compare"
BRANCHES = ("stable", "testing", "unstable")

TIMEOUT = 45
ATTEMPTS = 3
BACKOFF = 3.0

_GRID = "grid grid-cols"
_CELL_RE = re.compile(r"<div[^>]*>(.*?)</div>", re.S)
_TAG_RE = re.compile(r"<[^>]*>")


def _get(url: str) -> str:
    last: Exception | None = None
    for attempt in range(1, ATTEMPTS + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "gitlab-sync"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            if attempt < ATTEMPTS:
                time.sleep(BACKOFF * attempt)
    raise RuntimeError(f"branch compare failed: {url}: {last}")


def versions(package: str, arm: bool = False) -> dict[str, str]:
    """Return {branch: version} for one exact package name.

    An empty dict means the package is in no branch. The query is anchored, so
    a name is never confused with a longer one that contains it.
    """
    query = urllib.parse.quote("^" + re.escape(package) + "$")
    url = f"{BRANCH_COMPARE}?q={query}" + ("&arm=on" if arm else "")
    body = _get(url)
    grid = body[body.find(_GRID):] if _GRID in body else ""
    found: dict[str, str] = {}
    for cell in _CELL_RE.findall(grid):
        text = " ".join(html.unescape(_TAG_RE.sub(" ", cell)).split())
        for branch in BRANCHES:
            match = re.search(branch + r"\s*:\s*(\S+)", text)
            if match:
                found.setdefault(branch, match.group(1))
    return found
