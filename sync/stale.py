"""Find mirrored packages that no longer reach the Manjaro repositories.

A package whose PKGBUILD sits behind the version in the stable branch, and
which nobody has touched for a month, is probably not shipping any more. That
is a hint for a human, not a fact, so a candidate is only marked with a topic.
Nothing is archived and nothing is deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from . import branch_compare, pkgbuild
from .gitlab import Project

MIN_QUIET_DAYS = 30

TOPIC_PREFIX = "repo-"


class Verdict(str, Enum):
    SHIPPING = "shipping"        # some built package matches the repository
    BEHIND = "behind"            # every built package is older: the candidate
    AHEAD = "ahead"              # repository is newer, a build is pending
    ABSENT = "absent"            # in no branch at all
    NOT_A_PACKAGE = "not-a-package"
    UNPARSEABLE = "unparseable"


# One topic per reason, so the GitHub UI can filter by why a mirror was
# flagged. SHIPPING gets none: a healthy package needs no mark, and tagging
# every mirror would bury the interesting ones.
TOPICS = {
    Verdict.BEHIND: TOPIC_PREFIX + "behind-stable",
    Verdict.AHEAD: TOPIC_PREFIX + "build-pending",
    Verdict.ABSENT: TOPIC_PREFIX + "not-in-branches",
    Verdict.UNPARSEABLE: TOPIC_PREFIX + "unparseable-pkgbuild",
    Verdict.NOT_A_PACKAGE: TOPIC_PREFIX + "no-pkgbuild",
}

ALL_TOPICS = frozenset(TOPICS.values())


@dataclass(frozen=True)
class Result:
    project: Project
    verdict: Verdict
    repo_version: str | None = None
    branch_versions: dict[str, str] | None = None

    @property
    def topic(self) -> str | None:
        """The reason topic for this verdict, or None when nothing to say."""
        return TOPICS.get(self.verdict)

    @property
    def is_candidate(self) -> bool:
        """Only a package that fell behind, and then went quiet, is a candidate.

        AHEAD means somebody bumped the version and the build has not run yet,
        which is the opposite of abandoned. ABSENT is excluded on purpose: a
        package can be missing because it was renamed, and archiving a live
        package is far worse than missing a dead one.
        """
        return self.verdict is Verdict.BEHIND and self.quiet_days >= MIN_QUIET_DAYS

    @property
    def quiet_days(self) -> int:
        if not self.project.last_activity_at:
            return 0
        try:
            seen = datetime.fromisoformat(
                self.project.last_activity_at.replace("Z", "+00:00"))
        except ValueError:
            return 0
        return (datetime.now(timezone.utc) - seen).days


_SEGMENT_RE = re.compile(r"(\d+|\D+)")


def _segments(version: str) -> list:
    """Split a version so digits compare as numbers, not as text.

    Without this "6.10-1" sorts below "6.9-1".
    """
    return [int(s) if s.isdigit() else s for s in _SEGMENT_RE.findall(version)]


def compare(repo: str, built: str) -> int:
    """-1 when the repository is older, 0 when equal, 1 when newer."""
    if repo == built:
        return 0
    a, b = _segments(repo), _segments(built)
    for x, y in zip(a, b):
        if x == y:
            continue
        # A number always outranks a text segment, so "6" beats "rc".
        if isinstance(x, int) != isinstance(y, int):
            return 1 if isinstance(x, int) else -1
        return 1 if x > y else -1
    return (len(a) > len(b)) - (len(a) < len(b))


def classify(project: Project) -> Result:
    text = pkgbuild.fetch(project.path)
    if text is None:
        return Result(project, Verdict.NOT_A_PACKAGE)
    info = pkgbuild.parse(text)
    if info is None:
        return Result(project, Verdict.UNPARSEABLE)

    # Only x64 is mirrored, so the ARM view of branch compare is never queried.
    stable: dict[str, str] = {}
    for name in info.names:
        found = branch_compare.versions(name)
        if found.get("stable"):
            stable[name] = found["stable"]

    if not stable:
        return Result(project, Verdict.ABSENT, info.version)

    # A split package ships if any of its outputs matches. Only when every
    # output is behind is the whole project behind.
    ranks = [compare(info.version, v) for v in stable.values()]
    if 0 in ranks:
        verdict = Verdict.SHIPPING
    elif all(r > 0 for r in ranks):
        verdict = Verdict.AHEAD
    else:
        verdict = Verdict.BEHIND
    return Result(project, verdict, info.version, stable)
