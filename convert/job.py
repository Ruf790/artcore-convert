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
Job.ext — external tracks [{path, kind: audio | subtitle, delay (ms), lang, title, codec, kbps, default}].
2.0: a subtitle track burnt into the picture: Job.tracks[index]["burn"] / Job.ext[n]["burn"] = all | forced
(hardsub.py); settings["hardsub"] — the look of text subtitles; audio "downmix" — the 5.1 / 7.1 → 2.0 matrix."""
from __future__ import annotations

import copy
import glob
import itertools
import os
import re
import tempfile
from dataclasses import dataclass, field

from . import advanced as ADV
from . import encoders as E
from . import filters as F
from . import formats as FM
from . import hardsub as HS
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
    "split": {"mode": "off", "size_mb": 700, "minutes": 20, "chapters_n": 1, "names": "num"},  # 1.4 / 2.3: parts
    "fmt": {},           # 1.5: the preset of the format (formats.py; {} = the plain container)
    "mux": dict(FM.MUX_DEFAULT),     # 1.5: the container options (Format options)
    "venc": {},          # 1.5: the advanced settings of every encoder {encoder id: {…}} (advanced.py)
    "hardsub": dict(HS.HS_DEFAULT),  # 2.0: how burnt-in text subtitles look; auto = burn the forced ones by itself
}

PRESETS = {      # built-in; quality 0 = the encoder's own default
    "hevc_hw":  {"container": "mkv", "video": {"encoder": "auto_hevc", "bits": "10"}},
    "h264_mp4": {"container": "mp4", "video": {"encoder": "auto_h264", "bits": "8"},
                 "audio": {"codec": "aac"}, "subs": "first"},
    "av1_hw":   {"container": "mkv", "video": {"encoder": "auto_av1", "bits": "10"}},
    # 1.7: NVENC of the RTX 40 / 50 tuned (other cards: the same codec on their own encoder, untuned)
    "rtx_av1_q":    {"container": "mkv", "video": {"encoder": "auto_av1", "bits": "10", "quality": 30},
                     "venc": {"nvenc_av1": {"preset": "p7", "tune": "hq", "multipass": "fullres", "lookahead": 32,
                                            "spatial_aq": True, "temporal_aq": True, "aq_strength": 8,
                                            "b_ref_mode": "middle"}}},
    "rtx_av1_fast": {"container": "mkv", "video": {"encoder": "auto_av1", "bits": "10", "quality": 32},
                     "venc": {"nvenc_av1": {"preset": "p4", "tune": "hq", "multipass": "qres", "lookahead": 16,
                                            "spatial_aq": True, "temporal_aq": True, "b_ref_mode": "middle",
                                            "split_encode": "forced"}}},
    "rtx_hevc_q":   {"container": "mkv", "video": {"encoder": "auto_hevc", "bits": "10", "quality": 24},
                     "venc": {"nvenc_hevc": {"preset": "p7", "tune": "hq", "multipass": "fullres", "lookahead": 32,
                                             "spatial_aq": True, "temporal_aq": True, "aq_strength": 8,
                                             "b_ref_mode": "middle"}}},
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
            elif k == "fmt":                                   # 1.5: replaced whole, made sane
                out["fmt"] = FM.clean(v) if isinstance(v, dict) and v else {}
            elif k == "venc":
                if isinstance(v, dict):
                    for eid, d in v.items():
                        if isinstance(d, dict):
                            out["venc"][str(eid)] = dict(d)
            elif isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k].update(v)
            elif k in out:
                out[k] = v
    return out


def preset_settings(name: str, user: dict | None = None) -> dict:
    """A built-in preset, a device preset (1.5) or one of the user's (an old one opens too: what it lacks is
    taken from DEFAULT_SETTINGS)."""
    if user and name in user:
        return merged(user[name] if isinstance(user[name], dict) else {})
    if name in FM.DEVICE:
        return merged(FM.DEVICE[name])
    return merged(PRESETS.get(name, {}))


def preset_group(name: str, user: dict | None = None) -> str:
    if user and name in user:
        return "own"
    return FM.DEVICE_GROUP.get(name, "general")


def all_presets(user: dict | None = None) -> list[tuple[str, str]]:
    """[(group, key)] in the order of the list: general, the devices by group, the user's own."""
    out = [("general", k) for k in PRESETS]
    for g in FM.GROUPS:
        out += [(g, k) for gg, k, _st in FM.DEVICES if gg == g]
    out += [("own", k) for k in sorted(user or {})]
    return out


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
    out_dir: str = ""                               # 2.0: a watch folder's result folder (the name as usual)
    origin: str = ""                                # 2.0: "watch" — came from a watch folder
    attach: list = field(default_factory=list)      # 2.3: files attached to the result (fonts …): [path, …]

    def to_dict(self) -> dict:
        return {"src": self.src, "settings": self.settings, "preset": self.preset, "tracks": self.tracks,
                "status": self.status if self.status in ("done", "error", "stop", "skip") else "wait",
                "out": self.out if self.status == "done" else "", "error": self.error,
                "trim": self.trim, "trim_cut": self.trim_cut, "meta": self.meta, "chapters": self.chapters,
                "dest": self.dest, "log": self.log_path, "crop_found": self.crop_found,
                "joins": list(self.joins), "ext": [dict(e) for e in self.ext],
                "metrics": dict(self.metrics) if self.status == "done" else {},
                "parts": list(self.parts) if self.status == "done" else [],
                "out_dir": self.out_dir, "origin": self.origin, "attach": list(self.attach)}

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        j = cls(src=str(d.get("src", "")), settings=merged(d.get("settings") or {}), preset=d.get("preset", ""),
                tracks={str(k): v for k, v in (d.get("tracks") or {}).items() if isinstance(v, dict)})
        j.trim = [[float(a), float(b)] for a, b in (d.get("trim") or []) if float(b) > float(a)]
        j.trim_cut = bool(d.get("trim_cut"))
        j.dest = str(d.get("dest") or "")
        j.log_path = str(d.get("log") or "")
        cf = d.get("crop_found")
        j.crop_found = [int(x) for x in cf][:4] if isinstance(cf, list) and len(cf) == 4 else None  # 1.7: [] looked again
        j.meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        ch = d.get("chapters")
        j.chapters = [[float(c[0]), str(c[1])] for c in ch if isinstance(c, (list, tuple)) and len(c) >= 2] \
            if isinstance(ch, list) else None
        j.joins = [str(x) for x in (d.get("joins") or []) if isinstance(x, str) and x]
        j.ext = [clean_ext(e) for e in (d.get("ext") or []) if isinstance(e, dict) and e.get("path")]
        j.attach = [str(x) for x in (d.get("attach") or []) if isinstance(x, str) and x]
        j.metrics = {k: float(v) for k, v in (d.get("metrics") or {}).items() if isinstance(v, (int, float))}
        j.parts = [str(x) for x in (d.get("parts") or []) if isinstance(x, str)]
        j.out_dir = str(d.get("out_dir") or "")
        j.origin = str(d.get("origin") or "")
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
        cap = int(FM.of(self.settings).get("a_maxch", 0) or 0)          # 1.5: the format's most channels
        if cap:
            want = F.layout_channels(out.get("channels", "keep"), s.channels or 2, 8)
            if want > cap:
                out["channels"] = max((k for k, n in F.LAYOUT_CH.items() if n <= cap), key=F.LAYOUT_CH.get,
                                      default="stereo")
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
            "default": False, "burn": "", "scale": 1.0}   # 2.5: scale — a subtitle's stretch (new = scale·t + delay)         # 2.0: burn: "" | all | forced — an outside subtitle burnt in
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


def ff_inp(path: str) -> list[str]:
    """-i path (1.8: a disc title is a concat list, read with -f concat -safe 0)."""
    from .ff import inp
    return inp(path)


def ffconcat_entries(path: str) -> list[tuple[str, float | None, float | None]]:
    """The files of a concat list with their in / out points (seconds)."""
    out: list = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith("file "):
                    name = ln[5:].strip().strip("'").replace("'\\''", "'")
                    if not os.path.isabs(name):
                        name = os.path.join(os.path.dirname(path), name)
                    out.append([name, None, None])
                elif ln.startswith("inpoint ") and out:
                    out[-1][1] = float(ln.split()[1])
                elif ln.startswith("outpoint ") and out:
                    out[-1][2] = float(ln.split()[1])
    except (OSError, ValueError):
        return []
    return [tuple(x) for x in out]


