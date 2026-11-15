"""1.3: the video and sound filters of the Streams tab (as the filter rows of XviD4PSP 8): their settings,
the ffmpeg filter chain they make, how they change the frame size, and the automatic crop.

The settings live in settings["filters"] = {name: {"on": bool, …}}. Three rows keep using the 1.0 keys, so old
queues and presets stay as they were: Interlace switches with video.deint (auto | on | off), Framerate takes its
rate from video.fps, Pixel format its bit depth from video.bits.

The video chain, in this order:
  inverse telecine / deinterlace → delogo → crop → HDR → SDR → rotation → stabilisation → noise reduction →
  scale (the resolution) → pad → aspect → colour → sharpen → grain → frame rate → overlay → pixel format /
  colour space (→ the GIF palette)
Sound, per track: delay → volume → (normalisation) → sample rate; the bit depth is the encoder's sample format.
The pieces of a trim go through the same chain after the concat filter."""
from __future__ import annotations

import concurrent.futures
import copy
import os
import re

FILTER_DEFAULTS = {
    "crop":      {"on": False, "mode": "auto", "l": 0, "t": 0, "r": 0, "b": 0},
    "interlace": {"method": "bwdif", "parity": "auto", "double": False, "ivtc": False},
    "hdr":       {"mode": "auto", "engine": "zscale", "algo": "hable", "peak": 100},
    "rotate":    {"on": False, "angle": 0, "hflip": False, "vflip": False},
    "deshake":   {"on": False, "method": "deshake", "strength": 5},
    "denoise":   {"on": False, "method": "hqdn3d", "strength": 4},
    "grain":     {"on": False, "strength": 8},
    "delogo":    {"on": False, "x": 0, "y": 0, "w": 0, "h": 0},
    "sharpen":   {"on": False, "amount": 0.8},
    "color":     {"on": False, "brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0,
                  "temp": 6500, "r": 0.0, "g": 0.0, "b": 0.0},
    "pad":       {"on": False, "l": 0, "t": 0, "r": 0, "b": 0, "color": "black"},
    "aspect":    {"on": False, "dar": "16:9"},
    "overlay":   {"on": False, "path": "", "pos": "br", "margin": 20, "opacity": 80, "size": 0},
    "pixfmt":    {"on": False, "chroma": "420", "matrix": "auto", "range": "auto"},
    "fps":       {"mode": "auto", "interp": False, "dedup": False},
}
VIDEO_ROWS = ("crop", "interlace", "hdr", "rotate", "deshake", "denoise", "grain", "delogo", "sharpen", "color",
              "pad", "aspect", "overlay", "pixfmt", "fps")
TONEMAP_ALGOS = ("hable", "mobius", "reinhard", "clip", "linear", "gamma", "bt.2390", "spline")
ZSCALE_ALGOS = ("hable", "mobius", "reinhard", "clip", "linear", "gamma")
DARS = ("16:9", "4:3", "21:9", "2.35:1", "2.39:1", "1.85:1", "1:1", "9:16")
CHROMAS = {                            # which chroma layouts an encoder writes (none listed: 4:2:0 only)
    "x264": ("420", "422", "444"), "x265": ("420", "422", "444"), "vp9": ("420", "422", "444"),
    "mpeg2": ("420", "422"), "prores": ("422", "444"), "nvenc_h264": ("420", "444"), "nvenc_hevc": ("420", "444"),
}
MATRICES = ("auto", "bt709", "bt601", "bt2020")
SAMPLE_RATES = (0, 22050, 32000, 44100, 48000, 88200, 96000)
CODEC_RATES = {"opus": (48000, 24000, 16000, 12000, 8000), "ac3": (48000, 44100, 32000),
               "eac3": (48000, 44100, 32000), "mp3": (48000, 44100, 32000, 24000, 22050),
               "mp2": (48000, 44100, 32000, 24000, 22050), "vorbis": None, "aac": None}
LAYOUTS = ("keep", "mono", "stereo", "5.1", "7.1")
LAYOUT_CH = {"mono": 1, "stereo": 2, "5.1": 6, "7.1": 8}
AUDIO_FILTER_KEYS = ("delay", "gain", "rate", "abits")


