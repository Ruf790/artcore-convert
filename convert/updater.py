"""Update check against GitHub Releases + download of the Setup.exe (same scheme as ArtCore Client and ArtCore Music)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from . import REPO, VERSION

API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
UA = f"ArtCoreConvert/{VERSION}"


def _ver(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:4]) or (0,)


def check() -> dict | None:
    """The latest release if it is newer than this build: {version, notes, url, size, page}."""
    req = urllib.request.Request(API, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            rel = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    if rel.get("draft") or rel.get("prerelease"):
        return None
    tag = rel.get("tag_name", "")
    if _ver(tag) <= _ver(VERSION):
        return None
    asset = next((a for a in rel.get("assets", [])
                  if a.get("name", "").lower().startswith("artcoreconvert-setup") and a["name"].lower().endswith(".exe")), None)
    return {"version": tag.lstrip("vV"), "notes": rel.get("body") or "",
            "url": asset["browser_download_url"] if asset else "", "size": asset.get("size", 0) if asset else 0,
            "page": rel.get("html_url") or RELEASES_PAGE}


def download(url: str, size: int, progress=None) -> str:
    path = os.path.join(tempfile.gettempdir(), os.path.basename(url.split("?")[0]) or "ArtCoreConvert-Setup.exe")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    done = 0
    with urllib.request.urlopen(req, timeout=60) as r, open(path + ".part", "wb") as f:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, size)
    if size and done != size:
        os.remove(path + ".part")
        raise IOError(f"download incomplete: {done} of {size} bytes")
    os.replace(path + ".part", path)
    return path


def installer_cmdline(path: str) -> str:
    """A ready command line for cmd.exe (an argv list would get \\" quoting that cmd.exe misreads)."""
    inner = f'ping -n 3 127.0.0.1 >nul & start "" "{path}" /SILENT /SP- /NOCANCEL /CLOSEAPPLICATIONS'
    return f'cmd.exe /d /s /c "{inner}"'


def run_installer(path: str) -> None:
    """Start the silent update right after this process exits; setup.iss starts the converter again."""
    if sys.platform != "win32":
        raise OSError("installer runs on Windows only")
    subprocess.Popen(installer_cmdline(path), close_fds=True, creationflags=0x08000000)


def can_self_update() -> bool:
    from .config import is_portable
    return sys.platform == "win32" and getattr(sys, "frozen", False) and not is_portable()
