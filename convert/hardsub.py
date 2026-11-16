"""2.0: subtitles burnt into the picture («hardsub») — one subtitle track of a task drawn onto the video.

Which track: the Streams tab marks it (Job.tracks[index]["burn"] / Job.ext[n]["burn"] = "all" | "forced"; "" = no);
a preset can pick the forced subtitles by itself (settings["hardsub"]["auto"] = "forced"): a track flagged forced in
the language of the sound, or the forced lines of a Blu-ray / DVD track in that language.

How it is drawn, in the video chain after the crop, the HDR → SDR, the scale and the frame rate (so the letters are
sharp at the size of the result and stand where they should), before the logo and the pixel format:
  • text (SRT, ASS / SSA, WebVTT, MP4 text) — libass (the subtitles filter) on the frame itself; ASS with its own
    styles and the fonts of the MKV (attachments; for an outside .ass the film's fonts are taken out into a folder,
    a «Fonts» folder beside it is used too); SRT and the like with the font, size, outline and place set here;
  • pictures (PGS of a Blu-ray, VobSub of a DVD, DVB) — the subtitle stream laid over the picture (overlay), its
    canvas cropped, turned and scaled the way the film is, so it sits at the same place of the smaller frame;
    «forced only»: the decoder's forced_subs_only.
Time: the subtitle filter reads the times of the subtitle file, the frames carry the result's time. Cut into pieces,
joined, in chunks — the frames' time is moved to the source's own time for the filter (setpts, piece by piece) and
back after it. Pictures in several pieces: each piece's subtitles are laid on a transparent canvas of that piece's
length, the canvases joined with the pictures by the concat filter.
HDR kept: white letters would be 10 000 nits — they are drawn on a transparent canvas, brought down to about
200 nits (PQ / HLG), then laid over. HDR → SDR: the letters come after the tone mapping, plain BT.709."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from . import filters as F
from .probe import Stream

HS_DEFAULT = {"font": "Arial", "size": 54, "outline": 3, "shadow": 1, "pos": "bottom", "margin": 50,
              "color": "white", "bold": False, "nits": 200, "auto": "off"}
COLORS = {"white": (255, 255, 255), "yellow": (255, 240, 0), "gray": (220, 220, 220)}
POSITIONS = ("bottom", "top")
AUTO = ("off", "forced")
TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "microdvd", "subviewer", "subviewer1",
               "sami", "realtext", "mpl2", "pjs", "vplayer", "jacosub", "stl", "eia_608"}
ASS_CODECS = {"ass", "ssa"}
BITMAP_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
BIG = 10 ** 12                 # µs between two joined files on the time the filter sees (11.6 days)
FONT_EXT = (".ttf", ".otf", ".ttc", ".otc", ".pfb", ".woff", ".woff2")


def hset(st: dict) -> dict:
    """The look of text subtitles (a part of the preset)."""
    out = dict(HS_DEFAULT)
    h = st.get("hardsub")
    if isinstance(h, dict):
        out.update({k: v for k, v in h.items() if k in HS_DEFAULT})
    if out["pos"] not in POSITIONS:
        out["pos"] = "bottom"
    if out["color"] not in COLORS:
        out["color"] = "white"
    if out["auto"] not in AUTO:
        out["auto"] = "off"
    return out


@dataclass
class Burn:
    stream: Stream
    ext: int | None            # an outside track (its number in Job.ext), None = a stream of the source
    mode: str                  # all | forced
    auto: bool = False         # picked by the preset, not by hand

    @property
    def kind(self) -> str:
        c = self.stream.codec
        return "bitmap" if c in BITMAP_CODECS else "text" if c in TEXT_CODECS else ""

    @property
    def text(self) -> bool:
        return self.kind == "text"

    @property
    def bitmap(self) -> bool:
        return self.kind == "bitmap"

    @property
    def ass(self) -> bool:
        return self.stream.codec in ASS_CODECS

    @property
    def forced(self) -> bool:
        return self.mode == "forced" and self.bitmap

    def key(self) -> tuple:
        return ("ext", self.ext) if self.ext is not None else ("src", self.stream.index)


def mode_of(job, s: Stream) -> str | None:
    """What the Streams tab says of this track: "all" | "forced" | "" (no) — None when nothing was said."""
    n = J.ext_no(s)
    if n is not None:
        v = job.ext[n].get("burn", "") if n < len(job.ext) else ""
        return v if v in ("all", "forced") else ""
    v = job.tracks.get(str(s.index), {}).get("burn")
    return v if v in ("all", "forced", "") else None


def can_forced(s: Stream) -> bool:
    """«Forced lines only» is a flag of single pictures: PGS and VobSub have it, text has it for a whole track."""
    return s.codec in ("hdmv_pgs_subtitle", "dvd_subtitle")


def _audio_lang(job) -> str:
    info = job.info
    lang = (job.settings.get("lang") or "").lower()
    if lang:
        return lang
    kept = [s for s in info.of("audio") if job.track(s).get("keep", True)] or info.of("audio")
    if not kept:
        return ""
    first = next((s for s in kept if s.default), kept[0])
    m = (job.smeta(first).get("lang") or first.lang or "").lower()
    return "" if m == "und" else m


def auto_pick(job) -> Burn | None:
    """The preset's «burn the forced subtitles»: a track flagged forced in the language of the sound (or any, the
    sound's language unknown), else the forced lines of a Blu-ray / DVD track in that language."""
    lang = _audio_lang(job)
    subs = job.info.of("subtitle")

    def same(s):
        sl = (job.smeta(s).get("lang") or s.lang or "").lower()
        return not lang or sl in ("", "und", lang)
    for s in subs:
        if (job.smeta(s).get("forced", s.forced)) and same(s) and (s.codec in TEXT_CODECS or s.codec in BITMAP_CODECS):
            return Burn(s, None, "all", True)
    if lang:
        for s in subs:
            if can_forced(s) and (job.smeta(s).get("lang") or s.lang or "").lower() == lang:
                return Burn(s, None, "forced", True)
    return None


