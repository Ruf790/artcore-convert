"""A task of the queue: a source file, its settings, where it goes, its state — and the ffmpeg command for it.

Settings are a plain dict (JSON in the queue / presets):
  container  mkv | mp4 | mov | webm | avi | ts | m2ts | gif | mka | mp3 | m4a | flac | opus | ogg | wav | ac3
  video      {mode: encode | copy, encoder: auto_hevc | nvenc_hevc | x265 | …, rc: quality | bitrate | size,
              quality, bitrate (kbit/s), size_mb, speed 1..7, bits: auto | 8 | 10, width / height: the frame
              (0 = as is; height alone = a 16:9 box), fit: fit | pad | stretch, upscale: bool,
              fps: 0 = as is, deint: auto | on | off}
  audio      {codec: auto | copy | aac | ac3 | eac3 | opus | flac | mp3, kbps: 0 = auto, channels: keep | stereo | 5.1,
              normalize: bool, tracks: all | first}
  subs       all | first | none
  meta       bool (title, tags, chapters)
  filters    {name: {on, …}} — 1.3, the video filter rows (filters.py)
  audio also: delay (ms, + / −), gain (dB), rate (Hz, 0 = as is), abits (16 / 24 / 32, 0 = as is),
             channels keep | mono | stereo | 5.1 | 7.1 — and each of them per track in Job.tracks
Per-file choices of the Streams tab are in Job.tracks: {stream index: {"keep": bool, "codec": "" | id}}.
Per-file edits (1.1): Job.trim — pieces [[start, end], …] in seconds of the source (Job.trim_cut: the pieces are
thrown out instead of kept); Job.meta — {"tags": {key: value} | None, "streams": {index: {lang, title, default,
forced}}}; Job.chapters — None = as in the source, otherwise [[start, title], …] of the result."""
from __future__ import annotations

import copy
import itertools
import os
import tempfile
from dataclasses import dataclass, field

from . import encoders as E
from . import filters as F
from .probe import MediaInfo, Stream

DEFAULT_SETTINGS = {
    "container": "mkv",
    "video": {"mode": "encode", "encoder": "auto_hevc", "rc": "quality", "quality": 0, "bitrate": 8000,
              "size_mb": 4000, "speed": 5, "bits": "auto", "width": 0, "height": 0, "fit": "fit", "upscale": False,
              "fps": 0, "deint": "auto"},
    "audio": {"codec": "auto", "kbps": 0, "channels": "keep", "normalize": False, "tracks": "all"},
    "subs": "all",
    "lang": "",          # «Select streams»: only the audio / subtitles of this language ("" = any)
    "meta": True,
    "filters": F.defaults(),     # 1.3: the filter rows of the Streams tab (filters.py)
}

PRESETS = {      # built-in; quality 0 = the encoder's own default
    "hevc_hw":  {"container": "mkv", "video": {"encoder": "auto_hevc", "bits": "10"}},
    "h264_mp4": {"container": "mp4", "video": {"encoder": "auto_h264", "bits": "8"},
                 "audio": {"codec": "aac"}, "subs": "first"},
    "av1_hw":   {"container": "mkv", "video": {"encoder": "auto_av1", "bits": "10"}},
    "phone":    {"container": "mp4", "video": {"encoder": "auto_h264", "bits": "8", "height": 1080},
                 "audio": {"codec": "aac", "channels": "stereo", "tracks": "first"}, "subs": "none"},
    "small":    {"container": "mkv", "video": {"encoder": "auto_hevc", "bits": "10", "height": 720, "quality": 30},
                 "audio": {"codec": "aac", "channels": "stereo", "kbps": 160}},
    "audio_ac3": {"container": "mkv", "video": {"mode": "copy"}, "audio": {"codec": "ac3", "kbps": 640}},
    "remux_mkv": {"container": "mkv", "video": {"mode": "copy"}, "audio": {"codec": "copy"}},
    "remux_mp4": {"container": "mp4", "video": {"mode": "copy"}, "audio": {"codec": "auto"}, "subs": "all"},
    "webm":     {"container": "webm", "video": {"encoder": "vp9", "bits": "8"}, "audio": {"codec": "opus"}},
    "xvid_avi": {"container": "avi", "video": {"encoder": "auto_mpeg4", "quality": 4},
                 "audio": {"codec": "mp3", "channels": "stereo", "tracks": "first"}, "subs": "none"},
    "prores":   {"container": "mov", "video": {"encoder": "prores", "quality": 4}, "audio": {"codec": "pcm24"}},
    "gif":      {"container": "gif", "video": {"encoder": "gif", "height": 480, "fps": 15}, "subs": "none"},
    "mp3":      {"container": "mp3", "audio": {"codec": "mp3", "kbps": 320, "channels": "stereo"}},
    "flac":     {"container": "flac", "audio": {"codec": "flac"}},
}


def merged(*parts: dict) -> dict:
    """DEFAULT_SETTINGS with the parts laid over it (one level deep for video / audio)."""
    out = copy.deepcopy(DEFAULT_SETTINGS)
    for p in parts:
        for k, v in (p or {}).items():
            if k == "filters":
                F.merge(out["filters"], v if isinstance(v, dict) else {})
            elif isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update(v)
            elif k in out:
                out[k] = v
    return out


