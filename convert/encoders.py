"""The video / audio encoders, what ffmpeg arguments they take, and which of the hardware ones really work on
this PC (NVIDIA NVENC, AMD AMF, Intel QSV — checked by encoding a few black frames).

The speed slider is 1 (fastest) … 7 (best quality) for every encoder; the quality value is CQ / CRF / QP /
ICQ of that encoder (lower = better, bigger file)."""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass

from . import ff


@dataclass(frozen=True)
class VEnc:
    id: str
    ffname: str                 # -c:v
    family: str                 # h264 | hevc | av1
    vendor: str                 # nvidia | amd | intel | cpu
    label: str
    bits: tuple = (8,)          # bit depths it can write
    q_default: int = 23
    q_range: tuple = (1, 51)
    hw: bool = True
    rc: bool = True             # takes bit rate / file size (ProRes, GIF: quality only)


VIDEO = [
    VEnc("nvenc_h264", "h264_nvenc", "h264", "nvidia", "H.264 — NVIDIA NVENC", (8,), 23),
    VEnc("nvenc_hevc", "hevc_nvenc", "hevc", "nvidia", "HEVC — NVIDIA NVENC", (8, 10), 26),
    VEnc("nvenc_av1", "av1_nvenc", "av1", "nvidia", "AV1 — NVIDIA NVENC", (8, 10), 32, (1, 63)),
    VEnc("amf_h264", "h264_amf", "h264", "amd", "H.264 — AMD AMF", (8,), 23),
    VEnc("amf_hevc", "hevc_amf", "hevc", "amd", "HEVC — AMD AMF", (8, 10), 25),
    VEnc("amf_av1", "av1_amf", "av1", "amd", "AV1 — AMD AMF", (8, 10), 30, (1, 255)),
    VEnc("qsv_h264", "h264_qsv", "h264", "intel", "H.264 — Intel Quick Sync", (8,), 23),
    VEnc("qsv_hevc", "hevc_qsv", "hevc", "intel", "HEVC — Intel Quick Sync", (8, 10), 25),
    VEnc("qsv_av1", "av1_qsv", "av1", "intel", "AV1 — Intel Quick Sync", (8, 10), 30),
    VEnc("x264", "libx264", "h264", "cpu", "H.264 — x264 (процессор)", (8,), 20, (0, 51), False),
    VEnc("x265", "libx265", "hevc", "cpu", "HEVC — x265 (процессор)", (8, 10), 22, (0, 51), False),
    VEnc("svtav1", "libsvtav1", "av1", "cpu", "AV1 — SVT-AV1 (процессор)", (8, 10), 30, (1, 63), False),
    VEnc("vp9", "libvpx-vp9", "vp9", "cpu", "VP9 — libvpx (процессор)", (8, 10), 32, (0, 63), False),
    VEnc("xvid", "libxvid", "mpeg4", "cpu", "MPEG-4 ASP — Xvid (процессор)", (8,), 4, (1, 31), False),
    VEnc("mpeg4", "mpeg4", "mpeg4", "cpu", "MPEG-4 ASP — FFmpeg (процессор)", (8,), 4, (1, 31), False),
    VEnc("mpeg2", "mpeg2video", "mpeg2video", "cpu", "MPEG-2 (процессор)", (8,), 4, (1, 31), False),
    VEnc("prores", "prores_ks", "prores", "cpu", "ProRes 422 (процессор)", (10,), 4, (1, 4), False, False),
    VEnc("gif", "gif", "gif", "cpu", "GIF", (8,), 0, (0, 0), False, False),
]
BY_ID = {e.id: e for e in VIDEO}
FAMILIES = ("hevc", "h264", "av1", "mpeg4")          # these have an «auto» entry (more than one encoder)
FAMILY_LABEL = {"h264": "H.264", "hevc": "HEVC", "av1": "AV1", "vp9": "VP9", "mpeg4": "MPEG-4 ASP",
                "mpeg2video": "MPEG-2", "prores": "ProRes", "gif": "GIF"}
CPU_LABEL = {"x264": "x264", "x265": "x265", "svtav1": "SVT-AV1", "vp9": "libvpx", "xvid": "Xvid", "mpeg4": "FFmpeg",
             "mpeg2": "FFmpeg", "prores": "FFmpeg", "gif": "FFmpeg"}
