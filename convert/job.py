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
forced}}}; Job.chapters — None = as in the source, otherwise [[start, title], …] of the result.
1.4: video.passes 1 | 2 (two passes for bit rate / size), split {mode: off | size | time, size_mb, minutes};
Job.joins — more files joined after src into one result (the trim pieces then lie on the joined timeline);
Job.ext — external tracks [{path, kind: audio | subtitle, delay (ms), lang, title, codec, kbps, default}]."""
from __future__ import annotations

import copy
import glob
import itertools
import os
import re
import tempfile
from dataclasses import dataclass, field

from . import encoders as E
from . import filters as F
from .probe import MediaInfo, Stream

DEFAULT_SETTINGS = {
    "container": "mkv",
    "video": {"mode": "encode", "encoder": "auto_hevc", "rc": "quality", "quality": 0, "bitrate": 8000,
              "size_mb": 4000, "speed": 5, "bits": "auto", "width": 0, "height": 0, "fit": "fit", "upscale": False,
              "fps": 0, "deint": "auto", "passes": 1},
    "audio": {"codec": "auto", "kbps": 0, "channels": "keep", "normalize": False, "tracks": "all"},
    "subs": "all",
    "lang": "",          # «Select streams»: only the audio / subtitles of this language ("" = any)
    "meta": True,
    "filters": F.defaults(),     # 1.3: the filter rows of the Streams tab (filters.py)
    "split": {"mode": "off", "size_mb": 700, "minutes": 20},     # 1.4: the result in parts
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
    joins: list = field(default_factory=list)       # 1.4: files joined after src (paths)
    ext: list = field(default_factory=list)         # 1.4: external tracks (dicts, see the module text)
    metrics: dict = field(default_factory=dict)     # 1.4: {"psnr": dB, "ssim": 0..1, "vmaf": 0..100}
    parts: list = field(default_factory=list)       # 1.4: the files of a split result
    infos: dict = field(default_factory=dict, repr=False)   # path → MediaInfo of joined / external files

    def to_dict(self) -> dict:
        return {"src": self.src, "settings": self.settings, "preset": self.preset, "tracks": self.tracks,
                "status": self.status if self.status in ("done", "error", "stop", "skip") else "wait",
                "out": self.out if self.status == "done" else "", "error": self.error,
                "trim": self.trim, "trim_cut": self.trim_cut, "meta": self.meta, "chapters": self.chapters,
                "dest": self.dest, "log": self.log_path, "crop_found": self.crop_found,
                "joins": list(self.joins), "ext": [dict(e) for e in self.ext],
                "metrics": dict(self.metrics) if self.status == "done" else {},
                "parts": list(self.parts) if self.status == "done" else []}

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
        j.joins = [str(x) for x in (d.get("joins") or []) if isinstance(x, str) and x]
        j.ext = [clean_ext(e) for e in (d.get("ext") or []) if isinstance(e, dict) and e.get("path")]
        j.metrics = {k: float(v) for k, v in (d.get("metrics") or {}).items() if isinstance(v, (int, float))}
        j.parts = [str(x) for x in (d.get("parts") or []) if isinstance(x, str)]
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
        n = ext_no(s)
        if n is not None:                                   # 1.4: an external track keeps its own settings
            e = self.ext[n] if n < len(self.ext) else {}
            return {"keep": True, "codec": e.get("codec", "")} | {k: e[k] for k in ("kbps", "channels", "normalize",
                                                                                   "gain") if k in e}
        return self.tracks.get(str(s.index), {})

    def acfg(self, s: Stream) -> dict:
        """The audio settings of one track: the common ones with this track's own (Streams tab) laid over."""
        out = dict(self.settings.get("audio", {}))
        t = self.track(s)
        for k in ("kbps", "channels", "normalize") + F.AUDIO_FILTER_KEYS:
            if k in t:
                out[k] = t[k]
        if ext_no(s) is not None:
            out["delay"] = 0                                # its delay moves the input (ext_delay), no filter
        return out

    def smeta(self, s: Stream) -> dict:
        """The user's edits of a stream's language / title / default / forced (Metadata tab)."""
        n = ext_no(s)
        if n is not None:
            e = self.ext[n] if n < len(self.ext) else {}
            m = {k: e[k] for k in ("lang", "title") if e.get(k)}
            if e.get("default"):
                m["default"] = True
            return m
        return (self.meta.get("streams") or {}).get(str(s.index), {})

    @property
    def edited(self) -> bool:
        return bool(self.trim or self.chapters is not None or self.meta.get("tags") is not None
                    or any(self.meta.get("streams") or {}))


EXT_KEYS = {"path": "", "kind": "audio", "delay": 0, "lang": "", "title": "", "codec": "", "kbps": 0,
            "default": False}
EXT_AUDIO = (".ac3", ".eac3", ".dts", ".aac", ".m4a", ".mka", ".mp3", ".flac", ".wav", ".opus", ".ogg", ".thd",
             ".mp2", ".wv", ".ape")
EXT_SUBS = (".srt", ".ass", ".ssa", ".sup", ".vtt", ".sub", ".idx", ".mks")


