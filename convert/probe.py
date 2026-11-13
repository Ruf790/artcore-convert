"""What is inside a file (ffprobe): the container, the video, audio and subtitle tracks, chapters — and a
human description of it all for the Info tab."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import ff


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rate(s: str) -> float:
    try:
        a, b = str(s).split("/")
        return float(a) / float(b) if float(b) else 0.0
    except (ValueError, ZeroDivisionError):
        return _num(s)


@dataclass
class Stream:
    index: int                      # ffprobe index in the file (for -map 0:<index>)
    kind: str                       # video | audio | subtitle | attachment | data
    codec: str = ""
    profile: str = ""
    lang: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False
    bitrate: int = 0                # bit/s, 0 = unknown
    # video
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    bits: int = 8
    hdr: str = ""                   # "" | HDR10 | HLG | Dolby Vision
    interlaced: bool = False
    color: dict = field(default_factory=dict)   # color_primaries / color_transfer / color_space / color_range
    sar: str = ""
    dar: str = ""
    cover: bool = False             # an attached picture (cover art), not a real video
    # audio
    channels: int = 0
    layout: str = ""
    rate: int = 0
    # subtitle
    bitmap: bool = False            # PGS / VobSub / DVB: can't go into MP4

    def lang_label(self) -> str:
        return self.lang if self.lang and self.lang != "und" else ""


@dataclass
class MediaInfo:
    path: str
    format: str = ""
    format_long: str = ""
    duration: float = 0.0
    size: int = 0
    bitrate: int = 0
    title: str = ""
    streams: list[Stream] = field(default_factory=list)
    chapters: int = 0
    error: str = ""

    def of(self, kind: str) -> list[Stream]:
        return [s for s in self.streams if s.kind == kind and not s.cover]

    @property
    def video(self) -> Stream | None:
        v = self.of("video")
        return v[0] if v else None


BITMAP_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub", "dvb_teletext"}


def _bits(pix_fmt: str, raw: str) -> int:
    if raw and str(raw).isdigit() and int(raw) > 0:
        return int(raw)
    pf = pix_fmt or ""
    for b in (16, 14, 12, 10, 9):
        if f"p{b}" in pf or pf.endswith(f"{b}le") or pf.endswith(f"{b}be"):
            return b
    return 8


def _dv(st: dict) -> bool:
    tag = (st.get("codec_tag_string") or "").lower()
    return tag in ("dvh1", "dvhe", "dav1", "dva1")


def parse(path: str, j: dict) -> MediaInfo:
    fmt = j.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    info = MediaInfo(path=path, format=fmt.get("format_name", ""), format_long=fmt.get("format_long_name", ""),
                     duration=_num(fmt.get("duration")), size=int(_num(fmt.get("size"))),
                     bitrate=int(_num(fmt.get("bit_rate"))), title=tags.get("title", ""),
                     chapters=len(j.get("chapters") or []))
    for st in j.get("streams") or []:
        kind = st.get("codec_type", "")
        disp = st.get("disposition") or {}
        stags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        s = Stream(index=int(st.get("index", 0)), kind=kind, codec=st.get("codec_name", "") or "",
                   profile=st.get("profile", "") or "", lang=stags.get("language", ""), title=stags.get("title", ""),
                   default=bool(disp.get("default")), forced=bool(disp.get("forced")),
                   bitrate=int(_num(st.get("bit_rate") or stags.get("bps") or stags.get("bps-eng"))))
        if kind == "video":
            s.cover = bool(disp.get("attached_pic")) or s.codec in ("mjpeg", "png", "bmp") and not _rate(st.get("avg_frame_rate", "0"))
            s.width, s.height = int(st.get("width") or 0), int(st.get("height") or 0)
            s.fps = _rate(st.get("avg_frame_rate", "0")) or _rate(st.get("r_frame_rate", "0"))
            s.pix_fmt = st.get("pix_fmt", "") or ""
            s.bits = _bits(s.pix_fmt, st.get("bits_per_raw_sample", ""))
            s.color = {k: st.get(k, "") for k in ("color_primaries", "color_transfer", "color_space", "color_range")
                       if st.get(k)}
            trc = s.color.get("color_transfer", "")
            if any((sd.get("side_data_type") or "").startswith("DOVI") for sd in st.get("side_data_list") or []) \
                    or _dv(st):
                s.hdr = "Dolby Vision"
            elif trc == "smpte2084":
                s.hdr = "HDR10"
            elif trc == "arib-std-b67":
                s.hdr = "HLG"
            fo = st.get("field_order", "") or ""
            s.interlaced = fo in ("tt", "bb", "tb", "bt")
            s.sar, s.dar = st.get("sample_aspect_ratio", "") or "", st.get("display_aspect_ratio", "") or ""
        elif kind == "audio":
            s.channels = int(st.get("channels") or 0)
            s.layout = st.get("channel_layout", "") or ""
            s.rate = int(_num(st.get("sample_rate")))
        elif kind == "subtitle":
            s.bitmap = s.codec in BITMAP_SUBS
        info.streams.append(s)
    return info


def probe(path: str) -> MediaInfo:
    try:
        return parse(path, ff.probe_json(path))
    except Exception as e:
        return MediaInfo(path=path, size=os.path.getsize(path) if os.path.isfile(path) else 0, error=str(e))


# ── words ─────────────────────────────────────────────────────
CODEC_NAMES = {
    "h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "vp8": "VP8", "mpeg2video": "MPEG-2",
    "mpeg4": "MPEG-4", "msmpeg4v3": "DivX 3", "vc1": "VC-1", "wmv3": "WMV9", "prores": "ProRes", "dnxhd": "DNxHD",
    "mjpeg": "MJPEG", "theora": "Theora", "h263": "H.263", "ffv1": "FFV1", "rawvideo": "RAW",
    "aac": "AAC", "ac3": "AC-3", "eac3": "E-AC-3", "dts": "DTS", "truehd": "TrueHD", "mp3": "MP3", "mp2": "MP2",
    "opus": "Opus", "vorbis": "Vorbis", "flac": "FLAC", "alac": "ALAC", "wmav2": "WMA", "wmapro": "WMA Pro",
    "pcm_s16le": "PCM 16", "pcm_s24le": "PCM 24", "pcm_s32le": "PCM 32", "pcm_bluray": "LPCM", "pcm_dvd": "LPCM",
    "subrip": "SRT", "ass": "ASS", "ssa": "SSA", "mov_text": "MP4 Text", "webvtt": "WebVTT",
    "hdmv_pgs_subtitle": "PGS", "dvd_subtitle": "VobSub", "dvb_subtitle": "DVB",
}


def codec_name(s: Stream) -> str:
    n = CODEC_NAMES.get(s.codec, s.codec.upper() if s.codec else "?")
    p = (s.profile or "").lower()
    if s.codec == "dts" and ("ma" in p or "master" in p):
        n = "DTS-HD MA"
    elif s.codec == "dts" and "hra" in p:
        n = "DTS-HD HRA"
    elif s.codec in ("eac3", "truehd") and "atmos" in p:
        n += " Atmos"
    return n


def layout_name(ch: int, layout: str) -> str:
    lay = (layout or "").lower()
    for name in ("7.1", "6.1", "5.1", "5.0", "4.1", "4.0", "3.0", "2.1"):
        if lay.startswith(name):
            return name
    return {1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1"}.get(ch, f"{ch} ch")


def fmt_time(sec: float) -> str:
    sec = int(round(max(0.0, sec)))
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}" if n < 100 else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_rate(bps: float) -> str:
    if bps <= 0:
        return ""
    return f"{bps / 1e6:.1f} Mbit/s" if bps >= 1e6 else f"{bps / 1e3:.0f} kbit/s"


def resolution_name(w: int, h: int) -> str:
    big = max(w, h * 16 // 9)
    for name, px in (("8K", 7000), ("4K", 3400), ("1440p", 2400), ("1080p", 1800), ("720p", 1200), ("576p", 960),
                     ("480p", 800), ("360p", 600)):
        if big >= px:
            return name
    return f"{h}p" if h else ""


def video_line(s: Stream) -> str:
    bits = [codec_name(s)]
    if s.profile and s.profile.lower() not in ("main", "high", "lc"):
        bits[0] += f" {s.profile}"
    bits.append(f"{s.width}×{s.height}")
    if s.fps:
        bits.append(f"{s.fps:.3f}".rstrip("0").rstrip(".") + " fps")
    bits.append(f"{s.bits} bit")
    if s.hdr:
        bits.append(s.hdr)
    if s.interlaced:
        bits.append("interlaced")
    if s.bitrate:
        bits.append(fmt_rate(s.bitrate))
    return " · ".join(bits)


def audio_line(s: Stream) -> str:
    bits = [codec_name(s), layout_name(s.channels, s.layout)]
    if s.rate:
        bits.append(f"{s.rate / 1000:g} kHz")
    if s.bitrate:
        bits.append(fmt_rate(s.bitrate))
    return " · ".join(bits)


def sub_line(s: Stream) -> str:
    return codec_name(s) + (" (bitmap)" if s.bitmap else "")


def summary(info: MediaInfo) -> str:
    """One line for the queue: «HEVC 4K HDR10 · 2:01:33 · 18.2 GB»."""
    if info.error:
        return info.error
    v = info.video
    bits = []
    if v is not None:
        bits.append(" ".join(x for x in (codec_name(v), resolution_name(v.width, v.height), v.hdr) if x))
    a = info.of("audio")
    if a:
        bits.append(f"{codec_name(a[0])} {layout_name(a[0].channels, a[0].layout)}" + (f" +{len(a) - 1}" if len(a) > 1 else ""))
    if info.duration:
        bits.append(fmt_time(info.duration))
    if info.size:
        bits.append(fmt_size(info.size))
    return " · ".join(bits)
