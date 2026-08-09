"""Read package names and versions out of a PKGBUILD.

`pkgname` is frequently a shell expression, for example
`pkgname="${_linuxprefix}-${_module}"` in the extramodules packages, which are
150 of the 619 mirrored projects. Only a shell can resolve that, so
`makepkg --printsrcinfo` does the work.

SECURITY: `--printsrcinfo` sources the PKGBUILD, so it runs arbitrary shell
code from the source repository. Run it only in a job that holds no secrets.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

GITLAB = "https://gitlab.manjaro.org"
REFS = ("master", "main")
FETCH_TIMEOUT = 45
SRCINFO_TIMEOUT = 30

# makepkg refuses to parse when a referenced .install file is absent. Only the
# PKGBUILD is fetched, so the named file is stubbed and the parse is retried.
MISSING_INSTALL_RE = re.compile(r"install file \((.+?)\) does not exist")
MAX_STUBS = 8


@dataclass(frozen=True)
class SrcInfo:
    version: str          # "epoch:pkgver-pkgrel", the form branch compare uses
    names: tuple[str, ...]


def fetch(path: str) -> str | None:
    """Return the PKGBUILD text for a GitLab project, or None if it has none."""
    encoded = urllib.parse.quote(path, safe="")
    for ref in REFS:
        url = (f"{GITLAB}/api/v4/projects/{encoded}"
               f"/repository/files/PKGBUILD/raw?ref={ref}")
        req = urllib.request.Request(url, headers={"User-Agent": "gitlab-sync"})
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
    return None


def parse(text: str) -> SrcInfo | None:
    """Expand a PKGBUILD with makepkg. None when it cannot be parsed."""
    with tempfile.TemporaryDirectory() as work:
        with open(os.path.join(work, "PKGBUILD"), "w") as f:
            f.write(text)
        for _ in range(MAX_STUBS):
            try:
                result = subprocess.run(
                    ["makepkg", "--printsrcinfo"], cwd=work,
                    capture_output=True, text=True, timeout=SRCINFO_TIMEOUT)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                return None
            if result.returncode == 0:
                return _read_srcinfo(result.stdout)
            missing = MISSING_INSTALL_RE.search(result.stderr)
            if not missing:
                return None
            stub = os.path.join(work, os.path.basename(missing.group(1)))
            open(stub, "w").close()
    return None


def _read_srcinfo(out: str) -> SrcInfo | None:
    version = release = epoch = None
    names: list[str] = []
    for line in out.splitlines():
        key, _, value = line.strip().partition(" = ")
        if key == "pkgver":
            version = value
        elif key == "pkgrel":
            release = value
        elif key == "epoch":
            epoch = value
        elif key == "pkgname":
            names.append(value)
    if not version or not names:
        return None
    full = f"{version}-{release}" if release else version
    if epoch:
        full = f"{epoch}:{full}"
    return SrcInfo(version=full, names=tuple(names))