def preset_settings(name: str, user: dict | None = None) -> dict:
    if user and name in user:
        return merged(user[name])
    return merged(PRESETS.get(name, {}))


_ids = itertools.count(1)


@dataclass
class Job:
    src: str
    settings: dict
    info: MediaInfo | None = None
    preset: str = ""
    tracks: dict = field(default_factory=dict)      # {stream index (str): {"keep": bool, "codec": ""}}
    out: str = ""                                   # the output file, decided when it starts
    status: str = "wait"                            # wait | probe | run | pause | done | error | stop | skip
    progress: float = 0.0
    error: str = ""
    enc_used: str = ""                              # the encoder id it ran with (auto resolved)
    out_size: int = 0
    elapsed: float = 0.0
    uid: int = field(default_factory=lambda: next(_ids))
    trim: list = field(default_factory=list)        # [[start, end], …] seconds of the source
    trim_cut: bool = False                          # True: the pieces are cut out, the rest is kept
    meta: dict = field(default_factory=dict)        # {"tags": {…} | None, "streams": {"index": {…}}}
    chapters: list | None = None                    # None = from the source; [[start, title], …]
    dest: str = ""                                  # «Save as…»: the result's path (the extension follows the format)
    log_path: str = ""                              # the log of the last run
    crop_found: list | None = None                  # 1.3: [l, t, r, b] the automatic crop found ([] = none)

    def to_dict(self) -> dict:
        return {"src": self.src, "settings": self.settings, "preset": self.preset, "tracks": self.tracks,
                "status": self.status if self.status in ("done", "error", "stop", "skip") else "wait",
                "out": self.out if self.status == "done" else "", "error": self.error,
                "trim": self.trim, "trim_cut": self.trim_cut, "meta": self.meta, "chapters": self.chapters,
                "dest": self.dest, "log": self.log_path, "crop_found": self.crop_found}

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        j = cls(src=str(d.get("src", "")), settings=merged(d.get("settings") or {}), preset=d.get("preset", ""),
                tracks={str(k): v for k, v in (d.get("tracks") or {}).items() if isinstance(v, dict)})
        j.trim = [[float(a), float(b)] for a, b in (d.get("trim") or []) if float(b) > float(a)]
        j.trim_cut = bool(d.get("trim_cut"))
        j.dest = str(d.get("dest") or "")
        j.log_path = str(d.get("log") or "")
        cf = d.get("crop_found")
        j.crop_found = [int(x) for x in cf][:4] if isinstance(cf, list) and len(cf) in (0, 4) else None
        j.meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        ch = d.get("chapters")
        j.chapters = [[float(c[0]), str(c[1])] for c in ch if isinstance(c, (list, tuple)) and len(c) >= 2] \
            if isinstance(ch, list) else None
        j.status = d.get("status", "wait")
        j.out = d.get("out", "")
        j.error = d.get("error", "")
        if j.status == "done":
            j.progress = 1.0
        return j

    @property
    def name(self) -> str:
        return os.path.basename(self.src)

    def track(self, s: Stream) -> dict:
        return self.tracks.get(str(s.index), {})

    def acfg(self, s: Stream) -> dict:
        """The audio settings of one track: the common ones with this track's own (Streams tab) laid over."""
        out = dict(self.settings.get("audio", {}))
        t = self.track(s)
        for k in ("kbps", "channels", "normalize") + F.AUDIO_FILTER_KEYS:
            if k in t:
                out[k] = t[k]
        return out

    def smeta(self, s: Stream) -> dict:
        """The user's edits of a stream's language / title / default / forced (Metadata tab)."""
        return (self.meta.get("streams") or {}).get(str(s.index), {})

    @property
    def edited(self) -> bool:
        return bool(self.trim or self.chapters is not None or self.meta.get("tags") is not None
                    or any(self.meta.get("streams") or {}))


# ── time codes ───────────────────────────────────────────────
def fmt_tc(sec: float, ms: bool = True) -> str:
    """1:02:03.450 / 2:03.450 (h only when there are hours)."""
    sec = max(0.0, float(sec or 0))
    whole = int(sec)
    frac = int(round((sec - whole) * 1000))
    if frac == 1000:
        whole, frac = whole + 1, 0
    h, m, s = whole // 3600, whole // 60 % 60, whole % 60
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    return base + (f".{frac:03d}" if ms else "")