def choice(job) -> Burn | None:
    """The track to burn: the one marked on the Streams tab, else the preset's automatic pick."""
    info = job.info
    if info is None or info.error:
        return None
    subs = info.of("subtitle")
    for s in subs:
        m = mode_of(job, s)
        if m:
            return Burn(s, None, m if m == "all" or can_forced(s) else "all")
    for n, s in J.ext_streams(job):
        if job.ext[n].get("kind") == "subtitle":
            m = mode_of(job, s)
            if m:
                return Burn(s, n, m if m == "all" or can_forced(s) else "all")
    if hset(job.settings)["auto"] == "forced" and not any(mode_of(job, s) is not None for s in subs):
        return auto_pick(job)
    return None


def pin(job, part) -> None:
    """A part of the task (a chunk, a test piece) burns exactly what the task burns — the automatic pick included
    (its tracks are switched off there, so the pick could differ)."""
    b = choice(job)
    if b is not None and b.ext is None:
        for s in job.info.of("subtitle"):
            part.tracks.setdefault(str(s.index), {})["burn"] = b.mode if s.index == b.stream.index else ""


def video_out(job) -> bool:
    """The picture is encoded (a copy cannot get anything drawn on it)."""
    from . import encoders as E
    v = job.settings.get("video", {})
    return job.info is not None and job.info.video is not None and v.get("mode", "encode") == "encode" and \
        not E.container(job.settings.get("container", "mkv")).audio_only


def why_not(job, b: Burn) -> str:
    """"" when this track can be burnt in, else the i18n key of the reason."""
    if not video_out(job):
        return "w_burn_copy"
    if not b.kind:
        return "w_burn_codec"
    if b.text and b.ext is None and any(p.lower().endswith(".ffconcat") for p, _i in J.sources(job)):
        return "w_burn_disc_text"
    return ""


def active(job) -> Burn | None:
    b = choice(job)
    return b if b is not None and not why_not(job, b) else None


def describe(b: Burn) -> str:
    """«#3 PGS rus (forced only)» for the log."""
    from .probe import codec_name
    s = b.stream
    src = f"#ext{b.ext + 1}" if b.ext is not None else f"#{s.index}"
    bits = [src, codec_name(s)]
    if s.lang and s.lang != "und":
        bits.append(s.lang)
    if s.title:
        bits.append(f'"{s.title}"')
    if b.forced:
        bits.append("(forced only)")
    if b.auto:
        bits.append("(auto)")
    return " ".join(bits)


# ── filter-graph text ─────────────────────────────────────────
def esc(value: str, win: bool | None = None) -> str:
    """A value of a filter option inside a filter graph (filters.escape_path: C:\\Фильмы\\it's [1].srt →
    'C\\:/Фильмы/it\\'\\''s [1].srt')."""
    return F.escape_path(value, win)


def _us(x: float) -> int:
    return int(round(float(x) * 1_000_000))


