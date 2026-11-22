"""2.3: instant edit of an MKV in place (mkvpropedit from MKVToolNix): the film's title and the tracks' language,
name, default and forced flags — without writing the file again. mkvpropedit is looked for next to the program,
in PATH and in Program Files\\MKVToolNix; without it the button is hidden and a hint says where to get it."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

from . import job as J

URL = "https://mkvtoolnix.download/"
_found: str | None = None
_looked = False


def find(refresh: bool = False) -> str:
    """The path of mkvpropedit, or "" when there is none."""
    global _found, _looked
    if _looked and not refresh:
        return _found or ""
    _looked = True
    exe = "mkvpropedit.exe" if os.name == "nt" else "mkvpropedit"
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else sys.argv[0] or "."))
    cands = [os.path.join(here, exe), os.path.join(here, "mkvtoolnix", exe)]
    w = shutil.which("mkvpropedit")
    if w:
        cands.append(w)
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if os.environ.get(env):
            cands.append(os.path.join(os.environ[env], "MKVToolNix", exe))
    _found = next((c for c in cands if c and os.path.isfile(c)), "")
    return _found


def can(job: J.Job | None) -> bool:
    """A Matroska source the edits can be written into (nothing joined, no outside tracks)."""
    return job is not None and job.info is not None and not job.info.error and \
        "matroska" in (job.info.format or "") and os.path.isfile(job.src)


def _order(job: J.Job) -> dict[int, str]:
    """stream index → mkvpropedit's selector: track:v1, track:a2, track:s1 …"""
    out, seen = {}, {}
    for s in job.info.streams:
        k = {"video": "v", "audio": "a", "subtitle": "s"}.get(s.kind)
        if k is None:
            continue
        seen[k] = seen.get(k, 0) + 1
        out[s.index] = f"track:{k}{seen[k]}"
    return out


def changes(job: J.Job) -> list[str]:
    """mkvpropedit's arguments for the edits of the Metadata tab ([] = nothing to write)."""
    out: list[str] = []
    tags = job.meta.get("tags")
    if tags is not None:
        title = str(tags.get("title", "") or "").strip()
        if title != (job.info.title or ""):
            out += ["--edit", "info"] + (["--set", f"title={title}"] if title else ["--delete", "title"])
    sel = _order(job)
    for s in job.info.streams:
        if s.index not in sel or J.ext_no(s) is not None:
            continue
        m = job.smeta(s)
        e: list[str] = []
        if "lang" in m and (m["lang"] or "und") != (s.lang or "und"):
            e += ["--set", f"language={m['lang'] or 'und'}"]
        if "title" in m and m["title"] != (s.title or ""):
            e += ["--set", f"name={m['title']}"] if m["title"] else ["--delete", "name"]
        if "default" in m and bool(m["default"]) != bool(s.default):
            e += ["--set", f"flag-default={int(bool(m['default']))}"]
        if "forced" in m and bool(m["forced"]) != bool(s.forced):
            e += ["--set", f"flag-forced={int(bool(m['forced']))}"]
        if e:
            out += ["--edit", sel[s.index]] + e
    return out


def command(job: J.Job, exe: str) -> list[str]:
    ch = changes(job)
    return [exe, job.src] + ch if ch else []


def run(job: J.Job, exe: str) -> tuple[bool, str]:
    """Write the edits into the file. (ok, mkvpropedit's message)."""
    args = command(job, exe)
    if not args:
        return True, ""
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        r = subprocess.run(args, capture_output=True, timeout=120, **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    text = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    return r.returncode in (0, 1), text           # 1: done with warnings