def parse_tc(text: str) -> float | None:
    """«1:02:03.5», «2:03», «83.25», «83,25» → seconds; None if it isn't a time."""
    s = (text or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        parts = [float(x) for x in s.split(":")]
    except ValueError:
        return None
    if len(parts) > 3 or any(x < 0 for x in parts):
        return None
    out = 0.0
    for x in parts:
        out = out * 60 + x
    return round(out, 3)


# ── trim: pieces of the source ───────────────────────────────
def segments(job: Job) -> list[tuple[float, float]]:
    """The parts of the source that go out, in order: [] = the whole file. Overlapping pieces are joined;
    with trim_cut the pieces are what is thrown away."""
    dur = job.info.duration if job.info else 0.0
    pcs = sorted((max(0.0, float(a)), float(b)) for a, b in job.trim if float(b) > float(a))
    if dur > 0:
        pcs = [(a, min(b, dur)) for a, b in pcs if a < dur]
    joined: list[list[float]] = []
    for a, b in pcs:
        if joined and a <= joined[-1][1] + 0.001:
            joined[-1][1] = max(joined[-1][1], b)
        else:
            joined.append([a, b])
    if job.trim_cut and joined:
        keep, pos = [], 0.0
        for a, b in joined:
            if a - pos > 0.04:
                keep.append([pos, a])
            pos = b
        if dur <= 0 or dur - pos > 0.04:
            keep.append([pos, dur if dur > 0 else pos + 1e9])
        joined = keep
    out = [(round(a, 3), round(b, 3)) for a, b in joined if b - a > 0.04]
    if len(out) == 1 and dur > 0 and out[0][0] <= 0.001 and out[0][1] >= dur - 0.001:
        return []                                                # the whole file after all
    return out


def out_duration(job: Job) -> float:
    segs = segments(job)
    if segs:
        return sum(b - a for a, b in segs)
    return job.info.duration if job.info else 0.0


def map_time(segs: list[tuple[float, float]], t: float) -> float | None:
    """A moment of the source → the same moment of the result (a cut-out moment: the start of the next
    piece; past the last piece: None)."""
    if not segs:
        return t
    pos = 0.0
    for a, b in segs:
        if t < a:
            return pos
        if t < b:
            return pos + (t - a)
        pos += b - a
    return None


def out_chapters(job: Job) -> list[tuple[float, float, str]] | None:
    """The chapters of the result when we write them ourselves (edited, or the source's moved by the trim);
    None = ffmpeg copies the source's (or drops them, if metadata is off)."""
    dur = out_duration(job)
    segs = segments(job)
    if job.chapters is not None:
        starts = sorted((max(0.0, float(a)), str(t)) for a, t in job.chapters)
    elif segs and job.info and job.info.chapter_list and job.settings.get("meta", True):
        starts = []
        for a, _b, title in job.info.chapter_list:
            m = map_time(segs, a)
            if m is not None:
                starts.append((m, title))
    else:
        return None
    starts = [(a, t) for a, t in starts if dur <= 0 or a < dur - 0.5]
    seen, uniq = set(), []
    for a, t in starts:
        key = round(a, 1)
        if key in seen:                                         # two chapters that fell on one point
            uniq[-1] = (a, t)
            continue
        seen.add(key)
        uniq.append((a, t))
    return [(a, (uniq[i + 1][0] if i + 1 < len(uniq) else max(dur, a + 1)), t) for i, (a, t) in enumerate(uniq)]


def _ffesc(s: str) -> str:
    out = str(s)
    for ch in ("\\", "=", ";", "#", "\n"):
        out = out.replace(ch, "\\" + ch)
    return out


def ffmetadata(chapters: list[tuple[float, float, str]]) -> str:
    lines = [";FFMETADATA1"]
    for a, b, title in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(a * 1000))}", f"END={int(round(b * 1000))}",
                  f"title={_ffesc(title)}"]
    return "\n".join(lines) + "\n"


