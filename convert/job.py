"""A task of the queue: a source file, its settings, where it goes, its state — and the ffmpeg command for it.

Settings are a plain dict (JSON in the queue / presets):
  container  mkv | mp4
  video      {mode: encode | copy, encoder: auto_hevc | nvenc_hevc | x265 | …, rc: quality | bitrate | size,
              quality, bitrate (kbit/s), size_mb, speed 1..7, bits: auto | 8 | 10, height: 0 = as is,
              fps: 0 = as is, deint: auto | on | off}
  audio      {codec: auto | copy | aac | ac3 | eac3 | opus | flac | mp3, kbps: 0 = auto, channels: keep | stereo | 5.1,
              normalize: bool, tracks: all | first}
  subs       all | first | none
  meta       bool (title, tags, chapters)
Per-file choices of the Streams tab are in Job.tracks: {stream index: {"keep": bool, "codec": "" | id}}."""
from __future__ import annotations

import copy
import itertools
import os
from dataclasses import dataclass, field

from . import encoders as E
from .probe import MediaInfo, Stream

DEFAULT_SETTINGS = {
    "container": "mkv",
    "video": {"mode": "encode", "encoder": "auto_hevc", "rc": "quality", "quality": 0, "bitrate": 8000,
              "size_mb": 4000, "speed": 5, "bits": "auto", "height": 0, "fps": 0, "deint": "auto"},
    "audio": {"codec": "auto", "kbps": 0, "channels": "keep", "normalize": False, "tracks": "all"},
    "subs": "all",
    "meta": True,
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
}


def merged(*parts: dict) -> dict:
    """DEFAULT_SETTINGS with the parts laid over it (one level deep for video / audio)."""
    out = copy.deepcopy(DEFAULT_SETTINGS)
    for p in parts:
        for k, v in (p or {}).items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
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

    def to_dict(self) -> dict:
        return {"src": self.src, "settings": self.settings, "preset": self.preset, "tracks": self.tracks,
                "status": self.status if self.status in ("done", "error", "stop", "skip") else "wait",
                "out": self.out if self.status == "done" else "", "error": self.error}

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        j = cls(src=str(d.get("src", "")), settings=merged(d.get("settings") or {}), preset=d.get("preset", ""),
                tracks={str(k): v for k, v in (d.get("tracks") or {}).items() if isinstance(v, dict)})
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


# ── which streams go out, and how ────────────────────────────
def plan(job: Job) -> dict:
    """{video: [Stream], audio: [(Stream, codec id | "copy")], subs: [(Stream, "copy" | "mov_text")],
        warnings: [text keys]} — the decisions build_args turns into arguments."""
    info, st = job.info, job.settings
    cont = st.get("container", "mkv")
    out = {"video": [], "audio": [], "subs": [], "warnings": []}
    if info is None:
        return out
    v = info.of("video")
    if v:
        out["video"] = [v[0]]
    a_cfg = st.get("audio", {})
    audios = info.of("audio")
    if a_cfg.get("tracks") == "first":
        audios = audios[:1]
    ok_a = E.CONTAINER_AUDIO.get(cont)
    for s in info.of("audio"):
        t = job.track(s)
        keep = t.get("keep", s in audios)
        if not keep:
            continue
        codec = t.get("codec") or a_cfg.get("codec", "auto")
        if codec == "auto":
            changes = a_cfg.get("channels", "keep") != "keep" or a_cfg.get("normalize")
            codec = "copy" if (ok_a is None or s.codec in ok_a) and not changes else "aac"
        if codec == "copy" and ok_a is not None and s.codec not in ok_a:
            codec = "aac"
            out["warnings"].append("w_audio_reencoded")
        out["audio"].append((s, codec))
    subs = info.of("subtitle")
    mode = st.get("subs", "all")
    if mode == "none":
        subs = []
    elif mode == "first":
        subs = subs[:1]
    for s in info.of("subtitle"):
        t = job.track(s)
        if not t.get("keep", s in subs):
            continue
        if cont == "mp4":
            if s.codec in E.TEXT_SUBS:
                out["subs"].append((s, "mov_text"))
            else:
                out["warnings"].append("w_bitmap_subs")
        else:
            out["subs"].append((s, "copy"))
    return out