def defaults() -> dict:
    return copy.deepcopy(FILTER_DEFAULTS)


def merge(base: dict, over: dict | None) -> dict:
    """The filters of `over` laid over `base`, filter by filter (unknown names / keys are dropped)."""
    for name, vals in (over or {}).items():
        if name in base and isinstance(vals, dict):
            for k, v in vals.items():
                if k in base[name]:
                    base[name][k] = v
    return base


def fset(st: dict, name: str) -> dict:
    f = (st.get("filters") or {}).get(name)
    return f if isinstance(f, dict) else FILTER_DEFAULTS[name]


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _even(x: float) -> int:
    return max(2, int(x / 2 + 0.5) * 2)


def _ratio(s: str) -> float:
    try:
        a, b = str(s).split(":")
        return float(a) / float(b) if float(a) > 0 and float(b) > 0 else 1.0
    except (ValueError, ZeroDivisionError):
        return 1.0


def escape_path(path: str) -> str:
    """A file path as a value of a filter option, quoted for the filter graph (C:\\a → 'C\\:/a')."""
    p = path.replace("\\", "/").replace(":", "\\:")
    return "'" + p.replace("'", "'\\''") + "'"


# ── the rows that are switched on ────────────────────────────
def interlace_on(job) -> bool:
    v = job.settings.get("video", {})
    src = job.info.video if job.info else None
    d = v.get("deint", "auto")
    return d == "on" or (d == "auto" and src is not None and src.interlaced) or bool(fset(job.settings, "interlace").get("ivtc"))


def hdr_keeps(job, enc, bits: int) -> bool:
    """The HDR of the source goes into the result as is (10 bit HEVC / AV1 / VP9)."""
    src = job.info.video if job.info else None
    return src is not None and src.hdr in ("HDR10", "HLG") and bits >= 10 and enc is not None and \
        enc.family in ("hevc", "av1", "vp9")


def tonemap_on(job, enc, bits: int) -> bool:
    """HDR → SDR: switched on, or «auto» and the HDR can't be kept (8 bit, H.264, …)."""
    src = job.info.video if job.info else None
    if src is None or enc is None or src.hdr not in ("HDR10", "HLG", "Dolby Vision"):
        return False
    mode = fset(job.settings, "hdr").get("mode", "auto")
    if mode == "off":
        return False
    if mode == "on":
        return True
    if src.hdr == "Dolby Vision" and (src.color.get("color_transfer") not in ("smpte2084", "arib-std-b67")):
        return False                                     # profile 5: no base layer zscale can read
    return not hdr_keeps(job, enc, bits)


def fps_on(job) -> bool:
    v = job.settings.get("video", {})
    f = fset(job.settings, "fps")
    return bool(_num(v.get("fps", 0))) or f.get("mode", "auto") != "auto" or bool(f.get("interp")) or bool(f.get("dedup"))


def is_on(job, name: str, enc=None, bits: int = 8) -> bool:
    if name == "interlace":
        return interlace_on(job)
    if name == "hdr":
        return tonemap_on(job, enc, bits)
    if name == "fps":
        return fps_on(job)
    if name == "pixfmt":
        return bool(fset(job.settings, "pixfmt").get("on"))
    f = fset(job.settings, name)
    if not f.get("on"):
        return False
    if name == "crop":
        return any(crop_values(job))
    if name == "delogo":
        return _int(f.get("w")) > 0 and _int(f.get("h")) > 0
    if name == "overlay":
        return bool(f.get("path")) and os.path.isfile(f["path"])
    if name == "rotate":
        return _int(f.get("angle")) % 360 != 0 or bool(f.get("hflip")) or bool(f.get("vflip"))
    if name == "pad":
        return any(_int(f.get(k)) > 0 for k in ("l", "t", "r", "b"))
    if name == "color":
        return color_parts(f) != []
    return True