def clean_ext(e: dict) -> dict:
    out = {k: type(v)(e.get(k, v)) if e.get(k) is not None else v for k, v in EXT_KEYS.items()}
    out["kind"] = "subtitle" if out["kind"] == "subtitle" else "audio"
    return out


def ext_kind(path: str) -> str:
    return "subtitle" if path.lower().endswith(EXT_SUBS) else "audio"


def ext_no(s: Stream) -> int | None:
    """The number of the external track this stream came from (None: a stream of the source)."""
    return getattr(s, "ext", None)


# ── 1.4: the other files of a task (joined ones, external tracks) ─────────
def file_info(job: Job, path: str) -> MediaInfo | None:
    return job.infos.get(path)


def load_files(job: Job) -> None:
    """ffprobe the joined / external files not looked at yet (the window does it in its worker first)."""
    from . import probe as P
    for path in list(job.joins) + [e["path"] for e in job.ext]:
        if path not in job.infos:
            job.infos[path] = P.probe(path)


def sources(job: Job) -> list[tuple[str, MediaInfo | None]]:
    """The files of the joined timeline: src, then the joined ones."""
    return [(job.src, job.info)] + [(p, file_info(job, p)) for p in job.joins]


def _dur(info: MediaInfo | None) -> float:
    return info.duration if info is not None and not info.error else 0.0


def src_duration(job: Job) -> float:
    """The length of the timeline the trim works on: the source, or every joined file one after another."""
    return sum(_dur(i) for _p, i in sources(job)) if job.info is not None else 0.0


def media_src(job: Job) -> str:
    """What ffmpeg reads for the strip, the loudness and the still frames: src, or for a joined task a
    concat list of all its files (ff.inp reads it)."""
    if not job.joins:
        return job.src
    files = [p for p, i in sources(job) if i is not None and not i.error]
    import hashlib
    key = hashlib.sha1("\n".join(files).encode("utf-8")).hexdigest()[:12]
    d = os.path.join(tempfile.gettempdir(), "ArtCoreConvert")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"join_{key}.ffconcat")
    if not os.path.isfile(path):
        _concat_list(path, files, [(k, None, None) for k in range(len(files))])
    return path


def preview_src(job: Job) -> str:
    """What mpv plays on the Trim tab: src, or the joined files one after another (an EDL)."""
    if not job.joins:
        return job.src
    files = [p for p, i in sources(job) if i is not None and not i.error]
    return "edl://" + ";".join(f"%{len(p.encode('utf-8'))}%{p}" for p in files)


def ext_streams(job: Job) -> list[tuple[int, Stream]]:
    """[(its number in job.ext, the stream)] — the first stream of the chosen kind in every external file."""
    out = []
    for n, e in enumerate(job.ext):
        info = file_info(job, e["path"])
        if info is None or info.error:
            continue
        ss = info.of(e.get("kind", "audio"))
        if ss:
            s = copy.copy(ss[0])
            s.ext = n
            out.append((n, s))
    return out


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
    with trim_cut the pieces are what is thrown away. 1.4: the times are of the joined timeline (src_duration)."""
    dur = src_duration(job)
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


def pieces(job: Job) -> list[tuple[int, float, float]]:
    """1.4: what is read from which file: [(file number in sources(), start, end)] in the file's own time.
    [] = the whole src (no trim, nothing joined)."""
    segs = segments(job)
    if not job.joins:
        return [(0, a, b) for a, b in segs]
    files = sources(job)
    if not segs:
        segs = [(0.0, src_duration(job))]
    out, base = [], []
    pos = 0.0
    for _p, info in files:
        base.append(pos)
        pos += _dur(info)
    for a, b in segs:
        for k, (_p, info) in enumerate(files):
            lo, hi = base[k], base[k] + _dur(info)
            x, y = max(a, lo), min(b, hi)
            if y - x > 0.04:
                out.append((k, round(x - lo, 3), round(y - lo, 3)))
    return out


def match_stream(job: Job, k: int, s: Stream) -> Stream | None:
    """The stream of file k that stands for the source's stream s (the same kind, the same place among them)."""
    if k == 0 or ext_no(s) is not None:
        return s
    info = sources(job)[k][1]
    if info is None or info.error or job.info is None:
        return None
    mine = [x.index for x in job.info.of(s.kind)]
    other = info.of(s.kind)
    i = mine.index(s.index) if s.index in mine else -1
    return other[i] if 0 <= i < len(other) else None


