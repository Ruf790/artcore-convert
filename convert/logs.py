"""Conversion logs: one text file per finished task (what, into what, how it went, the ffmpeg command and all
of ffmpeg's output) in <data>\\logs, and a short history of them (history.json) for the Log tab."""
from __future__ import annotations

import json
import os
import re
import shutil
import time

from . import VERSION, config

KEEP = 300                          # the newest logs kept; older ones are deleted


def logs_dir() -> str:
    d = os.path.join(config.data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def _hist_path() -> str:
    return os.path.join(logs_dir(), "history.json")


def history() -> list[dict]:
    """Newest first: {time, src, out, status, error, elapsed, size, log, profile}."""
    try:
        with open(_hist_path(), encoding="utf-8") as f:
            data = json.load(f)
        return [h for h in data if isinstance(h, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _write_hist(items: list[dict]) -> None:
    p = _hist_path()
    try:
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)
    except OSError:
        pass


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name).strip()[:80] or "file"


def save(src: str, out: str, status: str, error: str, elapsed: float, size: int, profile: str, command: str,
         lines: list[str], settings: dict | None = None, next_to_output: bool = False, when: float | None = None,
         text: str = "") -> str:
    """Write the log of one conversion; returns its path ("" if it could not be written)."""
    when = when or time.time()
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(when))
    stem = os.path.splitext(os.path.basename(src))[0]
    path = os.path.join(logs_dir(), f"{stamp} {_safe(stem)}.log")
    n = 2
    while os.path.exists(path):
        path = os.path.join(logs_dir(), f"{stamp} {_safe(stem)} ({n}).log")
        n += 1
    head = [
        f"ArtCore Convert {VERSION} — {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when))}",
        f"Source:   {src}",
        f"Result:   {out or '—'}",
        f"Profile:  {profile}",
        f"Status:   {status}" + (f" — {error}" if error else ""),
        f"Time:     {int(elapsed // 3600)}:{int(elapsed // 60 % 60):02d}:{int(elapsed % 60):02d}",
        f"Size:     {size} bytes",
    ]
    if settings is not None:
        head.append("Settings: " + json.dumps(settings, ensure_ascii=False))
    if not text:                    # 1.6: xlog.build writes the full log; this is the plain fallback
        text = "\n".join(head) + "\n\n$ " + command + "\n\n" + "\n".join(lines) + "\n"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        return ""
    if next_to_output and out and status == "done":
        try:
            shutil.copyfile(path, os.path.splitext(out)[0] + ".log")
        except OSError:
            pass
    items = [{"time": when, "src": src, "out": out, "status": status, "error": error, "elapsed": round(elapsed, 1),
              "size": size, "log": path, "profile": profile}] + history()
    for old in items[KEEP:]:
        try:
            os.remove(old.get("log", ""))
        except OSError:
            pass
    _write_hist(items[:KEEP])
    return path


def clear() -> None:
    for h in history():
        try:
            os.remove(h.get("log", ""))
        except OSError:
            pass
    _write_hist([])