# ── crop ─────────────────────────────────────────────────────
def crop_values(job) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) in pixels of the source: typed in, or what the detection found."""
    f = fset(job.settings, "crop")
    if not f.get("on"):
        return 0, 0, 0, 0
    if f.get("mode", "auto") == "auto":
        found = getattr(job, "crop_found", None)
        if not found:
            return 0, 0, 0, 0
        vals = found
    else:
        vals = [f.get(k, 0) for k in ("l", "t", "r", "b")]
    l, t, r, b = (max(0, _int(x)) for x in vals)
    src = job.info.video if job.info else None
    if src is not None and src.width and src.height:
        if l + r > src.width - 16:
            l = r = 0
        if t + b > src.height - 16:
            t = b = 0
    # even, so 4:2:0 frames stay whole
    return l // 2 * 2, t // 2 * 2, r // 2 * 2, b // 2 * 2


CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def detect_crop(ffmpeg: str, src_path: str, duration: float, width: int, height: int, points: int = 8,
                run=None) -> tuple[int, int, int, int] | None:
    """cropdetect at several moments of the film (5 % … 95 %); the bars every moment has. None: nothing found
    (no bars, or the file could not be read)."""
    if run is None:
        from .ff import run
    if not width or not height:
        return None
    dur = max(0.0, duration)
    moments = [dur * (0.05 + 0.9 * i / max(1, points - 1)) for i in range(points)] if dur > 6 else [0.0]

    def one(at: float):
        args = [ffmpeg, "-hide_banner", "-nostdin", "-ss", f"{at:.2f}", "-i", src_path, "-map", "0:v:0", "-an",
                "-sn", "-dn", "-frames:v", "24", "-vf", "cropdetect=limit=0.094:round=2:reset=0", "-f", "null", "-"]
        try:
            r = run(args, 60)
        except Exception:
            return None
        found = CROP_RE.findall(r.stderr or "")
        return tuple(int(x) for x in found[-1]) if found else None

    with concurrent.futures.ThreadPoolExecutor(4) as ex:
        res = [x for x in ex.map(one, moments) if x]
    if not res:
        return None
    # the picture of every moment fits in: the smallest bars
    x0 = min(x for _w, _h, x, _y in res)
    y0 = min(y for _w, _h, _x, y in res)
    x1 = max(x + w for w, _h, x, _y in res)
    y1 = max(y + h for _w, h, _x, y in res)
    l, t, r, b = x0, y0, max(0, width - x1), max(0, height - y1)
    l, t, r, b = (v if v >= 4 else 0 for v in (l, t, r, b))          # a pixel or two of noise is no bar
    out = (l // 2 * 2, t // 2 * 2, r // 2 * 2, b // 2 * 2)
    return out if any(out) else (0, 0, 0, 0)


# ── geometry: the frame through the chain ─────────────────────
def pre_scale(job) -> tuple[int, int, float]:
    """The frame entering the scale filter (after crop and rotation): width, height, pixel aspect."""
    src = job.info.video if job.info else None
    if src is None or not src.width:
        return 0, 0, 1.0
    w, h = src.width, src.height
    sar = _ratio(src.sar) if src.sar and src.sar not in ("0:1", "1:1") else 1.0
    if is_on(job, "crop"):
        l, t, r, b = crop_values(job)
        w, h = w - l - r, h - t - b
    if is_on(job, "rotate") and _int(fset(job.settings, "rotate").get("angle")) % 180 == 90:
        w, h, sar = h, w, 1.0 / sar
    return w, h, sar


def post_pad(job, w: int, h: int) -> tuple[int, int]:
    if not is_on(job, "pad"):
        return w, h
    f = fset(job.settings, "pad")
    return w + _int(f.get("l")) // 2 * 2 + _int(f.get("r")) // 2 * 2, h + _int(f.get("t")) // 2 * 2 + _int(f.get("b")) // 2 * 2


# ── the pieces of the chain ──────────────────────────────────
def interlace_filters(job) -> list[str]:
    if not interlace_on(job):
        return []
    f = fset(job.settings, "interlace")
    if f.get("ivtc"):                                   # telecine 3:2 → the 24 frames back
        return ["fieldmatch=order=auto:combmatch=full", "yadif=deint=interlaced", "decimate"]
    par = f.get("parity", "auto")
    par = par if par in ("auto", "tff", "bff") else "auto"
    meth = f.get("method", "bwdif")
    if meth == "yadif":
        return [f"yadif=mode={'send_field' if f.get('double') else 'send_frame'}:parity={par}:deint=all"]
    return [f"bwdif=mode={'send_field' if f.get('double') else 'send_frame'}:parity={par}:deint=all"]


def delogo_filter(job) -> list[str]:
    if not is_on(job, "delogo"):
        return []
    f = fset(job.settings, "delogo")
    src = job.info.video if job.info else None
    x, y, w, h = (_int(f.get(k)) for k in ("x", "y", "w", "h"))
    if src is not None and src.width:                   # the band delogo needs: one pixel inside the frame
        x, y = max(1, min(x, src.width - 4)), max(1, min(y, src.height - 4))
        w, h = max(2, min(w, src.width - x - 1)), max(2, min(h, src.height - y - 1))
    return [f"delogo=x={x}:y={y}:w={w}:h={h}"]


def crop_filter(job) -> list[str]:
    if not is_on(job, "crop"):
        return []
    src = job.info.video
    l, t, r, b = crop_values(job)
    return [f"crop={src.width - l - r}:{src.height - t - b}:{l}:{t}"]


TRC_IN = {"smpte2084": "smpte2084", "arib-std-b67": "arib-std-b67"}


def tonemap_filters(job, enc, bits: int) -> list[str]:
    if not tonemap_on(job, enc, bits):
        return []
    f = fset(job.settings, "hdr")
    src = job.info.video
    algo = f.get("algo", "hable")
    fmt = "yuv420p10le" if bits >= 10 else "yuv420p"
    if f.get("engine") == "libplacebo":                 # on the graphics card (Vulkan)
        algo = algo if algo in TONEMAP_ALGOS else "hable"
        return [f"libplacebo=tonemapping={algo}:colorspace=bt709:color_primaries=bt709:color_trc=bt709:"
                f"range=tv:format={fmt}"]
    algo = algo if algo in ZSCALE_ALGOS else "hable"
    tin = TRC_IN.get(src.color.get("color_transfer", ""), "smpte2084" if src.hdr != "HLG" else "arib-std-b67")
    npl = max(50, min(1000, _int(f.get("peak"), 100)))
    return [f"zscale=tin={tin}:min=bt2020nc:pin=bt2020:rin=tv:t=linear:npl={npl}", "format=gbrpf32le",
            "zscale=p=bt709", f"tonemap=tonemap={algo}:desat=0", "zscale=t=bt709:m=bt709:r=tv", f"format={fmt}"]


def rotate_filters(job) -> list[str]:
    if not is_on(job, "rotate"):
        return []
    f = fset(job.settings, "rotate")
    a = _int(f.get("angle")) % 360
    out = {90: ["transpose=clock"], 180: ["hflip", "vflip"], 270: ["transpose=cclock"]}.get(a, [])
    if f.get("hflip"):
        out.append("hflip")
    if f.get("vflip"):
        out.append("vflip")
    return out


def deshake_filters(job, transforms: str = "", detect: bool = False) -> list[str]:
    if not is_on(job, "deshake"):
        return []
    f = fset(job.settings, "deshake")
    s = max(1, min(10, _int(f.get("strength"), 5)))
    if f.get("method") == "vidstab":
        if detect:
            return [f"vidstabdetect=shakiness={s}:accuracy=15:result={escape_path(transforms)}"]
        return [f"vidstabtransform=input={escape_path(transforms)}:smoothing={s * 3}:optzoom=1:interpol=bicubic"]
    r = min(64, 16 * (1 + s // 3))                      # deshake takes multiples of 16 only
    return [f"deshake=rx={r}:ry={r}:edge=mirror"]


def denoise_filters(job) -> list[str]:
    if not is_on(job, "denoise"):
        return []
    f = fset(job.settings, "denoise")
    s = max(1, min(10, _num(f.get("strength"), 4)))
    if f.get("method") == "nlmeans":
        return [f"nlmeans=s={s * 1.2:g}:p=7:r=9"]
    return [f"hqdn3d={s:g}:{s * 0.75:g}:{s * 1.5:g}:{s * 1.125:g}"]


def pad_filter(job) -> list[str]:
    if not is_on(job, "pad"):
        return []
    f = fset(job.settings, "pad")
    l, t, r, b = (_int(f.get(k)) // 2 * 2 for k in ("l", "t", "r", "b"))
    color = re.sub(r"[^#0-9A-Za-z@.]", "", str(f.get("color") or "black")) or "black"
    return [f"pad=iw+{l + r}:ih+{t + b}:{l}:{t}:color={color}"]


def aspect_filter(job) -> list[str]:
    if not is_on(job, "aspect"):
        return []
    dar = str(fset(job.settings, "aspect").get("dar", "16:9"))
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:/x]\s*(\d+(?:\.\d+)?)\s*", dar)
    if not m:
        return []
    return [f"setdar={float(m.group(1)):g}/{float(m.group(2)):g}"]


def color_parts(f: dict) -> list[str]:
    out = []
    br, co, sa, ga = (_num(f.get("brightness")), _num(f.get("contrast"), 1), _num(f.get("saturation"), 1),
                      _num(f.get("gamma"), 1))
    if abs(br) > 0.001 or abs(co - 1) > 0.001 or abs(sa - 1) > 0.001 or abs(ga - 1) > 0.001:
        out.append(f"eq=brightness={br:g}:contrast={co:g}:saturation={sa:g}:gamma={ga:g}")
    temp = _int(f.get("temp"), 6500)
    if temp != 6500:
        out.append(f"colortemperature=temperature={max(1000, min(40000, temp))}")
    r, g, b = (max(-1.0, min(1.0, _num(f.get(k)))) for k in ("r", "g", "b"))
    if abs(r) > 0.001 or abs(g) > 0.001 or abs(b) > 0.001:
        # the whole range (ffmpeg's midtones alone hardly touch a middle grey), the midtones most
        out.append(f"colorbalance=rs={r / 2:g}:gs={g / 2:g}:bs={b / 2:g}:rm={r:g}:gm={g:g}:bm={b:g}:"
                   f"rh={r / 2:g}:gh={g / 2:g}:bh={b / 2:g}")
    return out


def color_filters(job) -> list[str]:
    return color_parts(fset(job.settings, "color")) if is_on(job, "color") else []


def sharpen_filter(job) -> list[str]:
    if not is_on(job, "sharpen"):
        return []
    a = max(-2.0, min(3.0, _num(fset(job.settings, "sharpen").get("amount"), 0.8)))
    return [f"unsharp=5:5:{a:g}:5:5:0"]


def grain_filter(job) -> list[str]:
    if not is_on(job, "grain"):
        return []
    s = max(1, min(60, _int(fset(job.settings, "grain").get("strength"), 8)))
    return [f"noise=c0s={s}:c0f=t+u"]


def fps_filters(job, enc=None) -> list[str]:
    v = job.settings.get("video", {})
    src = job.info.video if job.info else None
    f = fset(job.settings, "fps")
    gif = enc is not None and enc.id == "gif"
    fps = _num(v.get("fps", 0))
    if gif and not fps:
        fps = 15.0
    out = []
    if f.get("dedup"):
        out.append("mpdecimate")
    if fps and f.get("interp") and not gif:
        out.append(f"minterpolate=fps={fps:g}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
    elif fps and src is not None and (f.get("mode") == "cfr" or not src.fps or fps < src.fps - 0.01):
        out.append(f"fps={ntsc(fps)}")
    return out


def ntsc(fps: float) -> str:
    """29.97 → 30000/1001 (and 23.976, 59.94 …): the exact NTSC rate, not 2997/100."""
    n = round(fps * 1.001)
    if n and abs(fps - n * 1000 / 1001) < 0.006 and abs(fps - n) > 0.01:
        return f"{n * 1000}/1001"
    return f"{fps:g}"


def fps_args(job) -> list[str]:
    mode = fset(job.settings, "fps").get("mode", "auto")
    if fset(job.settings, "fps").get("dedup") and mode == "auto":
        mode = "vfr"
    return ["-fps_mode", mode] if mode in ("cfr", "vfr") else []


OV_POS = {"tl": ("{m}", "{m}"), "tr": ("W-w-{m}", "{m}"), "bl": ("{m}", "H-h-{m}"), "br": ("W-w-{m}", "H-h-{m}"),
          "c": ("(W-w)/2", "(H-h)/2")}


def overlay_parts(job, frame_w: int) -> tuple[str, str]:
    """(the chain of the picture input, the overlay filter)."""
    f = fset(job.settings, "overlay")
    m = max(0, _int(f.get("margin"), 20))
    x, y = (s.format(m=m) for s in OV_POS.get(f.get("pos", "br"), OV_POS["br"]))
    chain = []
    size = max(0, min(100, _int(f.get("size"))))
    if size and frame_w:
        chain.append(f"scale={_even(frame_w * size / 100)}:-2")
    chain += ["format=rgba", f"colorchannelmixer=aa={max(0, min(100, _int(f.get('opacity'), 80))) / 100:g}"]
    return ",".join(chain), f"overlay=x={x}:y={y}:format=auto:eof_action=repeat"


# pixel format / colour space
SPACE_OF = {"bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470bg", "bt2020nc": "bt2020",
            "bt2020c": "bt2020", "smpte240m": "smpte240m"}
TAGS_OF = {"bt709": ("bt709", "bt709", "bt709"), "bt601": ("smpte170m", "smpte170m", "smpte170m"),
           "bt601-625": ("bt470bg", "bt470bg", "smpte170m"), "bt2020": ("bt2020nc", "bt2020", "bt2020-10")}
CS_ALL = {"bt709": "bt709", "bt601": "bt601-6-525", "bt601-625": "bt601-6-625", "bt2020": "bt2020"}


def chroma(job, enc) -> str:
    """The chroma layout the result gets (the encoder decides what it can)."""
    p = fset(job.settings, "pixfmt")
    want = str(p.get("chroma", "420")) if p.get("on") else "420"
    if enc is None:
        return "420"
    can = CHROMAS.get(enc.id, ("420",))
    from . import formats as FM
    want = FM.hold_chroma(want, FM.caps(job.settings, enc.family)[1])        # 1.5: the profile of the format
    if enc.id == "prores":
        return want if want in can else "422"
    return want if want in can else "420"


def source_matrix(job) -> str:
    src = job.info.video if job.info else None
    if src is None:
        return "bt709"
    sp = src.color.get("color_space", "")
    if sp in ("bt2020nc", "bt2020c"):
        return "bt2020"
    if sp == "bt470bg":
        return "bt601-625"
    if sp == "smpte170m":
        return "bt601"
    if sp == "bt709":
        return "bt709"
    if src.height >= 720:
        return "bt709"
    return "bt601-625" if round(src.fps) in (25, 50) else "bt601"


def target_matrix(job, enc, bits: int) -> tuple[str, str] | None:
    """(matrix, range) the user asked for — None: as the source (or the HDR → SDR result)."""
    p = fset(job.settings, "pixfmt")
    if not p.get("on"):
        return None
    m, r = p.get("matrix", "auto"), p.get("range", "auto")
    if m == "auto" and r == "auto":
        return None
    src_m = "bt709" if tonemap_on(job, enc, bits) else source_matrix(job)
    if m == "bt601":
        m = "bt601-625" if job.info and job.info.video and round(job.info.video.fps) in (25, 50) else "bt601"
    m = src_m if m == "auto" else m
    src_r = "tv"
    if job.info and job.info.video and job.info.video.color.get("color_range") == "pc" and not tonemap_on(job, enc, bits):
        src_r = "pc"
    r = src_r if r == "auto" else r
    return m, r


def colorspace_filter(job, enc, bits: int) -> list[str]:
    tm = target_matrix(job, enc, bits)
    if tm is None:
        return []
    m, r = tm
    src_m = "bt709" if tonemap_on(job, enc, bits) else source_matrix(job)
    src_r = "pc" if (job.info.video.color.get("color_range") == "pc" and not tonemap_on(job, enc, bits)) else "tv"
    if m == src_m and r == src_r:
        return []
    return [f"colorspace=all={CS_ALL[m]}:range={r}:iall={CS_ALL[src_m]}:irange={src_r}:fast=0"]


def color_tags(job, enc, bits: int) -> list[str]:
    """-colorspace / -color_primaries / -color_trc / -color_range of the result, when a filter decided them."""
    tm = target_matrix(job, enc, bits)
    if tm is not None:
        sp, prim, trc = TAGS_OF[tm[0]]
        return ["-colorspace", sp, "-color_primaries", prim, "-color_trc", trc, "-color_range", tm[1]]
    if tonemap_on(job, enc, bits):
        return ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
    return []


# ── sound ────────────────────────────────────────────────────
def audio_on(cfg: dict) -> bool:
    """Any of the sound filters (or a layout / normalisation) asks for the track to be encoded."""
    return bool(_int(cfg.get("delay")) or abs(_num(cfg.get("gain"))) > 0.01 or _int(cfg.get("rate"))
                or _int(cfg.get("abits")) or cfg.get("channels", "keep") != "keep" or cfg.get("normalize"))


def out_rate(codec: str, src_rate: int, cfg: dict) -> int:
    """The sample rate the result gets (0 = as the source); a rate the codec can't take → the nearest it can."""
    want = _int(cfg.get("rate"))
    if not want:
        return 0
    can = CODEC_RATES.get(codec)
    if can:
        want = min(can, key=lambda r: (abs(r - want), -r))
    return 0 if want == src_rate else want