def preview_src(job: Job) -> str:
    """What mpv plays on the Trim tab: src, or the joined files one after another (an EDL)."""
    if not job.joins and job.src.lower().endswith(".ffconcat"):       # 1.8: a disc title as an mpv EDL
        parts = []
        for p, a, b in ffconcat_entries(job.src):
            seg = f"%{len(p.encode('utf-8'))}%{p}"
            if a is not None:
                seg += f",start={a:.3f}"
                if b is not None:
                    seg += f",length={b - a:.3f}"
            parts.append(seg)
        return "edl://" + ";".join(parts) if parts else job.src
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
    most = float(FM.of(job.settings).get("dur", 0) or 0)           # 1.5: the format's longest result
    if most > 0 and dur > 0:
        pcs2 = out or [(0.0, round(dur, 3))]
        held, left = [], most
        for a, b in pcs2:
            if left <= 0.04:
                break
            held.append((a, round(min(b, a + left), 3)))
            left -= held[-1][1] - a
        out = held
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
    f = FM.of(st)
    if f and f.get("container") and f["container"] != c.id:           # 1.5: another container: not this format
        st["fmt"] = f = {}
    if c.id == "gif":
        v["mode"] = "encode"
    fam = enc_family(v.get("encoder", "auto_hevc"))
    if not c.audio_only and v.get("mode") != "copy" and not c.takes_video(fam):
        v["encoder"], v["quality"] = c.v_default, 0
        fam = enc_family(v["encoder"])
    if f and not c.audio_only and v.get("mode") != "copy" and not FM.takes_video(st, fam):
        first = next((x for x in f.get("vcodecs") or [] if c.takes_video(x)), "")
        if first:
            v["encoder"], v["quality"] = FM.encoder_for(first), 0
    codec = a.get("codec", "auto")
    if codec not in ("auto", "copy") and not (c.takes_audio(E.A_CODEC.get(codec, codec)) and
                                               FM.takes_audio(st, codec)):
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
        elif not FM.takes_video(st, enc_family(vcfg.get("encoder", "auto_hevc"))):
            w.append("w_fmt_codec")
    elif v and c.audio_only:
        w.append("w_audio_only")
    joined = multi_encode(job)
    # cut and the picture encoded: copied sound would start before the picture (ffmpeg copies from the
    # keyframe the seek lands on) — it is encoded again, into its own codec where we have one. 1.4: the
    # concat filter decodes the sound too (joined files)
    recut = (bool(segments(job)) and bool(out["video"]) and vcfg.get("mode") != "copy") or joined \
        or bool(getattr(job, "force_recut", False))            # 1.8: the sound of a task encoded in chunks
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
    adef = FM.audio_default(st, c)                                # 1.5: the format's first codec

    def takes(name: str) -> bool:
        return c.takes_audio(name) and FM.takes_audio(st, name)
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
            codec = "copy" if takes(s.codec) and not changes else adef
        if codec == "copy" and not takes(s.codec):
            codec = adef
            w.append("w_audio_reencoded")
        elif codec != "copy" and not takes(E.A_CODEC.get(codec, codec)):
            codec = adef
            w.append("w_audio_reencoded")
        if codec == "copy" and changes:                           # a filter can't work on copied sound
            same = next((k for k, cn in E.A_CODEC.items() if cn == s.codec), "")
            codec = same if same and takes(s.codec) else (adef or "aac")
            if codec == same:
                out["same_rate"].add(s.index)
            w.append("w_afilter_copy")
        if codec == "copy" and recut:
            same = next((k for k, cn in E.A_CODEC.items() if cn == s.codec), "")
            codec = same if same and takes(s.codec) else (adef or "aac")
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
        filtered = F.audio_on(tc)
        changes = filtered or bool(segs)
        if codec == "auto":                   # 1.6: only cut → into its own codec (AC3 stays AC3), as the
            codec = "copy" if takes(s.codec) and not filtered else adef     # source's own tracks do
        if codec != "copy" and not takes(E.A_CODEC.get(codec, codec)):
            codec = adef
            w.append("w_audio_reencoded")
        if codec == "copy" and (changes or not takes(s.codec)):
            same = next((k for k, cn in E.A_CODEC.items() if cn == s.codec), "")
            codec = same if same and takes(s.codec) else (adef or "aac")
            if codec == same:
                out["same_rate"].add(("ext", n))
            w.append("w_ext_reencoded" if segs else "w_audio_reencoded")
        out["audio"].append((s, codec))
    most = int(FM.of(st).get("max_audio", 0) or 0)                 # 1.5: the format's most sound tracks
    if most and len(out["audio"]) > most:
        out["audio"] = out["audio"][:most]
        w.append("w_fmt_audio_n")
    burn = HS.choice(job)                                          # 2.0: a subtitle track burnt in
    if burn is not None:
        why = HS.why_not(job, burn)
        if why:
            w.append(why)
            burn = None
    out["burn"] = burn
    if burn is not None and abs(sub_timing(job, burn.stream)[0] - 1) > 1e-7:
        w.append("w_sync_burn_scale")                             # burnt in: the delay is kept, not the stretch
    burnt = burn.key() if burn is not None else None
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
        if ("src", s.index) == burnt:                              # in the picture, not a track of its own
            continue
        if not t.get("keep", s in subs):
            continue
        if c.subs == "none":
            if not c.audio_only:
                w.append("w_subs_dropped")
            continue
        if joined:
            w.append("w_trim_subs")
            continue
        if sub_timed(job, s) and not sync_ok(job):
            w.append("w_sync_ignored")
        if c.subs == "any":
            out["subs"].append((s, "copy"))
        elif s.codec in E.TEXT_SUBS:
            out["subs"].append((s, "copy" if s.codec == c.subs else c.subs))
        else:
            w.append("w_bitmap_subs")
    for n, s in ext_streams(job):                                  # 1.4: external subtitles
        if job.ext[n].get("kind") != "subtitle" or ("ext", n) == burnt:
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
    if out["video"] and vcfg.get("mode") not in ("copy", "none"):        # 1.8: Dolby Vision / HDR10+
        eid = vcfg.get("encoder", "auto_hevc")
        e = E.LAST.resolve(eid) if E.LAST is not None else None
        if e is None:
            e = E.BY_ID.get(eid) or next((x for x in E.VIDEO if x.family == enc_family(eid) and not x.hw), None)
        if e is not None:
            dyn = dyn_hdr(job, e, out_bits(job, e))
            w += [x for x in dyn["lost"] if x not in w]
            if dyn["dv"]:
                w.append("w_dv_kept")
            if dyn["hdr10p"]:
                w.append("w_hdr10p_kept")
    if split_on(job) and (st.get("split") or {}).get("mode") == "chapters" and chapter_starts(job) == []:
        w.append("split_no_chapters")
    if not attach_ok(st) and (kept_attachments(job) or job.attach) and \
            any(s.codec in ("ass", "ssa") for s, _h in out["subs"]):          # 2.3: the fonts stay behind
        w.append("w_attach_lost")
    return out


# ── 2.3: attachments (fonts of ASS subtitles, covers …) — Matroska only ──
ATTACH_MIME = {".ttf": "font/ttf", ".otf": "font/otf", ".ttc": "font/collection", ".woff": "font/woff",
               ".woff2": "font/woff2", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".txt": "text/plain"}


def attach_mime(path: str) -> str:
    return ATTACH_MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def attach_ok(st: dict) -> bool:
    """Only Matroska (MKV / MKA) carries attachments."""
    return E.container(st.get("container", "mkv")).fmt == "matroska"


def kept_attachments(job: Job) -> list[Stream]:
    """The source's attachments that stay (each has its tick on the Streams tab)."""
    if job.info is None or job.info.error:
        return []
    return [s for s in job.info.of("attachment") if job.track(s).get("keep", True)]


def attach_args(job: Job, src_in: int | None) -> list[str]:
    """-map of the kept attachments of input src_in (the source) and -attach of the added files."""
    if not attach_ok(job.settings):
        return []
    out: list[str] = []
    kept = kept_attachments(job) if src_in is not None else []
    for s in kept:
        out += ["-map", f"{src_in}:{s.index}"]
    if kept:
        out += ["-c:t", "copy"]
    n = len(kept)
    for path in job.attach:
        if not os.path.isfile(path):
            continue
        out += ["-attach", path, f"-metadata:s:t:{n}", f"mimetype={attach_mime(path)}",
                f"-metadata:s:t:{n}", f"filename={os.path.basename(path)}"]
        n += 1
    return out


# ── 2.3: the result in parts by chapters ───────────────────────
def chapter_starts(job: Job) -> list[float] | None:
    """The chapter starts of the result as far as they are known before encoding (None: not known)."""
    if job.chapters is not None:
        return [float(c[0]) for c in job.chapters]
    if job.info is None or job.info.error or segments(job) or job.joins:
        return None
    return [float(a) for a, _b, _t in job.info.chapter_list]


def chapter_cuts(chaps: list[tuple[float, str]], every: int) -> tuple[list[float], list[str]]:
    """Chapters [(start, title)] → the cut times (every N chapters, not at 0) and the title of every part."""
    every = max(1, int(every or 1))
    chaps = sorted(chaps)
    groups = [chaps[i:i + every] for i in range(0, len(chaps), every)]
    cuts = [g[0][0] for g in groups[1:] if g[0][0] > 0.05]
    titles = [g[0][1] for g in groups]
    return cuts, titles


def read_chapters(ffprobe: str, path: str) -> list[tuple[float, str]]:
    import json
    import subprocess
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    r = subprocess.run([ffprobe, "-v", "error", "-show_chapters", "-show_entries",
                        "format=start_time:stream=codec_type,start_time", "-of", "json", path],
                       capture_output=True, timeout=60, **kw)
    j = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    vs = [x for x in j.get("streams") or [] if x.get("codec_type") == "video" and x.get("start_time") is not None]
    t0 = float((vs[0] if vs else (j.get("format") or {})).get("start_time") or 0)
    # the segment muxer counts from the first picture: a film whose video begins at 0.023 s is cut 0.023 s early
    out = []
    for ch in j.get("chapters") or []:
        tg = {k.lower(): v for k, v in (ch.get("tags") or {}).items()}
        out.append((round(max(0.0, float(ch.get("start_time") or 0) - t0), 3), str(tg.get("title", ""))))
    return out


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def part_names(parts: list[str], titles: list[str]) -> list[str]:
    """«Name_01 Title.mkv»: the parts named after their first chapter (a part without a title keeps _NN)."""
    out = []
    for n, p in enumerate(parts):
        tt = _BAD.sub("_", titles[n]).strip(" .") if n < len(titles) and titles[n] else ""
        stem, ext = os.path.splitext(p)
        out.append(f"{stem} {tt[:80]}{ext}" if tt else p)
    return out


