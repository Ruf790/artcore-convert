"""1.5: presets of the format (XviD4PSP's «Preset of the format»), the container options (Format options) and
the device presets.

A preset of the format is a dict kept in the task settings as settings["fmt"] (a copy, so a saved queue or a
preset file carries it whole); {} = no preset of the format, the plain container:
  name        its name (the list of the editor)
  container   the container it writes ("" = the one chosen)
  ext         the file extension ("" = the container's own: .mp4 → .m4v …)
  vcodecs     the video families it allows (h264, hevc, av1, vp9, mpeg4, mpeg2video …; [] = any of the container)
  acodecs     the audio codecs it allows (our ids aac, ac3 …; the first is what other sound becomes; [] = any)
  profile     {family: profile}     level  {family: level}      ("" / missing = the encoder decides)
  gop_min, gop_max, gop_unit (frames | sec), gop_closed — the keyframes
  vbv_buf (kbit), vbv_max (kbit/s) — the VBV buffer and the highest bit rate (0 = none)
  max_audio   sound tracks at most (0 = any); a_maxch — channels at most (0 = any)
  width, height, size_limit — the frame by default (the film fits inside, never bigger); size_limit: a frame the
              user chose is held inside it too
  dur         the result is at most this long (seconds, 0 = the whole film)

The container options are settings["mux"] (MUX_DEFAULT). The device presets are task settings with a preset of
the format inside (DEVICES), shown in the preset list in groups."""
from __future__ import annotations

import copy
import json
import os
import re

from . import encoders as E

FMT_DEFAULT = {
    "name": "", "container": "", "ext": "", "vcodecs": [], "acodecs": [], "profile": {}, "level": {},
    "gop_min": 0.0, "gop_max": 0.0, "gop_unit": "frames", "gop_closed": False, "vbv_buf": 0, "vbv_max": 0,
    "max_audio": 0, "a_maxch": 0, "width": 0, "height": 0, "size_limit": False, "dur": 0.0,
}

