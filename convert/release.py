"""2.4: «For a release» — what an upload to a tracker needs, made from a file in one go: the MediaInfo text (built
from ffprobe, in MediaInfo's own layout), screenshots (PNG, evenly over the film, HDR tone-mapped, anamorphic
made square), a contact sheet of them with time codes, a sample cut without re-encoding, and BBCode for the
description. Everything goes into «<name>_release» next to the file."""
from __future__ import annotations

import os
import subprocess

from . import probe as P

EVERY = (4, 6, 8, 9, 12, 16, 20, 24)
_NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


# ── the MediaInfo text ───────────────────────────────────────
def _row(name: str, value) -> str:
    return f"{name:<41}: {value}"


def _dur(sec: float) -> str:
    sec = max(0.0, sec)
    h, m, s = int(sec // 3600), int(sec // 60 % 60), int(sec % 60)
    return (f"{h} h {m} min" if h else f"{m} min {s} s") if sec >= 60 else f"{sec:.3f} s"


def _rate(bps: int) -> str:
    return f"{bps / 1e6:.1f} Mb/s" if bps >= 1e6 else f"{bps / 1e3:.0f} kb/s"


def _size(n: int) -> str:
    for unit, k in (("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if n >= k:
            return f"{n / k:.2f} {unit}"
    return f"{n} Bytes"


def _dar(dar: str) -> str:
    """«16:9» and «4:3» as they are, anything else as MediaInfo writes it: «2.40:1»."""
    try:
        a, b = (int(x) for x in dar.split(":"))
    except ValueError:
        return dar
    return dar if (a, b) in ((16, 9), (4, 3)) or not b else f"{a / b:.2f}:1"


def _lang(s: P.Stream) -> list[str]:
    out = []
    if s.title:
        out.append(_row("Title", s.title))
    if s.lang_label():
        out.append(_row("Language", s.lang))
    out.append(_row("Default", "Yes" if s.default else "No"))
    out.append(_row("Forced", "Yes" if s.forced else "No"))
    return out


def info_text(info: P.MediaInfo) -> str:
    """The file in MediaInfo's text layout (General, Video, Audio #N, Text #N, Menu)."""
    fmt = {"matroska,webm": "Matroska", "mov,mp4,m4a,3gp,3g2,mj2": "MPEG-4", "mpegts": "MPEG-TS", "avi": "AVI"}
    out = ["General", _row("Complete name", os.path.basename(info.path)),
           _row("Format", fmt.get(info.format, info.format_long or info.format))]
    if info.size:
        out.append(_row("File size", _size(info.size)))
    if info.duration:
        out.append(_row("Duration", _dur(info.duration)))
    if info.bitrate:
        out.append(_row("Overall bit rate", _rate(info.bitrate)))
    if info.title:
        out.append(_row("Movie name", info.title))
    for kind, head in (("video", "Video"), ("audio", "Audio"), ("subtitle", "Text")):
        ss = info.of(kind)
        for n, s in enumerate(ss, 1):
            out += ["", head + (f" #{n}" if len(ss) > 1 else "")]
            out.append(_row("ID", s.index + 1))
            out.append(_row("Format", P.codec_name(s)))
            if s.profile and kind != "subtitle":
                out.append(_row("Format profile", s.profile))
            if s.bitrate:
                out.append(_row("Bit rate", _rate(s.bitrate)))
            if kind == "video":
                out.append(_row("Width", f"{s.width} pixels"))
                out.append(_row("Height", f"{s.height} pixels"))
                if s.dar:
                    out.append(_row("Display aspect ratio", _dar(s.dar)))
                if s.fps:
                    out.append(_row("Frame rate", f"{s.fps:.3f} FPS"))
                out.append(_row("Bit depth", f"{s.bits} bits"))
                out.append(_row("Scan type", "Interlaced" if s.interlaced else "Progressive"))
                if s.hdr:
                    out.append(_row("HDR format", s.hdr))
                cl = s.color or {}
                if cl.get("color_primaries"):
                    out.append(_row("Color primaries", cl["color_primaries"].upper().replace("BT", "BT.")))
            elif kind == "audio":
                out.append(_row("Channel(s)", f"{s.channels} channel{'s' if s.channels != 1 else ''}"))
                out.append(_row("Channel layout", P.layout_name(s.channels, s.layout)))
                if s.rate:
                    out.append(_row("Sampling rate", f"{s.rate / 1000:g} kHz"))
            out += _lang(s)
    if info.chapter_list:
        out += ["", "Menu"]
        for a, _b, title in info.chapter_list:
            h, m, sec = int(a // 3600), int(a // 60 % 60), a % 60
            out.append(f"{h:02d}:{m:02d}:{sec:06.3f}".ljust(41) + f": {title}")
    return "\n".join(out) + "\n"


# ── screenshots ───────────────────────────────────────────────
def shot_times(duration: float, n: int) -> list[float]:
    """n moments evenly between 5 % and 95 % of the film (the opening logos and the credits left out)."""
    n = max(1, int(n))
    if duration <= 0:
        return [0.0] * n
    a, b = duration * 0.05, duration * 0.95
    return [round(a + (b - a) * (i + 0.5) / n, 3) for i in range(n)]


def shot_filter(s: P.Stream | None, tonemap: bool) -> str:
    vf = []
    if s is not None and s.hdr and tonemap:                         # HDR → SDR, else the shot looks washed out
        vf += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709", "tonemap=tonemap=hable:desat=0",
               "zscale=t=bt709:m=bt709:r=tv"]
    if s is not None and s.sar and s.sar not in ("1:1", "0:1"):     # anamorphic: square pixels
        vf.append("scale=trunc(iw*sar/2)*2:ih")
    vf += ["setsar=1", "format=rgb24"]
    return ",".join(vf)


def grab(ffmpeg: str, src: str, at: float, out: str, s: P.Stream | None = None, tonemap: bool = True) -> bool:
    args = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{at:.3f}", "-i", src, "-map", "0:v:0",
            "-frames:v", "1", "-vf", shot_filter(s, tonemap), out]
    r = subprocess.run(args, capture_output=True, timeout=120, stdin=subprocess.DEVNULL, **_NO_WINDOW)
    return r.returncode == 0 and os.path.isfile(out)


def tc(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}"


def sheet(pngs: list[str], times: list[float], head: list[str], cols: int, out: str, width: int = 1920) -> bool:
    """A contact sheet: a header (name, size, video, sound) and the shots in a grid, each with its time code."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter
    pics = [QImage(p) for p in pngs]
    pics = [(im, t) for im, t in zip(pics, times) if not im.isNull()]
    if not pics:
        return False
    cols = max(1, min(cols, len(pics)))
    rows = (len(pics) + cols - 1) // cols
    gap, pad = 8, 16
    cw = (width - 2 * pad - gap * (cols - 1)) // cols
    w0, h0 = pics[0][0].width(), pics[0][0].height()
    ch = max(1, round(cw * h0 / max(1, w0)))
    head_h = pad + 26 * len(head) + 6
    img = QImage(width, head_h + rows * (ch + gap) - gap + pad, QImage.Format_RGB32)
    img.fill(QColor("#16181d"))
    p = QPainter(img)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    f = QFont("Segoe UI")
    f.setPixelSize(18)
    p.setFont(f)
    for i, line in enumerate(head):
        f.setBold(i == 0)
        p.setFont(f)
        p.setPen(QColor("#ffffff" if i == 0 else "#b8bcc6"))
        p.drawText(pad, pad + 20 + 26 * i, line)
    f.setBold(True)
    f.setPixelSize(16)
    p.setFont(f)
    for n, (im, t) in enumerate(pics):
        x = pad + (n % cols) * (cw + gap)
        y = head_h + (n // cols) * (ch + gap)
        p.drawImage(QRectF(x, y, cw, ch), im)
        label = tc(t)
        r = QRectF(x + cw - 92, y + ch - 30, 86, 24)
        p.fillRect(r, QColor(0, 0, 0, 170))
        p.setPen(QColor("#ffffff"))
        p.drawText(r, Qt.AlignCenter, label)
    p.end()
    return img.save(out, "PNG")


# ── the sample ────────────────────────────────────────────────
def sample_args(ffmpeg: str, src: str, out: str, duration: float, length: float) -> list[str]:
    """A piece from about 40 % of the film, copied (cut at the keyframe before it), all tracks, no chapters."""
    length = max(5.0, float(length))
    start = max(0.0, min(duration * 0.4, duration - length)) if duration > 0 else 0.0
    return [ffmpeg, "-hide_banner", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", src, "-t", f"{length:g}",
            "-map", "0", "-c", "copy", "-map_chapters", "-1", out]


# ── the whole kit ─────────────────────────────────────────────
def folder_for(src: str) -> str:
    stem = os.path.splitext(src)[0]
    return stem + "_release"


def bbcode(info: str, shots: list[str], sheet_name: str = "") -> str:
    """The description's text: MediaInfo under a spoiler, and the names of the pictures to upload."""
    lines = ["[spoiler][nfo]" + info.rstrip() + "[/nfo][/spoiler]"]
    if sheet_name or shots:
        lines.append("")
        lines += [f"[img]{os.path.basename(x)}[/img]" for x in ([sheet_name] if sheet_name else []) + shots]
    return "\n".join(lines) + "\n"


def make(ffmpeg: str, info: P.MediaInfo, opts: dict, progress=lambda done, total, what: None) -> dict:
    """Make the kit. opts: shots (N), cols, singles (keep the PNGs), sample (seconds, 0 = none), tonemap.
    → {"dir", "info", "shots", "sheet", "sample", "bbcode", "errors"}."""
    src = info.path
    d = opts.get("dir") or folder_for(src)
    os.makedirs(d, exist_ok=True)
    base = os.path.splitext(os.path.basename(src))[0]
    res = {"dir": d, "info": "", "shots": [], "sheet": "", "sample": "", "bbcode": "", "errors": []}
    n = int(opts.get("shots", 12) or 0)
    sample = float(opts.get("sample", 0) or 0)
    total = 1 + n + (1 if n else 0) + (1 if sample else 0)
    k = 0
    text = info_text(info)
    with open(os.path.join(d, base + "_mediainfo.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    res["info"] = text
    k += 1
    progress(k, total, "info")
    v = info.video
    times = shot_times(info.duration, n) if n and v is not None else []
    for i, at in enumerate(times, 1):
        out = os.path.join(d, f"{base}_{i:02d}.png")
        if grab(ffmpeg, src, at, out, v, bool(opts.get("tonemap", True))):
            res["shots"].append(out)
        else:
            res["errors"].append(f"shot {i} at {tc(at)}")
        k += 1
        progress(k, total, "shot")
    if res["shots"]:
        head = [os.path.basename(src),
                " · ".join(x for x in (P.fmt_size(info.size) if info.size else "", P.fmt_time(info.duration)) if x),
                P.video_line(v)] + [f"{P.audio_line(a)}" + (f" · {a.lang}" if a.lang_label() else "")
                                    for a in info.of("audio")[:3]]
        out = os.path.join(d, f"{base}_sheet.png")
        ok_times = [at for at, i in zip(times, range(1, len(times) + 1))
                    if os.path.join(d, f"{base}_{i:02d}.png") in res["shots"]]
        if sheet(res["shots"], ok_times, head, int(opts.get("cols", 4) or 4), out):
            res["sheet"] = out
        k += 1
        progress(k, total, "sheet")
        if not opts.get("singles", True):
            for x in res["shots"]:
                try:
                    os.remove(x)
                except OSError:
                    pass
            res["shots"] = []
    if sample:
        ext = os.path.splitext(src)[1] or ".mkv"
        out = os.path.join(d, f"{base}_sample{ext}")
        r = subprocess.run(sample_args(ffmpeg, src, out, info.duration, sample), capture_output=True, timeout=600,
                           stdin=subprocess.DEVNULL, **_NO_WINDOW)
        if r.returncode == 0 and os.path.isfile(out):
            res["sample"] = out
        else:
            res["errors"].append("sample: " + r.stderr.decode("utf-8", "replace")[-200:])
        k += 1
        progress(k, total, "sample")
    res["bbcode"] = bbcode(text, res["shots"], os.path.basename(res["sheet"]) if res["sheet"] else "")
    with open(os.path.join(d, base + "_bbcode.txt"), "w", encoding="utf-8") as f:
        f.write(res["bbcode"])
    return res