def out_duration(job: Job) -> float:
    pcs = pieces(job)
    if pcs:
        return sum(b - a for _k, a, b in pcs)
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
    elif job.joins and job.info and job.settings.get("meta", True):        # 1.4: a chapter per joined file
        starts, pos, last = [], 0.0, None
        files = sources(job)
        for k, a, b in pieces(job):
            if k != last:
                starts.append((pos, os.path.splitext(os.path.basename(files[k][0]))[0]))
                last = k
            pos += b - a
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
    """Several pieces (trim pieces, joined files) put together by the concat filter: always when the video is
    encoded; with joined files also when only sound goes out (other files: other codecs, decoded)."""
    if job.info is None:
        return False
    pcs = pieces(job)
    if len(pcs) <= 1:
        return False
    c = E.container(job.settings.get("container", "mkv"))
    mode = job.settings.get("video", {}).get("mode", "encode")
    if job.info.video is not None and not c.audio_only and mode == "encode":
        return True
    return bool(job.joins) and (c.audio_only or mode == "none" or job.info.video is None)


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
    # keyframe the seek lands on) — it is encoded again, into its own codec where we have one. 1.4: the
    # concat filter decodes the sound too (joined files)
    recut = (bool(segments(job)) and bool(out["video"]) and vcfg.get("mode") != "copy") or joined
    if job.joins:
        files = sources(job)
        if any(i is None or i.error for _p, i in files[1:]):
            w.append("w_join_missing")
        if out["video"] and vcfg.get("mode") == "copy":
            v0 = out["video"][0]
            for _p, i in files[1:]:
                vk = i.video if i is not None and not i.error else None
                if vk is not None and (vk.codec, vk.width, vk.height) != (v0.codec, v0.width, v0.height):
                    w.append("w_join_copy")
                    break
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
    # 1.4: external sound — after the source's own tracks; cut / joined: trimmed by filters, so encoded
    segs = segments(job)
    for n, s in ext_streams(job):
        if job.ext[n].get("kind") != "audio" or (c.audio is not None and not c.audio):
            continue
        if c.one_audio and out["audio"]:
            w.append("w_one_audio")
            break
        codec = job.ext[n].get("codec") or a_cfg.get("codec", "auto")
        tc = job.acfg(s)
        changes = F.audio_on(tc) or bool(segs)
        if codec == "auto":
            codec = "copy" if c.takes_audio(s.codec) and not changes else c.a_default
        if codec != "copy" and not c.takes_audio(E.A_CODEC.get(codec, codec)):
            codec = c.a_default
            w.append("w_audio_reencoded")
        if codec == "copy" and (changes or not c.takes_audio(s.codec)):
            same = next((k for k, cn in E.A_CODEC.items() if cn == s.codec), "")
            codec = same if same and c.takes_audio(s.codec) else (c.a_default or "aac")
            if codec == same:
                out["same_rate"].add(("ext", n))
            w.append("w_ext_reencoded" if segs else "w_audio_reencoded")
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
    for n, s in ext_streams(job):                                  # 1.4: external subtitles
        if job.ext[n].get("kind") != "subtitle":
            continue
        if c.subs == "none":
            if not c.audio_only:
                w.append("w_subs_dropped")
            continue
        if len(segs) > 1:
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


def _concat_list(path: str, files: list[str], pcs) -> None:
    """The concat demuxer's list: every piece as a file entry (in / out points where it is cut)."""
    with open(path, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for k, a, b in pcs:
            q = files[k].replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{q}'\n")
            if a is not None:
                f.write(f"inpoint {a:.3f}\noutpoint {b:.3f}\n")


def work_file(job: Job, kind: str, work_dir: str | None = None, ext: str = "txt") -> str:
    d = work_dir or os.path.join(tempfile.gettempdir(), "ArtCoreConvert")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"job{job.uid}_{os.getpid()}_{kind}.{ext}")


WORK_KINDS = (("pieces", "txt"), ("chapters", "txt"), ("vidstab", "trf"), ("frame", "png"))


def pass_prefix(job: Job, work_dir: str | None = None) -> str:
    """Where the first pass writes its statistics (x264 adds «-0.log», x265 «.x265.log»)."""
    return os.path.splitext(work_file(job, "2pass", work_dir, "log"))[0]


def cleanup(job: Job, work_dir: str | None = None) -> None:
    """The small files of a run: the trim list, chapters, vid.stab motion, the 2-pass statistics, the frame."""
    for kind, ext in WORK_KINDS:
        try:
            os.remove(work_file(job, kind, work_dir, ext))
        except OSError:
            pass
    for f in glob.glob(glob.escape(pass_prefix(job, work_dir)) + "*"):
        try:
            os.remove(f)
        except OSError:
            pass


def resolve(job: Job, av: E.Available) -> E.VEnc | None:
    v = job.settings.get("video", {})
    if v.get("mode", "encode") != "encode" or job.info is None or job.info.video is None or \
            E.container(job.settings.get("container", "mkv")).audio_only:
        return None
    return av.resolve(v.get("encoder", "auto_hevc"))


def two_pass(job: Job, enc: E.VEnc | None, av: E.Available) -> bool:
    """1.4: a first pass runs before the encode (bit rate / file size, an encoder that has one)."""
    v = job.settings.get("video", {})
    return enc is not None and enc.rc and int(v.get("passes", 1) or 1) == 2 and \
        v.get("rc", "quality") in ("bitrate", "size") and v.get("mode", "encode") == "encode" and av.two_pass(enc)