def out_bits(job: Job, enc: E.VEnc | None) -> int:
    want = str(job.settings.get("video", {}).get("bits", "auto"))
    if enc is None:
        return 8
    if want == "auto":
        src = job.info.video.bits if job.info and job.info.video else 8
        return 10 if (src > 8 or enc.family in ("hevc", "av1")) and 10 in enc.bits else 8
    return 10 if want == "10" and 10 in enc.bits else 8


def video_filters(job: Job) -> list[str]:
    v, src = job.settings.get("video", {}), job.info.video if job.info else None
    out = []
    deint = v.get("deint", "auto")
    if deint == "on" or (deint == "auto" and src is not None and src.interlaced):
        out.append("bwdif=mode=send_frame:parity=auto:deint=all")
    h = int(v.get("height", 0) or 0)
    if h and src is not None and src.height and h < src.height:
        out.append(f"scale=-2:{h}:flags=lanczos")
    fps = float(v.get("fps", 0) or 0)
    if fps and src is not None and (not src.fps or fps < src.fps - 0.01):
        out.append(f"fps={fps:g}")
    return out


def audio_kbps(codec: str, channels: int, cfg: dict) -> int:
    a = E.A_BY_ID.get(codec)
    if a is None or a.kbps == (0, 0):
        return 0
    k = int(cfg.get("kbps", 0) or 0)
    if k:
        return k
    return a.kbps[1] if channels > 2 else a.kbps[0]