def out_bits(job: Job, enc: E.VEnc | None) -> int:
    want = str(job.settings.get("video", {}).get("bits", "auto"))
    if enc is None:
        return 8
    if len(enc.bits) == 1:
        return enc.bits[0]
    cap = FM.caps(job.settings, enc.family)[0]                  # 1.5: the profile of the format (Main: 8 bit)
    if want == "auto":
        src = job.info.video.bits if job.info and job.info.video else 8
        b = 10 if (src > 8 or enc.family in ("hevc", "av1")) and 10 in enc.bits else 8
    else:
        b = 10 if want == "10" and 10 in enc.bits else 8
    return min(b, cap) if cap >= 8 else b


OVERLAY = "\x00overlay"          # where the picture is laid on (the chain is cut there into two)
SUBS = "\x00subs"                # 2.0: where burnt-in subtitles are drawn (after the scale and the frame rate)


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
    if HS.active(job) is not None:
        out.append(SUBS)
    if F.is_on(job, "overlay") and not gif:
        out.append(OVERLAY)
    out += F.colorspace_filter(job, enc, bits)
    if gif:
        out.append("split[g0][g1];[g0]palettegen=stats_mode=diff[gp];[g1][gp]paletteuse=dither=bayer:bayer_scale=5")
    return out


def video_filters(job: Job, enc: E.VEnc | None = None) -> list[str]:
    """The chain as one -vf (the overlay and the burnt-in subtitles left out: build_args puts them in)."""
    return [f for f in video_chain(job, enc) if f not in (OVERLAY, SUBS)]


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
    f = FM.of(job.settings)                                       # 1.5: the frame of the format
    fw, fh = int(f.get("width", 0) or 0), int(f.get("height", 0) or 0)
    if fw and fh and not (enc is not None and enc.id == "gif"):
        if not (w and h):
            return fw, fh
        if f.get("size_limit"):
            return min(w, fw), min(h, fh)
    return (w, h) if w and h else (0, 0)


FILL_AR = (16, 9)                 # 2.1: «16:9 without bars»: the shape of «Fill» with no frame size chosen


def fill_plan(job: Job, enc: E.VEnc | None = None) -> dict | None:
    """2.1: «Fill the frame (cut the edges)» — video.fit = "fill". The picture (after Crop and rotation) is scaled so
    it COVERS the frame (k = max of the two ratios, not min), then cut in the middle to exactly W×H. One k for both
    sides: never stretched or squeezed, only the edges cut off; an anamorphic source (SAR ≠ 1) becomes square
    pixels first; the result has SAR 1, DAR = W/H. Without
    upscaling, k > 1 is not done: only cut to the frame's shape at the source's size (1920×872 → 16:9 = 1550×872).
    No frame size (the resolution «16:9 without bars»): cut to 16:9, no scaling.
    → {"scale": (sw, sh) the scaled picture | None, "out": (ow, oh), "cut": (cw, ch) the part of the pre-scale frame
    kept (its pixels), "lr" / "tb": pixels cut off each side of the pre-scale frame}; None if not this mode."""
    src = job.info.video if job.info else None
    v = job.settings.get("video", {})
    if v.get("fit") != "fill" or src is None or not src.width:
        return None
    fw, fh, sar = F.pre_scale(job)
    if not fw or not fh:
        return None
    w, h = target_box(job, enc)
    dw, dh = fw * sar, float(fh)                       # the picture in square pixels
    if w and h:
        k = max(w / dw, h / dh)
        if not v.get("upscale"):
            k = min(k, 1.0)
        aw, ah = w, h
    else:
        k, (aw, ah) = 1.0, FILL_AR
    sw, sh = dw * k, dh * k
    ratio = aw / ah
    oh = min(sh, sw / ratio)
    ow = oh * ratio
    if w and h and k == max(w / dw, h / dh):            # covered: exactly the frame asked for
        ow, oh = w, h
    ow, oh = min(_even(ow), _even(sw)), min(_even(oh), _even(sh))
    ssw, ssh = _even(sw), _even(sh)
    scale = (ssw, ssh) if (ssw, ssh) != (fw, fh) or abs(sar - 1.0) > 0.001 else None
    cw, ch = fw * ow / max(1.0, sw), fh * oh / max(1.0, sh)   # the kept part in pixels of the pre-scale frame
    lr, tb = max(0, int(round((fw - cw) / 2))), max(0, int(round((fh - ch) / 2)))
    return {"scale": scale, "out": (ow, oh), "cut": (fw - 2 * lr, fh - 2 * tb), "lr": lr, "tb": tb,
            "pre": (fw, fh)}


def final_crop(job: Job, enc: E.VEnc | None = None) -> tuple[int, int, int, int]:
    """2.1: what is cut off the source in all — Crop and then «Fill» — as (left, top, right, bottom) in pixels of
    the source: the frame drawn on the preview, «Show the result». (Rotation by 90°: Crop only.)"""
    l, t, r, b = F.crop_values(job) if F.is_on(job, "crop") else (0, 0, 0, 0)
    fp = fill_plan(job, enc)
    if fp is None or (F.is_on(job, "rotate") and F._int(F.fset(job.settings, "rotate").get("angle")) % 180 == 90):
        return l, t, r, b
    cw, ch = fp["cut"]
    fw, fh = fp["pre"]
    el, et = (fw - cw) // 4 * 2, (fh - ch) // 4 * 2     # even, as the crop filter after the scale: 4:2:0 whole
    return l + el, t + et, r + fw - cw - el, b + fh - ch - et


def out_size(job: Job, enc: E.VEnc | None = None) -> tuple[int, int]:
    """The frame size of the result (what the scale filters make)."""
    src = job.info.video if job.info else None
    if src is None or not src.width:
        return 0, 0
    fp = fill_plan(job, enc)
    if fp is not None:
        return F.post_pad(job, *fp["out"])
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
    stretch (exactly W×H), 2.1 fill (cover W×H, the edges cut off in the middle). Anamorphic sources (DVD, SAR ≠ 1) become square pixels on the way."""
    src = job.info.video if job.info else None
    fp = fill_plan(job, enc)
    if fp is not None:                                   # 2.1: cover the frame, cut the edges in the middle
        out = [f"scale={fp['scale'][0]}:{fp['scale'][1]}:flags=lanczos", "setsar=1"] if fp["scale"] else []
        sw, sh = fp["scale"] or fp["pre"]
        ow, oh = fp["out"]
        if (ow, oh) != (sw, sh):
            out.append(f"crop={ow}:{oh}:{(sw - ow) // 4 * 2}:{(sh - oh) // 4 * 2}")
        if out and out[-1] != "setsar=1":
            out.append("setsar=1")                       # square pixels: DAR = W/H, never stretched
        return out
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
            for name, x, y in _disc_parts(files[k], a, b):   # 1.8: a disc title: the clips themselves
                q = name.replace("\\", "/").replace("'", "'\\''")
                f.write(f"file '{q}'\n")
                if x is not None:
                    f.write(f"inpoint {x:.3f}\n")
                if y is not None:
                    f.write(f"outpoint {y:.3f}\n")


def _disc_parts(path: str, a, b) -> list[tuple[str, float | None, float | None]]:
    """A piece [a, b) of a file; of a disc title's concat list, the pieces of its clips (a concat list cannot
    hold another one)."""
    if not path.lower().endswith(".ffconcat"):
        return [(path, a, b)]
    out, pos = [], 0.0
    for name, x, y in ffconcat_entries(path):
        if x is None or y is None:
            return [(path, a, b)]
        d = y - x
        lo, hi = (0.0 if a is None else a), (9e18 if b is None else b)
        s, e = max(lo, pos), min(hi, pos + d)
        if e > s + 1e-6:
            out.append((name, x + (s - pos), x + (e - pos)))
        pos += d
    return out


def work_file(job: Job, kind: str, work_dir: str | None = None, ext: str = "txt") -> str:
    d = work_dir or os.path.join(tempfile.gettempdir(), "ArtCoreConvert")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"job{job.uid}_{os.getpid()}_{kind}.{ext}")


WORK_KINDS = (("pieces", "txt"), ("chapters", "txt"), ("vidstab", "trf"), ("frame", "png"), ("vmaf", "json"))


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
    job.pre_kinds = []
    enc0 = resolve(job, av)
    if enc0 is not None and dyn_hdr(job, enc0, out_bits(job, enc0))["hdr10p"]:      # 1.8: HDR10+ metadata
        out += hdr10p_passes(job, ffmpeg, work_dir)
        job.pre_kinds += ["hdr10p"] * len(out)
    if needs_vidstab(job):
        out.append(build_args(job, ffmpeg, out_path, av, hw_decode, work_dir, mode="vidstab", threads=threads))
        job.pre_kinds.append("vidstab")
    load_files(job)
    if two_pass(job, resolve(job, av), av):
        out.append(build_args(job, ffmpeg, out_path, av, hw_decode, work_dir, mode="pass1", threads=threads,
                              preview=preview))
        job.pre_kinds.append("pass1")
    return out


# ── 1.8: Dolby Vision and HDR10+ into the result ──────────────
def hdr10p_tool() -> str:
    from .ff import find
    return find("hdr10plus_tool")


def dyn_hdr(job: Job, enc: E.VEnc | None, bits: int) -> dict:
    """What of the dynamic HDR goes into the result: {dv, hdr10p, lost: [what is lost and why]}.
    Dolby Vision: its RPUs carried by ffmpeg (-dolbyvision, libx265 / libsvtav1 of a new ffmpeg) frame by frame —
    through the cut, the crop and the chunks. HDR10+: hdr10plus_tool takes the metadata out, x265 writes it in
    (the whole film only: no cut, no chunks, the same frame rate)."""
    out = {"dv": False, "hdr10p": False, "lost": []}
    src = job.info.video if job.info is not None and not job.info.error else None
    v = job.settings.get("video", {})
    if src is None or enc is None or v.get("mode") in ("copy", "none") or not v.get("dyn_hdr", True):
        return out
    keeps = F.hdr_keeps(job, enc, bits) and not F.tonemap_on(job, enc, bits)
    if src.hdr == "Dolby Vision":
        if keeps and enc.id in ("x265", "svtav1") and "dolbyvision" in ADV.caps(enc):
            out["dv"] = True
        else:
            out["lost"].append("w_dv_hw" if enc.hw else "w_dv")
    if src.hdr10plus:
        why = ""
        if not keeps or enc.id != "x265":
            why = "w_hdr10p_enc"
        elif segments(job) or job.joins:
            why = "w_hdr10p_cut"
        elif F.fps_on(job):
            why = "w_hdr10p_fps"
        elif not hdr10p_tool():
            why = "w_hdr10p_tool"
        elif getattr(job, "in_chunks", False):
            why = "w_hdr10p_chunks"
        if why:
            out["lost"].append(why)
        else:
            out["hdr10p"] = True
    return out


def hdr10p_json(job: Job, work_dir: str | None = None) -> str:
    return work_file(job, "hdr10plus", work_dir, "json")


def hdr10p_passes(job: Job, ffmpeg: str, work_dir: str | None = None) -> list[list[str]]:
    """hdr10plus_tool reads an MKV / HEVC file itself; anything else gives it the bare HEVC stream first."""
    js = hdr10p_json(job, work_dir)
    src = job.src
    out = []
    if not src.lower().endswith((".mkv", ".hevc", ".h265", ".265")):
        raw = work_file(job, "video", work_dir, "hevc")
        out.append([ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
                   + ff_inp(src) + ["-map", "0:v:0", "-c", "copy", "-bsf:v", "hevc_mp4toannexb", "-f", "hevc", raw])
        src = raw
    out.append([hdr10p_tool(), "extract", "-o", js, src])
    return out


METRICS = ("psnr", "ssim", "vmaf", "psnr_hvs", "ms_ssim", "ciede", "cambi")
# 1.9: the metrics libvmaf computes in the same pass as VMAF: key → (its feature, the name in its JSON log)
VMAF_FEATS = {"psnr_hvs": ("psnr_hvs", "psnr_hvs"), "ms_ssim": ("float_ms_ssim", "float_ms_ssim"),
              "ciede": ("ciede", "ciede2000"), "cambi": ("cambi", "cambi")}
LIBVMAF = ("vmaf",) + tuple(VMAF_FEATS)
METRIC_NAMES = {"psnr": "PSNR", "ssim": "SSIM", "vmaf": "VMAF", "psnr_hvs": "PSNR-HVS", "ms_ssim": "MS-SSIM",
                "ciede": "CIEDE2000", "cambi": "CAMBI"}


def metric_ok(m: str, av: E.Available) -> bool:
    """Can this ffmpeg measure it: PSNR / SSIM always, VMAF with libvmaf, the 1.9 ones with libvmaf's feature."""
    if m in ("psnr", "ssim"):
        return True
    if "libvmaf" not in av.filters:
        return False
    return m == "vmaf" or (m in VMAF_FEATS and VMAF_FEATS[m][0] in getattr(av, "vmaf_feats", set()))