def pre_passes(job: Job, ffmpeg: str, out_path: str, av: E.Available, hw_decode: bool = True,
               work_dir: str | None = None, threads: int = 0, preview: str = "") -> list[list[str]]:
    """The commands that run before the conversion: the analysis pass of vid.stab (writes the motion file),
    1.4: the first pass of the two-pass encode (writes its statistics)."""
    out = []
    if needs_vidstab(job):
        out.append(build_args(job, ffmpeg, out_path, av, hw_decode, work_dir, mode="vidstab", threads=threads))
    load_files(job)
    if two_pass(job, resolve(job, av), av):
        out.append(build_args(job, ffmpeg, out_path, av, hw_decode, work_dir, mode="pass1", threads=threads,
                              preview=preview))
    return out


METRICS = ("psnr", "ssim", "vmaf")


def can_metrics(job: Job, av: E.Available) -> bool:
    enc = resolve(job, av)
    return enc is not None and enc.id != "gif"


def post_passes(job: Job, ffmpeg: str, out_path: str, av: E.Available, metrics=(), work_dir: str | None = None,
                threads: int = 0) -> list[list[str]]:
    """1.4: what runs on the finished file — the quality metrics against the filtered source."""
    want = [m for m in METRICS if m in (metrics or ()) and (m != "vmaf" or "libvmaf" in av.filters)]
    if not want or not can_metrics(job, av):
        return []
    return [build_args(job, ffmpeg, out_path, av, False, work_dir, mode="metrics", threads=threads, metrics=want)]


_RE_PSNR = re.compile(r"PSNR .*average:\s*([0-9.]+|inf)")
_RE_SSIM = re.compile(r"SSIM .*All:\s*([0-9.]+)")
_RE_VMAF = re.compile(r"VMAF score[:=]\s*([0-9.]+)")


def parse_metrics(lines) -> dict:
    """The summary lines of psnr / ssim / libvmaf in ffmpeg's log → {"psnr": 41.2, "ssim": 0.987, "vmaf": 95.1}."""
    out = {}
    for ln in lines:
        for key, rx in (("psnr", _RE_PSNR), ("ssim", _RE_SSIM), ("vmaf", _RE_VMAF)):
            m = rx.search(ln)
            if m:
                out[key] = 100.0 if m.group(1) == "inf" else float(m.group(1))
    return out


def _sar(s: Stream) -> str:
    return "1" if not s.sar or s.sar in ("0:1", "1:1") else s.sar.replace(":", "/")


def _fps_str(f: float) -> str:
    n = round(f * 1.001)
    return f"{n * 1000}/1001" if n and abs(f - n * 1000 / 1001) < 0.002 and abs(f - n) > 0.01 else f"{f:.6g}"


def _layout(s: Stream) -> str:
    return s.layout or f"{s.channels or 2}c"


