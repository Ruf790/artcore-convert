"""2.1: the delayed start of the queue — «Start at 01:00», «In 30 min». Pure functions (no Qt): the window keeps
the moment in cfg["start_at"] (seconds since the epoch, 0 = no delayed start) and checks it every second."""
from __future__ import annotations

import datetime as _dt
import re

IN_MINUTES = (15, 30, 60, 120)                 # «In 15 min / 30 min / 1 h / 2 h»

_HHMM = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """«01:00», «1:00», «23.30» → (hour, minute); None if it is no time of day."""
    m = _HHMM.match(text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h, mi


def next_start(now: float, hhmm: str) -> float:
    """The next moment the clock shows <hhmm> (local time), as seconds since the epoch: today if that time is
    still ahead, else tomorrow. ValueError for a bad «HH:MM»."""
    hm = parse_hhmm(hhmm)
    if hm is None:
        raise ValueError(f"not a time of day: {hhmm!r}")
    base = _dt.datetime.fromtimestamp(now)
    at = base.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if at.timestamp() <= now:
        at = (base + _dt.timedelta(days=1)).replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    return at.timestamp()


def after_minutes(now: float, minutes: int) -> float:
    """«In 30 min»: whole seconds, so the countdown starts at 30:00."""
    return float(int(now) + int(minutes) * 60)


def clock(at: float) -> str:
    """«01:00» — the local time of the delayed start, for the button."""
    return _dt.datetime.fromtimestamp(at).strftime("%H:%M")


def countdown(left: float) -> str:
    """Seconds left → «1:02:03» / «12:05» / «0:07»."""
    s = max(0, int(left + 0.999))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def state_on_open(start_at: float, now: float, keep_queue: bool) -> float:
    """What a saved delayed start becomes when the program opens: kept if it is still ahead (and the queue came
    back), else 0 — a moment that passed while the program was closed does not start the queue."""
    try:
        at = float(start_at or 0)
    except (TypeError, ValueError):
        return 0.0
    return at if keep_queue and at > now else 0.0