def auto_chapters(duration: float, every_min: float, name: str = "Chapter {0}") -> list[list]:
    step = max(10.0, every_min * 60)
    n = max(1, int(duration // step) + (1 if duration % step > 30 else 0)) if duration > 0 else 1
    return [[round(i * step, 3), name.format(i + 1)] for i in range(n) if i * step < max(duration, 1)]


HIDDEN_TAGS = {"encoder", "major_brand", "minor_version", "compatible_brands", "duration", "creation_time"}


def source_tags(info: MediaInfo | None) -> dict:
    return {k: v for k, v in (info.tags if info else {}).items() if k.lower() not in HIDDEN_TAGS
            and not k.lower().startswith("_statistics")}


# ── which streams go out, and how ────────────────────────────
def enc_family(enc_id: str) -> str:
    if E.is_auto(enc_id):
        return enc_id[5:]
    e = E.BY_ID.get(enc_id)
    return e.family if e else ""


def fit_container(st: dict) -> dict:
    """After the container changed: an encoder / audio codec it can't hold is swapped for one it can."""
    c = E.container(st.get("container", "mkv"))
    v, a = st.setdefault("video", {}), st.setdefault("audio", {})
    if c.id == "gif":
        v["mode"] = "encode"
    if not c.audio_only and v.get("mode") != "copy" and not c.takes_video(enc_family(v.get("encoder", "auto_hevc"))):
        v["encoder"], v["quality"] = c.v_default, 0
    codec = a.get("codec", "auto")
    if codec not in ("auto", "copy") and not c.takes_audio(E.A_CODEC.get(codec, codec)):
        a["codec"], a["kbps"] = "auto", 0
    return st


def multi_encode(job: Job) -> bool:
    """Several pieces with the video encoded: they are joined by the concat filter."""
    c = E.container(job.settings.get("container", "mkv"))
    return len(segments(job)) > 1 and job.settings.get("video", {}).get("mode", "encode") == "encode" and \
        not c.audio_only and job.info is not None and job.info.video is not None


def plan(job: Job) -> dict:
    """{video: [Stream], audio: [(Stream, codec id | "copy")], subs: [(Stream, "copy" | "mov_text" | "webvtt")],
        warnings: [text keys]} — the decisions build_args turns into arguments."""
    info, st = job.info, job.settings
    c = E.container(st.get("container", "mkv"))
    out = {"video": [], "audio": [], "subs": [], "warnings": []}
    if info is None:
        return out
    w = out["warnings"]
    v = info.of("video")
    vcfg = st.get("video", {})
    if v and not c.audio_only and vcfg.get("mode") != "none":
        out["video"] = [v[0]]
        if vcfg.get("mode") == "copy":
            if not c.takes_video(v[0].codec):
                w.append("w_copy_container")
        elif not c.takes_video(enc_family(vcfg.get("encoder", "auto_hevc"))):
            w.append("w_codec_container")
    elif v and c.audio_only:
        w.append("w_audio_only")
    joined = multi_encode(job)
    # cut and the picture encoded: copied sound would start before the picture (ffmpeg copies from the
    # keyframe the seek lands on) — it is encoded again, into its own codec where we have one
    recut = bool(segments(job)) and bool(out["video"]) and vcfg.get("mode") != "copy"
    out["same_rate"] = set()
    a_cfg = st.get("audio", {})
    lang = (st.get("lang") or "").lower()
    audios = info.of("audio")
    if lang:                                                      # «any language» or only this one
        audios = [s for s in audios if (s.lang or "").lower() == lang] or audios[:1]
    if a_cfg.get("tracks") == "first":
        audios = audios[:1]
    if c.audio is not None and not c.audio:                       # GIF: no sound
        audios = []
    for s in info.of("audio"):
        if c.audio is not None and not c.audio:
            break
        t = job.track(s)
        if not t.get("keep", s in audios):
            continue
        if c.one_audio and out["audio"]:
            w.append("w_one_audio")
            break
        codec = t.get("codec") or a_cfg.get("codec", "auto")
        tc = job.acfg(s)
        changes = F.audio_on(tc)
        if codec == "auto":
            codec = "copy" if c.takes_audio(s.codec) and not changes else c.a_default
        if codec == "copy" and not c.takes_audio(s.codec):
            codec = c.a_default
            w.append("w_audio_reencoded")
        elif codec != "copy" and not c.takes_audio(E.A_CODEC.get(codec, codec)):
            codec = c.a_default
            w.append("w_audio_reencoded")
        if codec == "copy" and changes:                           # a filter can't work on copied sound
            same = next((k for k, cn in E.A_CODEC.items() if cn == s.codec), "")
            codec = same if same and c.takes_audio(s.codec) else (c.a_default or "aac")
            if codec == same:
                out["same_rate"].add(s.index)
            w.append("w_afilter_copy")
        if codec == "copy" and recut:
            same = next((k for k, cn in E.A_CODEC.items() if cn == s.codec), "")
            codec = same if same and c.takes_audio(s.codec) else (c.a_default or "aac")
            if codec == same:
                out["same_rate"].add(s.index)
            w.append("w_trim_audio")
        out["audio"].append((s, codec))
    subs = info.of("subtitle")
    if lang:
        subs = [s for s in subs if (s.lang or "").lower() == lang]
    mode = st.get("subs", "all")
    if mode == "none":
        subs = []
    elif mode == "first":
        subs = subs[:1]
    for s in info.of("subtitle"):
        t = job.track(s)
        if not t.get("keep", s in subs):
            continue
        if c.subs == "none":
            if not c.audio_only:
                w.append("w_subs_dropped")
            continue
        if joined:
            w.append("w_trim_subs")
            continue
        if c.subs == "any":
            out["subs"].append((s, "copy"))
        elif s.codec in E.TEXT_SUBS:
            out["subs"].append((s, "copy" if s.codec == c.subs else c.subs))
        else:
            w.append("w_bitmap_subs")
    if segments(job) and vcfg.get("mode") == "copy" and out["video"]:
        w.append("w_trim_copy")
    return out


def out_bits(job: Job, enc: E.VEnc | None) -> int:
    want = str(job.settings.get("video", {}).get("bits", "auto"))
    if enc is None:
        return 8
    if len(enc.bits) == 1:
        return enc.bits[0]
    if want == "auto":
        src = job.info.video.bits if job.info and job.info.video else 8
        return 10 if (src > 8 or enc.family in ("hevc", "av1")) and 10 in enc.bits else 8
    return 10 if want == "10" and 10 in enc.bits else 8


OVERLAY = "\x00overlay"          # where the picture is laid on (the chain is cut there into two)


def video_chain(job: Job, enc: E.VEnc | None = None, transforms: str = "", detect: bool = False) -> list[str]:
    """The video filters in their order (filters.py); OVERLAY marks the overlay. detect: the first pass of the
    two-pass stabilisation — the chain up to vidstabdetect."""
    gif = enc is not None and enc.id == "gif"
    bits = out_bits(job, enc)
    out = F.interlace_filters(job) + F.delogo_filter(job) + F.crop_filter(job) + F.tonemap_filters(job, enc, bits)
    out += F.rotate_filters(job)
    if detect:
        return out + F.deshake_filters(job, transforms, detect=True)
    out += F.deshake_filters(job, transforms) + F.denoise_filters(job)
    out += scale_filters(job, enc)
    out += F.pad_filter(job) + F.aspect_filter(job) + F.color_filters(job) + F.sharpen_filter(job)
    out += F.grain_filter(job) + F.fps_filters(job, enc)
    if F.is_on(job, "overlay") and not gif:
        out.append(OVERLAY)
    out += F.colorspace_filter(job, enc, bits)
    if gif:
        out.append("split[g0][g1];[g0]palettegen=stats_mode=diff[gp];[g1][gp]paletteuse=dither=bayer:bayer_scale=5")
    return out


def video_filters(job: Job, enc: E.VEnc | None = None) -> list[str]:
    """The chain as one -vf (the overlay left out: build_args lays it on in the filter graph)."""
    return [f for f in video_chain(job, enc) if f != OVERLAY]


def needs_vidstab(job: Job) -> bool:
    return F.is_on(job, "deshake") and F.fset(job.settings, "deshake").get("method") == "vidstab" and \
        job.settings.get("video", {}).get("mode", "encode") == "encode" and job.info is not None and \
        job.info.video is not None and not E.container(job.settings.get("container", "mkv")).audio_only


RESOLUTIONS = ((3840, 2160), (2560, 1440), (1920, 1080), (1280, 720), (1024, 576), (854, 480), (640, 360),
               (720, 576), (720, 480), (640, 480))


def _even(x: float) -> int:
    return max(2, int(x / 2 + 0.5) * 2)


def _ratio(s: str) -> float:
    try:
        a, b = str(s).split(":")
        return float(a) / float(b) if float(a) > 0 and float(b) > 0 else 1.0
    except (ValueError, ZeroDivisionError):
        return 1.0


def target_box(job: Job, enc: E.VEnc | None = None) -> tuple[int, int]:
    """The frame the user asked for (0, 0 = as the source). A height alone (1.0 presets) is a 16:9 box."""
    v = job.settings.get("video", {})
    w, h = int(v.get("width", 0) or 0), int(v.get("height", 0) or 0)
    src = job.info.video if job.info else None
    if enc is not None and enc.id == "gif" and not h and src is not None and src.height > 480:
        h = 480
    if h and not w:
        w = _even(h * 16 / 9)
    return (w, h) if w and h else (0, 0)


def out_size(job: Job, enc: E.VEnc | None = None) -> tuple[int, int]:
    """The frame size of the result (what the scale filters make)."""
    src = job.info.video if job.info else None
    if src is None or not src.width:
        return 0, 0
    fw, fh, sar = F.pre_scale(job)
    w, h = target_box(job, enc)
    v = job.settings.get("video", {})
    if not w:
        return F.post_pad(job, fw, fh)
    if v.get("fit", "fit") in ("stretch", "pad"):
        return F.post_pad(job, w, h)
    dw, dh = fw * sar, float(fh)
    k = min(w / dw, h / dh)
    if not v.get("upscale"):
        k = min(k, 1.0)
    return F.post_pad(job, _even(dw * k), _even(dh * k))


def scale_filters(job: Job, enc: E.VEnc | None = None) -> list[str]:
    """Scale into the chosen frame: fit (inside, keeping the shape), pad (fit + black bars to exactly W×H),
    stretch (exactly W×H). Anamorphic sources (DVD, SAR ≠ 1) become square pixels on the way."""
    src = job.info.video if job.info else None
    w, h = target_box(job, enc)
    if src is None or not src.width or not w:
        return []
    fw, fh, sar = F.pre_scale(job)                       # the frame after crop / rotation
    v = job.settings.get("video", {})
    mode = v.get("fit", "fit")
    anamorphic = abs(sar - 1.0) > 0.001
    if mode == "stretch":
        if (w, h) == (fw, fh) and not anamorphic:
            return []
        return [f"scale={w}:{h}:flags=lanczos", "setsar=1"]
    dw, dh = fw * sar, float(fh)
    k = min(w / dw, h / dh)
    if not v.get("upscale"):
        k = min(k, 1.0)
    ow, oh = _even(dw * k), _even(dh * k)
    out = []
    if (ow, oh) != (fw, fh) or anamorphic:
        out += [f"scale={ow}:{oh}:flags=lanczos", "setsar=1"]
    if mode == "pad" and (ow, oh) != (w, h):
        out.append(f"pad={w}:{h}:({w}-iw)/2:({h}-ih)/2:color=black")
    return out


def res_label(v: dict) -> str:
    w, h = int(v.get("width", 0) or 0), int(v.get("height", 0) or 0)
    if not h:
        return ""
    if not w or w == _even(h * 16 / 9):
        return f"{h}p"
    return f"{w}×{h}"


def audio_kbps(codec: str, channels: int, cfg: dict) -> int:
    a = E.A_BY_ID.get(codec)
    if a is None or a.kbps == (0, 0):
        return 0
    k = int(cfg.get("kbps", 0) or 0)
    if k:
        return k
    return a.kbps[1] if channels > 2 else a.kbps[0]


def _concat_list(path: str, src: str, segs) -> None:
    q = src.replace("\\", "/").replace("'", "'\\''")
    with open(path, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for a, b in segs:
            f.write(f"file '{q}'\ninpoint {a:.3f}\noutpoint {b:.3f}\n")


def work_file(job: Job, kind: str, work_dir: str | None = None, ext: str = "txt") -> str:
    d = work_dir or os.path.join(tempfile.gettempdir(), "ArtCoreConvert")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"job{job.uid}_{os.getpid()}_{kind}.{ext}")


WORK_KINDS = (("pieces", "txt"), ("chapters", "txt"), ("vidstab", "trf"))


def pre_passes(job: Job, ffmpeg: str, out_path: str, av: E.Available, hw_decode: bool = True,
               work_dir: str | None = None) -> list[list[str]]:
    """The commands that run before the conversion: the analysis pass of vidstab (writes the motion file)."""
    if not needs_vidstab(job):
        return []
    return [build_args(job, ffmpeg, out_path, av, hw_decode, work_dir, analysis=True)]


def _graph_video(job: Job, enc, src: str, ov_in: int | None, transforms: str, detect: bool) -> list[str]:
    """The video part of the filter graph: src → the chain (with the overlay laid on) → [vout]."""
    chain = video_chain(job, enc, transforms, detect)
    if OVERLAY not in chain or ov_in is None:
        chain = [f for f in chain if f != OVERLAY]
        return [f"{src}{','.join(chain) if chain else 'null'}[vout]"]
    i = chain.index(OVERLAY)
    pre, post = chain[:i], chain[i + 1:]
    ov_chain, ov = F.overlay_parts(job, out_size(job, enc)[0])
    out = [f"{src}{','.join(pre) if pre else 'null'}[vpre]", f"[{ov_in}:v]{ov_chain}[wm]",
           f"[vpre][wm]{ov}" + ("," + ",".join(post) if post else "") + "[vout]"]
    return out


def build_args(job: Job, ffmpeg: str, out_path: str, av: E.Available, hw_decode: bool = True,
               work_dir: str | None = None, analysis: bool = False) -> list[str]:
    """The whole ffmpeg command line. Progress goes to stdout (-progress pipe:1), the log to stderr.
    Trim with several pieces and our own chapters need small text files: they go to work_dir (temp).
    analysis: the first pass of the two-pass stabilisation (video only, into nothing)."""
    st = job.settings
    v = st.get("video", {})
    cont = st.get("container", "mkv")
    c = E.container(cont)
    p = plan(job)
    enc = None
    copy_v = v.get("mode") == "copy"
    if p["video"] and copy_v and not c.takes_video(p["video"][0].codec):
        raise RuntimeError("bad_copy")
    if p["video"] and not copy_v:
        enc = av.resolve(v.get("encoder", "auto_hevc"))
        if enc is None:
            raise RuntimeError("no_encoder")
        if not c.takes_video(enc.family):
            raise RuntimeError("bad_codec")
        job.enc_used = enc.id
    else:
        job.enc_used = "copy" if p["video"] else ""
    if not p["video"] and not p["audio"]:
        raise RuntimeError("nothing_out")
    if analysis:
        p = dict(p, audio=[], subs=[])
    segs = segments(job)
    joined = multi_encode(job)
    meta_on = st.get("meta", True) and not analysis
    transforms = work_file(job, "vidstab", work_dir, "trf") if needs_vidstab(job) else ""
    hw = ["-hwaccel", "auto"] if enc is not None and enc.hw and hw_decode else []
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "info", "-nostats", "-progress", "pipe:1"]
    # inputs: 0 = the source (or the concat list); meta_in = where the file's tags / chapters come from
    if not segs:
        args += hw + ["-i", job.src]
        n_in, meta_in = 1, 0
    elif len(segs) == 1:
        a, b = segs[0]
        args += hw + ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", job.src]
        n_in, meta_in = 1, 0
    elif joined:
        for a, b in segs:
            args += hw + ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", job.src]
        n_in, meta_in = len(segs), 0
    else:                                                         # copied video / sound only: concat demuxer
        lst = work_file(job, "pieces", work_dir)
        _concat_list(lst, job.src, segs)
        args += ["-f", "concat", "-safe", "0", "-i", lst, "-i", job.src]
        n_in, meta_in = 2, 1
    ov_in = None                                                  # the picture of the Overlay row
    if enc is not None and enc.id != "gif" and F.is_on(job, "overlay") and not analysis:
        args += ["-i", F.fset(st, "overlay")["path"]]
        ov_in = n_in
        n_in += 1
    chapters = out_chapters(job)
    if analysis:
        args += ["-map_metadata", "-1", "-map_chapters", "-1"]
    elif chapters is not None and c.chapters:
        mf = work_file(job, "chapters", work_dir)
        with open(mf, "w", encoding="utf-8") as f:
            f.write(ffmetadata(chapters))
        args += ["-i", mf]
        ch_in = n_in
        n_in += 1
        args += ["-map_metadata", str(meta_in) if meta_on else "-1", "-map_chapters", str(ch_in)]
    else:
        args += ["-map_metadata", str(meta_in) if meta_on else "-1",
                 "-map_chapters", str(meta_in) if meta_on and c.chapters and chapters is None else "-1"]
    graph = []
    in_graph = joined or ov_in is not None                       # the video goes through -filter_complex
    # video
    for s in p["video"]:
        if not in_graph:
            args += ["-map", f"0:{s.index}"]
        if copy_v:
            args += ["-c:v", "copy"]
        else:
            bits = out_bits(job, enc)
            dur = out_duration(job)
            a_total = sum(audio_kbps(cd, F.layout_channels(job.acfg(a).get("channels", "keep"), a.channels, 8),
                                     job.acfg(a)) or
                          (a.bitrate // 1000 if cd == "copy" else 0) for a, cd in p["audio"])
            vv = dict(v)
            vv["audio_kbps"] = a_total
            vv["chroma"] = F.chroma(job, enc)
            if not vv.get("quality"):
                vv["quality"] = enc.q_default
            if not in_graph:
                vf = video_chain(job, enc, transforms, analysis)
                if vf:
                    args += ["-vf", ",".join(vf)]
            if analysis:
                args += ["-an", "-sn", "-dn", "-f", "null", "-"]
                return _with_graph(args, job, enc, segs, joined, p, ov_in, transforms, analysis, graph)
            args += ["-c:v", enc.ffname] + E.video_args(enc, vv, bits, dur)
            args += F.fps_args(job)
            if F.hdr_keeps(job, enc, bits) and not F.tonemap_on(job, enc, bits):      # keep HDR
                cl = s.color
                prim = cl.get("color_primaries", "bt2020")
                trc = cl.get("color_transfer", "smpte2084" if s.hdr == "HDR10" else "arib-std-b67")
                mat = cl.get("color_space", "bt2020nc")
                args += ["-color_primaries", prim, "-color_trc", trc, "-colorspace", mat]
                if enc.id == "x265":                                      # into the VUI itself, every build
                    i = args.index("-x265-params") + 1
                    args[i] += f":colorprim={prim}:transfer={trc}:colormatrix={mat}" + \
                        (":hdr10=1:repeat-headers=1" if s.hdr == "HDR10" else "")
            else:
                args += F.color_tags(job, enc, bits)
        fam = s.codec if copy_v else enc.family
        if cont in ("mp4", "mov") and fam == "hevc":
            args += ["-tag:v", "hvc1"]                                      # plays on Apple / TVs
        if cont == "avi" and fam == "mpeg4":
            args += ["-tag:v", "XVID"]
        if cont == "gif":
            args += ["-loop", "0"]
    _video_graph(args, job, enc, segs, joined, p, ov_in, transforms, analysis, graph)
    # audio
    for n, (s, codec) in enumerate(p["audio"]):
        a_cfg = job.acfg(s)
        af = F.audio_filters(a_cfg, codec, s.rate) if codec != "copy" else []
        if joined:
            if af:
                graph.append(f"[aj{n}]{','.join(af)}[ao{n}]")
            args += ["-map", f"[ao{n}]" if af else f"[aj{n}]"]
        else:
            args += ["-map", f"0:{s.index}"]
        if codec == "copy":
            args += [f"-c:a:{n}", "copy"]
            continue
        a = E.A_BY_ID[codec]
        ch = F.layout_channels(a_cfg.get("channels", "keep"), s.channels, a.max_ch)
        ffname, bits_args = F.audio_bits_args(codec, a.ffname, a_cfg, n)
        args += [f"-c:a:{n}", ffname] + bits_args
        if ch != s.channels or (codec == "opus" and ch > 2):          # opus wants the plain 5.1 / 7.1 layout
            args += [f"-ac:a:{n}", str(ch)]
        k = audio_kbps(codec, ch, a_cfg)
        if k and s.index in p.get("same_rate", ()) and s.bitrate and not a_cfg.get("kbps"):
            k = max(64, min(1536, int(round(s.bitrate / 1000 / 32)) * 32))     # as the source had it
        if k:
            args += [f"-b:a:{n}", f"{k}k"]
        if codec == "opus" and ch > 2:
            args += [f"-mapping_family:a:{n}", "1"]
        if af and not joined:
            args += [f"-filter:a:{n}", ",".join(af)]
    if graph:
        fi = args.index("-map_metadata")
        args[fi:fi] = ["-filter_complex", ";".join(graph)]
    # subtitles
    for n, (s, how) in enumerate(p["subs"]):
        args += ["-map", f"0:{s.index}", f"-c:s:{n}", how]
    if not p["subs"]:
        args += ["-sn"]
    args += meta_args(job, p, joined, meta_on)
    args += ["-dn", "-max_muxing_queue_size", "4096"]
    if cont in ("mp4", "mov", "m4a"):
        args += ["-movflags", "+faststart"]
    if cont == "m2ts":
        args += ["-mpegts_m2ts_mode", "1"]
    if cont == "mp3":
        args += ["-id3v2_version", "3"]
    args += ["-f", c.fmt, out_path]
    return args


def _video_graph(args, job, enc, segs, joined, p, ov_in, transforms, analysis, graph):
    """The concat of the pieces and / or the overlay: the video's part of -filter_complex, mapped as [vout]."""
    if not p["video"] or (not joined and ov_in is None):
        return
    s0 = p["video"][0]
    if joined:
        ins = "".join(f"[{i}:{s0.index}]" + "".join(f"[{i}:{a.index}]" for a, _ in p["audio"])
                      for i in range(len(segs)))
        outs = "[vj]" + "".join(f"[aj{k}]" for k in range(len(p["audio"])))
        graph.append(f"{ins}concat=n={len(segs)}:v=1:a={len(p['audio'])}{outs}")
        src = "[vj]"
    else:
        src = f"[0:{s0.index}]"
    graph += _graph_video(job, enc, src, ov_in, transforms, analysis)
    if analysis:
        return
    vi = args.index("-c:v")
    args[vi:vi] = ["-map", "[vout]"]


def _with_graph(args, job, enc, segs, joined, p, ov_in, transforms, analysis, graph):
    """The analysis pass: the graph goes in, [vout] is the one output."""
    if joined:
        _video_graph(args, job, enc, segs, joined, p, ov_in, transforms, analysis, graph)
        fi = args.index("-map_metadata")
        args[fi:fi] = ["-filter_complex", ";".join(graph)]
        ai = args.index("-an")
        args[ai:ai] = ["-map", "[vout]"]
    return args


def meta_args(job: Job, p: dict, joined: bool, meta_on: bool) -> list[str]:
    """-metadata / -disposition: the edited file tags; per stream the language, title and flags — the edited
    ones, and all of them for streams out of the concat filter (those carry no metadata of their own)."""
    out: list[str] = []
    tags = job.meta.get("tags")
    if tags is not None:
        for k in source_tags(job.info) if meta_on else {}:
            if k not in tags:
                out += ["-metadata", f"{k}="]                        # deleted
        for k, val in tags.items():
            if str(k).strip():
                out += ["-metadata", f"{str(k).strip()}={val}"]
    kinds = (("v", p["video"]), ("a", [s for s, _ in p["audio"]]), ("s", [s for s, _ in p["subs"]]))
    for spec, streams in kinds:
        edited_flags = any(("default" in job.smeta(s) or "forced" in job.smeta(s)) for s in streams)
        for n, s in enumerate(streams):
            m = job.smeta(s)
            from_graph = joined and spec in ("v", "a")
            for key, src_val in (("lang", s.lang), ("title", s.title)):
                if key in m:
                    out += [f"-metadata:s:{spec}:{n}", f"{'language' if key == 'lang' else 'title'}={m[key]}"]
                elif from_graph and src_val and meta_on:
                    out += [f"-metadata:s:{spec}:{n}", f"{'language' if key == 'lang' else 'title'}={src_val}"]
            if edited_flags or (from_graph and (s.default or s.forced)):
                d = m.get("default", s.default)
                f = m.get("forced", s.forced)
                out += [f"-disposition:{spec}:{n}", "+".join(x for x, on in (("default", d), ("forced", f)) if on) or "0"]
    return out


def out_name(job: Job, cfg: dict) -> str:
    """Where the result goes: next to the source or into the chosen folder, name + suffix, never over the
    source; an existing file gets « (2)» unless overwrite is on."""
    ext = "." + E.container(job.settings.get("container", "mkv")).ext
    if job.dest:                                         # chosen in «Save as…» (overwrite was asked there)
        path = os.path.splitext(job.dest)[0] + ext
        if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(job.src)):
            return path
    stem = os.path.splitext(os.path.basename(job.src))[0]
    folder = cfg.get("out_dir") if cfg.get("out_mode") == "folder" and cfg.get("out_dir") else os.path.dirname(job.src)
    base = os.path.join(folder, stem + (cfg.get("out_suffix") or ""))
    path = base + ext
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(job.src)):
        base += "_convert"
        path = base + ext
    if cfg.get("overwrite"):
        return path
    n = 2
    while os.path.exists(path):
        path = f"{base} ({n}){ext}"
        n += 1
    return path


def describe(st: dict, av: E.Available | None = None, tr=None) -> str:
    """A short profile line for the queue: «HEVC 10 bit · NVENC · CQ 26 · MKV»."""
    tr = tr or (lambda k, *a: k)
    v = st.get("video", {})
    c = E.container(st.get("container", "mkv"))
    bits = []
    if c.audio_only:
        pass
    elif v.get("mode") == "none":
        bits.append(tr("v_none"))
    elif v.get("mode") == "copy":
        bits.append(tr("v_copy"))
    else:
        eid = v.get("encoder", "auto_hevc")
        e = av.resolve(eid) if av is not None else E.BY_ID.get(eid)
        fam = E.FAMILY_LABEL.get(e.family if e else eid[5:], "?")
        b = v.get("bits", "auto")
        if e is not None and e.id == "prores":
            q = int(v.get("quality") or e.q_default)
            bits.append("ProRes " + E.PRORES_PROFILES[max(0, min(3, q - 1))])
        elif e is not None and e.id == "gif":
            bits.append("GIF")
        else:
            bits.append(fam + (f" {b} bit" if b in ("8", "10") and (e is None or len(e.bits) > 1) else ""))
            if e is not None:
                bits.append(E.VENDOR_LABEL[e.vendor] if e.hw else E.CPU_LABEL.get(e.id, e.ffname))
            rc = v.get("rc", "quality")
            if rc == "quality" or (e is not None and not e.rc):
                q = v.get("quality") or (e.q_default if e else 0)
                qn = "Q " if e is None or e.hw or e.family in ("mpeg4", "mpeg2video") else "CRF "
                bits.append(qn + str(q))
            elif rc == "bitrate":
                bits.append(f"{v.get('bitrate')} kbit/s")
            else:
                bits.append(f"{v.get('size_mb')} MB")
        if res_label(v):
            bits.append(res_label(v))
    a = st.get("audio", {})
    if a.get("codec") not in ("auto", "copy") and a.get("codec") in E.A_BY_ID:
        bits.append(E.A_BY_ID[a["codec"]].label.split(" (")[0])
    elif c.audio_only and a.get("codec") == "copy":
        bits.append(tr("a_copy_short"))
    bits.append(c.id.upper())
    return " · ".join(bits)