def can_metrics(job: Job, av: E.Available) -> bool:
    enc = resolve(job, av)
    return enc is not None and enc.id != "gif"


def vmaf_log(job: Job, work_dir: str | None = None) -> str:
    """1.9: libvmaf's JSON log of a task's metrics pass (PSNR-HVS, MS-SSIM, CIEDE2000, CAMBI are read from it)."""
    return work_file(job, "vmaf", work_dir, "json")


def post_passes(job: Job, ffmpeg: str, out_path: str, av: E.Available, metrics=(), work_dir: str | None = None,
                threads: int = 0) -> list[list[str]]:
    """1.4: what runs on the finished file — the quality metrics against the filtered source (1.9: all of them
    in one pass: psnr, ssim and one libvmaf with its extra features)."""
    want = [m for m in METRICS if m in (metrics or ()) and metric_ok(m, av)]
    if not want or not can_metrics(job, av):
        return []
    return [build_args(job, ffmpeg, out_path, av, False, work_dir, mode="metrics", threads=threads, metrics=want)]


_RE_PSNR = re.compile(r"PSNR .*average:\s*([0-9.]+|inf)")
_RE_SSIM = re.compile(r"SSIM .*All:\s*([0-9.]+)")
_RE_VMAF = re.compile(r"VMAF score[:=]\s*([0-9.]+)")


def parse_metrics(lines, vmaf_json: str = "", want=None) -> dict:
    """The summary lines of psnr / ssim / libvmaf in ffmpeg's log → {"psnr": 41.2, "ssim": 0.987, "vmaf": 95.1};
    1.9: and the pooled means of libvmaf's JSON log (psnr_hvs, ms_ssim, ciede, cambi). want: only these keys."""
    out = {}
    for ln in lines:
        for key, rx in (("psnr", _RE_PSNR), ("ssim", _RE_SSIM), ("vmaf", _RE_VMAF)):
            m = rx.search(ln)
            if m:
                out[key] = 100.0 if m.group(1) == "inf" else float(m.group(1))
    if vmaf_json and os.path.isfile(vmaf_json):
        import json
        try:
            with open(vmaf_json, encoding="utf-8") as f:
                pooled = (json.load(f) or {}).get("pooled_metrics") or {}
            for key, (_feat, name) in list(VMAF_FEATS.items()) + [("vmaf", ("", "vmaf"))]:
                v = (pooled.get(name) or {}).get("mean")
                if isinstance(v, (int, float)) and key not in out:
                    out[key] = float(v)
        except (OSError, ValueError, AttributeError):
            pass
    if want is not None:
        out = {k: v for k, v in out.items() if k in want}
    return {k: out[k] for k in METRICS if k in out}


def ciede_de(score: float) -> float:
    """libvmaf's CIEDE2000 score is 45 − 20·log10(ΔE): back to the mean colour difference ΔE."""
    return 10 ** ((45.0 - score) / 20.0)


# the bounds of the verdicts: value ≥ bound → that verdict (CIEDE2000 by ΔE and CAMBI: value < bound)
_SCALES = {"vmaf": (95, 90, 80, 70), "psnr": (45, 40, 35, 30), "psnr_hvs": (45, 40, 35, 30),
           "ssim": (0.99, 0.98, 0.95, 0.90), "ms_ssim": (0.997, 0.993, 0.98, 0.95),
           "ciede": (1.0, 2.0, 3.5, 5.0), "cambi": (1.0, 3.0, 5.0, 10.0)}
_VERDICTS = {"ciede": ("m_c_same", "m_c_close", "m_c_near", "m_c_shift", "m_c_off"),
             "cambi": ("m_b_none", "m_b_faint", "m_b_some", "m_b_bad", "m_b_heavy")}


def metric_level(key: str, v: float) -> int:
    """0 (as the source) … 4 (poor) — the colour and the words of a metric's result."""
    bounds = _SCALES.get(key)
    if not bounds:
        return 2
    if key in ("ciede", "cambi"):
        x = ciede_de(v) if key == "ciede" else v
        return next((i for i, b in enumerate(bounds) if x < b), 4)
    return next((i for i, b in enumerate(bounds) if v >= b), 4)


def metric_value(key: str, v: float) -> str:
    """«41.92 dB», «0.9851», «95.20», «47.73 (ΔE 0.73)»."""
    if key in ("psnr", "psnr_hvs"):
        return f"{v:.2f} dB"
    if key in ("ssim", "ms_ssim"):
        return f"{v:.4f}"
    if key == "ciede":
        return f"{v:.2f} (ΔE {ciede_de(v):.2f})"
    return f"{v:.2f}"


def metric_verdict(key: str, v: float) -> str:
    from .i18n import t
    keys = _VERDICTS.get(key, ("m_v_same", "m_v_great", "m_v_good", "m_v_fair", "m_v_poor"))
    return t(keys[metric_level(key, v)])


def metric_text(key: str, v: float) -> str:
    """«VMAF 95.20 — indistinguishable from the source» (the log, the Info tab)."""
    return f"{METRIC_NAMES.get(key, key.upper())} {metric_value(key, v)} — {metric_verdict(key, v)}"


def _sar(s: Stream) -> str:
    return "1" if not s.sar or s.sar in ("0:1", "1:1") else s.sar.replace(":", "/")


def _fps_str(f: float) -> str:
    n = round(f * 1.001)
    return f"{n * 1000}/1001" if n and abs(f - n * 1000 / 1001) < 0.002 and abs(f - n) > 0.01 else f"{f:.6g}"


def _layout(s: Stream) -> str:
    return s.layout or f"{s.channels or 2}c"