PRORES_PROFILES = ("Proxy", "LT", "422", "HQ")
# "auto_<family>": the best encoder of that family this PC has — GPU first, then the CPU one
AUTO_ORDER = ("nvidia", "amd", "intel", "cpu")
VENDOR_LABEL = {"nvidia": "NVIDIA NVENC", "amd": "AMD AMF", "intel": "Intel Quick Sync", "cpu": "CPU"}


TWO_PASS = ("x264", "x265", "vp9", "xvid", "mpeg4", "mpeg2", "svtav1")     # 1.4: encoders with -pass 1 / 2
ONE_RUN_2PASS = ("nvidia",)                                                  # both passes inside one run


def is_auto(enc_id: str) -> bool:
    return enc_id.startswith("auto_")


class Available:
    """Which encoders work here. `ok` maps encoder id → True / False; CPU ones are True when ffmpeg has them."""

    def __init__(self, ok: dict | None = None):
        self.ok: dict[str, bool] = dict(ok or {})
        self.audio: set[str] = set()
        self.errors: dict[str, str] = {}            # encoder id → the telling ffmpeg line of its failed test
        self.reasons: dict[str, str] = {}           # encoder id → why_* key (no_amd, driver_old, …)
        self.svt_2pass = False                      # 1.4: this ffmpeg's libsvtav1 takes -pass 1 / 2
        self.filters: set[str] = set()              # 1.4: the ffmpeg filters of this build (libvmaf …)
        self.h264_10 = False                        # 1.5: NVENC writes 10 bit H.264 here (RTX 50, FFmpeg 7.1+)

    def two_pass(self, e: "VEnc | None") -> bool:
        """1.4: the encoder has a first pass of its own here (x264, x265, VP9, MPEG-4 / 2, SVT-AV1 where the
        build has it); NVENC does both passes in one run (-multipass fullres) and needs none."""
        if e is None:
            return False
        if e.id == "svtav1":
            return self.svt_2pass
        return e.id in TWO_PASS

    def works(self, enc_id: str) -> bool:
        if is_auto(enc_id):
            return self.resolve(enc_id) is not None
        return bool(self.ok.get(enc_id))

    def resolve(self, enc_id: str) -> VEnc | None:
        """auto_hevc → the first working HEVC encoder in AUTO_ORDER; a fixed id → itself if it works."""
        if not is_auto(enc_id):
            e = BY_ID.get(enc_id)
            return e if e is not None and self.ok.get(e.id) else None
        fam = enc_id[5:]
        for v in AUTO_ORDER:
            for e in VIDEO:
                if e.family == fam and e.vendor == v and self.ok.get(e.id):
                    return e
        return None

    def gpu(self) -> str:
        """The vendor of the first working hardware encoder ("" = none)."""
        for v in AUTO_ORDER[:-1]:
            if any(self.ok.get(e.id) for e in VIDEO if e.vendor == v):
                return v
        return ""


def ffmpeg_encoders() -> set[str]:
    exe = ff.ffmpeg()
    if not exe:
        return set()
    try:
        out = ff.run([exe, "-hide_banner", "-encoders"], 20).stdout
    except Exception:
        return set()
    names = set()
    for line in out.splitlines():
        bits = line.split()
        if len(bits) >= 2 and len(bits[0]) == 6 and bits[0][0] in "VAS.":
            names.add(bits[1])
    return names


# ffmpeg's words → why a hardware encoder can't run here (the why_* strings of i18n)
WHY = (
    ("nvidia", ("nvcuda.dll", "cannot load libcuda", "cuinit", "no nvidia", "cuda_error_no_device"), "no_nvidia"),
    ("nvidia", ("driver does not support the required nvenc api", "minimum required nvidia driver"), "driver_old"),
    ("nvidia", ("no capable devices found", "unsupported device", "codec not supported", "not supported by the device"),
     "gpu_old"),
    ("amd", ("amfrt64.dll", "amfrt32.dll", "dll amfrt", "failed to open amf", "amf runtime", "cannot load amf"), "no_amd"),
    ("amd", ("codec not supported", "encoder not supported", "amf_not_supported", "no device"), "gpu_old"),
    ("intel", ("mfx session", "mfx_err_unsupported", "unsupported", "no device available", "device creation failed",
               "failed to create a vaapi", "libvpl", "mfxinit", "-9 "), "no_intel"),
)
GENERIC_FFMPEG = ("nothing was written", "conversion failed", "error while opening encoder", "exiting with exit code",
                  "could not open encoder before eof", "error sending frames", "terminating thread", "task finished")


