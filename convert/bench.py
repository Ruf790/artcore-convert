"""1.4: the speed test (Tools menu) — one short piece through every encoder that works on this PC: frames a
second, how fast against real time, the size it came out at. The piece is taken from the selected task (from a
third of the film on) or, with no task, from ffmpeg's own test picture (1080p, 25 fps)."""
from __future__ import annotations

import os
import tempfile
import time

from . import encoders as E
from . import ff

SECONDS = 10
TEST_SRC = "testsrc2=s=1920x1080:r=25"


def piece(src: str | None, duration: float) -> tuple[str | None, float, float]:
    """(file or None for the test picture, start, length) of the piece the test encodes."""
    if not src or duration <= 0:
        return None, 0.0, float(SECONDS)
    n = min(float(SECONDS), duration)
    start = max(0.0, min(duration * 0.33, duration - n))
    return src, round(start, 3), round(n, 3)


def encoders(av: E.Available) -> list[E.VEnc]:
    """Every video encoder that works here (the GIF one is left out: it is not a film encoder)."""
    return [e for e in E.VIDEO if av.ok.get(e.id) and e.id != "gif"]


def args(e: E.VEnc, ffmpeg: str, src: str | None, start: float, length: float, out: str,
         threads: int = 0) -> list[str]:
    a = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    if e.hw:
        a += ["-hwaccel", "auto"]
    if src is None:
        a += ["-f", "lavfi", "-t", f"{length:g}", "-i", TEST_SRC]
    else:
        a += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src]
    v = {"rc": "quality", "quality": e.q_default, "speed": 5}
    a += ["-map", "0:v:0", "-an", "-sn", "-dn", "-c:v", e.ffname] + E.video_args(e, v, 8, length) + \
        E.thread_args(e, threads)
    if e.id == "x265" and threads > 0:
        i = a.index("-x265-params") + 1
        a[i] += f":pools={threads}"
    return a + ["-f", "matroska", out]


def frames(ffprobe: str, path: str) -> int:
    try:
        r = ff.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                    "stream=nb_read_packets", "-of", "csv=p=0", path], 60)
        return int((r.stdout.strip().splitlines() or ["0"])[0].strip(",") or 0)
    except (ValueError, OSError, Exception):
        return 0


def run_one(e: E.VEnc, ffmpeg: str, ffprobe: str, src: str | None, start: float, length: float,
            threads: int = 0, work_dir: str | None = None, timeout: float = 600) -> dict:
    """Encode the piece with one encoder: {"id", "label", "ok", "fps", "speed", "size", "seconds", "error"}."""
    d = work_dir or tempfile.gettempdir()
    out = os.path.join(d, f"acbench_{os.getpid()}_{e.id}.mkv")
    res = {"id": e.id, "label": e.label, "ok": False, "fps": 0.0, "speed": 0.0, "size": 0, "seconds": 0.0,
           "error": ""}
    t0 = time.perf_counter()
    try:
        r = ff.run(args(e, ffmpeg, src, start, length, out, threads), timeout)
        wall = max(1e-3, time.perf_counter() - t0)
        res["seconds"] = round(wall, 2)
        if r.returncode != 0 or not os.path.isfile(out):
            res["error"] = (r.stderr or "").strip().splitlines()[-1:] and (r.stderr or "").strip().splitlines()[-1] \
                or f"exit {r.returncode}"
            return res
        n = frames(ffprobe, out)
        res.update(ok=n > 0, fps=round(n / wall, 1), speed=round(length / wall, 2), size=os.path.getsize(out))
        if n <= 0:
            res["error"] = "no frames"
    except Exception as ex:                       # timeout, no ffmpeg …
        res["error"] = str(ex)
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    return res