def _concat_graph(job: Job, p: dict, pcs, graph: list[str]) -> tuple[str | None, list[str]]:
    """The concat filter over the inputs 0 … n-1 (one per piece): every piece made like the first file
    (frame size, pixel aspect, frame rate; sample rate, channels; silence / black where a joined file lacks a
    track). Returns the video label (or None) and the labels of the source's sound tracks."""
    v0 = p["video"][0] if p["video"] else None
    auds = [s for s, _c in p["audio"] if ext_no(s) is None]
    files = sources(job)
    ins = ""
    if v0 is not None:
        W, H, sar0, fps0 = v0.width, v0.height, _sar(v0), v0.fps or 25.0
        vids = [match_stream(job, k, v0) for k, _a, _b in pcs]
        mixed = any(x is None or (x.width, x.height, _sar(x)) != (W, H, sar0) for x in vids)
    for i, (k, a, b) in enumerate(pcs):
        if v0 is not None:
            sv = vids[i]
            if sv is None:                                         # a file with no picture: black
                graph.append(f"color=c=black:s={W}x{H}:r={_fps_str(fps0)}:d={b - a:.3f},setsar={sar0}[cv{i}]")
                ins += f"[cv{i}]"
            else:
                ch = []
                if (sv.width, sv.height) != (W, H) or _sar(sv) != sar0:
                    ch += [f"scale={W}:{H}:force_original_aspect_ratio=decrease", f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"]
                if mixed:
                    ch.append(f"setsar={sar0}")
                if k != 0 and sv.fps and abs(sv.fps - fps0) > 0.01:
                    ch.append(f"fps={_fps_str(fps0)}")
                if ch:
                    graph.append(f"[{i}:{sv.index}]{','.join(ch)}[cv{i}]")
                    ins += f"[cv{i}]"
                else:
                    ins += f"[{i}:{sv.index}]"
        for n, s in enumerate(auds):
            sa = match_stream(job, k, s)
            rate = s.rate or 48000
            if sa is None:                                         # a file without this track: silence
                graph.append(f"anullsrc=channel_layout={_layout(s)}:sample_rate={rate},atrim=duration={b - a:.3f}"
                             f"[cs{i}_{n}]")
                ins += f"[cs{i}_{n}]"
            elif k != 0 and ((sa.rate or rate) != rate or sa.channels != s.channels):
                graph.append(f"[{i}:{sa.index}]aresample={rate},aformat=sample_rates={rate}:"
                             f"channel_layouts={_layout(s)}[ca{i}_{n}]")
                ins += f"[ca{i}_{n}]"
            else:
                ins += f"[{i}:{sa.index}]"
    outs = ("[vj]" if v0 is not None else "") + "".join(f"[aj{n}]" for n in range(len(auds)))
    graph.append(f"{ins}concat=n={len(pcs)}:v={1 if v0 is not None else 0}:a={len(auds)}{outs}")
    return ("[vj]" if v0 is not None else None), [f"[aj{n}]" for n in range(len(auds))]


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


def ext_delay(job: Job, s: Stream) -> float:
    n = ext_no(s)
    return float(job.ext[n].get("delay", 0) or 0) / 1000 if n is not None and n < len(job.ext) else 0.0


def _ext_input(job: Job, s: Stream, segs) -> list[str]:
    """The input options of an external track: its delay moves it (or, cut / joined, its filters do)."""
    d = ext_delay(job, s)
    path = job.ext[ext_no(s)]["path"]
    if s.kind == "subtitle" and segs:                               # one piece (more: the subtitles are dropped)
        a = segs[0][0]
        opt = []
        if a - d > 0.0005:
            opt += ["-ss", f"{a - d:.3f}"]
        if d - a > 0.0005:
            opt += ["-itsoffset", f"{d - a:.3f}"]
        return opt + ["-i", path]
    if segs:                                                        # sound: aligned and cut in the graph
        return ["-i", path]
    if d > 0:
        return ["-itsoffset", f"{d:.3f}", "-i", path]
    if d < 0:
        return ["-ss", f"{-d:.3f}", "-i", path]
    return ["-i", path]


def _ext_audio_graph(job: Job, s: Stream, inp: int, segs, n: int, af: list[str], graph: list[str]) -> str:
    """An external sound track on the cut / joined timeline: delayed, cut into the pieces, put together."""
    d = ext_delay(job, s)
    head = f"[{inp}:{s.index}]"
    align = f"adelay=delays={int(round(d * 1000))}:all=1" if d > 0 else \
        f"atrim=start={-d:.3f},asetpts=PTS-STARTPTS" if d < 0 else "anull"
    tail = ("," + ",".join(af)) if af else ""
    if len(segs) == 1:
        a, b = segs[0]
        graph.append(f"{head}{align},atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS{tail}[ao{n}]")
        return f"[ao{n}]"
    m = len(segs)
    graph.append(f"{head}{align},asplit={m}" + "".join(f"[e{n}_{i}]" for i in range(m)))
    for i, (a, b) in enumerate(segs):
        graph.append(f"[e{n}_{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[e{n}t{i}]")
    graph.append("".join(f"[e{n}t{i}]" for i in range(m)) + f"concat=n={m}:v=0:a=1{tail}[ao{n}]")
    return f"[ao{n}]"


def build_args(job: Job, ffmpeg: str, out_path: str, av: E.Available, hw_decode: bool = True,
               work_dir: str | None = None, analysis: bool = False, *, mode: str = "main", threads: int = 0,
               preview: str = "", metrics=()) -> list[str]:
    """The whole ffmpeg command line. Progress goes to stdout (-progress pipe:1), the log to stderr.
    Trim with several pieces and our own chapters need small text files: they go to work_dir (temp).
    mode: main — the conversion (the second pass of a two-pass encode); vidstab — the analysis pass of the
    two-pass stabilisation (analysis=True, 1.3); pass1 — the first pass (statistics); metrics — PSNR / SSIM /
    VMAF of out_path against the filtered source. threads: the CPU limit (0 = all of it). preview: a PNG the
    frame being encoded is written to (once a second of the film)."""
    if analysis:
        mode = "vidstab"
    load_files(job)
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
    side = mode != "main"
    if side and (enc is None):
        raise RuntimeError("nothing_out")
    pass_n = 1 if mode == "pass1" else 2 if mode == "main" and two_pass(job, enc, av) else 0
    if side:
        p = dict(p, audio=[], subs=[])
    segs = segments(job)
    pcs = pieces(job)
    files = sources(job)
    joined = multi_encode(job)
    meta_on = st.get("meta", True) and not side
    transforms = work_file(job, "vidstab", work_dir, "trf") if needs_vidstab(job) else ""
    hw = ["-hwaccel", "auto"] if enc is not None and enc.hw and hw_decode else []
    dec = ["-threads", str(threads)] if threads > 0 else []
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "info", "-nostats", "-progress", "pipe:1"]
    if threads > 0:
        args += ["-filter_threads", str(threads), "-filter_complex_threads", str(threads)]
    # inputs: 0 = the source (or the concat list); meta_in = where the file's tags / chapters come from;
    # base = the file input 0 reads (a single piece can lie in a joined file)
    base = 0
    if not pcs:
        args += hw + dec + ["-i", job.src]
        n_in, meta_in = 1, 0
    elif len(pcs) == 1:
        base, a, b = pcs[0]
        args += hw + dec + ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", files[base][0]]
        n_in, meta_in = 1, 0
    elif joined:
        for k, a, b in pcs:
            args += hw + dec + ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", files[k][0]]
        n_in, meta_in = len(pcs), 0
    else:                                                         # copied video / sound only: concat demuxer
        lst = work_file(job, "pieces", work_dir)
        full = [(k, None if (a <= 0.001 and b >= _dur(files[k][1]) - 0.001) else a, b) for k, a, b in pcs]
        _concat_list(lst, [f for f, _i in files], full)
        args += ["-f", "concat", "-safe", "0", "-i", lst, "-i", job.src]
        n_in, meta_in = 2, 1
        base = pcs[0][0]
    ov_in = None                                                  # the picture of the Overlay row
    if enc is not None and enc.id != "gif" and F.is_on(job, "overlay") and mode != "vidstab":
        args += ["-i", F.fset(st, "overlay")["path"]]
        ov_in = n_in
        n_in += 1
    ext_in: dict[int, int] = {}                                   # 1.4: external track → its input
    for s, _how in p["audio"] + p["subs"]:
        if ext_no(s) is not None:
            args += _ext_input(job, s, segs)
            ext_in[ext_no(s)] = n_in
            n_in += 1
    dist_in = None
    if mode == "metrics":
        args += ["-i", out_path]
        dist_in = n_in
        n_in += 1
    chapters = out_chapters(job)
    if side:
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
    graph: list[str] = []
    a_labels: list[str] = []
    want_preview = bool(preview) and enc is not None and enc.id != "gif" and mode in ("main", "pass1")
    in_graph = joined or ov_in is not None or want_preview or mode == "metrics"
    vsrc = vmap = ""
    if joined:
        vlab, a_labels = _concat_graph(job, p, pcs, graph)
        vsrc = vlab or ""
    elif p["video"]:
        sv = match_stream(job, base, p["video"][0]) or p["video"][0]
        vsrc, vmap = f"[0:{sv.index}]", f"0:{sv.index}"
    # video
    for s in p["video"]:
        if copy_v:
            args += ["-map", vmap, "-c:v", "copy"]
        else:
            if in_graph:
                graph += _graph_video(job, enc, vsrc, ov_in, transforms, mode == "vidstab")
                vmap = "[vout]"
            else:
                vf = video_chain(job, enc, transforms, mode == "vidstab")
                if vf:
                    args += ["-vf", ",".join(vf)]
            if want_preview:                                      # the frame on the Status tab
                graph[-1] = graph[-1][:-len("[vout]")] + "[vfull]"
                graph.append("[vfull]split[vout][vpv];[vpv]fps=1,scale=320:-2:flags=bilinear,format=rgb24[vprev]")
            args += ["-map", vmap]
            if mode == "vidstab":
                break
            if mode == "metrics":
                bits = out_bits(job, enc)
                fmt = "yuv420p10le" if bits > 8 else "yuv420p"
                graph[-1] = graph[-1][:-len("[vout]")] + "[vref]"
                k = len(metrics)
                same = f"settb=1/1000,setpts=N*1000,format={fmt}"         # frame by frame, not by time stamps
                graph.append(f"[vref]{same},split={k}" + "".join(f"[r{i}]" for i in range(k)))
                cur = f"[{dist_in}:v:0]{same}[d0]"
                graph.append(cur)
                for i, m in enumerate(metrics):
                    name = {"psnr": "psnr", "ssim": "ssim",
                            "vmaf": f"libvmaf=n_threads={max(1, threads or os.cpu_count() or 4)}"}[m]
                    graph.append(f"[d{i}][r{i}]{name}[d{i + 1}]")
                args[args.index(vmap)] = f"[d{k}]"
                break
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
            if mode == "pass1" and not a_total:                    # the second pass counts the sound in
                vv["audio_kbps"] = sum(audio_kbps(cd, F.layout_channels(job.acfg(a).get("channels", "keep"),
                                                                         a.channels, 8), job.acfg(a)) or
                                       (a.bitrate // 1000 if cd == "copy" else 0)
                                       for a, cd in plan(job)["audio"])
            args += ["-c:v", enc.ffname] + E.video_args(enc, vv, bits, dur) + E.thread_args(enc, threads)
            if enc.id == "x265":
                i = args.index("-x265-params") + 1
                if threads > 0:
                    args[i] += f":pools={threads}"
                if pass_n:
                    args[i] += f":pass={pass_n}:stats={E.x265_path(pass_prefix(job, work_dir) + '.x265.log')}"
            if pass_n:
                args += E.pass_args(enc, pass_n, pass_prefix(job, work_dir))
            if mode == "pass1":
                break
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
    if side:
        if graph:
            fi = args.index("-map_metadata")
            args[fi:fi] = ["-filter_complex", ";".join(graph)]
        args += ["-an", "-sn", "-dn", "-f", "null", "-"]
        if want_preview:
            args += _preview_out(preview)
        return args
    # audio
    for n, (s, codec) in enumerate(p["audio"]):
        a_cfg = job.acfg(s)
        af = F.audio_filters(a_cfg, codec, s.rate) if codec != "copy" else []
        en = ext_no(s)
        if en is not None and segs:
            args += ["-map", _ext_audio_graph(job, s, ext_in[en], segs, n, af, graph)]
            af = []
        elif en is not None:
            args += ["-map", f"{ext_in[en]}:{s.index}"]
        elif joined:
            lab = a_labels[n]
            if af:
                graph.append(f"{lab}{','.join(af)}[ao{n}]")
                lab = f"[ao{n}]"
            args += ["-map", lab]
            af = []
        else:
            sa = match_stream(job, base, s) or s
            args += ["-map", f"0:{sa.index}"]
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
        key = ("ext", en) if en is not None else s.index
        if k and key in p.get("same_rate", ()) and s.bitrate and not a_cfg.get("kbps"):
            k = max(64, min(1536, int(round(s.bitrate / 1000 / 32)) * 32))     # as the source had it
        if k:
            args += [f"-b:a:{n}", f"{k}k"]
        if codec == "opus" and ch > 2:
            args += [f"-mapping_family:a:{n}", "1"]
        if af:
            args += [f"-filter:a:{n}", ",".join(af)]
    if graph:
        fi = args.index("-map_metadata")
        args[fi:fi] = ["-filter_complex", ";".join(graph)]
    # subtitles
    for n, (s, how) in enumerate(p["subs"]):
        en = ext_no(s)
        if en is not None:
            args += ["-map", f"{ext_in[en]}:{s.index}", f"-c:s:{n}", how]
            continue
        ss = match_stream(job, base, s) or s
        args += ["-map", f"0:{ss.index}", f"-c:s:{n}", how]
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
    if want_preview:
        args += _preview_out(preview)
    return args


def _preview_out(path: str) -> list[str]:
    """The second output: the frame being encoded, one PNG written over and over."""
    return ["-map", "[vprev]", "-c:v", "png", "-update", "1", "-f", "image2", path]


# ── 1.4: the result in parts ─────────────────────────────────
def split_on(job: Job) -> bool:
    sp = job.settings.get("split") or {}
    mode = sp.get("mode", "off")
    return job.settings.get("container", "mkv") != "gif" and (
        (mode == "size" and float(sp.get("size_mb", 0) or 0) > 0) or
        (mode == "time" and float(sp.get("minutes", 0) or 0) > 0))


def split_pattern(out: str) -> str:
    stem, ext = os.path.splitext(out)
    return stem.replace("%", "%%") + "_%02d" + ext


def split_parts(out: str) -> list[str]:
    stem, ext = os.path.splitext(out)
    rx = re.compile(re.escape(os.path.basename(stem)) + r"_\d{2,}" + re.escape(ext) + "$")
    d = os.path.dirname(out) or "."
    try:
        names = sorted(f for f in os.listdir(d) if rx.match(f))
    except OSError:
        return []
    return [os.path.join(d, f) for f in names]


def split_args(job: Job, ffmpeg: str, src: str, times: list[float] | None = None) -> list[str]:
    """The finished file cut (copied, at keyframes) into parts: by time every N minutes, or at the given
    times (by size: size_times finds them). The parts are <name>_01.ext, <name>_02.ext …"""
    c = E.container(job.settings.get("container", "mkv"))
    sp = job.settings.get("split") or {}
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "info", "-nostats", "-progress", "pipe:1",
            "-i", src, "-map", "0", "-c", "copy", "-map_metadata", "0", "-f", "segment", "-segment_format", c.fmt,
            "-segment_start_number", "1", "-reset_timestamps", "1"]
    if times is not None:
        args += ["-segment_times", ",".join(f"{max(0.0, x - 0.0005):.3f}" for x in times) or "1e9"]
    else:
        args += ["-segment_time", f"{float(sp.get('minutes', 20)) * 60:g}"]
    if c.fmt in ("mp4", "mov"):
        args += ["-segment_format_options", "movflags=+faststart"]
    return args + [split_pattern(src)]