def time_map(job, b: Burn) -> list[tuple[int, int, int]]:
    """[(start on the result, start on the subtitles' own time, length)] in µs, piece by piece. The subtitles'
    time: of an outside file — the joined timeline moved by its delay; of a stream of the source — the raw time of
    its file (+ its start time: a TS / m2ts does not begin at 0), each joined file BIG further."""
    files = J.sources(job)
    pcs = J.pieces(job) or [(0, 0.0, None)]
    base, pos = [], 0.0
    for _p, info in files:
        base.append(pos)
        pos += J._dur(info)
    out, at = [], 0.0
    for k, a, e in pcs:
        length = (e - a) if e is not None else 10 ** 7
        if b.ext is not None:
            src = base[k] + a - J.ext_delay(job, b.stream)
            out.append((_us(at), _us(src), _us(length)))
        else:
            info = files[k][1]
            start = info.start if info is not None and not info.error else 0.0
            out.append((_us(at), k * BIG + _us(start + a), _us(length)))
        at += length
    return out


def _nested(cond: list[int], vals: list[int]) -> str:
    e = str(vals[-1])
    for th, v in zip(reversed(cond), reversed(vals[:-1])):
        e = f"if(lt(PTS,{th}),{v},{e})"
    return e


def shift_filters(tm: list[tuple[int, int, int]]) -> tuple[list[str], list[str]]:
    """setpts to the subtitles' time and back. The way there decides the piece by the result's time, the way back
    by the subtitles' time halfway between two pieces — always the same piece, so every frame gets its time back."""
    d = [a - p for p, a, _l in tm]
    if len(tm) == 1 and d[0] == 0:
        return [], []
    there = _nested([p for p, _a, _l in tm[1:]], d)
    mids = [(tm[i][1] + tm[i][2] + tm[i + 1][1]) // 2 for i in range(len(tm) - 1)]
    back = _nested(mids, d)
    return ["settb=1/1000000", f"setpts='PTS+{there}'"], [f"setpts='PTS-{back}'"]


def _ass_colour(rgb: tuple[int, int, int], alpha: int = 0) -> str:
    r, g, b = rgb
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def force_style(st: dict) -> str:
    """SRT and the like: the font, size, outline, shadow, place and colour — sizes given for a 1080-line frame
    (libass draws SRT on a 288-line script, scaled to the picture)."""
    h = hset(st)
    k = 288 / 1080

    def n(x: float) -> str:
        return f"{float(x) * k:.2f}".rstrip("0").rstrip(".")
    font = str(h["font"] or "Arial").replace(",", " ").replace(":", " ").replace("'", " ")
    return ",".join([f"FontName={font}", f"FontSize={n(h['size'])}",
                     f"PrimaryColour={_ass_colour(COLORS[h['color']])}", "OutlineColour=&H00000000",
                     "BackColour=&H80000000", f"Bold={-1 if h['bold'] else 0}", "BorderStyle=1",
                     f"Outline={n(h['outline'])}", f"Shadow={n(h['shadow'])}",
                     f"Alignment={8 if h['pos'] == 'top' else 2}", f"MarginV={n(h['margin'])}"])


def charenc(path: str) -> str:
    """An outside SRT not in UTF-8: Windows-1251 (Cyrillic) or Windows-1252 — FFmpeg wants to be told."""
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 20)
    except OSError:
        return ""
    if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return ""
    try:
        data.decode("utf-8")
        return ""
    except UnicodeDecodeError:
        pass
    text = data.decode("cp1251", "replace")
    cyr = sum(1 for ch in text if "\u0400" <= ch <= "\u04ff")
    return "CP1251" if cyr > len(text) * 0.05 else "CP1252"


def _si(info, s: Stream) -> int:
    """The subtitles filter counts subtitle streams only."""
    idx = [x.index for x in info.of("subtitle")] if info is not None else []
    return idx.index(s.index) if s.index in idx else -1