def _concat_graph(job: Job, p: dict, pcs, graph: list[str], canv=None) -> tuple[str | None, list[str]]:
    """The concat filter over the inputs 0 … n-1 (one per piece): every piece made like the first file
    (frame size, pixel aspect, frame rate; sample rate, channels; silence / black where a joined file lacks a
    track). Returns the video label (or None) and the labels of the source's sound tracks. 2.0: canv — the
    burnt-in subtitle pictures of every piece ([(input, stream index) | None], canvas size, fps): a second video
    stream of the concat, [sj]."""
    v0 = p["video"][0] if p["video"] else None
    auds = [s for s, _c in p["audio"] if ext_no(s) is None]
    ins = ""
    if v0 is None:
        canv = None
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
            if canv is not None:                                   # 2.0: this piece's subtitle pictures
                srcs, (cw, chh), fps = canv
                n_in, sidx, size = srcs[i] if srcs[i] is not None else (None, None, (cw, chh))
                graph += HS.piece_canvas(i, n_in, sidx, size[0], size[1], fps, b - a, (cw, chh))
                ins += f"[cs{i}]"
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
    outs = ("[vj]" if v0 is not None else "") + ("[sj]" if canv is not None else "") + \
        "".join(f"[aj{n}]" for n in range(len(auds)))
    nv = (1 if v0 is not None else 0) + (1 if canv is not None else 0)
    graph.append(f"{ins}concat=n={len(pcs)}:v={nv}:a={len(auds)}{outs}")
    return ("[vj]" if v0 is not None else None), [f"[aj{n}]" for n in range(len(auds))]


def _graph_video(job: Job, enc, src: str, ov_in: int | None, transforms: str, detect: bool, hs=None,
                 hs_label: str = "", work_dir: str | None = None) -> list[str]:
    """The video part of the filter graph: src → the chain (the burnt-in subtitles and the overlay laid on where
    their marks stand) → [vout]."""
    chain = video_chain(job, enc, transforms, detect)
    if ov_in is None:
        chain = [f for f in chain if f != OVERLAY]
    bits = out_bits(job, enc)
    nodes, cur, seg, k = [], src, [], 0
    for f in chain:
        if f == SUBS:
            if hs is None or (hs.bitmap and not hs_label):
                continue
            if HS.inline(job, enc, bits, hs):                         # text on SDR: on the frames themselves
                seg += HS.text_filters(job, hs, work_dir=work_dir)
                continue
            nodes.append(f"{cur}{','.join(seg) if seg else 'null'}[vs{k}]")
            seg = []
            if hs.bitmap:
                nodes.append(f"{hs_label}{','.join(HS.bitmap_chain(job, enc, bits, hs))}[hsc]")
            else:                                                     # text on HDR: on a canvas, brought down
                nodes.append(f"{','.join(HS.text_canvas(job, enc, bits, hs, work_dir))}[hsc]")
            nodes.append(f"[vs{k}][hsc]{HS.overlay()}[vs{k + 1}]")
            cur = f"[vs{k + 1}]"
            k += 2
        elif f == OVERLAY:
            nodes.append(f"{cur}{','.join(seg) if seg else 'null'}[vpre]")
            seg = []
            ov_chain, ov = F.overlay_parts(job, out_size(job, enc)[0])
            nodes.append(f"[{ov_in}:v]{ov_chain}[wm]")
            nodes.append(f"[vpre][wm]{ov}[vov]")
            cur = "[vov]"
        else:
            seg.append(f)
    if nodes and not seg:
        nodes[-1] = nodes[-1][:nodes[-1].rindex("[")] + "[vout]"
    else:
        nodes.append(f"{cur}{','.join(seg) if seg else 'null'}[vout]")
    return nodes


# ── 2.5: subtitle timing — new time = scale · old time + delay ──
FPS_EXACT = {23.976: 24000 / 1001, 29.97: 30000 / 1001, 59.94: 60000 / 1001}
SYNC_FPS = ((25, 23.976), (23.976, 25), (25, 24), (24, 25), (24, 23.976), (23.976, 24), (29.97, 25), (25, 29.97))


def fps_scale(made_for: float, video: float) -> float:
    """Subtitles timed for a «made_for» fps version on a «video» fps one: 25 → 23.976 = ×1.0427 (PAL runs faster)."""
    return FPS_EXACT.get(made_for, made_for) / FPS_EXACT.get(video, video)


def sub_timing(job: Job, s: Stream) -> tuple[float, float]:
    """(scale, delay in seconds) of a subtitle track — of the source (Streams tab) or an outside one."""
    n = ext_no(s)
    if n is not None:
        e = job.ext[n] if n < len(job.ext) else {}
        return float(e.get("scale", 1.0) or 1.0), float(e.get("delay", 0) or 0) / 1000
    tr = job.tracks.get(str(s.index), {})
    return float(tr.get("scale", 1.0) or 1.0), float(tr.get("delay", 0) or 0) / 1000


def sub_timed(job: Job, s: Stream) -> bool:
    k, d = sub_timing(job, s)
    return abs(k - 1) > 1e-7 or abs(d) > 0.0005


def sync_ok(job: Job) -> bool:
    """A source subtitle track can be re-timed: no joined files and at most one trim piece."""
    return not job.joins and len(segments(job)) <= 1


def two_points(s1: float, r1: float, s2: float, r2: float) -> tuple[float, float]:
    """Two lines: «at s1 on the subtitles it should be r1», «at s2 — r2» → (scale, delay seconds)."""
    if abs(s2 - s1) < 1:
        raise ValueError("the two points are too close")
    k = (r2 - r1) / (s2 - s1)
    if not 0.8 < k < 1.25:
        raise ValueError("the stretch is out of range")
    return k, r1 - k * s1


def timed_input(path: str, k: float, d: float, segs) -> list[str]:
    """-i of a subtitle file / the source again with its own time: ffmpeg makes it (t + itsoffset) · itsscale,
    -ss counts in the file's own time. Output time = k·t + d − (the piece's start)."""
    a = segs[0][0] if segs else 0.0
    start = (a - d) / k                                   # the subtitles' time that lands at 0 of the result
    opt = ["-itsscale", f"{k:.9f}"] if abs(k - 1) > 1e-7 else []
    if start > 0.0005:
        opt += ["-ss", f"{start:.3f}"]
    elif start < -0.0005:
        opt += ["-itsoffset", f"{-start:.3f}"]
    if segs:
        opt += ["-t", f"{(segs[0][1] - segs[0][0]) / k:.3f}"]
    return opt + ["-i", path]


def ext_delay(job: Job, s: Stream) -> float:
    n = ext_no(s)
    return float(job.ext[n].get("delay", 0) or 0) / 1000 if n is not None and n < len(job.ext) else 0.0


