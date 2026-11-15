"""1.5: two presets side by side (Tools → Compare presets). The same short piece goes through both — with
everything the preset does (its format, filters, two passes, the advanced encoder settings) — and the results
are put next to each other: the size (and what it comes to for the whole film), how fast it encoded, and the
quality against the source (VMAF where this ffmpeg has libvmaf, PSNR and SSIM always).

The piece is taken from the selected task (from a third of the film on) or, with no task, from a test clip
made here (ffmpeg's test picture, 1080p, with a tone)."""
from __future__ import annotations

import os
import tempfile
import time

from . import bench as B
from . import encoders as E
from . import ff
from . import job as J
from . import probe as P

LENGTHS = (5, 10, 20, 30)


def test_clip(ffmpeg: str, folder: str, seconds: int = 10) -> str:
    """A 1080p clip with sound to compare on when no task is selected (made once per folder)."""
    path = os.path.join(folder, f"compare_test_{seconds}s.mkv")
    if not os.path.isfile(path):
        os.makedirs(folder, exist_ok=True)
        r = ff.run([ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=s=1920x1080:r=25:d={seconds}", "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", "-c:a", "aac", "-b:a", "192k", path], 300)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-300:] or "the test clip was not made")
    return path


def make_job(settings: dict, src: str, info: P.MediaInfo, start: float, length: float) -> J.Job:
    """A task of the piece: the preset's settings, cut to [start, start + length], not split into parts."""
    st = J.merged(settings)
    st["split"] = {"mode": "off", "size_mb": 700, "minutes": 20}
    j = J.Job(src=src, settings=st)
    j.info = info
    dur = info.duration if info is not None else 0.0
    if dur > 0 and (start > 0.001 or start + length < dur - 0.001):
        j.trim = [[round(start, 3), round(min(dur, start + length), 3)]]
    return j


def run_one(key: str, label: str, settings: dict, src: str, info: P.MediaInfo, start: float, length: float,
            av: E.Available, out_dir: str, threads: int = 0, timeout: float = 1800,
            stop=lambda: False, n: int = 0) -> dict:
    """Encode the piece with one preset: {"key", "label", "ok", "size", "seconds", "fps", "speed", "psnr", "ssim",
    "vmaf", "error", "out", "profile"}. The commands are the ones a real task of it runs."""
    exe, probe_exe = ff.ffmpeg(), ff.ffprobe()
    res = {"key": key, "label": label, "ok": False, "size": 0, "seconds": 0.0, "fps": 0.0, "speed": 0.0,
           "error": "", "out": "", "profile": J.describe(settings, av)}
    j = make_job(settings, src, info, start, length)
    out = os.path.join(out_dir, f"compare_{n}_{key_safe(key)}.{J.out_ext(j.settings)}")
    res["out"] = out
    try:
        main = J.build_args(j, exe, out, av, hw_decode=True, work_dir=out_dir, threads=threads)
        pre = J.pre_passes(j, exe, out, av, True, out_dir, threads)
        metrics = ["psnr", "ssim"] + (["vmaf"] if "libvmaf" in av.filters else [])
        post = J.post_passes(j, exe, out, av, metrics, out_dir, threads)
    except (RuntimeError, OSError) as ex:
        res["error"] = str(ex)
        return res
    t0 = time.perf_counter()
    try:
        for args in pre + [main]:
            if stop():
                res["error"] = "stopped"
                return res
            r = ff.run(args, timeout)
            if r.returncode != 0 or not (args is not main or os.path.isfile(out)):
                lines = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
                res["error"] = lines[-1] if lines else f"exit {r.returncode}"
                return res
        wall = max(1e-3, time.perf_counter() - t0)
        res["seconds"] = round(wall, 2)
        res["size"] = os.path.getsize(out)
        n = B.frames(probe_exe, out) if j.info is not None and j.info.video is not None else 0
        done = J.out_duration(j) or length
        res.update(ok=True, fps=round(n / wall, 1) if n else 0.0, speed=round(done / wall, 2), length=done)
        for args in post:
            if stop():
                break
            r = ff.run(args, timeout)
            res.update(J.parse_metrics((r.stderr or "").splitlines()))
    except Exception as ex:                              # a timeout, no ffmpeg …
        res["error"] = str(ex)
    finally:
        J.cleanup(j, out_dir)
    return res


def key_safe(key: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in key)[:40] or "preset"


def work_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "ArtCoreConvert", "compare")
    os.makedirs(d, exist_ok=True)
    return d


def full_size(res: dict, film: float) -> int:
    """What the whole film would come to at the piece's rate (bytes; 0 = not known)."""
    n = float(res.get("length") or 0)
    return int(res["size"] * film / n) if res.get("ok") and n > 0 and film > 0 else 0


def best(results: list[dict]) -> dict:
    """{"size": key, "speed": key, "vmaf": key …} — who wins each column (only when both finished)."""
    ok = [r for r in results if r.get("ok")]
    if len(ok) < 2:
        return {}
    out = {"size": min(ok, key=lambda r: r["size"])["key"], "speed": max(ok, key=lambda r: r["speed"])["key"]}
    for m in ("vmaf", "psnr", "ssim"):
        have = [r for r in ok if m in r]
        if len(have) == len(ok):
            out[m] = max(have, key=lambda r: r[m])["key"]
    return out
