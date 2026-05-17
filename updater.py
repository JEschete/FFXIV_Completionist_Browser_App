"""GitHub releases-based updater for FFXIV Completion Tracker.

Queries the public releases API, compares the latest tag against the locally
bundled __version__, and exposes a small data class + download helper that the
GUI (launch_gui.py) wires up to buttons.

No external dependencies — stdlib only. Network calls are short-timeout and
fail soft: if GitHub is unreachable the caller gets a clear error string.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from _version import __version__ as CURRENT_VERSION

GITHUB_OWNER = "JEschete"
GITHUB_REPO = "FFXIV_Completionist_Browser_App"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

_USER_AGENT = f"FFXIVTracker-Updater/{CURRENT_VERSION}"
_HTTP_TIMEOUT = 8.0


@dataclass
class ReleaseInfo:
    tag: str                # e.g. "v1.0.2"
    name: str               # human-readable release title
    html_url: str           # browser URL for the release page
    body: str               # release notes (markdown)
    installer_url: str | None  # direct download for the -Setup.exe asset, if present
    installer_name: str | None
    installer_size: int | None


@dataclass
class UpdateCheckResult:
    current: str
    latest: ReleaseInfo | None
    is_newer: bool
    error: str | None = None


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

_VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.](.+))?$", re.IGNORECASE)


def _parse(tag: str) -> tuple[int, int, int, str]:
    """Return a comparable tuple (major, minor, patch, prerelease).

    Anything we can't parse sorts as (0,0,0,tag) so unparseable tags compare
    as older than any real semver tag. Prerelease strings sort lexically; the
    empty string (a release tag) is considered newer than any prerelease.
    """
    m = _VER_RE.match(tag.strip())
    if not m:
        return (0, 0, 0, tag.strip())
    major, minor, patch, pre = m.groups()
    return (int(major), int(minor), int(patch), pre or "")


def is_newer(latest: str, current: str) -> bool:
    """True if `latest` is a newer release than `current`."""
    lmaj, lmin, lpat, lpre = _parse(latest)
    cmaj, cmin, cpat, cpre = _parse(current)
    if (lmaj, lmin, lpat) != (cmaj, cmin, cpat):
        return (lmaj, lmin, lpat) > (cmaj, cmin, cpat)
    # Same numeric version: an empty prerelease (real release) beats any prerelease.
    if lpre == cpre:
        return False
    if not lpre:
        return True
    if not cpre:
        return False
    return lpre > cpre


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def _request_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_latest_release() -> ReleaseInfo:
    data = _request_json(RELEASES_API)
    assets = data.get("assets") or []
    installer_url = None
    installer_name = None
    installer_size = None
    for a in assets:
        name = a.get("name") or ""
        if name.lower().endswith("-setup.exe"):
            installer_url = a.get("browser_download_url")
            installer_name = name
            installer_size = a.get("size")
            break
    return ReleaseInfo(
        tag=str(data.get("tag_name") or "").strip(),
        name=str(data.get("name") or "").strip(),
        html_url=str(data.get("html_url") or RELEASES_PAGE),
        body=str(data.get("body") or ""),
        installer_url=installer_url,
        installer_name=installer_name,
        installer_size=installer_size,
    )


def check_for_update() -> UpdateCheckResult:
    """Query GitHub and report whether a newer release exists.

    Network errors are caught and surfaced via `result.error` so the GUI can
    show a friendly message instead of crashing.
    """
    try:
        rel = fetch_latest_release()
    except urllib.error.HTTPError as e:
        return UpdateCheckResult(CURRENT_VERSION, None, False,
                                 error=f"GitHub returned HTTP {e.code}")
    except urllib.error.URLError as e:
        return UpdateCheckResult(CURRENT_VERSION, None, False,
                                 error=f"Network error: {e.reason}")
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return UpdateCheckResult(CURRENT_VERSION, None, False,
                                 error=f"Could not parse release info: {e}")

    return UpdateCheckResult(
        current=CURRENT_VERSION,
        latest=rel,
        is_newer=is_newer(rel.tag, CURRENT_VERSION),
    )


# ---------------------------------------------------------------------------
# Installer download (streamed, with progress callback)
# ---------------------------------------------------------------------------

def download_installer(url: str, dest: Path,
                       progress=None) -> Path:
    """Stream the installer to `dest`. `progress(bytes_done, total_or_None)`
    is called periodically. Returns the final path."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    ctx = ssl.create_default_context()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT, context=ctx) as resp:
        total = resp.headers.get("Content-Length")
        total_i = int(total) if total and total.isdigit() else None
        done = 0
        chunk = 64 * 1024
        with tmp.open("wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if progress:
                    progress(done, total_i)
    tmp.replace(dest)
    return dest
