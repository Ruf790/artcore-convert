"""Where ffmpeg / ffprobe are: ARTCORE_FFMPEG_DIR (tests), ffmpeg\\ next to the exe (the bundled build), PATH."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from . import config
from .winint import IS_WIN, popen_flags

EXE = ".exe" if IS_WIN else ""


def _dirs() -> list[str]:
    out = []
    env = os.environ.get("ARTCORE_FFMPEG_DIR")
    if env:
        out.append(env)
    base = config.app_dir()
    out += [os.path.join(base, "ffmpeg", "bin"), os.path.join(base, "ffmpeg"), base]
    return out


def find(name: str) -> str:
    for d in _dirs():
        p = os.path.join(d, name + EXE)
        if os.path.isfile(p):
            return p
    return shutil.which(name) or ""


def ffmpeg() -> str:
    return find("ffmpeg")


def ffprobe() -> str:
    return find("ffprobe")


def run(args: list[str], timeout: float = 60, binary: bool = False):
    """Run ffmpeg / ffprobe and wait: CompletedProcess (stdout as text unless binary)."""
    return subprocess.run(args, capture_output=True, timeout=timeout, text=not binary,
                          encoding=None if binary else "utf-8", errors=None if binary else "replace",
                          stdin=subprocess.DEVNULL, **popen_flags())


_version: str | None = None


def version() -> str:
    """«7.1.1» / «N-118000-g…» — the first line of ffmpeg -version; "" when there is no ffmpeg."""
    global _version
    if _version is None:
        _version = ""
        exe = ffmpeg()
        if exe:
            try:
                first = run([exe, "-hide_banner", "-version"], 15).stdout.splitlines()[0]
                m = re.match(r"ffmpeg version (\S+)", first)
                _version = m.group(1) if m else first[:40]
            except Exception:
                pass
    return _version


def probe_json(path: str) -> dict:
    exe = ffprobe()
    if not exe:
        raise FileNotFoundError("ffprobe")
    r = run([exe, "-v", "error", "-hide_banner", "-print_format", "json", "-show_format", "-show_streams",
             "-show_chapters", path], 60)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or "").strip().splitlines()[-1] if r.stderr.strip() else f"ffprobe: {r.returncode}")
    return json.loads(r.stdout or "{}")