def _ext_input(job: Job, s: Stream, segs) -> list[str]:
    """The input options of an external track: its delay moves it (or, cut / joined, its filters do)."""
    d = ext_delay(job, s)
    path = job.ext[ext_no(s)]["path"]
    if s.kind == "subtitle":                                        # one piece (more: the subtitles are dropped)
        k, d = sub_timing(job, s)
        return timed_input(path, k, d, segs)
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
    hs = p.get("burn") if p["video"] and not copy_v and mode != "vidstab" else None     # 2.0: burnt-in subtitles
    if hs is not None and hs.forced and hs.ext is None:
        dec = dec + ["-forced_subs_only:s", "1"]                    # Blu-ray / DVD: only the forced pictures
    # 1.6: verbose — the read / decoded / encoded / muxed counts of every stream for the log
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "verbose", "-nostats", "-progress", "pipe:1"]
    if threads > 0:
        args += ["-filter_threads", str(threads), "-filter_complex_threads", str(threads)]
    # inputs: 0 = the source (or the concat list); meta_in = where the file's tags / chapters come from;
    # base = the file input 0 reads (a single piece can lie in a joined file)
    base = 0
    if not pcs:
        args += hw + dec + ff_inp(job.src)
        n_in, meta_in = 1, 0
    elif len(pcs) == 1:
        base, a, b = pcs[0]
        args += hw + dec + ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}"] + ff_inp(files[base][0])
        n_in, meta_in = 1, 0
    elif joined:
        for k, a, b in pcs:
            args += hw + dec + ["-ss", f"{a:.3f}", "-t", f"{b - a:.3f}"] + ff_inp(files[k][0])
        n_in, meta_in = len(pcs), 0
    else:                                                         # copied video / sound only: concat demuxer
        lst = work_file(job, "pieces", work_dir)
        full = [(k, None if (a <= 0.001 and b >= _dur(files[k][1]) - 0.001) else a, b) for k, a, b in pcs]
        _concat_list(lst, [f for f, _i in files], full)
        args += ["-f", "concat", "-safe", "0", "-i", lst] + ff_inp(job.src)
        n_in, meta_in = 2, 1
        base = pcs[0][0]
    ov_in = None                                                  # the picture of the Overlay row
    if enc is not None and enc.id != "gif" and F.is_on(job, "overlay") and mode != "vidstab":
        args += ["-i", F.fset(st, "overlay")["path"]]
        ov_in = n_in
        n_in += 1
    hs_label, canv = "", None                                     # 2.0: where the subtitle pictures come from
    if hs is not None and hs.bitmap:
        size0 = HS.canvas_size(job, hs)
        fps0 = (p["video"][0].fps or 25.0) if p["video"] else 25.0
        if hs.ext is not None:
            path = job.ext[hs.ext]["path"]
            forced = ["-forced_subs_only:s", "1"] if hs.forced else []
            if joined:                                            # the outside file read once per piece
                srcs = []
                for (p_at, a_us, l_us) in HS.time_map(job, hs):
                    a = a_us / 1e6
                    seek = ["-ss", f"{a:.3f}"] if a >= 0 else ["-itsoffset", f"{-a:.3f}"]
                    args += forced + seek + ["-t", f"{l_us / 1e6:.3f}", "-i", path]
                    srcs.append((n_in, hs.stream.index, size0))
                    n_in += 1
                canv = (srcs, size0, fps0)
            else:
                args += forced + _ext_input(job, hs.stream, segs)
                hs_label = f"[{n_in}:{hs.stream.index}]"
                n_in += 1
        elif joined:
            srcs = []
            for i, (k, _a, _b) in enumerate(pcs):
                sk = match_stream(job, k, hs.stream)
                srcs.append((i, sk.index, (sk.width or files[k][1].video.width, sk.height or files[k][1].video.height))
                            if sk is not None and files[k][1] is not None and files[k][1].video is not None else None)
            canv = (srcs, size0, fps0)
        else:
            sk = match_stream(job, base, hs.stream)
            hs_label = f"[0:{sk.index}]" if sk is not None else ""
        if canv is not None:
            hs_label = "[sj]"
    ext_in: dict[int, int] = {}                                   # 1.4: external track → its input
    for s, _how in p["audio"] + p["subs"]:
        if ext_no(s) is not None:
            args += _ext_input(job, s, segs)
            ext_in[ext_no(s)] = n_in
            n_in += 1
    emb_in: dict[int, int] = {}                                   # 2.5: a re-timed subtitle of the source
    for s, _how in p["subs"]:
        if ext_no(s) is None and sub_timed(job, s) and sync_ok(job):
            k, d = sub_timing(job, s)
            args += timed_input(job.src, k, d, segs)
            emb_in[s.index] = n_in
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
    in_graph = joined or ov_in is not None or want_preview or mode == "metrics" or \
        (hs is not None and not HS.inline(job, enc, out_bits(job, enc), hs))
    vsrc = vmap = ""
    if joined:
        vlab, a_labels = _concat_graph(job, p, pcs, graph, canv)
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
                graph += _graph_video(job, enc, vsrc, ov_in, transforms, mode == "vidstab", hs, hs_label, work_dir)
                vmap = "[vout]"
            else:
                vf = []
                for f in video_chain(job, enc, transforms, mode == "vidstab"):
                    if f == SUBS:                                    # 2.0: text on SDR, drawn on the frames
                        vf += HS.text_filters(job, hs, work_dir=work_dir) if hs is not None else []
                    elif f != OVERLAY:
                        vf.append(f)
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
                names = [m for m in metrics if m in ("psnr", "ssim")]
                lv = [m for m in metrics if m in LIBVMAF]
                if lv:                                             # 1.9: one libvmaf for VMAF and its features
                    opt = f"libvmaf=n_threads={max(1, threads or os.cpu_count() or 4)}"
                    feats = [VMAF_FEATS[m][0] for m in lv if m in VMAF_FEATS]
                    if feats:
                        from .filters import escape_path
                        opt += ":feature=" + "|".join("name=" + f for f in feats) + \
                            f":log_fmt=json:log_path={escape_path(vmaf_log(job, work_dir))}"
                    names.append(opt)
                k = len(names)
                same = f"settb=1/1000,setpts=N*1000,format={fmt}"         # frame by frame, not by time stamps
                graph.append(f"[vref]{same},split={k}" + "".join(f"[r{i}]" for i in range(k)) if k > 1
                             else f"[vref]{same}[r0]")
                cur = f"[{dist_in}:v:0]{same}[d0]"
                graph.append(cur)
                for i, name in enumerate(names):
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
            vargs = encoder_args(job, enc, E.video_args(enc, vv, bits, dur), bits, vv["chroma"])     # 1.5
            args += ["-c:v", enc.ffname] + vargs + E.thread_args(enc, threads)
            if enc.id == "x265":
                i = args.index("-x265-params") + 1
                if threads > 0:
                    args[i] += f":pools={threads}"
                if pass_n:
                    args[i] += f":pass={pass_n}:stats={E.x265_path(pass_prefix(job, work_dir) + '.x265.log')}"
            if pass_n:
                args += E.pass_args(enc, pass_n, pass_prefix(job, work_dir))
            sp = st.get("split") or {}
            if split_on(job) and sp.get("mode") == "time":           # 1.4: a keyframe where every part begins
                args += ["-force_key_frames", f"expr:gte(t,n_forced*{float(sp.get('minutes', 20)) * 60:g})"]
                if enc.vendor == "nvidia":                          # (both passes: x264 keeps pass 1's types)
                    args += ["-forced-idr", "1"]
            elif split_on(job) and sp.get("mode") == "chapters":    # 2.3: a keyframe at every cut
                cs = chapter_starts(job)
                cuts = chapter_cuts([(x, "") for x in cs], sp.get("chapters_n", 1))[0] if cs else []
                if cuts:
                    args += ["-force_key_frames", ",".join(f"{x:.3f}" for x in cuts)]
                    if enc.vendor == "nvidia":
                        args += ["-forced-idr", "1"]
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
                        (":hdr10=1:repeat-headers=1" if trc == "smpte2084" else "")
                dyn = dyn_hdr(job, enc, bits)                            # 1.8: Dolby Vision / HDR10+
                if dyn["dv"]:
                    args += ["-dolbyvision", "1"]
                    if enc.id == "x265":
                        i = args.index("-x265-params") + 1
                        if "vbv-maxrate" not in args[i]:              # x265 writes DV only with VBV (level 5.1)
                            args[i] += ":vbv-maxrate=160000:vbv-bufsize=160000"
                if dyn["hdr10p"] and enc.id == "x265":
                    i = args.index("-x265-params") + 1
                    js = hdr10p_json(job, work_dir).replace("\\", "/").replace(":", "\\:")
                    args[i] += f":dhdr10-info={js}:dhdr10-opt=1"
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
        och = F.layout_channels(a_cfg.get("channels", "keep"), s.channels, E.A_BY_ID[codec].max_ch) \
            if codec != "copy" else s.channels
        af = F.audio_filters(a_cfg, codec, s.rate, s.channels, s.layout, och) if codec != "copy" else []
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
        if s.index in emb_in:
            args += ["-map", f"{emb_in[s.index]}:{s.index}", f"-c:s:{n}", how]
            continue
        ss = match_stream(job, base, s) or s
        args += ["-map", f"0:{ss.index}", f"-c:s:{n}", how]
    if not p["subs"]:
        args += ["-sn"]
    if not side and mode == "main":
        src_in = meta_in if (not pcs or not joined and len(pcs) > 1 or pcs[0][0] == 0) else None
        args += attach_args(job, src_in)                          # 2.3: fonts and other attachments
    args += meta_args(job, p, joined, meta_on)
    args += ["-dn", "-max_muxing_queue_size", "4096"]
    args += FM.mux_args(st, cont)                                 # 1.5: the container options
    if cont == "mp3":
        args += ["-id3v2_version", "3"]
    args += ["-f", c.fmt, out_path]
    if want_preview:
        args += _preview_out(preview)
    return args


def out_fps(job: Job) -> float:
    v = job.settings.get("video", {})
    f = float(v.get("fps", 0) or 0)
    if f:
        return f
    src = job.info.video if job.info else None
    return float(src.fps) if src is not None and src.fps else 25.0


def encoder_args(job: Job, enc: E.VEnc, base: list[str], bits: int, chroma: str) -> list[str]:
    """1.5: the encoder's own arguments with the advanced settings laid over them, then the preset of the
    format (its profile, level, keyframes and VBV win: they are what the device needs)."""
    caps = ADV.caps(enc)
    a, p = ADV.args(enc, ADV.of(job.settings, enc), caps)
    out = FM.put(base, a, p)
    a, p = FM.video_args(enc, job.settings, bits, chroma, out_fps(job), caps)
    return FM.put(out, a, p)


def _preview_out(path: str) -> list[str]:
    """The second output: the frame being encoded, one PNG written over and over."""
    return ["-map", "[vprev]", "-c:v", "png", "-update", "1", "-f", "image2", path]


# ── 1.4: the result in parts ─────────────────────────────────
def split_on(job: Job) -> bool:
    sp = job.settings.get("split") or {}
    mode = sp.get("mode", "off")
    return job.settings.get("container", "mkv") != "gif" and (
        (mode == "size" and float(sp.get("size_mb", 0) or 0) > 0) or
        (mode == "time" and float(sp.get("minutes", 0) or 0) > 0) or mode == "chapters")


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
    ext = "." + out_ext(job.settings)
    if job.dest:                                         # chosen in «Save as…» (overwrite was asked there)
        path = os.path.splitext(job.dest)[0] + ext
        if os.path.normcase(os.path.abspath(path)) != os.path.normcase(os.path.abspath(job.src)):
            return path
    stem = os.path.splitext(os.path.basename(job.src))[0]
    folder = cfg.get("out_dir") if cfg.get("out_mode") == "folder" and cfg.get("out_dir") else os.path.dirname(job.src)
    if job.out_dir:                                      # 2.0: the result folder of a watch folder
        folder = job.out_dir
    tpl = (cfg.get("name_template") or "").strip()
    if tpl:                                              # 1.8: «{title} ({year}) {res} {vcodec}», folders by «/»
        base = os.path.join(folder, *render_name(tpl, job).split("/"))
    else:
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


def out_ext(st: dict) -> str:
    """The extension of the result: the container's, or the one of the preset of the format (1.5)."""
    c = E.container(st.get("container", "mkv"))
    f = FM.of(st)
    if f.get("ext") and (not f.get("container") or f["container"] == c.id):
        return f["ext"]
    return c.ext


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
    f = FM.of(st)
    bits.append(f["name"] if f.get("name") else c.id.upper())          # 1.5: the preset of the format
    sp = st.get("split") or {}
    if sp.get("mode") == "size" and sp.get("size_mb"):
        bits.append(tr("split_short_size", sp["size_mb"]))
    elif sp.get("mode") == "time" and sp.get("minutes"):
        bits.append(tr("split_short_time", sp["minutes"]))
    elif sp.get("mode") == "chapters":
        bits.append(tr("split_short_chap", int(sp.get("chapters_n", 1) or 1)))
    return " · ".join(bits)


