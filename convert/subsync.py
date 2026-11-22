"""2.5: subtitles synced to the sound by themselves. The film's sound becomes a «speech now» curve (the energy of
the voice band, 10 ms steps, the slow part — music, noise — taken away); the subtitles become «a line is on
screen» (packet times from ffprobe: SRT / ASS / PGS / VobSub alike). The curve and the lines are matched by
cross-correlation (FFT) for every usual stretch (25 ↔ 23.976, 24 ↔ 25 …); the best match gives the scale and
the delay: new time = scale · old time + delay."""
from __future__ import annotations

import json
import os
import subprocess

import numpy as np

STEP = 0.01                                     # s, one frame of the curves
RATE = 8000                                     # Hz, the sound as it is read
MAX_SHIFT = 600.0                               # s, the furthest delay looked for
SURE = 10.0                                     # «sure» above it: a clear match (random lines reach ~5–7)
_NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


def scales() -> list[float]:
    from .job import SYNC_FPS, fps_scale
    return [1.0] + [fps_scale(a, b) for a, b in SYNC_FPS]


def sub_events(ffprobe: str, path: str, index: int, start: float = 0.0) -> list[tuple[float, float]]:
    """[(start, end)] of the lines of subtitle stream `index` (its own time, the file's start time taken off)."""
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", str(index), "-show_entries",
                        "packet=pts_time,duration_time,size", "-of", "json", path],
                       capture_output=True, timeout=600, stdin=subprocess.DEVNULL, **_NO_WINDOW)
    pk = json.loads(r.stdout.decode("utf-8", "replace") or "{}").get("packets") or []
    times = []
    for p in pk:
        try:
            t = float(p.get("pts_time"))
        except (TypeError, ValueError):
            continue
        try:
            d = float(p.get("duration_time"))
        except (TypeError, ValueError):
            d = 0.0
        times.append((t - start, d, int(p.get("size") or 0)))
    times.sort()
    out = []
    for i, (t, d, size) in enumerate(times):
        if size and size <= 32 and not d:              # a PGS «clear the screen» packet: the end of the line before
            continue
        if d <= 0:
            nxt = times[i + 1][0] if i + 1 < len(times) else t + 3
            d = min(max(0.3, nxt - t), 8.0)
        out.append((t, t + min(d, 15.0)))
    return out


def speech(ffmpeg: str, path: str, a_index: int, duration: float = 0.0, progress=None, stop=None) -> np.ndarray:
    """The «speech now» curve of sound stream `a_index` (its number among the sound streams), one value per STEP."""
    args = [ffmpeg, "-v", "error", "-nostdin", "-i", path, "-map", f"0:a:{a_index}", "-vn", "-sn", "-dn",
            "-ac", "1", "-af", "highpass=f=250,lowpass=f=3400", "-ar", str(RATE), "-f", "s16le", "-"]
    pr = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                          **_NO_WINDOW)
    hop = int(RATE * STEP)
    chunk = hop * 2 * 4000                               # 40 s of sound a read
    energy, rest, read = [], b"", 0
    try:
        while True:
            if stop is not None and stop():
                pr.kill()
                raise InterruptedError
            buf = pr.stdout.read(chunk)
            if not buf:
                break
            read += len(buf)
            buf = rest + buf
            n = len(buf) // (hop * 2)
            rest = buf[n * hop * 2:]
            x = np.frombuffer(buf[:n * hop * 2], dtype="<i2").astype(np.float32).reshape(n, hop)
            energy.append(np.sqrt((x * x).mean(axis=1)) + 1.0)
            if progress is not None and duration > 0:
                progress(min(0.99, read / 2 / RATE / duration))
    finally:
        pr.stdout.close()
        pr.wait()
    if not energy:
        return np.zeros(0, np.float32)
    e = np.log10(np.concatenate(energy))
    return curve(e)


def _smooth(x: np.ndarray, n: int) -> np.ndarray:
    if n <= 1 or len(x) < n:
        return x
    c = np.cumsum(np.concatenate([[0.0], x]))
    m = (c[n:] - c[:-n]) / n
    pad = (len(x) - len(m))
    return np.concatenate([np.full(pad // 2, m[0]), m, np.full(pad - pad // 2, m[-1])])


def curve(log_e: np.ndarray) -> np.ndarray:
    """The log energy minus its slow course (music beds, room noise): what is left rises where somebody speaks."""
    slow = _smooth(log_e, int(4 / STEP))
    x = _smooth(log_e, int(0.15 / STEP)) - slow
    sd = float(x.std()) or 1.0
    return np.clip((x - float(np.median(x))) / sd, -2.0, 3.0).astype(np.float32)


def mask(events, k: float, d: float, n: int) -> np.ndarray:
    m = np.zeros(n, np.float32)
    for a, b in events:
        i, j = int((k * a + d) / STEP), int((k * b + d) / STEP)
        if j > 0 and i < n:
            m[max(0, i):min(n, j)] = 1.0
    return m


def find(sp: np.ndarray, events, scales_=None) -> dict:
    """The best (scale, delay) for the subtitle lines on the speech curve: {"scale", "delay", "score", "sure"}.
    sure: how far the best match stands above the other delays (≥ SURE is a clear match)."""
    if len(sp) < 100 or len(events) < 5:
        return {"scale": 1.0, "delay": 0.0, "score": 0.0, "sure": 0.0}
    n = len(sp)
    span = int(MAX_SHIFT / STEP)
    size = 1 << int(np.ceil(np.log2(n + n + span)))
    fa = np.fft.rfft(sp - sp.mean(), size)
    best = None
    for k in scales_ or scales():
        m = mask(events, k, 0.0, n + span)
        if not m.any():
            continue
        m = m - m.mean()
        corr = np.fft.irfft(fa * np.conj(np.fft.rfft(m, size)), size)
        # corr[lag] = Σ sp[t + lag] · m[t]: lag > 0 → the lines come later
        lags = np.concatenate([corr[:span + 1], corr[-span:]])
        shifts = np.concatenate([np.arange(0, span + 1), np.arange(-span, 0)])
        i = int(np.argmax(lags))
        peak = float(lags[i])
        far = np.abs(shifts - shifts[i]) > int(2 / STEP)
        others = lags[far]
        sure = (peak - float(others.mean())) / (float(others.std()) or 1.0)
        score = peak / (float(np.sqrt((m * m).sum())) or 1.0)
        if best is None or score > best["score"]:
            best = {"scale": float(k), "delay": round(float(shifts[i]) * STEP, 3), "score": score, "sure": sure}
    return best or {"scale": 1.0, "delay": 0.0, "score": 0.0, "sure": 0.0}


def sync(ffmpeg: str, ffprobe: str, src: str, a_index: int, sub_path: str, sub_index: int, duration: float,
         sub_start: float = 0.0, progress=None, stop=None) -> dict:
    """The whole thing: the sound of `src`, the lines of stream `sub_index` of `sub_path` → find()."""
    events = sub_events(ffprobe, sub_path, sub_index, sub_start)
    sp = speech(ffmpeg, src, a_index, duration, progress, stop)
    res = find(sp, events)
    res["lines"] = len(events)
    return res