def audio_filters(cfg: dict, codec: str, src_rate: int) -> list[str]:
    out = []
    d = _int(cfg.get("delay"))
    if d > 0:
        out.append(f"adelay=delays={d}:all=1")
    elif d < 0:
        out += [f"atrim=start={-d / 1000:g}", "asetpts=PTS-STARTPTS"]
    g = _num(cfg.get("gain"))
    if abs(g) > 0.01:
        out.append(f"volume={g:g}dB")
    if cfg.get("normalize"):
        out.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    rate = out_rate(codec, src_rate, cfg)
    if rate:
        out.append(f"aresample={rate}")
    elif cfg.get("normalize") and src_rate:
        out.append(f"aresample={src_rate}")                # loudnorm works at 192 kHz
    return out


SAMPLE_FMT = {"flac": {16: "s16", 24: "s32", 32: "s32"}, "alac": {16: "s16p", 24: "s32p", 32: "s32p"}}
PCM_BY_BITS = {16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}


def audio_bits_args(codec: str, ffname: str, cfg: dict, n: int) -> tuple[str, list[str]]:
    """(the encoder, extra arguments) for the chosen bit depth — only lossless codecs have one."""
    b = _int(cfg.get("abits"))
    if not b:
        return ffname, []
    if codec.startswith("pcm"):
        return PCM_BY_BITS.get(b, ffname), []
    fmts = SAMPLE_FMT.get(codec)
    if not fmts or b not in fmts:
        return ffname, []
    extra = [f"-sample_fmt:a:{n}", fmts[b]]
    if b == 24:
        extra += [f"-bits_per_raw_sample:a:{n}", "24"]
    return ffname, extra


def layout_channels(want: str, src_ch: int, max_ch: int) -> int:
    """mono / stereo / 5.1 / 7.1: a mix down to at most that many channels (never up)."""
    ch = src_ch or 2
    if want in LAYOUT_CH:
        ch = min(ch, LAYOUT_CH[want])
    return min(ch, max_ch)


def needed_filters() -> tuple[str, ...]:
    """The ffmpeg filters 1.3 uses (the self-test checks the build has them)."""
    return ("bwdif", "yadif", "fieldmatch", "decimate", "delogo", "crop", "cropdetect", "zscale", "tonemap",
            "libplacebo", "transpose", "hflip", "vflip", "deshake", "vidstabdetect", "vidstabtransform", "hqdn3d",
            "nlmeans", "pad", "setdar", "eq", "colortemperature", "colorbalance", "unsharp", "noise", "fps",
            "minterpolate", "mpdecimate", "overlay", "colorchannelmixer", "colorspace", "adelay", "atrim", "volume",
            "loudnorm", "aresample")
