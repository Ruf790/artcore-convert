"""1.5: the advanced settings of an encoder (⚙ at the codec on the Streams tab): tune, profile, level, reference
frames, B-frames, AQ, psy-rd, deblock, own parameters for x264 / x265; preset p1–p7, tune, multipass, B-frames as
reference, look-ahead, AQ, split encode for NVENC; quality, pre-analysis, look-ahead for AMF / QSV; preset, film
grain, tune, overlays for SVT-AV1; deadline, cpu-used, tile columns, row-mt for VP9.

They are kept per encoder in the task settings: settings["venc"] = {encoder id: {field: value}} ("" / missing =
the encoder decides). Only what this encoder has in this ffmpeg build is shown and used: every field names the
option it needs, and `ffmpeg -h encoder=<name>` tells which options (and which named values) the build has."""
from __future__ import annotations

import functools
import shlex
from dataclasses import dataclass

from . import encoders as E
from . import ff
from .formats import LEVELS


@dataclass(frozen=True)
class Field:
    key: str                    # in settings["venc"][enc id]
    kind: str                   # choice | int | float | bool | text
    opt: str                    # the ffmpeg option it needs ("" = a general one every encoder has)
    how: str = "opt"            # opt: -opt value · param: key=value in -<opt> · raw: the text into -<opt> · extra
    pkey: str = ""              # the key inside the params option
    values: tuple = ()          # choice: the values ("" first = default); int / float: (min, max, step)
    arg: str = ""               # the argument written (-arg value) when it differs from -opt


X264_TUNES = ("", "film", "animation", "grain", "stillimage", "psnr", "ssim", "fastdecode", "zerolatency")
X265_TUNES = ("", "psnr", "ssim", "grain", "zerolatency", "fastdecode", "animation")