def why(e: VEnc, stderr: str) -> tuple[str, str]:
    """(why_* key, the most telling line) for a failed hardware test."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    low_all = "\n".join(lines).lower()
    key = ""
    for vendor, words, k in WHY:
        if vendor == e.vendor and any(w in low_all for w in words):
            key = k
            break
    useful = [ln for ln in lines if not any(g in ln.lower() for g in GENERIC_FFMPEG)]
    line = next((ln for ln in useful if any(x in ln.lower() for x in ("cannot", "failed", "error", "not supported",
                                                                       "unsupported", "no capable", "dll"))),
                useful[-1] if useful else (lines[-1] if lines else ""))
    import re
    line = re.sub(r"^\[[^\]]*@ [0-9a-fx]+\]\s*", "", line)          # «[h264_amf @ 0000…] » prefix
    return key or {"nvidia": "no_nvidia", "amd": "no_amd", "intel": "no_intel"}.get(e.vendor, "unknown"), line


def _try(e: VEnc) -> tuple[bool, str, str]:
    """Encode 5 black frames with this encoder and its normal arguments (10-bit too when it has it)."""
    exe = ff.ffmpeg()
    args = [exe, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=0.2",
            "-frames:v", "5", "-an", "-c:v", e.ffname]
    args += video_args(e, {"rc": "quality", "quality": e.q_default, "speed": 4}, max(e.bits))
    args += ["-f", "null", "-"]
    try:
        r = ff.run(args, 30)
        if r.returncode == 0:
            return True, "", ""
        key, line = why(e, r.stderr)
        return False, key, line or f"exit {r.returncode}"
    except Exception as ex:
        return False, "unknown", str(ex)


def _svt_2pass() -> bool:
    """Does this ffmpeg's libsvtav1 write first-pass statistics (FFmpeg 8.1+)? Three frames tell."""
    import os
    import tempfile
    exe = ff.ffmpeg()
    d = tempfile.mkdtemp(prefix="acsvt")
    log = os.path.join(d, "p")
    try:
        r = ff.run([exe, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=0.2",
                    "-frames:v", "3", "-c:v", "libsvtav1", "-b:v", "200k", "-pass", "1", "-passlogfile", log,
                    "-an", "-f", "null", "-"], 60)
        return r.returncode == 0 and os.path.isfile(log + "-0.log") and os.path.getsize(log + "-0.log") > 0
    except Exception:
        return False
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _try_h264_10() -> bool:
    """1.5: does NVENC write High 10 H.264 here (an RTX 50 card and a new enough ffmpeg)? Five frames tell."""
    exe = ff.ffmpeg()
    try:
        r = ff.run([exe, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=0.2",
                    "-frames:v", "5", "-an", "-c:v", "h264_nvenc", "-pix_fmt", "p010le", "-profile:v", "high10",
                    "-f", "null", "-"], 30)
        return r.returncode == 0
    except Exception:
        return False


def enable_h264_10(on: bool = True) -> None:
    """NVENC H.264 gets 10 bit among its depths (after the test above said the card has it)."""
    old = BY_ID["nvenc_h264"]
    new = VEnc(old.id, old.ffname, old.family, old.vendor, old.label, (8, 10) if on else (8,), old.q_default,
               old.q_range, old.hw, old.rc)
    VIDEO[VIDEO.index(old)] = new
    BY_ID[new.id] = new


def ffmpeg_filters() -> set[str]:
    exe = ff.ffmpeg()
    if not exe:
        return set()
    try:
        out = ff.run([exe, "-hide_banner", "-filters"], 20).stdout
    except Exception:
        return set()
    return {ln.split()[1] for ln in out.splitlines() if len(ln.split()) > 2 and ln.startswith(" ")}