def fonts_dir(job, b: Burn, work_dir: str | None = None) -> str:
    """Fonts for an outside .ass: those attached to the film (MKV) taken out once, and a «Fonts» folder beside the
    subtitles or the film. "" when there are none."""
    if b.ext is None or not b.ass:
        return ""
    path = job.ext[b.ext]["path"]
    sources = [d for d in {os.path.join(os.path.dirname(path), n) for n in ("Fonts", "fonts", "FONTS")} |
               {os.path.join(os.path.dirname(job.src), n) for n in ("Fonts", "fonts")} if os.path.isdir(d)]
    att = [s for s in (job.info.streams if job.info is not None and not job.info.error else [])
           if s.kind == "attachment" and (s.filename.lower().endswith(FONT_EXT) or "font" in s.mime.lower())]
    if not att and len(sources) == 1:
        return sources[0]
    if not att and not sources:
        return ""
    try:
        stt = os.stat(job.src)
        key = f"{os.path.abspath(job.src)}|{stt.st_size}|{int(stt.st_mtime)}|{'|'.join(sorted(sources))}"
    except OSError:
        key = os.path.abspath(job.src)
    d = os.path.join(work_dir or os.path.join(tempfile.gettempdir(), "ArtCoreConvert"), "fonts",
                     hashlib.sha1(key.encode("utf-8")).hexdigest()[:12])
    if os.path.isdir(d) and os.listdir(d):
        return d
    os.makedirs(d, exist_ok=True)
    for src in sources:
        for name in os.listdir(src):
            if name.lower().endswith(FONT_EXT):
                try:
                    shutil.copy2(os.path.join(src, name), os.path.join(d, name))
                except OSError:
                    pass
    if att:
        from . import ff
        from .winint import popen_flags
        exe = ff.ffmpeg()
        if exe:
            try:
                subprocess.run([exe, "-hide_banner", "-nostdin", "-y", "-v", "error", "-dump_attachment:t", "",
                                *ff.inp(job.src), "-t", "0", "-f", "null", "-"], cwd=d, capture_output=True,
                               timeout=120, stdin=subprocess.DEVNULL, **popen_flags())
            except (OSError, subprocess.SubprocessError):
                pass
    return d if os.listdir(d) else ""


def text_filters(job, b: Burn, canvas: bool = False, work_dir: str | None = None) -> list[str]:
    """libass on the frames: to the subtitles' time, the subtitles filter (one per joined file for a stream of the
    source), back to the result's time."""
    pre, post = shift_filters(time_map(job, b))
    style = force_style(job.settings) if not b.ass else ""
    fonts = fonts_dir(job, b, work_dir)

    def one(path: str, si: int, enc: str = "") -> str:
        opts = [f"filename={esc(path)}"]
        if si >= 0:
            opts.append(f"si={si}")
        if canvas:
            opts.append("alpha=1")
        if fonts:
            opts.append(f"fontsdir={esc(fonts)}")
        if style:
            opts.append(f"force_style={esc(style)}")
        if enc:
            opts.append(f"charenc={enc}")
        return "subtitles=" + ":".join(opts)
    if b.ext is not None:
        path = job.ext[b.ext]["path"]
        info = J.file_info(job, path)
        si = _si(info, b.stream) if info is not None and len(info.of("subtitle")) > 1 else -1
        enc = charenc(path) if b.stream.codec in ("subrip", "srt", "text", "microdvd") else ""
        return pre + [one(path, si, enc)] + post
    files = J.sources(job)
    used = sorted({k for k, _a, _e in J.pieces(job)}) or [0]
    mid = []
    for k in used:
        path, info = files[k]
        s = J.match_stream(job, k, b.stream)
        if s is None or info is None:
            continue
        f = one(path, _si(info, s))
        mid += [f] if k == 0 else [f"setpts=PTS-{k * BIG}", f, f"setpts=PTS+{k * BIG}"]
    return pre + mid + post


# ── HDR and colour ────────────────────────────────────────────
def hdr_kind(job, enc, bits: int) -> str:
    """pq | hlg when the result keeps the source's HDR ("" otherwise: SDR, or tone mapped to SDR)."""
    if enc is None or not F.hdr_keeps(job, enc, bits) or F.tonemap_on(job, enc, bits):
        return ""
    trc = job.info.video.color.get("color_transfer", "")
    if trc == "arib-std-b67" or (not trc and job.info.video.hdr == "HLG"):
        return "hlg"
    return "pq"


def hdr_lut(kind: str, nits: float) -> list[str]:
    """The canvas (sRGB-like 0…255) into PQ / HLG with white at <nits> (BT.2408: graphics white ≈ 203 nits)."""
    nits = max(80.0, min(1000.0, float(nits or 200)))
    lin = f"pow(val/255,2.4)"
    if kind == "pq":
        e = (f"st(0,pow({lin}*{nits / 10000:g},0.1593017578125));"
             f"pow((0.8359375+18.8515625*ld(0))/(1+18.6875*ld(0)),78.84375)*255")
    else:
        e = (f"st(0,pow({lin}*{nits / 1000:g},1/1.2));"
             f"if(lte(ld(0),1/12),sqrt(3*ld(0)),0.17883277*log(12*ld(0)-0.28466892)+0.55991073)*255")
    return [f"lutrgb=r='{e}':g='{e}':b='{e}'"]


SCALE_MATRIX = {"bt709": "bt709", "bt601": "bt601", "bt601-625": "bt601", "bt2020": "bt2020"}