X264 = (
    Field("tune", "choice", "tune", values=X264_TUNES),
    Field("profile", "choice", "profile", values=("", "baseline", "main", "high", "high10", "high422", "high444"),
          arg="profile:v"),
    Field("level", "choice", "level", values=("",) + LEVELS["h264"]),
    Field("ref", "int", "", values=(1, 16, 1), arg="refs"),
    Field("bframes", "int", "", values=(0, 16, 1), arg="bf"),
    Field("aq_mode", "choice", "aq-mode", values=("", "*", "0", "1", "2", "3")),
    Field("aq_strength", "float", "aq-strength", values=(0.0, 3.0, 0.1)),
    Field("psy_rd", "text", "psy-rd"),
    Field("deblock", "text", "deblock"),
    Field("params", "text", "x264-params", how="raw"),
)
X265 = (
    Field("tune", "choice", "tune", values=X265_TUNES),
    Field("profile", "choice", "profile", values=("", "main", "main10", "main12", "main422-10", "main444-8",
                                                  "main444-10", "mainstillpicture"), arg="profile:v"),
    Field("level", "choice", "x265-params", how="param", pkey="level-idc", values=("",) + LEVELS["hevc"]),
    Field("ref", "int", "x265-params", how="param", pkey="ref", values=(1, 16, 1)),
    Field("bframes", "int", "x265-params", how="param", pkey="bframes", values=(0, 16, 1)),
    Field("aq_mode", "choice", "x265-params", how="param", pkey="aq-mode", values=("", "0", "1", "2", "3", "4")),
    Field("aq_strength", "float", "x265-params", how="param", pkey="aq-strength", values=(0.0, 3.0, 0.1)),
    Field("psy_rd", "float", "x265-params", how="param", pkey="psy-rd", values=(0.0, 5.0, 0.1)),
    Field("deblock", "text", "x265-params", how="param", pkey="deblock"),
    Field("params", "text", "x265-params", how="raw"),
)
NVENC = (
    Field("preset", "choice", "preset", values=("", "p1", "p2", "p3", "p4", "p5", "p6", "p7")),
    Field("tune", "choice", "tune", values=("", "hq", "uhq", "ll", "ull", "lossless")),
    Field("multipass", "choice", "multipass", values=("", "disabled", "qres", "fullres")),
    Field("b_ref_mode", "choice", "b_ref_mode", values=("", "disabled", "each", "middle")),
    Field("lookahead", "int", "rc-lookahead", values=(0, 250, 1)),
    Field("spatial_aq", "bool", "spatial-aq"),
    Field("temporal_aq", "bool", "temporal-aq"),
    Field("aq_strength", "int", "aq-strength", values=(1, 15, 1)),
    Field("split_encode", "choice", "split_encode_mode", values=("", "*")),          # its values: as the build names them
    Field("extra", "text", "", how="extra"),
)
AMF = (
    Field("quality", "choice", "quality", values=("", "speed", "balanced", "quality", "high_quality")),
    Field("preanalysis", "bool", "preanalysis"),
    Field("lookahead", "int", "pa_lookahead_buffer_depth", values=(0, 41, 1)),
    Field("vbaq", "bool", "vbaq"),
    Field("extra", "text", "", how="extra"),
)
QSV = (
    Field("preset", "choice", "preset", values=("", "veryfast", "faster", "fast", "medium", "slow", "slower",
                                                "veryslow")),
    Field("look_ahead", "bool", "look_ahead"),
    Field("lookahead", "int", "look_ahead_depth", values=(0, 100, 1)),
    Field("extbrc", "bool", "extbrc"),
    Field("extra", "text", "", how="extra"),
)
SVT = (
    Field("preset", "int", "preset", values=(-1, 13, 1)),
    Field("film_grain", "int", "svtav1-params", how="param", pkey="film-grain", values=(0, 50, 1)),
    Field("tune", "choice", "svtav1-params", how="param", pkey="tune", values=("", "0", "1", "2")),
    Field("overlays", "bool", "svtav1-params", how="param", pkey="enable-overlays"),
    Field("params", "text", "svtav1-params", how="raw"),
)
VP9 = (
    Field("deadline", "choice", "deadline", values=("", "good", "best", "realtime")),
    Field("cpu_used", "int", "cpu-used", values=(-8, 8, 1)),
    Field("tile_columns", "int", "tile-columns", values=(0, 6, 1)),
    Field("row_mt", "bool", "row-mt"),
    Field("extra", "text", "", how="extra"),
)
OTHER = (Field("extra", "text", "", how="extra"),)


def fields(e: E.VEnc) -> tuple[Field, ...]:
    if e.id == "x264":
        return X264
    if e.id == "x265":
        return X265
    if e.id == "svtav1":
        return SVT
    if e.id == "vp9":
        return VP9
    if e.vendor == "nvidia":
        return NVENC
    if e.vendor == "amd":
        return AMF
    if e.vendor == "intel":
        return QSV
    if e.id == "gif":
        return ()
    return OTHER


# ── what this ffmpeg build has ───────────────────────────────
def parse_help(text: str) -> tuple[dict[str, set], list[str]]:
    """`ffmpeg -h encoder=…` → ({option: {named values}}, [pixel formats])."""
    lines = text.splitlines()
    opts: dict[str, set] = {}
    cur = None
    for ln in lines:
        st = ln.strip()
        if st.startswith("-"):
            cur = st.split()[0][1:]
            opts[cur] = set()
        elif cur and st and ln.startswith("     ") and not st.startswith("-"):
            opts[cur].add(st.split()[0])
    pix = next((ln.split(":", 1)[1].split() for ln in lines if "Supported pixel formats" in ln), [])
    return opts, pix


@functools.lru_cache(maxsize=64)
def _caps(exe: str, ffname: str) -> tuple:
    try:
        text = ff.run([exe, "-hide_banner", "-h", f"encoder={ffname}"], 20).stdout
    except Exception:
        return {}, []
    if "is not recognized" in text or not text.strip():
        return {}, []
    opts, pix = parse_help(text)
    return {k: frozenset(v) for k, v in opts.items()}, pix