def estimate_size_times(job: Job, av: E.Available) -> list[float]:
    """For «Copy the ffmpeg command» only: the cut times of a split by size from the planned bit rate
    (the real ones are read from the finished file)."""
    sp = job.settings.get("split") or {}
    dur = out_duration(job)
    v = job.settings.get("video", {})
    p = plan(job)
    a = sum(audio_kbps(cd, F.layout_channels(job.acfg(s).get("channels", "keep"), s.channels, 8), job.acfg(s)) or
            (s.bitrate // 1000 if cd == "copy" else 0) for s, cd in p["audio"])
    if v.get("rc") == "size" and dur > 0:
        kbps = float(v.get("size_mb", 0)) * 8 * 1024 * 1024 / 1000 / dur
    elif v.get("rc") == "bitrate":
        kbps = float(v.get("bitrate", 0)) + a
    else:
        kbps = (job.info.bitrate / 1000) if job.info and job.info.bitrate else 8000
    step = float(sp.get("size_mb", 700) or 700) * 8 * 1024 * 1024 * 0.97 / 1000 / max(1.0, kbps)
    out, x = [], step
    while x < dur - 1:
        out.append(round(x, 3))
        x += step
    return out


def cut_times(packets: list[tuple[float, int, bool]], limit: int) -> list[float]:
    """Where to cut for parts of at most `limit` bytes: packets [(time, size, a keyframe of the video)] in time
    order → the keyframe times to cut at (2 % and 64 KiB of every part are left for the container)."""
    room = max(1, int(limit * 0.98) - 65536)
    cuts: list[float] = []
    start, total, last = 0, 0, None                 # bytes at the last cut, bytes so far, (time, bytes) of a key
    for t, size, key in packets:
        if key:
            if total - start > room and last is not None and last[1] > start:
                cuts.append(last[0])
                start = last[1]
            if total - start > room and total > start:           # one GOP bigger than a part
                cuts.append(t)
                start = total
            last = (t, total)
        total += size
    if total - start > room and last is not None and last[1] > start:
        cuts.append(last[0])
    return [x for x in cuts if x > 0]


def size_times(ffprobe: str, path: str, size_mb: float) -> list[float]:
    """ffprobe the packets of the finished file and pick the keyframes to cut it at for size_mb parts."""
    from . import ff
    r = ff.run([ffprobe, "-v", "error", "-show_entries", "stream=index,codec_type", "-of", "csv=p=0", path], 60)
    kinds = {}
    for ln in r.stdout.splitlines():
        bits = ln.strip().split(",")
        if len(bits) >= 2 and bits[0].isdigit():
            kinds[int(bits[0])] = bits[1]
    has_v = "video" in kinds.values()
    r = ff.run([ffprobe, "-v", "error", "-show_entries", "packet=stream_index,pts_time,dts_time,size,flags",
                "-of", "csv=p=0", path], 3600)
    pk = []
    for ln in r.stdout.splitlines():
        b = ln.strip().split(",")
        if len(b) < 5:
            continue
        try:
            idx = int(b[0])
            t = float(b[1]) if b[1] not in ("", "N/A") else float(b[2])
            size = int(b[3])
        except ValueError:
            continue
        video = kinds.get(idx) == "video"
        key = "K" in b[4] and (video or not has_v)
        pk.append((t, size, key))
    pk.sort(key=lambda x: x[0])
    # the segment muxer counts from the first frame of the picture (its first keyframe)
    t0 = next((t for t, _s, k in pk if k), pk[0][0] if pk else 0.0)
    return [round(x - t0, 3) for x in cut_times(pk, int(float(size_mb) * 1024 * 1024)) if x - t0 > 0.01]


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
        ext_default = any(ext_no(s) is not None and job.smeta(s).get("default") for s in streams)
        for n, s in enumerate(streams):
            m = job.smeta(s)
            from_graph = joined and spec in ("v", "a")
            for key, src_val in (("lang", s.lang), ("title", s.title)):
                if key in m:
                    out += [f"-metadata:s:{spec}:{n}", f"{'language' if key == 'lang' else 'title'}={m[key]}"]
                elif from_graph and src_val and meta_on:
                    out += [f"-metadata:s:{spec}:{n}", f"{'language' if key == 'lang' else 'title'}={src_val}"]
            if edited_flags or (from_graph and (s.default or s.forced)):
                d = m.get("default", False if ext_default else s.default)
                f = m.get("forced", s.forced)
                out += [f"-disposition:{spec}:{n}", "+".join(x for x, on in (("default", d), ("forced", f)) if on) or "0"]
    return out


def out_name(job: Job, cfg: dict, taken=()) -> str:
    """Where the result goes: next to the source or into the chosen folder, name + suffix, never over the
    source; an existing file gets « (2)» unless overwrite is on. taken: results of tasks running now (1.4)."""
    taken = {os.path.normcase(os.path.abspath(x)) for x in taken}
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
    def used(x):
        return os.path.normcase(os.path.abspath(x)) in taken
    if cfg.get("overwrite") and not used(path):
        return path
    n = 2
    while (os.path.exists(path) and not cfg.get("overwrite")) or used(path):
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
            if rc in ("bitrate", "size") and int(v.get("passes", 1) or 1) == 2 and (e is None or e.rc):
                bits[-1] += " · " + tr("two_pass_short")
        if res_label(v):
            bits.append(res_label(v))
    a = st.get("audio", {})
    if a.get("codec") not in ("auto", "copy") and a.get("codec") in E.A_BY_ID:
        bits.append(E.A_BY_ID[a["codec"]].label.split(" (")[0])
    elif c.audio_only and a.get("codec") == "copy":
        bits.append(tr("a_copy_short"))
    bits.append(c.id.upper())
    sp = st.get("split") or {}
    if sp.get("mode") == "size" and sp.get("size_mb"):
        bits.append(tr("split_short_size", sp["size_mb"]))
    elif sp.get("mode") == "time" and sp.get("minutes"):
        bits.append(tr("split_short_time", sp["minutes"]))
    return " · ".join(bits)