def detect(cache: dict | None = None) -> Available:
    """CPU encoders: present in this ffmpeg. Hardware ones: actually encode (in parallel, ~1 s in all).
    `cache` = {ffmpeg version: {id: ok}} from the config; reused when the ffmpeg build is the same."""
    names = ffmpeg_encoders()
    av = Available()
    av.audio = names
    ver = ff.version()
    cached = (cache or {}).get(ver) if ver else None
    for e in VIDEO:
        if not e.hw:
            av.ok[e.id] = e.ffname in names
    hw = [e for e in VIDEO if e.hw and e.ffname in names]
    # the cache: {id: [ok, why key, line]} (1.0.0 kept a bare bool — those are tested again)
    if isinstance(cached, dict) and all(isinstance(cached.get(e.id), list) for e in hw):
        for e in hw:
            ok, key, line = (list(cached[e.id]) + ["", ""])[:3]
            av.ok[e.id] = bool(ok)
            if not ok:
                av.reasons[e.id], av.errors[e.id] = key, line
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for e, (ok, key, line) in zip(hw, pool.map(_try, hw)):
                av.ok[e.id] = ok
                if not ok:
                    av.reasons[e.id], av.errors[e.id] = key, line[:300]
    for e in VIDEO:
        av.ok.setdefault(e.id, False)
    av.filters = ffmpeg_filters()
    if av.ok.get("nvenc_h264"):
        if isinstance(cached, dict) and "_h264_10" in cached:
            av.h264_10 = bool(cached["_h264_10"])
        else:
            av.h264_10 = _try_h264_10()
    enable_h264_10(av.h264_10)
    if av.ok.get("svtav1"):
        if isinstance(cached, dict) and "_svt2pass" in cached:
            av.svt_2pass = bool(cached["_svt2pass"])
        else:
            av.svt_2pass = _svt_2pass()
    return av


def cache_entry(av: Available) -> dict:
    ver = ff.version()
    if not ver:
        return {}
    d = {e.id: [av.ok.get(e.id, False), av.reasons.get(e.id, ""), av.errors.get(e.id, "")] for e in VIDEO if e.hw}
    d["_svt2pass"] = bool(av.svt_2pass)
    d["_h264_10"] = bool(av.h264_10)
    return {ver: d}


# ── arguments ────────────────────────────────────────────────
NV_PRESETS = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")
AMF_QUALITY = ("speed", "speed", "balanced", "balanced", "balanced", "quality", "quality")
QSV_PRESETS = ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
X_PRESETS = ("superfast", "veryfast", "faster", "fast", "medium", "slow", "slower")
SVT_PRESETS = ("12", "10", "8", "7", "6", "5", "4")
VPX_CPU = ("5", "4", "3", "2", "2", "1", "0")
XVID_ME = ("1", "2", "3", "4", "5", "6", "6")


def pix_fmt(e: VEnc, bits: int, chroma: str = "420") -> str:
    """chroma 422 / 444 (1.3, the Pixel format row) only for the encoders that write it (filters.CHROMAS)."""
    if e.id == "prores":
        return "yuv444p10le" if chroma == "444" else "yuv422p10le"
    from .filters import CHROMAS
    if chroma in ("422", "444") and chroma in CHROMAS.get(e.id, ()):
        if e.hw:
            return "yuv444p16le" if bits >= 10 and 10 in e.bits else "yuv444p"
        return f"yuv{chroma}p10le" if bits >= 10 and 10 in e.bits else f"yuv{chroma}p"
    if bits >= 10 and 10 in e.bits:
        return "p010le" if e.hw else "yuv420p10le"
    return "nv12" if e.vendor in ("amd", "intel") else "yuv420p"