VFAMILIES = ("h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "prores", "gif")
PROFILES = {
    "h264": ("baseline", "main", "high", "high10", "high422", "high444"),
    "hevc": ("main", "main10", "rext"),
    "av1": ("main", "high", "professional"),
    "mpeg4": ("simple", "asp"),
    "mpeg2video": ("simple", "main", "high"),
}
LEVELS = {
    "h264": ("1.0", "1.1", "1.2", "1.3", "2.0", "2.1", "2.2", "3.0", "3.1", "3.2", "4.0", "4.1", "4.2", "5.0", "5.1",
             "5.2", "6.0", "6.1", "6.2"),
    "hevc": ("1.0", "2.0", "2.1", "3.0", "3.1", "4.0", "4.1", "5.0", "5.1", "5.2", "6.0", "6.1", "6.2"),
    "av1": ("2.0", "2.1", "3.0", "3.1", "4.0", "4.1", "5.0", "5.1", "5.2", "5.3", "6.0", "6.1", "6.2", "6.3"),
    "mpeg4": ("0", "1", "2", "3", "4", "5"),
    "mpeg2video": ("low", "main", "high1440", "high"),
}
# the highest bit depth / chroma a profile allows (what the result is held to)
PROFILE_CAP = {("h264", "baseline"): (8, "420"), ("h264", "main"): (8, "420"), ("h264", "high"): (8, "420"),
               ("h264", "high10"): (10, "420"), ("h264", "high422"): (10, "422"),
               ("hevc", "main"): (8, "420"), ("hevc", "main10"): (10, "420"),
               ("av1", "main"): (10, "420"), ("av1", "high"): (10, "444"),
               ("mpeg4", "simple"): (8, "420"), ("mpeg4", "asp"): (8, "420"),
               ("mpeg2video", "simple"): (8, "420"), ("mpeg2video", "main"): (8, "420")}
CHROMA_ORDER = ("420", "422", "444")


def clean(f: dict | None) -> dict:
    """A preset of the format with every key, the values made sane (an old or hand-written file opens too)."""
    f = f if isinstance(f, dict) else {}
    out = copy.deepcopy(FMT_DEFAULT)
    for k, d in FMT_DEFAULT.items():
        if k not in f or f[k] is None:
            continue
        v = f[k]
        try:
            if isinstance(d, bool):
                out[k] = bool(v)
            elif isinstance(d, float):
                out[k] = max(0.0, round(float(v), 3))
            elif isinstance(d, int):
                out[k] = max(0, int(float(v)))
            elif isinstance(d, str):
                out[k] = str(v)
            elif isinstance(d, list):
                out[k] = [str(x) for x in v] if isinstance(v, (list, tuple)) else []
            elif isinstance(d, dict):
                out[k] = {str(a): str(b) for a, b in v.items() if str(b)} if isinstance(v, dict) else {}
        except (TypeError, ValueError):
            pass
    if out["gop_unit"] not in ("frames", "sec"):
        out["gop_unit"] = "frames"
    if out["container"] and out["container"] not in E.CONT:
        out["container"] = ""
    out["ext"] = re.sub(r"[^0-9A-Za-z]", "", out["ext"])[:8].lower()
    out["vcodecs"] = [x for x in out["vcodecs"] if x in VFAMILIES]
    out["acodecs"] = [x for x in out["acodecs"] if x in E.A_BY_ID]
    out["profile"] = {k: v for k, v in out["profile"].items() if v in PROFILES.get(k, ())}
    out["level"] = {k: v for k, v in out["level"].items() if v in LEVELS.get(k, ())}
    return out


def of(st: dict) -> dict:
    """The preset of the format of these task settings ({} when there is none)."""
    f = st.get("fmt") if isinstance(st, dict) else None
    return f if isinstance(f, dict) and f else {}


def active(st: dict) -> bool:
    return bool(of(st))


def gop_frames(f: dict, fps: float, key: str = "gop_max") -> int:
    n = float(f.get(key, 0) or 0)
    if n <= 0:
        return 0
    if f.get("gop_unit") == "sec":
        return max(1, int(round(n * (fps or 25.0))))
    return max(1, int(round(n)))


# ── what the format allows ───────────────────────────────────
def takes_video(st: dict, family: str) -> bool:
    vc = of(st).get("vcodecs") or []
    return not vc or family in vc


def takes_audio(st: dict, codec_id: str) -> bool:
    """codec_id: our audio id (aac …) or the ffprobe codec name of a source track."""
    ac = of(st).get("acodecs") or []
    if not ac:
        return True
    names = {E.A_CODEC.get(a, a) for a in ac}
    return codec_id in ac or codec_id in names


def audio_default(st: dict, c: E.Container) -> str:
    """What sound the format does not take becomes: its first codec the container holds."""
    for a in of(st).get("acodecs") or []:
        if c.takes_audio(E.A_CODEC.get(a, a)):
            return a
    return c.a_default


def encoder_for(family: str) -> str:
    """The encoder id to switch to for a family («auto_…» where there is more than one)."""
    if family in E.FAMILIES:
        return "auto_" + family
    return next((e.id for e in E.VIDEO if e.family == family), "auto_hevc")


def caps(st: dict, family: str) -> tuple[int, str]:
    """(the highest bit depth, the widest chroma) the profile of the format allows the family (16, 444 = any)."""
    p = (of(st).get("profile") or {}).get(family, "")
    return PROFILE_CAP.get((family, p), (16, "444"))


def hold_chroma(want: str, cap: str) -> str:
    if want not in CHROMA_ORDER or cap not in CHROMA_ORDER:
        return want
    return want if CHROMA_ORDER.index(want) <= CHROMA_ORDER.index(cap) else cap


def apply(st: dict, f: dict | None) -> dict:
    """Put a preset of the format into task settings (None / {} takes it away): its container, an encoder and
    a sound codec it allows, its frame."""
    f = clean(f) if f else {}
    st["fmt"] = f
    if f.get("container"):
        st["container"] = f["container"]
    from . import job as J
    J.fit_container(st)
    return st


# ── arguments ────────────────────────────────────────────────
NV_PROFILE = {"high444": "high444p"}
AMF_PROFILE = {"baseline": "constrained_baseline"}
NUM_PROFILE = {("av1", "main"): "0", ("av1", "high"): "1", ("av1", "professional"): "2",
               ("mpeg4", "simple"): "0", ("mpeg4", "asp"): "15",
               ("mpeg2video", "simple"): "5", ("mpeg2video", "main"): "4", ("mpeg2video", "high"): "1"}
MPEG2_LEVEL = {"low": "10", "main": "8", "high1440": "6", "high": "4"}


def _named(caps_: dict, opt: str, value: str) -> bool:
    """The encoder has the option and (where it lists named values) this value."""
    if opt not in caps_:
        return False
    vals = caps_[opt]
    return not vals or value in vals


def profile_args(e: E.VEnc, profile: str, bits: int, chroma: str, caps_: dict) -> tuple[list[str], dict]:
    """(arguments, {params option: [key=value]}) for a profile; [] when this encoder here can't set it."""
    fam = e.family
    if not profile:
        return [], {}
    if e.id == "x264":
        return ["-profile:v", profile], {}
    if e.id == "x265":
        if profile == "rext":
            if chroma in ("422", "444"):
                return ["-profile:v", f"main{chroma}-{bits if bits > 8 else 8}"], {}
            return [], {}
        return ["-profile:v", profile], {}
    if e.vendor in ("nvidia", "amd", "intel"):
        name = NV_PROFILE.get(profile, profile) if e.vendor == "nvidia" else \
            AMF_PROFILE.get(profile, profile) if e.vendor == "amd" else profile
        if _named(caps_, "profile", name):
            return ["-profile:v", name], {}
        return [], {}
    num = NUM_PROFILE.get((fam, profile))
    if num is not None and e.id in ("svtav1", "xvid", "mpeg4", "mpeg2"):
        return ["-profile:v", num], {}
    return [], {}


def level_args(e: E.VEnc, level: str, caps_: dict) -> tuple[list[str], dict]:
    if not level:
        return [], {}
    fam = e.family
    if e.id == "x264":
        return ["-level", level], {}
    if e.id == "x265":
        return [], {"-x265-params": [f"level-idc={level}"]}
    if e.vendor in ("nvidia", "amd"):
        if _named(caps_, "level", level):
            return ["-level", level], {}
        return [], {}
    try:
        x = float(level)
    except ValueError:
        x = 0.0
    if e.vendor == "intel" and caps_.get("level"):                  # a QSV build with named levels
        return (["-level", level], {}) if level in caps_["level"] else ([], {})
    if e.vendor == "intel":                                         # QSV: the general -level, as a number
        if fam == "h264":
            return ["-level", str(int(round(x * 10)))], {}
        if fam == "hevc":
            return ["-level", str(int(round(x * 30)))], {}
        return [], {}
    if e.id == "svtav1":
        return ["-level", str(int(round(x * 10)))], {}
    if fam == "mpeg4" and e.id in ("xvid", "mpeg4"):
        return ["-level", level], {}
    if fam == "mpeg2video":
        return ["-level", MPEG2_LEVEL.get(level, "8")], {}
    return [], {}


def gop_args(e: E.VEnc, f: dict, fps: float) -> tuple[list[str], dict]:
    out: list[str] = []
    params: dict = {}
    g = gop_frames(f, fps, "gop_max")
    m = gop_frames(f, fps, "gop_min")
    if e.id in ("gif", "prores"):
        return [], {}
    if g:
        out += ["-g", str(g)]
    if m and (not g or m <= g) and e.id not in ("svtav1", "vp9"):
        out += ["-keyint_min", str(m)]
    if f.get("gop_closed"):
        if e.id == "x265":
            params["-x265-params"] = ["open-gop=0"]
        elif e.id == "svtav1":
            params["-svtav1-params"] = ["irefresh-type=2"]
        elif e.id in ("x264", "mpeg2", "mpeg4", "xvid") or e.vendor == "intel":
            out += ["-flags", "+cgop"]
            if e.id in ("mpeg2", "mpeg4"):                       # FFmpeg's: no scene cuts in a closed GOP
                out += ["-sc_threshold", "1000000000"]
    return out, params


def vbv_args(e: E.VEnc, f: dict) -> list[str]:
    if e.id in ("gif", "prores"):
        return []
    out = []
    if int(f.get("vbv_max", 0) or 0) > 0:
        out += ["-maxrate", f"{int(f['vbv_max'])}k"]
    if int(f.get("vbv_buf", 0) or 0) > 0:
        out += ["-bufsize", f"{int(f['vbv_buf'])}k"]
    return out


def video_args(e: E.VEnc, st: dict, bits: int, chroma: str, fps: float, caps_: dict) -> tuple[list[str], dict]:
    """The arguments of the preset of the format for this encoder: profile, level, keyframes, VBV."""
    f = of(st)
    if not f:
        return [], {}
    fam = e.family
    args: list[str] = []
    params: dict = {}
    for a, p in (profile_args(e, (f.get("profile") or {}).get(fam, ""), bits, chroma, caps_),
                 level_args(e, (f.get("level") or {}).get(fam, ""), caps_), gop_args(e, f, fps)):
        args += a
        for k, v in p.items():
            params.setdefault(k, []).extend(v)
    if fam == "mpeg4" and (f.get("profile") or {}).get("mpeg4") == "simple":
        args += ["-bf", "0"]                                      # Simple Profile: no B-frames
    args += vbv_args(e, f)
    return args, params


def put(args: list[str], extra: list[str], params: dict | None = None) -> list[str]:
    """Lay options over a list of encoder arguments: one already there gets the new value (-maxrate: the lower
    one); -x264-params / -x265-params / -svtav1-params get the new key=value pairs added (a later key wins)."""
    out = list(args)
    i = 0
    while i < len(extra) - 1:
        k, v = extra[i], extra[i + 1]
        i += 2
        if k in out and out.index(k) + 1 < len(out):
            j = out.index(k) + 1
            if k in ("-maxrate", "-bufsize") and out[j].endswith("k") and v.endswith("k"):
                try:
                    v = f"{min(int(float(out[j][:-1])), int(float(v[:-1])))}k"
                except ValueError:
                    pass
            if k == "-flags" and out[j] != v:
                v = out[j] + v
            out[j] = v
        else:
            out += [k, v]
    for k, pairs in (params or {}).items():
        pairs = [p for p in pairs if p]
        if not pairs:
            continue
        if k in out:
            j = out.index(k) + 1
            out[j] = ":".join(([out[j]] if out[j] else []) + pairs)
        else:
            out += [k, ":".join(pairs)]
    # a bit rate above the highest one the format allows is lowered to it
    if "-maxrate" in out and "-b:v" in out:
        bj, mj = out.index("-b:v") + 1, out.index("-maxrate") + 1
        try:
            b, m = int(float(out[bj].rstrip("k"))), int(float(out[mj].rstrip("k")))
            if out[bj].endswith("k") and b > m > 0:
                out[bj] = f"{m}k"
        except ValueError:
            pass
    return out


# ── container options (Format options) ───────────────────────
MUX_DEFAULT = {
    "mkv_index_kb": 0,          # space kept at the start for the index (KiB)
    "mkv_cues_front": False,    # move the Cues to the front (one more pass over the file)
    "mkv_cluster_bytes": 0,     # a cluster at most this big (0 = ffmpeg's)
    "mkv_cluster_ms": 0,        # and this long
    "mkv_crc32": True,          # CRC32 in every element
    "mkv_default": "auto",      # the «default» flag of the tracks: auto (ffmpeg) | passthrough | infer
    "mp4_faststart": True,      # the index at the start (plays while downloading)
    "mp4_frag": False,          # fragmented MP4
    "ts_muxrate": 0,            # kbit/s (0 = variable)
    "ts_pcr_ms": 0,             # PCR every … ms (0 = ffmpeg's)
}
MKV_DEFAULT_MODES = ("auto", "passthrough", "infer", "infer_no_subs")
MATROSKA = ("mkv", "mka", "webm")
MOV = ("mp4", "mov", "m4a")
TS = ("ts", "m2ts")


def mux(st: dict) -> dict:
    m = dict(MUX_DEFAULT)
    got = st.get("mux") if isinstance(st.get("mux"), dict) else {}
    for k, d in MUX_DEFAULT.items():
        if k in got:
            try:
                m[k] = type(d)(got[k]) if not isinstance(d, bool) else bool(got[k])
            except (TypeError, ValueError):
                pass
    if m["mkv_default"] not in MKV_DEFAULT_MODES:
        m["mkv_default"] = "auto"
    return m


def mux_args(st: dict, cont: str) -> list[str]:
    m = mux(st)
    out: list[str] = []
    if cont in MATROSKA:
        if m["mkv_index_kb"] > 0:
            out += ["-reserve_index_space", str(int(m["mkv_index_kb"]) * 1024)]
        if m["mkv_cues_front"]:
            out += ["-cues_to_front", "1"]
        if m["mkv_cluster_bytes"] > 0:
            out += ["-cluster_size_limit", str(int(m["mkv_cluster_bytes"]))]
        if m["mkv_cluster_ms"] > 0:
            out += ["-cluster_time_limit", str(int(m["mkv_cluster_ms"]))]
        if not m["mkv_crc32"]:
            out += ["-write_crc32", "0"]
        if m["mkv_default"] != "auto":
            out += ["-default_mode", m["mkv_default"]]
    elif cont in MOV:
        if m["mp4_frag"]:
            out += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
        elif m["mp4_faststart"]:
            out += ["-movflags", "+faststart"]
    elif cont in TS:
        if m["ts_muxrate"] > 0:
            out += ["-muxrate", f"{int(m['ts_muxrate'])}k"]
        if m["ts_pcr_ms"] > 0:
            out += ["-pcr_period", str(int(m["ts_pcr_ms"]))]
        if cont == "m2ts":
            out += ["-mpegts_m2ts_mode", "1"]
    elif cont == "mpg":                                          # DVD: the VOB muxer's own rate and packets
        out += ["-muxrate", "10080k", "-packetsize", "2048"]
    return out


# ── device presets ───────────────────────────────────────────
GROUPS = ("general", "apple", "android", "tv", "console", "web", "disc", "own")


def _f(**kw) -> dict:
    return clean(kw)


DEVICES: list[tuple[str, str, dict]] = [
    ("apple", "iphone_hevc", {
        "container": "mp4", "video": {"encoder": "auto_hevc", "bits": "10"},
        "audio": {"codec": "aac"}, "subs": "first",
        "fmt": _f(name="iPhone / iPad (HEVC)", container="mp4", vcodecs=["hevc", "h264"],
                  acodecs=["aac", "alac", "ac3", "eac3"], profile={"hevc": "main10", "h264": "high"},
                  level={"hevc": "5.1", "h264": "5.1"}, width=3840, height=2160, size_limit=True, a_maxch=6)}),
    ("apple", "iphone_h264", {
        "container": "mp4", "video": {"encoder": "auto_h264", "bits": "8"},
        "audio": {"codec": "aac", "channels": "stereo"}, "subs": "first",
        "fmt": _f(name="iPhone / iPad (H.264)", container="mp4", vcodecs=["h264"], acodecs=["aac", "alac"],
                  profile={"h264": "high"}, level={"h264": "4.2"}, width=1920, height=1080, size_limit=True,
                  vbv_max=25000, vbv_buf=25000, a_maxch=2)}),
    ("android", "android", {
        "container": "mp4", "video": {"encoder": "auto_h264", "bits": "8"},
        "audio": {"codec": "aac", "channels": "stereo"}, "subs": "first",
        "fmt": _f(name="Android (H.264)", container="mp4", vcodecs=["h264", "hevc"], acodecs=["aac", "opus", "mp3"],
                  profile={"h264": "high", "hevc": "main"}, level={"h264": "4.1", "hevc": "4.1"},
                  width=1920, height=1080, size_limit=True, vbv_max=20000, vbv_buf=20000, a_maxch=2)}),
    ("tv", "tv_samsung", {
        "container": "mkv", "video": {"encoder": "auto_hevc", "bits": "10"}, "audio": {"codec": "auto"},
        "fmt": _f(name="Samsung TV (Tizen)", container="mkv", vcodecs=["hevc", "h264", "av1", "vp9"],
                  acodecs=["eac3", "ac3", "aac", "mp3", "flac", "opus", "vorbis", "pcm16"],
                  profile={"hevc": "main10", "h264": "high"}, level={"hevc": "5.1", "h264": "5.1"},
                  width=3840, height=2160, size_limit=True, vbv_max=60000, vbv_buf=60000)}),
    ("tv", "tv_lg", {
        "container": "mkv", "video": {"encoder": "auto_hevc", "bits": "10"}, "audio": {"codec": "auto"},
        "fmt": _f(name="LG TV (webOS)", container="mkv", vcodecs=["hevc", "h264", "av1", "vp9"],
                  acodecs=["ac3", "eac3", "aac", "mp3", "flac", "pcm16"], profile={"hevc": "main10", "h264": "high"},
                  level={"hevc": "5.1", "h264": "5.1"}, width=3840, height=2160, size_limit=True,
                  vbv_max=60000, vbv_buf=60000)}),
    ("tv", "tv_sony", {
        "container": "mkv", "video": {"encoder": "auto_hevc", "bits": "10"}, "audio": {"codec": "auto"},
        "fmt": _f(name="Sony Bravia (Google TV)", container="mkv", vcodecs=["hevc", "h264", "vp9", "av1"],
                  acodecs=["eac3", "ac3", "aac", "opus", "flac", "mp3", "vorbis"],
                  profile={"hevc": "main10", "h264": "high"}, level={"hevc": "5.1", "h264": "5.1"},
                  width=3840, height=2160, size_limit=True, vbv_max=50000, vbv_buf=50000)}),
    ("console", "ps5", {
        "container": "mp4", "video": {"encoder": "auto_hevc", "bits": "10"}, "audio": {"codec": "aac"},
        "subs": "none",
        "fmt": _f(name="PlayStation 5", container="mp4", vcodecs=["hevc", "h264"], acodecs=["aac", "ac3"],
                  profile={"hevc": "main10", "h264": "high"}, level={"hevc": "5.1", "h264": "5.1"},
                  width=3840, height=2160, size_limit=True, a_maxch=6, max_audio=1)}),
    ("console", "xbox", {
        "container": "mkv", "video": {"encoder": "auto_hevc", "bits": "10"}, "audio": {"codec": "auto"},
        "fmt": _f(name="Xbox Series X|S", container="mkv", vcodecs=["hevc", "h264", "vp9"],
                  acodecs=["ac3", "eac3", "aac", "mp3", "flac"], profile={"hevc": "main10", "h264": "high"},
                  level={"hevc": "5.1", "h264": "5.1"}, width=3840, height=2160, size_limit=True, a_maxch=8)}),
    ("web", "youtube", {
        "container": "mp4", "video": {"encoder": "auto_h264", "bits": "8", "quality": 18},
        "audio": {"codec": "aac", "kbps": 384, "tracks": "first"}, "subs": "none",
        "fmt": _f(name="YouTube", container="mp4", vcodecs=["h264", "hevc", "av1", "vp9"], acodecs=["aac", "opus"],
                  profile={"h264": "high"}, gop_max=0.5, gop_unit="sec", gop_closed=True,
                  max_audio=1, a_maxch=6)}),
    ("web", "telegram", {
        "container": "mp4", "video": {"encoder": "auto_h264", "bits": "8", "quality": 0},
        "audio": {"codec": "aac", "kbps": 128, "channels": "stereo", "tracks": "first"}, "subs": "none",
        "fmt": _f(name="Telegram", container="mp4", vcodecs=["h264"], acodecs=["aac"], profile={"h264": "high"},
                  level={"h264": "4.1"}, width=1920, height=1080, size_limit=True, max_audio=1, a_maxch=2)}),
    ("web", "discord10", {
        "container": "mp4", "video": {"encoder": "auto_h264", "bits": "8", "rc": "size", "size_mb": 9, "passes": 2,
                                      "speed": 6},
        "audio": {"codec": "aac", "kbps": 96, "channels": "stereo", "tracks": "first"}, "subs": "none",
        "fmt": _f(name="Discord 10 MB", container="mp4", vcodecs=["h264"], acodecs=["aac"],
                  profile={"h264": "high"}, width=1280, height=720, size_limit=True, max_audio=1, a_maxch=2)}),
    ("web", "discord50", {
        "container": "mp4", "video": {"encoder": "auto_h264", "bits": "8", "rc": "size", "size_mb": 47, "passes": 2,
                                      "speed": 6},
        "audio": {"codec": "aac", "kbps": 128, "channels": "stereo", "tracks": "first"}, "subs": "none",
        "fmt": _f(name="Discord 50 MB", container="mp4", vcodecs=["h264"], acodecs=["aac"],
                  profile={"h264": "high"}, width=1920, height=1080, size_limit=True, max_audio=1, a_maxch=2)}),
    ("disc", "bluray", {
        "container": "m2ts", "video": {"encoder": "auto_h264", "bits": "8", "rc": "bitrate", "bitrate": 25000},
        "audio": {"codec": "ac3", "kbps": 640}, "subs": "none",
        "venc": {"x264": {"params": "bluray-compat=1:slices=4:nal-hrd=vbr:b-pyramid=strict"}},
        "fmt": _f(name="Blu-ray (H.264)", container="m2ts", vcodecs=["h264"], acodecs=["ac3", "eac3"],
                  profile={"h264": "high"}, level={"h264": "4.1"}, gop_max=1, gop_unit="sec", gop_closed=True,
                  vbv_buf=30000, vbv_max=40000, width=1920, height=1080, size_limit=True, a_maxch=6)}),
    ("disc", "dvd_pal", {
        "container": "mpg", "video": {"encoder": "mpeg2", "rc": "bitrate", "bitrate": 5000, "width": 720,
                                      "height": 576, "fit": "stretch", "upscale": True, "fps": 25},
        "audio": {"codec": "ac3", "kbps": 448, "rate": 48000}, "subs": "none",
        "filters": {"aspect": {"on": True, "dar": "16:9"}, "fps": {"mode": "cfr"}},
        "fmt": _f(name="DVD PAL", container="mpg", vcodecs=["mpeg2video"], acodecs=["ac3", "mp2"],
                  profile={"mpeg2video": "main"}, level={"mpeg2video": "main"}, gop_max=15, gop_closed=True,
                  vbv_buf=1835, vbv_max=9000, a_maxch=6, max_audio=8)}),
    ("disc", "dvd_ntsc", {
        "container": "mpg", "video": {"encoder": "mpeg2", "rc": "bitrate", "bitrate": 5000, "width": 720,
                                      "height": 480, "fit": "stretch", "upscale": True, "fps": 29.97},
        "audio": {"codec": "ac3", "kbps": 448, "rate": 48000}, "subs": "none",
        "filters": {"aspect": {"on": True, "dar": "16:9"}, "fps": {"mode": "cfr"}},
        "fmt": _f(name="DVD NTSC", container="mpg", vcodecs=["mpeg2video"], acodecs=["ac3", "mp2"],
                  profile={"mpeg2video": "main"}, level={"mpeg2video": "main"}, gop_max=18, gop_closed=True,
                  vbv_buf=1835, vbv_max=9000, a_maxch=6, max_audio=8)}),
]
DEVICE = {key: st for _g, key, st in DEVICES}
DEVICE_GROUP = {key: g for g, key, _st in DEVICES}

# the presets of the format that come with the program: every device's, and a few plain ones
BUILTIN: list[dict] = [
    _f(name="MP4 — H.264 High@4.1", container="mp4", vcodecs=["h264"], acodecs=["aac", "ac3", "eac3", "mp3"],
       profile={"h264": "high"}, level={"h264": "4.1"}),
    _f(name="MKV — HEVC Main10@5.1", container="mkv", vcodecs=["hevc"], profile={"hevc": "main10"},
       level={"hevc": "5.1"}),
    _f(name="AVI — Xvid (MPEG-4 ASP)", container="avi", vcodecs=["mpeg4"], acodecs=["mp3", "ac3"],
       profile={"mpeg4": "asp"}, level={"mpeg4": "5"}, max_audio=1),
    _f(name="AVI — MPEG-4 Simple (DivX Home)", container="avi", vcodecs=["mpeg4"], acodecs=["mp3"],
       profile={"mpeg4": "simple"}, level={"mpeg4": "3"}, gop_max=250, max_audio=1, a_maxch=2,
       width=720, height=576, size_limit=True),
    _f(name="MP4 — fragment / stream (GOP 2 s)", container="mp4", vcodecs=["h264", "hevc", "av1"],
       acodecs=["aac", "opus"], gop_max=2, gop_unit="sec", gop_closed=True),
] + [st["fmt"] for _g, _k, st in DEVICES]


# ── the library of presets of the format: built in + the user's (formats.json) ─
def user_formats() -> dict:
    from . import config
    got = config.read("formats.json") or {}
    return {str(k): clean(v) | {"name": str(k)} for k, v in got.items() if isinstance(v, dict)} \
        if isinstance(got, dict) else {}


def save_user_formats(d: dict) -> None:
    from . import config
    config.write("formats.json", {k: clean(v) | {"name": k} for k, v in d.items()})


def builtin_names() -> list[str]:
    return [f["name"] for f in BUILTIN]


def find(name: str, user: dict | None = None) -> dict | None:
    if user and name in user:
        return clean(user[name])
    return next((clean(f) for f in BUILTIN if f["name"] == name), None)


def export_file(path: str, formats: list[dict]) -> None:
    """One preset or several, as JSON: {"artcore_formats": 1, "formats": [...]}."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"artcore_formats": 1, "formats": [clean(f) for f in formats]}, fh, ensure_ascii=False, indent=2)


def import_file(path: str) -> list[dict]:
    """The presets in a file written by export_file (or a bare preset / list of them). ValueError: not ours."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("formats"), list):
        items = data["formats"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict) and ("vcodecs" in data or "container" in data):
        items = [data]
    else:
        raise ValueError("not a preset of the format")
    out = []
    for it in items:
        if isinstance(it, dict):
            f = clean(it)
            if not f["name"]:
                f["name"] = os.path.splitext(os.path.basename(path))[0]
            out.append(f)
    if not out:
        raise ValueError("not a preset of the format")
    return out


def unique_name(name: str, taken) -> str:
    taken = set(taken)
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"