# ── 1.7: apply one task's settings to others ──────────────────
APPLY_PARTS = ("settings", "tracks", "trim")


def _kind_order(info: MediaInfo | None) -> dict[int, tuple[str, int]]:
    """stream index → (kind, its place among the streams of that kind)."""
    out, seen = {}, {}
    for s in (info.streams if info is not None and not info.error else []):
        k = seen.get(s.kind, 0)
        out[s.index] = (s.kind, k)
        seen[s.kind] = k + 1
    return out


def apply_from(src: Job, dst: Job, parts=("settings", "tracks")) -> list[str]:
    """Copy src's settings onto dst: «settings» (codec, quality, size, filters, container, sound, split — the whole
    profile), «tracks» (which audio / subtitle tracks are kept and how: matched by kind and place, the 2nd audio to
    the 2nd audio; their language / title / default too), «trim» (the pieces, cut to dst's length). Returns the parts
    that changed."""
    done = []
    if src is dst:
        return done
    if "settings" in parts:
        st = copy.deepcopy(src.settings)
        if st != dst.settings:
            dst.settings, dst.preset = st, src.preset
            done.append("settings")
    if "tracks" in parts and src.info is not None and dst.info is not None and not dst.info.error:
        a, b = _kind_order(src.info), _kind_order(dst.info)
        where = {v: k for k, v in b.items()}
        tracks, smeta = {}, {}
        for key, val in src.tracks.items():
            to = where.get(a.get(int(key)) if key.isdigit() else None)
            if to is not None:
                tracks[str(to)] = copy.deepcopy(val)
        for key, val in (src.meta.get("streams") or {}).items():
            to = where.get(a.get(int(key)) if str(key).isdigit() else None)
            if to is not None:
                smeta[str(to)] = copy.deepcopy(val)
        old = (dst.tracks, dst.meta.get("streams"))
        dst.tracks = tracks
        if smeta:
            dst.meta["streams"] = smeta
        else:
            dst.meta.pop("streams", None)
        if (dst.tracks, dst.meta.get("streams")) != old:
            done.append("tracks")
    if "trim" in parts:
        end = src_duration(dst) or 0.0
        pcs = [[a, min(b, end) if end else b] for a, b in src.trim if not end or a < end - 0.05]
        if (pcs, src.trim_cut) != (dst.trim, dst.trim_cut):
            dst.trim, dst.trim_cut = pcs, src.trim_cut
            done.append("trim")
    return done


# ── 1.8: EDL — lists of pieces from editors and players ───────
def _edl_tc(text: str, fps: float) -> float | None:
    """«01:02:03:12» (frames) / «01:02:03.500» / «3723.5» → seconds."""
    text = text.strip()
    m = re.fullmatch(r"(\d+):(\d\d):(\d\d)[:;](\d\d)", text)
    if m:
        h, mi, s, f = (int(x) for x in m.groups())
        return h * 3600 + mi * 60 + s + f / (fps or 25.0)
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:[.,]\d+)?)", text)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3).replace(",", "."))
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def parse_edl(text: str, fps: float = 25.0) -> tuple[list[list[float]], bool]:
    """An EDL → (pieces, cut). Three kinds are read:
    • MPlayer / Kodi / ComSkip «start end action» lines — the pieces to throw away (cut = True);
    • CMX 3600 (DaVinci Resolve, Premiere, Avid) events — the source in / out of each video event are kept,
      the time code of the first one taken as the start of the file when it is an hour mark (01:00:00:00);
    • mpv's «# mpv EDL v0» «file,start,length» — kept.
    Raises ValueError when nothing is understood."""
    lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]
    if lines and lines[0].lower().startswith("# mpv edl"):
        out = []
        for ln in lines[1:]:
            if ln.startswith("#"):
                continue
            parts = ln.split(",")
            if len(parts) >= 3:
                a, d = _edl_tc(parts[1].split("=")[-1], fps), _edl_tc(parts[2].split("=")[-1], fps)
                if a is not None and d:
                    out.append([round(a, 3), round(a + d, 3)])
        if out:
            return out, False
        raise ValueError("edl")
    cmx = re.compile(r"^\d{1,6}\s+\S+\s+(\S+)\s+\S+(?:\s+\d+)?\s+([\d:;]{11})\s+([\d:;]{11})\s+[\d:;]{11}\s+[\d:;]{11}")
    ev = [m for m in (cmx.match(ln) for ln in lines) if m and "V" in m.group(1).upper()]
    if ev:
        pcs = [(_edl_tc(m.group(2), fps), _edl_tc(m.group(3), fps)) for m in ev]
        base = min(a for a, _b in pcs)
        hour = int(base // 3600) * 3600 if base >= 3600 else 0     # a timeline starting at 01:00:00:00
        return [[round(a - hour, 3), round(b - hour, 3)] for a, b in pcs if b > a], False
    out = []
    for ln in lines:
        if ln.startswith(("#", "TITLE", "FCM")):
            continue
        parts = ln.split()
        if len(parts) >= 2:
            a, b = _edl_tc(parts[0], fps), _edl_tc(parts[1], fps)
            if a is not None and b is not None and b > a:
                out.append([round(a, 3), round(b, 3)])
    if out:
        return out, True
    raise ValueError("edl")


def write_edl(pieces, cut: bool) -> str:
    """The pieces as an MPlayer / Kodi EDL (the parts to skip): the cut-out pieces, or what lies between kept ones."""
    pcs = sorted((float(a), float(b)) for a, b in pieces)
    if not cut:
        gaps, pos = [], 0.0
        for a, b in pcs:
            if a > pos + 0.001:
                gaps.append((pos, a))
            pos = max(pos, b)
        gaps.append((pos, 9e9))
        pcs = gaps
    return "".join(f"{a:.3f}\t{b:.3f}\t0\n" if b < 9e9 else f"{a:.3f}\t99999\t0\n" for a, b in pcs)


# ── 1.8: name templates ──────────────────────────────────────
NAME_TEMPLATES = (
    ("tpl_none", ""),
    ("tpl_film", "{title} ({year}) {res} {vcodec}"),
    ("tpl_film_folder", "{title} ({year})/{title} ({year}) {res} {hdr} {vcodec} {acodec}"),
    ("tpl_series", "{series} - {sxe}"),
    ("tpl_series_folder", "{series}/Season {season}/{series} - {sxe} {res}"),
    ("tpl_release", "{title}.{year}.{res}.{source}.{vcodec}.{acodec}"),
)
_RE_SXE = re.compile(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})|(\d{1,2})x(\d{2,3})")
_RE_YEAR = re.compile(r"[(\[ ._-]((?:19|20)\d\d)(?:[)\] ._-]|$)")
_RE_SOURCE = re.compile(r"\b(UHD[ ._-]?Blu-?ray|Blu-?ray|BDRip|BDRemux|Remux|WEB-?DL|WEBRip|WEB|HDTV|DVDRip|DVD)\b", re.I)


def name_fields(job: Job) -> dict:
    """What a template can use: from the file name (title, year, series, S01E02, source), its tags, and what
    the result will be (resolution, codecs, HDR, bits, container)."""
    stem = os.path.splitext(os.path.basename(job.src))[0]
    tags = job.info.tags if job.info is not None and not job.info.error else {}
    tags = dict(tags) | {k: v for k, v in (job.meta.get("tags") or {}).items() if v}
    clean = re.sub(r"[._]+", " ", stem).strip()
    year = ""
    m = _RE_YEAR.search(" " + stem + " ")
    if m:
        year = m.group(1)
    elif re.match(r"(19|20)\d\d", str(tags.get("date", tags.get("DATE", "")))):
        year = str(tags.get("date", tags.get("DATE", "")))[:4]
    se = _RE_SXE.search(stem)
    season = episode = ""
    if se:
        season, episode = (se.group(1), se.group(2)) if se.group(1) else (se.group(3), se.group(4))
    cut = len(clean)
    for rx in (_RE_YEAR, _RE_SXE, _RE_SOURCE):
        mm = rx.search(" " + clean + " ")
        if mm:
            cut = min(cut, max(0, mm.start() - 1))
    title_guess = re.sub(r"[\s(\[-]+$", "", clean[:cut]).strip() or clean
    title = str(tags.get("title") or tags.get("TITLE") or "").strip() if not se else ""
    title = title or title_guess
    src_m = _RE_SOURCE.search(stem)
    p = plan(job)
    v = job.settings.get("video", {})
    out = {"name": stem, "title": title, "year": year, "series": title_guess, "season": f"{int(season):02d}" if season else "",
           "episode": f"{int(episode):02d}" if episode else "",
           "sxe": f"S{int(season):02d}E{int(episode):02d}" if season else "", "source": src_m.group(1) if src_m else "",
           "res": "", "vcodec": "", "acodec": "", "hdr": "", "bits": "", "container": (job.settings.get("container") or "").upper()}
    if p["video"]:
        s = p["video"][0]
        try:
            if v.get("mode") == "copy":
                w, h = s.width, s.height
            else:
                eid = v.get("encoder", "auto_hevc")
                e = E.BY_ID.get(job.enc_used) or (E.LAST.resolve(eid) if E.LAST is not None else None) or E.BY_ID.get(eid)
                w, h = out_size(job, e)
        except Exception:                                  # noqa: BLE001
            w, h = s.width, s.height
        from .probe import resolution_name
        out["res"] = resolution_name(w, h)
        if v.get("mode") == "copy":
            fam = {"h264": "H264", "hevc": "HEVC", "av1": "AV1"}.get(s.codec, s.codec.upper())
        else:
            eid = v.get("encoder", "auto_hevc")
            fam = enc_family(eid) if eid.startswith("auto_") else (E.BY_ID[eid].family if eid in E.BY_ID else eid)
            fam = {"h264": "H264", "hevc": "HEVC", "av1": "AV1", "mpeg4": "XviD", "vp9": "VP9"}.get(fam, str(fam).upper())
        out["vcodec"] = fam
        out["bits"] = "10bit" if str(v.get("bits")) == "10" or (v.get("bits") in ("auto", None) and s.bits > 8) else ""
        keep_hdr = s.hdr and not F.fset(job.settings, "hdr").get("mode") == "sdr"
        out["hdr"] = s.hdr.replace("Dolby Vision", "DV") if keep_hdr else ""
    if p["audio"]:
        s, c = p["audio"][0]
        name = s.codec if c == "copy" else E.A_CODEC.get(c, c)
        out["acodec"] = {"ac3": "AC3", "eac3": "EAC3", "dts": "DTS", "truehd": "TrueHD", "aac": "AAC", "opus": "Opus",
                         "flac": "FLAC", "mp3": "MP3"}.get(name, str(name).upper())
    return out


