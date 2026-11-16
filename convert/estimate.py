"""1.8: the size of the result before encoding. A few short samples from all over the film (6 × 6 s) are encoded
with the task's settings — at its quality and, on request, two steps either side — and their bytes per second
give the picture's size; the sound comes from its bit rate (copied: the source's), plus the container."""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
import threading

from PySide6.QtCore import QObject, Signal

from . import ff
from . import filters as F
from . import job as J
from .chunks import chunk_job

SAMPLES = 6
LENGTH = 6.0
OVERHEAD = 1.006


def sample_points(job: J.Job, n: int = SAMPLES, length: float = LENGTH) -> list[list[float]]:
    """n pieces of the result's timeline spread from 5 % to 95 %, in the source's time."""
    segs = J.segments(job) or [(0.0, J.src_duration(job))]
    total = sum(b - a for a, b in segs)
    if total <= length * n:
        return [[a, b] for a, b in segs]
    out = []
    for i in range(n):
        t = total * (0.05 + 0.9 * i / max(1, n - 1)) - length / 2
        t = max(0.0, min(total - length, t))
        pos = 0.0
        for a, b in segs:                                 # the piece of the source this moment lies in
            if t < pos + (b - a):
                start = a + (t - pos)
                out.append([round(start, 3), round(min(b, start + length), 3)])
                break
            pos += b - a
    return out


def audio_bytes(job: J.Job, seconds: float) -> int:
    """The sound of the result: copied tracks at the source's bit rate, encoded at theirs."""
    total = 0
    for s, codec in J.plan(job)["audio"]:
        if codec == "copy":
            kbps = (s.bitrate or 0) / 1000
            if not kbps:
                kbps = {"truehd": 3500, "dts": 1500, "flac": 900, "pcm_bluray": 4600}.get(s.codec, 640)
        else:
            kbps = J.audio_kbps(codec, F.layout_channels(job.acfg(s).get("channels", "keep"), s.channels, 8) or s.channels,
                                job.acfg(s)) or 192
        total += int(kbps * 1000 / 8 * seconds)
    return total


def with_quality(job: J.Job, q) -> J.Job:
    k = copy.copy(job)
    k.settings = copy.deepcopy(job.settings)
    if q is not None:
        k.settings.setdefault("video", {})["quality"] = q
    return k


class Estimator(QObject):
    """Runs in a thread; row(quality, bytes of the whole result, video kbps) per quality, then done(error)."""
    row = Signal(object, float, float)
    progress = Signal(int, int)
    done = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stop = False
        self.busy = False

    def run(self, job: J.Job, av, qualities: list, hw: bool = True):
        if self.busy:
            return
        self.busy, self.stop = True, False
        threading.Thread(target=self._work, args=(job, av, list(qualities), hw), daemon=True).start()

    def measure(self, job: J.Job, av, q, hw: bool = True, tick=None) -> tuple[float, float]:
        """(bytes of the whole result, video kbps) at quality q (None: the task's own settings)."""
        k = with_quality(job, q)
        pts = sample_points(k)
        tmp = tempfile.mkdtemp(prefix="artcore_estimate_")
        exe = ff.ffmpeg() or "ffmpeg"
        got, secs = 0, 0.0
        try:
            for i, (a, b) in enumerate(pts):
                if self.stop:
                    raise InterruptedError
                c = chunk_job(k, [[a, b]])
                out = os.path.join(tmp, f"s{i}.mkv")
                c.settings["container"] = "mkv"
                args = J.build_args(c, exe, out, av, hw)
                r = ff.run(args, 1800)
                if getattr(r, "returncode", 1) != 0 or not os.path.isfile(out):
                    err = (getattr(r, "stderr", "") or "").strip().splitlines()
                    raise RuntimeError(err[-1] if err else "ffmpeg")
                got += os.path.getsize(out)
                secs += b - a
                if tick:
                    tick()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if secs <= 0:
            return 0.0, 0.0
        rate = got / secs                                  # bytes per second of picture
        dur = J.out_duration(k)
        return (rate * dur + audio_bytes(k, dur)) * OVERHEAD, rate * 8 / 1000

    def _work(self, job, av, qualities, hw):
        err = ""
        try:
            n = len(qualities) * len(sample_points(job))
            k = [0]

            def tick():
                k[0] += 1
                self.progress.emit(k[0], n)
            for q in qualities:
                size, kbps = self.measure(job, av, q, hw, tick)
                self.row.emit(q, size, kbps)
        except InterruptedError:
            err = "stopped"
        except Exception as e:                            # noqa: BLE001
            err = str(e)
        self.busy = False
        self.done.emit(err)


def steps_around(enc, q: int) -> list[int]:
    """The task's quality and two steps either side (CRF / CQ, in the encoder's range)."""
    lo, hi = enc.q_range if enc is not None else (1, 51)
    step = 2 if hi > 15 else 1
    return [x for x in (q - 2 * step, q - step, q, q + step, q + 2 * step) if lo <= x <= hi]
