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
]
BY_ID = {e.id: e for e in VIDEO}
FAMILIES = ("hevc", "h264", "av1")
FAMILY_LABEL = {"h264": "H.264", "hevc": "HEVC", "av1": "AV1"}
# "auto_<family>": the best encoder of that family this PC has — GPU first, then the CPU one
AUTO_ORDER = ("nvidia", "amd", "intel", "cpu")
VENDOR_LABEL = {"nvidia": "NVIDIA NVENC", "amd": "AMD AMF", "intel": "Intel Quick Sync", "cpu": "CPU"}


def is_auto(enc_id: str) -> bool:
    return enc_id.startswith("auto_")


class Available:
    """Which encoders work here. `ok` maps encoder id → True / False; CPU ones are True when ffmpeg has them."""

    def __init__(self, ok: dict | None = None):
        self.ok: dict[str, bool] = dict(ok or {})
        self.audio: set[str] = set()
        self.errors: dict[str, str] = {}            # encoder id → why its test failed (last ffmpeg line)

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


def _try(e: VEnc) -> tuple[bool, str]:
    """Encode 5 black frames with this encoder and its normal arguments (10-bit too when it has it)."""
    exe = ff.ffmpeg()
    args = [exe, "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=0.2",
            "-frames:v", "5", "-an", "-c:v", e.ffname]
    args += video_args(e, {"rc": "quality", "quality": e.q_default, "speed": 4}, max(e.bits))
    args += ["-f", "null", "-"]
    try:
        r = ff.run(args, 30)
        lines = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
        return r.returncode == 0, (lines[-1] if lines else f"exit {r.returncode}") if r.returncode else ""
    except Exception as ex:
        return False, str(ex)


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
    if isinstance(cached, dict) and all(e.id in cached for e in hw):
        for e in hw:
            av.ok[e.id] = bool(cached[e.id])
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for e, (ok, err) in zip(hw, pool.map(_try, hw)):
                av.ok[e.id] = ok
                if err:
                    av.errors[e.id] = err[:300]
    for e in VIDEO:
        av.ok.setdefault(e.id, False)
    return av


def cache_entry(av: Available) -> dict:
    ver = ff.version()
    return {ver: {e.id: av.ok.get(e.id, False) for e in VIDEO if e.hw}} if ver else {}


# ── arguments ────────────────────────────────────────────────
NV_PRESETS = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")
AMF_QUALITY = ("speed", "speed", "balanced", "balanced", "balanced", "quality", "quality")
QSV_PRESETS = ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow")
X_PRESETS = ("superfast", "veryfast", "faster", "fast", "medium", "slow", "slower")
SVT_PRESETS = ("12", "10", "8", "7", "6", "5", "4")


def pix_fmt(e: VEnc, bits: int) -> str:
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
    if e.vendor == "nvidia":
        out += ["-preset", NV_PRESETS[sp], "-tune", "hq"]
        if rc == "quality" or kbps <= 0:
            out += ["-rc", "vbr", "-cq", str(q), "-b:v", "0"]
        else:
            out += ["-rc", "vbr", "-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k", "-bufsize", f"{kbps * 2}k"]
        out += ["-spatial-aq", "1", "-rc-lookahead", "20"]
        if sp >= 5:
            out += ["-multipass", "qres"]
    elif e.vendor == "amd":
        out += ["-quality", AMF_QUALITY[sp]]
        if rc == "quality" or kbps <= 0:
            out += ["-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q)]
            if e.family != "av1":
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
    else:                                                  # x264 / x265
        out += ["-preset", X_PRESETS[sp]]
        if rc == "quality" or kbps <= 0:
            out += ["-crf", str(q)]
        else:
            out += ["-b:v", f"{kbps}k", "-maxrate", f"{kbps * 3 // 2}k", "-bufsize", f"{kbps * 2}k"]
        if e.id == "x265":
            out += ["-x265-params", "log-level=error"]
    out += ["-pix_fmt", pix_fmt(e, bits)]
    if bits >= 10 and 10 in e.bits and e.family == "hevc":
        out += ["-profile:v", "main10"]
    elif e.family == "h264":
        out += ["-profile:v", "high"]
    return out


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
]
A_BY_ID = {a.id: a for a in AUDIO}
# what each container takes as it is (otherwise "auto" re-encodes to AAC)
CONTAINER_AUDIO = {
    "mkv": None,                                            # everything
    "mp4": {"aac", "ac3", "eac3", "mp3", "opus", "flac", "alac"},
}
CONTAINER_VIDEO = {
    "mkv": None,
    "mp4": {"h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video"},
}
CONTAINER_SUBS = {
    "mkv": None,
    "mp4": {"mov_text"},                                    # text subtitles are converted to mov_text
}
TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


# ── self-check without the hardware ──────────────────────────
GENERIC = {"b:v", "maxrate", "bufsize", "pix_fmt", "global_quality", "profile:v", "x265-params"}


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
    for rc in ("quality", "bitrate"):
        for speed in range(1, 8):
            for bits in e.bits:
                a = video_args(e, {"rc": rc, "quality": e.q_default, "bitrate": 5000, "speed": speed}, bits, 60)
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