def render_name(tpl: str, job: Job) -> str:
    """A template filled in; empty fields leave no «()» / doubled spaces or dots; characters Windows refuses go."""
    f = name_fields(job)
    if f["res"] in ("4K", "8K"):                         # in a file name: the number of lines
        f["res"] = {"4K": "2160p", "8K": "4320p"}[f["res"]]
    if "}.{" in tpl:                                     # a release name: words joined by dots
        f = {k: re.sub(r"\s+", ".", str(v)) for k, v in f.items()}
    text = re.sub(r"\{(\w+)\}", lambda m: str(f.get(m.group(1), m.group(0))), tpl)
    parts = []
    for part in text.replace("\\", "/").split("/"):
        part = re.sub(r"\(\s*\)|\[\s*\]|\bSeason\s*$", "", part)
        part = re.sub(r'[<>:"|?*\x00-\x1f]', "", part)
        part = re.sub(r"\s{2,}", " ", part)
        part = re.sub(r"\.{2,}", ".", part)
        part = re.sub(r"\s+-\s*$|^\s*-\s+", "", part)
        part = part.strip(" .-")
        if part:
            parts.append(part)
    return "/".join(parts) or os.path.splitext(os.path.basename(job.src))[0]


# ── 1.8: demux — every track into a file of its own, as it is ─
DEMUX_EXT = {"h264": ("h264", "h264"), "hevc": ("hevc", "hevc"), "av1": ("ivf", "ivf"), "mpeg2video": ("m2v", "mpeg2video"),
             "mpeg1video": ("m1v", "mpeg1video"), "vc1": ("vc1", "vc1"), "vp9": ("ivf", "ivf"), "vp8": ("ivf", "ivf"),
             "mpeg4": ("avi", "avi"), "ac3": ("ac3", "ac3"), "eac3": ("eac3", "eac3"), "dts": ("dts", "dts"),
             "truehd": ("thd", "truehd"), "mlp": ("mlp", "mlp"), "aac": ("aac", "adts"), "mp3": ("mp3", "mp3"),
             "mp2": ("mp2", "mp2"), "flac": ("flac", "flac"), "opus": ("opus", "opus"), "vorbis": ("ogg", "ogg"),
             "alac": ("m4a", "ipod"), "subrip": ("srt", "srt"), "ass": ("ass", "ass"), "ssa": ("ssa", "ass"),
             "webvtt": ("vtt", "webvtt"), "hdmv_pgs_subtitle": ("sup", "sup"), "dvd_subtitle": ("mks", "matroska"),
             "dvb_subtitle": ("mks", "matroska"), "mov_text": ("srt", "srt")}


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", s).strip()[:120]


def demux_items(job: Job, folder: str, cores: bool = True) -> list[dict]:
    """What a demux writes: [{stream, path, args, what}] — each track as it is (PCM as WAV, mov_text as SRT, DVD
    and DVB subtitles in MKS); 1.8 cores: the DTS core of DTS-HD and the AC3 core of E-AC3 as files of their own
    (the AC3 core of a Blu-ray's TrueHD is a track of its own already)."""
    if job.info is None or job.info.error:
        return []
    stem = _safe_name(os.path.splitext(os.path.basename(job.src))[0]) or "track"
    out = []
    for s in job.info.streams:
        if s.kind not in ("video", "audio", "subtitle") or s.cover:
            continue
        lang = (job.smeta(s).get("lang") or s.lang or "").replace("und", "")
        tag = " ".join(x for x in (f"#{s.index}", lang, _safe_name(s.title)) if x)
        ext, fmt = DEMUX_EXT.get(s.codec, ("", ""))
        args = ["-c", "copy"]
        if s.codec.startswith("pcm_"):
            bits = 24 if ("24" in s.codec or s.codec == "pcm_bluray") else 16
            ext, fmt, args = "wav", "wav", ["-c", f"pcm_s{bits}le"]
        elif s.codec == "mov_text":
            args = ["-c", "srt"]
        elif not ext:
            ext, fmt = ("mka", "matroska") if s.kind == "audio" else ("mks", "matroska") if s.kind == "subtitle" \
                else ("mkv", "matroska")
        if s.kind == "video" and s.codec in ("h264", "hevc"):
            args += ["-bsf:v", f"{s.codec}_mp4toannexb"]
        base = os.path.join(folder, f"{stem} - {tag}")
        out.append({"stream": s, "path": f"{base}.{ext}", "args": ["-map", f"0:{s.index}"] + args + ["-f", fmt],
                    "what": P_codec(s)})
        if cores and s.codec == "dts" and dts_hd(job, s):
            out.append({"stream": s, "path": f"{base} core.dts", "what": "DTS core", "core": True,
                        "args": ["-map", f"0:{s.index}", "-c", "copy", "-bsf:a", "dca_core", "-f", "dts"]})
        if cores and s.codec == "eac3" and (s.channels or 0) > 6:
            out.append({"stream": s, "path": f"{base} core.ac3", "what": "AC3 core", "core": True,
                        "args": ["-map", f"0:{s.index}", "-c", "copy", "-bsf:a", "eac3_core", "-f", "ac3"]})
    return out


# ── 1.9: is a DTS track DTS-HD (MA / HRA, with a core that dca_core can cut out)? ─
DTS_EXSS = b"\x64\x58\x20\x25"          # the sync word of DTS-HD's extension substream (XLL, XBR, X96 …)
_dts_sniffed: dict = {}


def dts_hd(job: Job, s: Stream, sniff: bool = True) -> bool:
    """ffprobe's profile first (DTS-HD MA, DTS-HD HRA, DTS-HD MA + DTS:X …); then a Blu-ray's stream type
    (0x86 MA, 0x85 HRA) and a bit rate a bare DTS can't have; at last a look at the stream's first frames for
    the extension substream — a short probe of a busy Blu-ray file can report only «DTS». DTS Express has no core."""
    if s.codec != "dts":
        return False
    p = (s.profile or "").upper()
    if "EXPRESS" in p:
        return False
    if "HD" in p or "XLL" in p or "DTS:X" in p:
        return True
    if s.tag in (0x85, 0x86) or s.bitrate > 1_600_000:
        return True
    return bool(sniff and dts_sniff(job.src, s.index))


def dts_sniff(src: str, index: int, frames: int = 12) -> bool:
    """The first frames of the track copied out (no decoding): an extension substream in them = DTS-HD."""
    key = (src, index)
    if key not in _dts_sniffed:
        from . import ff
        exe = ff.ffmpeg()
        found = False
        if exe:
            try:
                r = ff.run([exe, "-hide_banner", "-nostdin", "-v", "error"] + ff_inp(src) +
                           ["-map", f"0:{index}", "-c", "copy", "-frames:a", str(frames), "-f", "dts", "-"], 30,
                           binary=True)
                found = DTS_EXSS in (r.stdout or b"")
            except Exception:
                found = False
        _dts_sniffed[key] = found
    return _dts_sniffed[key]


def P_codec(s: Stream) -> str:
    from .probe import codec_name
    name = codec_name(s)
    return name + (f" ({s.profile})" if s.profile and s.kind == "audio" and s.profile.lower() not in name.lower()
                   else "")


def demux_args(job: Job, ffmpeg: str, items: list[dict], chapters_txt: str = "") -> list[str]:
    """One ffmpeg run that reads the file once and writes every chosen track."""
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "info", "-nostats", "-progress", "pipe:1"]
    args += ff_inp(job.src)
    for it in items:
        args += it["args"] + [it["path"]]
    return args


def chapters_text(job: Job) -> str:
    """The chapters as an OGM chapter file (CHAPTER01=00:00:00.000 / CHAPTER01NAME=…), "" when none."""
    ch = out_chapters(job) if job.chapters is not None else [(a, b, n) for a, b, n in (job.info.chapter_list if job.info else [])]
    lines = []
    for i, (a, _b, name) in enumerate(ch or []):
        h, rest = divmod(float(a), 3600)
        m, s = divmod(rest, 60)
        lines.append(f"CHAPTER{i + 1:02d}={int(h):02d}:{int(m):02d}:{s:06.3f}")
        lines.append(f"CHAPTER{i + 1:02d}NAME={name or f'Chapter {i + 1}'}")
    return "\n".join(lines) + ("\n" if lines else "")