def to_yuva(job, enc, bits: int) -> list[str]:
    """The RGBA canvas into the picture's YUV with the picture's matrix (swscale would take BT.601 otherwise)."""
    hk = hdr_kind(job, enc, bits)
    if hk:
        m = "bt2020"
    elif F.tonemap_on(job, enc, bits):
        m = "bt709"
    else:
        m = SCALE_MATRIX.get(F.source_matrix(job), "bt709")
    deep = hk or (job.info.video.bits > 8 and not F.tonemap_on(job, enc, bits)) or bits > 8
    return [f"scale=out_color_matrix={m}:out_range=tv", "format=yuva420p10le" if deep else "format=yuva420p"]


def overlay() -> str:
    return "overlay=x=0:y=0:format=auto:eof_action=pass"


def canvas_size(job, b: Burn) -> tuple[int, int]:
    s = b.stream
    v = job.info.video
    return (s.width or v.width, s.height or v.height) if b.bitmap else (v.width, v.height)


def geometry(job, enc, cw: int, ch: int) -> list[str]:
    """A subtitle canvas of cw × ch (the source's frame, in the subtitles' own pixels) made the way the film is:
    the same crop, rotation, scale and padding — ending at the size of the frame where it is laid on."""
    src = job.info.video
    sw, sh = src.width or cw, src.height or ch
    out = []
    if F.is_on(job, "crop"):
        l, t, r, bt = F.crop_values(job)
        kx, ky = cw / sw, ch / sh
        out.append(f"crop={max(2, round((sw - l - r) * kx))}:{max(2, round((sh - t - bt) * ky))}:"
                   f"{round(l * kx)}:{round(t * ky)}")
    out += F.rotate_filters(job)
    fw, fh, _sar = F.pre_scale(job)
    to = (fw, fh)
    pads = []
    for f in J.scale_filters(job, enc):
        if f.startswith("scale="):
            w, h = f[6:].split(":")[:2]
            to = (int(w), int(h))
        elif f.startswith("pad="):
            pads.append(f.replace(":color=black", ":color=black@0"))
    out.append(f"scale={to[0]}:{to[1]}:flags=bicubic")
    out += pads
    for f in F.pad_filter(job):
        out.append(f.split(":color=")[0] + ":color=black@0")
    return out


def bitmap_chain(job, enc, bits: int, b: Burn) -> list[str]:
    """The subtitle pictures (RGBA) → the result's frame, HDR brought down, in the picture's YUV."""
    cw, ch = canvas_size(job, b)
    out = ["format=rgba"] + geometry(job, enc, cw, ch)
    hk = hdr_kind(job, enc, bits)
    if hk:
        out += hdr_lut(hk, hset(job.settings)["nits"])
    return out + to_yuva(job, enc, bits)


def text_canvas(job, enc, bits: int, b: Burn, work_dir: str | None = None) -> list[str]:
    """HDR kept: the letters on a transparent canvas of the result's size (then brought down and laid over)."""
    w, h = J.out_size(job, enc)
    fps = J.out_fps(job)
    dur = J.out_duration(job) + 10
    src = f"color=c=black@0:s={w}x{h}:r={F.ntsc(fps)}:d={dur:.3f}"
    return [src, "format=rgba"] + text_filters(job, b, canvas=True, work_dir=work_dir) + \
        ["unpremultiply=inplace=1"] + hdr_lut(hdr_kind(job, enc, bits), hset(job.settings)["nits"]) + \
        to_yuva(job, enc, bits)


def inline(job, enc, bits: int, b: Burn) -> bool:
    """Text on an SDR picture: drawn right on the frames (no graph needed)."""
    return b.text and not hdr_kind(job, enc, bits)


def piece_canvas(i: int, n_in: int | None, sidx: int | None, cw: int, ch: int, fps: float, length: float,
                 size0: tuple[int, int] | None = None) -> list[str]:
    """A piece's subtitle pictures on a transparent canvas of exactly the piece's length (a piece without the
    track: the canvas alone) → [cs{i}] for the concat filter."""
    src = f"color=c=black@0:s={cw}x{ch}:r={F.ntsc(fps)}:d={length:.3f},format=rgba"
    tail = f",scale={size0[0]}:{size0[1]}" if size0 and size0 != (cw, ch) else ""
    if n_in is None or sidx is None:
        return [f"{src}{tail}[cs{i}]"]
    return [f"{src}[cb{i}]", f"[cb{i}][{n_in}:{sidx}]overlay=eof_action=pass:format=rgb{tail}[cs{i}]"]


from . import job as J          # noqa: E402 — at the end: job.py imports this module while it is being loaded