def video_args(e: VEnc, v: dict, bits: int, duration: float = 0.0) -> list[str]:
    """-c:v is given by the caller; this is the rest: speed preset, rate control, pixel format, profile.
    v = {"rc": quality | bitrate | size, "quality", "bitrate" (kbit/s), "size_mb", "speed" 1..7}."""
    sp = max(1, min(7, int(v.get("speed", 5)))) - 1
    rc = v.get("rc", "quality")
    q = int(v.get("quality", e.q_default))
    q = max(e.q_range[0], min(e.q_range[1], q))
    kbps = int(v.get("bitrate", 0) or 0)
    if rc == "size" and duration > 0:
        kbps = target_kbps(float(v.get("size_mb", 0) or 0), duration, int(v.get("audio_kbps", 0) or 0))
    out: list[str] = []
    if e.id == "gif":                                      # the palette filters do it all
        return out
    if e.id == "prores":                                   # the «quality» is the profile: 1 Proxy … 4 HQ
        chroma = str(v.get("chroma", "422"))
        prof = "4" if chroma == "444" else str(q - 1)              # 4444 for 4:4:4
        return ["-profile:v", prof, "-vendor", "apl0", "-pix_fmt", pix_fmt(e, bits, chroma)]
    if e.vendor == "nvidia":
        out += ["-preset", NV_PRESETS[sp], "-tune", "hq"]
        if rc == "quality" or kbps <= 0:
            out += ["-rc", "vbr", "-cq", str(q), "-b:v", "0"]
        else:
            out += ["-rc", "vbr", "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k", "-bufsize", f"{kbps * 2}k"]
        out += ["-spatial-aq", "1", "-rc-lookahead", "20"]
        if int(v.get("passes", 1) or 1) == 2 and rc != "quality" and kbps > 0:
            out += ["-multipass", "fullres"]               # 1.4: two passes inside the one run
        elif sp >= 5:
            out += ["-multipass", "qres"]
    elif e.vendor == "amd":
        out += ["-quality", AMF_QUALITY[sp]]
        if rc == "quality" or kbps <= 0:
            out += ["-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q)]
            if e.family == "h264":                       # HEVC / AV1 AMF have no qp_b (no B-frame QP of their own)
                out += ["-qp_b", str(q)]
        else:
            out += ["-rc", "vbr_peak", "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k"]
    elif e.vendor == "intel":
        out += ["-preset", QSV_PRESETS[sp]]
        if rc == "quality" or kbps <= 0:
            out += ["-global_quality", str(q)]
        else:
            out += ["-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k"]
    elif e.id == "svtav1":
        out += ["-preset", SVT_PRESETS[sp]]
        out += ["-crf", str(q)] if rc == "quality" or kbps <= 0 else ["-b:v", f"{kbps}k"]
    elif e.id == "vp9":
        out += ["-deadline", "good", "-cpu-used", VPX_CPU[sp], "-row-mt", "1"]
        out += ["-crf", str(q), "-b:v", "0"] if rc == "quality" or kbps <= 0 else \
            ["-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k", "-bufsize", f"{kbps * 2}k"]
        if bits >= 10 and str(v.get("chroma", "420")) == "420":
            out += ["-profile:v", "2"]                       # 4:2:2 / 4:4:4: libvpx picks profile 1 / 3
    elif e.family in ("mpeg4", "mpeg2video"):            # Xvid / FFmpeg MPEG-4 / MPEG-2: q = qscale 1..31
        if e.id == "xvid":
            out += ["-me_quality", XVID_ME[sp]]
        out += ["-q:v", str(q)] if rc == "quality" or kbps <= 0 else \
            ["-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k", "-bufsize", f"{max(kbps * 2, 1835)}k"]
        if e.family == "mpeg2video":
            out += ["-bf", "2"]
    else:                                                  # x264 / x265
        out += ["-preset", X_PRESETS[sp]]
        if rc == "quality" or kbps <= 0:
            out += ["-crf", str(q)]
        else:
            out += ["-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k", "-bufsize", f"{kbps * 2}k"]
        if e.id == "x265":
            out += ["-x265-params", "log-level=error"]
    chroma = str(v.get("chroma", "420"))
    out += ["-pix_fmt", pix_fmt(e, bits, chroma)]
    if e.family not in ("h264", "hevc") or not out[-1].startswith(("yuv420", "nv12", "p010")):
        return out                                       # 4:2:2 / 4:4:4: the encoder picks the profile
    if bits >= 10 and 10 in e.bits and e.family == "hevc":
        out += ["-profile:v", "main10"]
    elif e.family == "h264":
        out += ["-profile:v", "high10" if bits >= 10 and 10 in e.bits else "high"]
    return out


def thread_args(e: VEnc, n: int) -> list[str]:
    """1.4, the CPU limit: the encoder's own thread count (x265: its thread pool, given in -x265-params)."""
    if n <= 0 or e.hw or e.id in ("x265", "gif"):
        return []
    return ["-threads", str(n)]


def pass_args(e: VEnc, n: int, prefix: str) -> list[str]:
    """1.4: -pass 1 / 2 and where the statistics go (x265 writes them by its own -x265-params)."""
    if e.id == "x265":
        return []
    return ["-pass", str(n), "-passlogfile", prefix]


def x265_path(p: str) -> str:
    """A path as a value inside -x265-params (C:\\a → C\\:/a; the «:» would split the list)."""
    return p.replace("\\", "/").replace(":", "\\:")


def target_kbps(size_mb: float, duration: float, audio_kbps: int) -> int:
    """The video bit rate for a file of size_mb (MiB) — minus the audio and ~1.5 % for the container."""
    if size_mb <= 0 or duration <= 0:
        return 0
    total = size_mb * 1024 * 1024 * 8 * 0.985 / duration / 1000
    return max(100, int(total - audio_kbps))


# ── audio ────────────────────────────────────────────────────
@dataclass(frozen=True)
class AEnc:
    id: str
    ffname: str
    label: str
    max_ch: int
    kbps: tuple                 # default kbit/s for (stereo, 5.1); (0, 0) = lossless


AUDIO = [
    AEnc("aac", "aac", "AAC", 8, (192, 448)),
    AEnc("ac3", "ac3", "AC-3 (Dolby Digital)", 6, (224, 640)),
    AEnc("eac3", "eac3", "E-AC-3 (Dolby Digital Plus)", 8, (224, 640)),
    AEnc("opus", "libopus", "Opus", 8, (160, 384)),
    AEnc("flac", "flac", "FLAC (без потерь)", 8, (0, 0)),
    AEnc("mp3", "libmp3lame", "MP3", 2, (256, 256)),
    AEnc("vorbis", "libvorbis", "Vorbis", 8, (192, 448)),
    AEnc("mp2", "mp2", "MP2", 2, (224, 224)),
    AEnc("alac", "alac", "ALAC (без потерь)", 8, (0, 0)),
    AEnc("pcm16", "pcm_s16le", "PCM 16 bit (WAV)", 8, (0, 0)),
    AEnc("pcm24", "pcm_s24le", "PCM 24 bit (WAV)", 8, (0, 0)),
]
A_BY_ID = {a.id: a for a in AUDIO}
# our audio id → the codec name ffprobe gives the result (to know what a container takes)
A_CODEC = {"aac": "aac", "ac3": "ac3", "eac3": "eac3", "opus": "opus", "flac": "flac", "mp3": "mp3",
           "vorbis": "vorbis", "mp2": "mp2", "alac": "alac", "pcm16": "pcm_s16le", "pcm24": "pcm_s24le"}


@dataclass(frozen=True)
class Container:
    id: str
    label: str
    fmt: str                        # -f
    video: frozenset | None         # codec names it takes (None = any); empty = audio only
    audio: frozenset | None
    a_default: str                  # what «auto» encodes to when the source audio doesn't fit
    subs: str                       # any | mov_text | webvtt | none
    v_default: str = "auto_hevc"    # the encoder to switch to when the chosen one doesn't fit
    one_audio: bool = False         # a single audio track (MP3, FLAC, WAV …)
    chapters: bool = True

    @property
    def audio_only(self) -> bool:
        return self.video is not None and not self.video

    @property
    def ext(self) -> str:
        return self.id

    def takes_video(self, codec: str) -> bool:
        if codec == "gif" or self.id == "gif":            # GIF only in .gif, and .gif only GIF
            return codec == "gif" and self.id == "gif"
        return self.video is None or codec in self.video

    def takes_audio(self, codec: str) -> bool:
        return self.audio is None or codec in self.audio


_fs = frozenset
_PCM = _fs({"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_u8"})
CONTAINERS = [
    Container("mkv", "MKV (Matroska)", "matroska", None, None, "aac", "any"),
    Container("mp4", "MP4", "mp4", _fs({"h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video"}),
              _fs({"aac", "ac3", "eac3", "mp3", "opus", "flac", "alac"}), "aac", "mov_text"),
    Container("mov", "MOV (QuickTime)", "mov", _fs({"h264", "hevc", "prores", "mpeg4", "mpeg2video", "mjpeg", "av1"}),
              _fs({"aac", "ac3", "eac3", "alac", "mp3"}) | _PCM, "aac", "mov_text"),
    Container("webm", "WebM", "webm", _fs({"vp9", "av1", "vp8"}), _fs({"opus", "vorbis"}), "opus", "webvtt", "vp9"),
    Container("avi", "AVI", "avi", _fs({"mpeg4", "h264", "mpeg2video", "mjpeg", "msmpeg4v3"}),
              _fs({"mp3", "ac3", "mp2", "pcm_s16le"}), "mp3", "none", "auto_mpeg4", chapters=False),
    Container("ts", "TS (MPEG-TS)", "mpegts", _fs({"h264", "hevc", "mpeg2video", "av1"}),
              _fs({"aac", "ac3", "eac3", "mp2", "mp3", "opus", "dts", "truehd"}), "aac", "none", chapters=False),
    Container("m2ts", "M2TS (Blu-ray)", "mpegts", _fs({"h264", "hevc", "mpeg2video"}),
              _fs({"ac3", "eac3", "dts", "truehd", "aac", "mp2"}), "ac3", "none", chapters=False),
    Container("mpg", "MPG (MPEG-PS / DVD)", "dvd", _fs({"mpeg2video"}), _fs({"ac3", "mp2"}), "ac3", "none", "mpeg2",
              chapters=False),                                       # 1.5: DVD-Video streams (VOB)
    Container("gif", "GIF", "gif", _fs({"gif"}), _fs(), "", "none", "gif", chapters=False),
    # audio only
    Container("mka", "MKA (Matroska Audio)", "matroska", _fs(), None, "aac", "none"),
    Container("mp3", "MP3", "mp3", _fs(), _fs({"mp3"}), "mp3", "none", one_audio=True),
    Container("m4a", "M4A (AAC / ALAC)", "mp4", _fs(), _fs({"aac", "alac"}), "aac", "none", one_audio=True),
    Container("flac", "FLAC", "flac", _fs(), _fs({"flac"}), "flac", "none", one_audio=True, chapters=False),
    Container("opus", "Opus", "opus", _fs(), _fs({"opus"}), "opus", "none", one_audio=True, chapters=False),
    Container("ogg", "OGG (Vorbis)", "ogg", _fs(), _fs({"vorbis", "opus", "flac"}), "vorbis", "none", one_audio=True,
              chapters=False),
    Container("wav", "WAV", "wav", _fs(), _PCM, "pcm16", "none", one_audio=True, chapters=False),
    Container("ac3", "AC-3 (Dolby Digital)", "ac3", _fs(), _fs({"ac3"}), "ac3", "none", one_audio=True, chapters=False),
]
CONT = {c.id: c for c in CONTAINERS}


def container(cid: str) -> Container:
    return CONT.get(cid) or CONT["mkv"]


# 1.0 names, used by tests and the Tracks tab
CONTAINER_AUDIO = {c.id: (None if c.audio is None else set(c.audio)) for c in CONTAINERS}
CONTAINER_VIDEO = {c.id: (None if c.video is None else set(c.video)) for c in CONTAINERS}
TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


# ── self-check without the hardware ──────────────────────────
GENERIC = {"b:v", "maxrate", "bufsize", "pix_fmt", "global_quality", "profile:v", "x265-params", "q:v", "bf",
           "threads", "forced-idr"}


def check_options(e: VEnc) -> list[str]:
    """Every option (and named value) our arguments use must be in `ffmpeg -h encoder=<name>` — this works
    with no graphics card, so CI catches an NVENC / AMF / QSV argument a new ffmpeg no longer takes."""
    exe = ff.ffmpeg()
    try:
        helptext = ff.run([exe, "-hide_banner", "-h", f"encoder={e.ffname}"], 20).stdout
    except Exception as ex:
        return [f"{e.ffname}: {ex}"]
    if "is not recognized" in helptext or not helptext.strip():
        return [f"{e.ffname}: not in this build"]
    lines = helptext.splitlines()
    opts: dict[str, set[str]] = {}
    cur = None
    for ln in lines:
        st = ln.strip()
        if st.startswith("-"):
            cur = st.split()[0][1:]
            opts[cur] = set()
        elif cur and st and ln.startswith("     ") and not st.startswith("-"):
            opts[cur].add(st.split()[0])                   # a named value of the option above
    pix = next((ln.split(":", 1)[1].split() for ln in lines if "Supported pixel formats" in ln), [])
    problems = []
    for rc, passes in (("quality", 1), ("bitrate", 1), ("bitrate", 2)):
        for speed in range(1, 8):
            for bits in e.bits:
                a = video_args(e, {"rc": rc, "quality": e.q_default, "bitrate": 5000, "speed": speed,
                                   "passes": passes}, bits, 60)
                for i in range(0, len(a) - 1, 2):
                    k, v = a[i].lstrip("-"), a[i + 1]
                    if k == "pix_fmt":
                        if pix and v not in pix:
                            problems.append(f"{e.ffname}: pix_fmt {v} not in {pix}")
                        continue
                    if k in GENERIC:
                        continue
                    if k not in opts:
                        problems.append(f"{e.ffname}: no option -{k}")
                    elif opts[k] and not v.lstrip("-").isdigit() and v not in opts[k]:
                        problems.append(f"{e.ffname}: -{k} has no value {v} ({', '.join(sorted(opts[k]))})")
    return sorted(set(problems))