def build_args(job: Job, ffmpeg: str, out_path: str, av: E.Available, hw_decode: bool = True) -> list[str]:
    """The whole ffmpeg command line. Progress goes to stdout (-progress pipe:1), the log to stderr."""
    st = job.settings
    v = st.get("video", {})
    cont = st.get("container", "mkv")
    p = plan(job)
    enc = None
    copy_v = v.get("mode") == "copy"
    if p["video"] and not copy_v:
        enc = av.resolve(v.get("encoder", "auto_hevc"))
        if enc is None:
            raise RuntimeError("no_encoder")
        job.enc_used = enc.id
    else:
        job.enc_used = "copy" if p["video"] else ""
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "info", "-nostats", "-progress", "pipe:1"]
    if enc is not None and enc.hw and hw_decode:
        args += ["-hwaccel", "auto"]
    args += ["-i", job.src]
    args += ["-map_metadata", "0" if st.get("meta", True) else "-1",
             "-map_chapters", "0" if st.get("meta", True) else "-1"]
    # video
    for s in p["video"]:
        args += ["-map", f"0:{s.index}"]
        if copy_v:
            args += ["-c:v", "copy"]
        else:
            bits = out_bits(job, enc)
            dur = job.info.duration if job.info else 0.0
            a_total = sum(audio_kbps(c, a.channels if st["audio"].get("channels") == "keep" else
                                     (2 if st["audio"].get("channels") == "stereo" else 6), st["audio"]) or
                          (a.bitrate // 1000 if c == "copy" else 0) for a, c in p["audio"])
            vv = dict(v)
            vv["audio_kbps"] = a_total
            if not vv.get("quality"):
                vv["quality"] = enc.q_default
            args += ["-c:v", enc.ffname] + E.video_args(enc, vv, bits, dur)
            vf = video_filters(job)
            if vf:
                args += ["-vf", ",".join(vf)]
            if bits >= 10 and s.hdr in ("HDR10", "HLG"):                  # keep the HDR colours
                c = s.color
                prim = c.get("color_primaries", "bt2020")
                trc = c.get("color_transfer", "smpte2084" if s.hdr == "HDR10" else "arib-std-b67")
                mat = c.get("color_space", "bt2020nc")
                args += ["-color_primaries", prim, "-color_trc", trc, "-colorspace", mat]
                if enc.id == "x265":                                      # into the VUI itself, every build
                    i = args.index("-x265-params") + 1
                    args[i] += f":colorprim={prim}:transfer={trc}:colormatrix={mat}" + \
                        (":hdr10=1:repeat-headers=1" if s.hdr == "HDR10" else "")
        if cont == "mp4" and ((copy_v and s.codec == "hevc") or (enc is not None and enc.family == "hevc")):
            args += ["-tag:v", "hvc1"]                                      # plays on Apple / TVs
    # audio
    a_cfg = st.get("audio", {})
    for n, (s, codec) in enumerate(p["audio"]):
        args += ["-map", f"0:{s.index}"]
        if codec == "copy":
            args += [f"-c:a:{n}", "copy"]
            continue
        a = E.A_BY_ID[codec]
        ch = s.channels
        want = a_cfg.get("channels", "keep")
        if want == "stereo":
            ch = min(ch, 2) if ch else 2
        elif want == "5.1" and ch > 6:
            ch = 6
        ch = min(ch or 2, a.max_ch)
        args += [f"-c:a:{n}", a.ffname]
        if ch != s.channels:
            args += [f"-ac:a:{n}", str(ch)]
        k = audio_kbps(codec, ch, a_cfg)
        if k:
            args += [f"-b:a:{n}", f"{k}k"]
        if codec == "opus" and ch > 2:
            args += [f"-mapping_family:a:{n}", "1"]
        if a_cfg.get("normalize"):
            args += [f"-filter:a:{n}", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    # subtitles
    for n, (s, how) in enumerate(p["subs"]):
        args += ["-map", f"0:{s.index}", f"-c:s:{n}", how]
    if not p["subs"]:
        args += ["-sn"]
    args += ["-dn", "-max_muxing_queue_size", "4096"]
    if cont == "mp4":
        args += ["-movflags", "+faststart", "-f", "mp4"]
    else:
        args += ["-f", "matroska"]
    args += [out_path]
    return args


def out_name(job: Job, cfg: dict) -> str:
    """Where the result goes: next to the source or into the chosen folder, name + suffix, never over the
    source; an existing file gets « (2)» unless overwrite is on."""
    stem = os.path.splitext(os.path.basename(job.src))[0]
    folder = cfg.get("out_dir") if cfg.get("out_mode") == "folder" and cfg.get("out_dir") else os.path.dirname(job.src)
    ext = "." + job.settings.get("container", "mkv")
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
    bits = []
    if v.get("mode") == "copy":
        bits.append(tr("v_copy"))
    else:
        eid = v.get("encoder", "auto_hevc")
        e = av.resolve(eid) if av is not None else E.BY_ID.get(eid)
        fam = E.FAMILY_LABEL.get(e.family if e else eid[5:], "?")
        b = v.get("bits", "auto")
        bits.append(fam + (f" {b} bit" if b in ("8", "10") else ""))
        if e is not None:
            bits.append(E.VENDOR_LABEL[e.vendor] if e.hw else e.ffname.replace("lib", ""))
        rc = v.get("rc", "quality")
        if rc == "quality":
            q = v.get("quality") or (e.q_default if e else 0)
            bits.append(("CRF " if e is not None and not e.hw else "Q ") + str(q))
        elif rc == "bitrate":
            bits.append(f"{v.get('bitrate')} kbit/s")
        else:
            bits.append(f"{v.get('size_mb')} MB")
        if v.get("height"):
            bits.append(f"{v['height']}p")
    a = st.get("audio", {})
    if a.get("codec") not in ("auto", "copy"):
        bits.append(E.A_BY_ID[a["codec"]].label.split(" ")[0])
    bits.append(st.get("container", "mkv").upper())
    return " · ".join(bits)