def caps(e: E.VEnc | str) -> dict:
    """{option: frozenset of named values} of the encoder in this ffmpeg ({} when ffmpeg / it is missing)."""
    exe = ff.ffmpeg()
    if not exe:
        return {}
    return dict(_caps(exe, e.ffname if isinstance(e, E.VEnc) else e)[0])


def offered(e: E.VEnc, caps_: dict | None = None) -> list[Field]:
    """The fields this encoder has here (a choice keeps only the values the build names)."""
    caps_ = caps(e) if caps_ is None else caps_
    out = []
    for f in fields(e):
        if f.opt and f.opt not in caps_:
            continue
        if f.kind == "choice" and f.how == "opt":
            named = caps_.get(f.opt) or frozenset()
            if "*" in f.values:                           # the build's own names, or ours when it lists none
                vals = ("",) + tuple(sorted(named)) if named else tuple(v for v in f.values if v != "*")
            elif named:
                vals = tuple(v for v in f.values if v == "" or v in named)
            else:
                vals = f.values
            if len(vals) <= 1:
                continue
            f = Field(f.key, f.kind, f.opt, f.how, f.pkey, vals, f.arg)
        out.append(f)
    return out


def clean(e: E.VEnc, adv: dict | None) -> dict:
    """Only known keys with sane values ("" / None dropped)."""
    out = {}
    for f in fields(e):
        v = (adv or {}).get(f.key)
        if v is None or v == "":
            continue
        try:
            if f.kind == "bool":
                out[f.key] = bool(v)
            elif f.kind == "int":
                lo, hi = f.values[0], f.values[1]
                out[f.key] = max(lo, min(hi, int(float(v))))
            elif f.kind == "float":
                lo, hi = f.values[0], f.values[1]
                out[f.key] = round(max(lo, min(hi, float(v))), 3)
            elif f.kind == "choice":
                if str(v) in f.values or "*" in f.values:          # (a build's own name: checked when used)
                    out[f.key] = str(v)
            else:
                s = str(v).strip()
                if s:
                    out[f.key] = s
        except (TypeError, ValueError):
            pass
    return out


def of(st: dict, e: E.VEnc) -> dict:
    venc = st.get("venc") if isinstance(st.get("venc"), dict) else {}
    return clean(e, venc.get(e.id))


def args(e: E.VEnc, adv: dict, caps_: dict | None = None) -> tuple[list[str], dict]:
    """(-option value … laid over the encoder's own arguments, {params option: [key=value …]})."""
    caps_ = caps(e) if caps_ is None else caps_
    adv = clean(e, adv)
    out: list[str] = []
    params: dict = {}
    for f in offered(e, caps_):
        if f.key not in adv:
            continue
        v = adv[f.key]
        if f.kind == "choice" and str(v) not in f.values:        # a name this build does not have
            continue
        if f.how == "extra":
            try:
                out += shlex.split(str(v), posix=True)
            except ValueError:
                pass
            continue
        if f.how == "raw":
            params.setdefault("-" + f.opt, []).extend(p for p in str(v).split(":") if p.strip())
            continue
        text = ("1" if v else "0") if f.kind == "bool" else f"{v:g}" if f.kind == "float" else str(v)
        if f.how == "param":
            params.setdefault("-" + f.opt, []).append(f"{f.pkey}={text}")
        else:
            out += ["-" + (f.arg or f.opt), text]
    return out, params


def summary(e: E.VEnc, adv: dict) -> str:
    """«tune film · ref 4 · bf 3» — a short line of what is set (the Streams tab shows it under the codec)."""
    adv = clean(e, adv)
    return " · ".join(f"{k.replace('_', '-')} {('on' if v else 'off') if isinstance(v, bool) else v}"
                      for k, v in adv.items())
