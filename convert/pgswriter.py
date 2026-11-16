"""2.0: a tiny PGS (.sup, Blu-ray subtitle) writer for the self-test and the tests of burnt-in subtitles — FFmpeg can
read PGS but not write it.

Each event is a white box (palette entry 1) with a black frame (entry 2) at (x, y) of a canvas, shown from start to
end; forced=True marks the object as forced (only the «forced» subtitles of a Blu-ray are shown then)."""
from __future__ import annotations

import struct


def _seg(kind: int, pts: float, data: bytes) -> bytes:
    t = int(round(pts * 90000)) & 0xFFFFFFFF
    return b"PG" + struct.pack(">IIBH", t, t, kind, len(data)) + data


def _rle(w: int, h: int, border: int = 3) -> bytes:
    out = bytearray()

    def run(n: int, c: int):
        while n > 0:
            k = min(n, 16383)
            if c == 0:
                out.extend(b"\x00" + (bytes([k]) if k < 64 else bytes([0x40 | (k >> 8), k & 0xFF])))
            elif k < 3:
                out.extend(bytes([c]) * k)
            else:
                out.extend(b"\x00" + (bytes([0x80 | k, c]) if k < 64 else bytes([0xC0 | (k >> 8), k & 0xFF, c])))
            n -= k
    for y in range(h):
        if y < border or y >= h - border:
            run(w, 2)
        else:
            run(border, 2)
            run(w - 2 * border, 1)
            run(border, 2)
        out.extend(b"\x00\x00")
    return bytes(out)


def display_set(n: int, pts: float, cw: int, ch: int, box=None, forced: bool = False, epoch: bool = True) -> bytes:
    """One display set: box = (x, y, w, h) shown from pts, None = clear the screen."""
    objs = b""
    if box is not None:
        x, y, w, h = box
        objs = struct.pack(">HBBHH", 0, 0, 0x40 if forced else 0x00, x, y)
    pcs = struct.pack(">HHBHBBBB", cw, ch, 0x10, n, 0x80 if (epoch and box is not None) else 0x00, 0, 0,
                      1 if box is not None else 0) + objs
    out = _seg(0x16, pts, pcs)
    if box is not None:
        x, y, w, h = box
        out += _seg(0x17, pts, struct.pack(">BBHHHH", 1, 0, x, y, w, h))
        pal = struct.pack(">BB", 0, 0) + bytes([0, 16, 128, 128, 0]) + bytes([1, 235, 128, 128, 255]) + \
            bytes([2, 16, 128, 128, 255])
        out += _seg(0x14, pts, pal)
        data = struct.pack(">HH", w, h) + _rle(w, h)
        ln = len(data)
        out += _seg(0x15, pts, struct.pack(">HBB", 0, 0, 0xC0) + bytes([(ln >> 16) & 0xFF, (ln >> 8) & 0xFF, ln & 0xFF])
                    + data)
    else:
        out += _seg(0x17, pts, struct.pack(">BBHHHH", 1, 0, 0, 0, cw, ch))
    return out + _seg(0x80, pts, b"")


def write_sup(path: str, events, cw: int = 1920, ch: int = 1080) -> str:
    """events: [(start, end, (x, y, w, h), forced)] in time order."""
    out = display_set(0, 0.0, cw, ch, None)            # starts at 0: FFmpeg counts a file from its first packet
    n = 1
    for start, end, box, forced in events:
        out += display_set(n, start, cw, ch, box, forced)
        out += display_set(n + 1, end, cw, ch, None)
        n += 2
    with open(path, "wb") as f:
        f.write(out)
    return path
