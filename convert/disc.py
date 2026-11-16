"""1.8: Blu-ray and DVD folders (or discs) as titles. A Blu-ray's playlists (BDMV/PLAYLIST/*.mpls) are read
here: their clips with in / out points, the languages of every audio / subtitle stream and the chapter marks; a
DVD's title sets (VIDEO_TS/VTS_xx_*.VOB) with their length from the IFO and the languages. A chosen title becomes
a concat list (data/discs/*.ffconcat) that the task reads as its source, with the languages and chapters set."""
from __future__ import annotations

import glob
import os
import re
import struct

from . import config

AUDIO_CODINGS = {0x03, 0x04, 0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0xA1, 0xA2}
AUDIO_NAMES = {0x80: "LPCM", 0x81: "AC3", 0x82: "DTS", 0x83: "TrueHD", 0x84: "EAC3", 0x85: "DTS-HD HR",
               0x86: "DTS-HD MA", 0xA1: "EAC3", 0xA2: "DTS-HD", 0x03: "MP1", 0x04: "MP2"}
VIDEO_NAMES = {0x02: "MPEG-2", 0x1B: "H.264", 0xEA: "VC-1", 0x24: "HEVC", 0x20: "MVC"}


def disc_root(path: str) -> tuple[str, str] | None:
    """(kind, root) for a folder that is (or holds) a Blu-ray («bd») or a DVD («dvd»); None otherwise."""
    p = os.path.abspath(path)
    if os.path.isfile(p):
        p = os.path.dirname(p)
    for _ in range(3):
        base = os.path.basename(p.rstrip("\\/")).upper()
        if base == "BDMV" and os.path.isdir(os.path.join(p, "PLAYLIST")):
            return "bd", os.path.dirname(p.rstrip("\\/"))
        if base == "VIDEO_TS":
            return "dvd", os.path.dirname(p.rstrip("\\/"))
        if os.path.isdir(os.path.join(p, "BDMV", "PLAYLIST")):
            return "bd", p
        if os.path.isdir(os.path.join(p, "VIDEO_TS")):
            return "dvd", p
        if base in ("PLAYLIST", "STREAM", "CLIPINF"):
            p = os.path.dirname(p.rstrip("\\/"))
            continue
        break
    return None


def _find(folder: str, name: str) -> str:
    """A file or folder name on a disc may be in any case."""
    want = name.lower()
    try:
        for n in os.listdir(folder):
            if n.lower() == want:
                return os.path.join(folder, n)
    except OSError:
        pass
    return os.path.join(folder, name)


def disc_name(kind: str, root: str) -> str:
    """The disc's title: BDMV/META/DL/bdmt_*.xml, else the folder / volume name."""
    if kind == "bd":
        for x in sorted(glob.glob(os.path.join(_find(_find(_find(root, "BDMV"), "META"), "DL"), "bdmt_*.xml"))):
            try:
                with open(x, encoding="utf-8", errors="replace") as f:
                    m = re.search(r"<di:name>(.*?)</di:name>", f.read(), re.S)
                if m and m.group(1).strip():
                    return re.sub(r'[\\/:*?"<>|]+', " ", m.group(1).strip())
            except OSError:
                pass
    name = os.path.basename(root.rstrip("\\/"))
    if not name:                                          # a drive root: its volume label
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(261)
            if ctypes.windll.kernel32.GetVolumeInformationW(root, buf, 261, None, None, None, None, 0):  # type: ignore
                name = buf.value
        except Exception:                                 # noqa: BLE001
            name = ""
    return name or ("Blu-ray" if kind == "bd" else "DVD")


# ── Blu-ray ───────────────────────────────────────────────────
def _u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def _stn(b: bytes, o: int) -> list[dict]:
    """The stream table of a play item: [{kind, pid, codec, lang}]."""
    n_video, n_audio, n_pg, n_ig, n_sa, n_sv, n_pip = b[o + 4:o + 11]
    p = o + 16
    out = []
    for kind, n in (("video", n_video), ("audio", n_audio), ("subtitle", n_pg), ("ig", n_ig), ("audio2", n_sa),
                    ("video2", n_sv), ("pip", n_pip)):
        for _ in range(n):
            el = b[p]
            entry = b[p + 1:p + 1 + el]
            p += 1 + el
            al = b[p]
            attr = b[p + 1:p + 1 + al]
            p += 1 + al
            t = entry[0] if entry else 0
            pid = {1: 1, 2: 3, 3: 2, 4: 3}.get(t)
            pid = _u16(entry, pid) if pid is not None and len(entry) >= pid + 2 else -1
            coding = attr[0] if attr else 0
            lang = ""
            if coding in AUDIO_CODINGS and len(attr) >= 5:
                lang = attr[2:5].decode("ascii", "replace")
            elif coding in (0x90, 0x91) and len(attr) >= 4:
                lang = attr[1:4].decode("ascii", "replace")
            elif coding == 0x92 and len(attr) >= 5:
                lang = attr[2:5].decode("ascii", "replace")
            out.append({"kind": kind, "pid": pid, "coding": coding, "lang": lang.strip("\x00 ").lower()})
    return out


def read_mpls(path: str) -> dict:
    """A playlist: {clips: [(name, in, out)], duration, streams (of the first clip), marks: [seconds]}."""
    with open(path, "rb") as f:
        b = f.read()
    if b[:4] != b"MPLS":
        raise ValueError("not an MPLS")
    pl, mk = _u32(b, 8), _u32(b, 12)
    n_items = _u16(b, pl + 6)
    p = pl + 10
    clips, streams, starts = [], [], []
    total = 0.0
    for _ in range(n_items):
        ln = _u16(b, p)
        it = p + 2
        name = b[it:it + 5].decode("ascii", "replace")
        multi = (_u16(b, it + 9) >> 4) & 1
        t_in, t_out = _u32(b, it + 12) / 45000.0, _u32(b, it + 16) / 45000.0
        stn = it + 32
        if multi:
            angles = b[it + 32]
            stn += 2 + max(0, angles - 1) * 10
        if not streams:
            try:
                streams = _stn(b, stn)
            except (IndexError, struct.error):
                streams = []
        starts.append(total)
        clips.append((name, t_in, t_out))
        total += max(0.0, t_out - t_in)
        p += 2 + ln
    marks = []
    try:
        n_marks = _u16(b, mk + 4)
        for i in range(n_marks):
            m = mk + 6 + 14 * i
            if b[m + 1] != 1:                              # entry marks: the chapters
                continue
            item, at = _u16(b, m + 2), _u32(b, m + 4) / 45000.0
            if item < len(clips):
                marks.append(round(starts[item] + max(0.0, at - clips[item][1]), 3))
    except (IndexError, struct.error):
        pass
    return {"clips": clips, "duration": total, "streams": streams, "marks": sorted(set(marks))}


def bd_titles(root: str, min_seconds: float = 60.0) -> list[dict]:
    """The playlists worth showing: longer than <min_seconds>, each sequence of clips once, longest first."""
    bdmv = _find(root, "BDMV")
    stream_dir = _find(bdmv, "STREAM")
    out, seen = [], set()
    for f in sorted(glob.glob(os.path.join(_find(bdmv, "PLAYLIST"), "*.[mM][pP][lL][sS]"))):
        try:
            pl = read_mpls(f)
        except (OSError, ValueError, IndexError, struct.error):
            continue
        if pl["duration"] < min_seconds or not pl["clips"]:
            continue
        sig = tuple((c, round(a, 2), round(b, 2)) for c, a, b in pl["clips"])
        if sig in seen:
            continue
        seen.add(sig)
        files = []
        for name, a, b in pl["clips"]:
            m2 = _find(stream_dir, name + ".m2ts")
            if not os.path.isfile(m2):
                alt = _find(stream_dir, name + ".mts")
                m2 = alt if os.path.isfile(alt) else m2
            files.append((m2, a, b))
        size = sum(os.path.getsize(x) for x, _a, _b in {(x, 0, 0) for x, _a, _b in files} if os.path.isfile(x))
        st = pl["streams"]
        out.append({"kind": "bd", "id": os.path.splitext(os.path.basename(f))[0], "duration": pl["duration"],
                    "files": files, "size": size, "marks": pl["marks"],
                    "video": [VIDEO_NAMES.get(s["coding"], "?") for s in st if s["kind"] == "video"],
                    "audio": [(AUDIO_NAMES.get(s["coding"], "?"), s["lang"]) for s in st if s["kind"] == "audio"],
                    "subs": [s["lang"] for s in st if s["kind"] == "subtitle"],
                    "langs": {s["pid"]: s["lang"] for s in st if s["lang"] and s["pid"] >= 0}})
    out.sort(key=lambda x: -x["duration"])
    if out:
        out[0]["main"] = True
    return out


# ── DVD ───────────────────────────────────────────────────────
def _bcd(x: int) -> int:
    return (x >> 4) * 10 + (x & 0x0F)


def read_ifo(path: str) -> dict:
    """A title set's IFO: the longest program chain's length, the audio and subpicture languages."""
    with open(path, "rb") as f:
        b = f.read()
    if b[:12] != b"DVDVIDEO-VTS":
        raise ValueError("not a VTS IFO")
    n_audio = min(8, _u16(b, 0x202))
    audio = []
    for i in range(n_audio):
        a = b[0x204 + 8 * i:0x204 + 8 * i + 8]
        fmt = {0: "AC3", 2: "MPEG", 3: "MPEG", 4: "LPCM", 6: "DTS"}.get(a[0] >> 5, "?")
        lang = a[2:4].decode("ascii", "replace").strip("\x00 ").lower() if a[0] & 0x0C == 0x04 else ""
        audio.append((fmt, lang))
    n_sub = min(32, _u16(b, 0x254))
    subs = []
    for i in range(n_sub):
        s = b[0x256 + 6 * i:0x256 + 6 * i + 6]
        subs.append(s[2:4].decode("ascii", "replace").strip("\x00 ").lower() if s[0] & 0x03 == 0x01 else "")
    best = 0.0
    try:
        pgcit = _u32(b, 0xCC) * 2048
        n = _u16(b, pgcit)
        for i in range(n):
            off = pgcit + _u32(b, pgcit + 8 + 8 * i + 4)
            h, m, s, fr = b[off + 4:off + 8]
            fps = 30.0 if (fr >> 6) == 3 else 25.0
            sec = _bcd(h) * 3600 + _bcd(m) * 60 + _bcd(s) + _bcd(fr & 0x3F) / fps
            best = max(best, sec)
    except (IndexError, struct.error):
        pass
    return {"duration": best, "audio": audio, "subs": subs}


LANG2 = {"ru": "rus", "en": "eng", "uk": "ukr", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "ja": "jpn",
         "zh": "chi", "ko": "kor", "pl": "pol", "cs": "cze", "bg": "bul", "pt": "por", "tr": "tur", "he": "heb",
         "nl": "nld", "sv": "swe", "fi": "fin", "da": "dan", "no": "nor", "hu": "hun", "el": "gre", "ar": "ara"}


def dvd_titles(root: str, min_seconds: float = 60.0) -> list[dict]:
    vts = _find(root, "VIDEO_TS")
    out = []
    for ifo in sorted(glob.glob(os.path.join(vts, "[vV][tT][sS]_[0-9][0-9]_0.[iI][fF][oO]"))):
        num = os.path.basename(ifo)[4:6]
        vobs = sorted(x for x in glob.glob(os.path.join(vts, f"[vV][tT][sS]_{num}_*.[vV][oO][bB]"))
                      if not x.upper().endswith("_0.VOB"))
        if not vobs:
            continue
        try:
            info = read_ifo(ifo)
        except (OSError, ValueError, IndexError, struct.error):
            info = {"duration": 0.0, "audio": [], "subs": []}
        size = sum(os.path.getsize(x) for x in vobs)
        if info["duration"] and info["duration"] < min_seconds:
            continue
        langs = {0x80 + i: LANG2.get(lang, lang) for i, (_f, lang) in enumerate(info["audio"]) if lang}
        langs.update({0x20 + i: LANG2.get(lang, lang) for i, lang in enumerate(info["subs"]) if lang})
        out.append({"kind": "dvd", "id": f"VTS_{num}", "duration": info["duration"], "size": size,
                    "files": [(x, None, None) for x in vobs], "marks": [], "video": ["MPEG-2"],
                    "audio": [(f, LANG2.get(lang, lang)) for f, lang in info["audio"]],
                    "subs": [LANG2.get(lang, lang) for lang in info["subs"]], "langs": langs})
    out.sort(key=lambda x: (-x["duration"], -x["size"]))
    if out:
        out[0]["main"] = True
    return out


def titles(kind: str, root: str) -> list[dict]:
    """Titles longer than a minute; on a disc with none, every one (a short clip disc)."""
    read = bd_titles if kind == "bd" else dvd_titles
    return read(root) or read(root, 1.0)


def title_source(title: dict, disc: str) -> str:
    """The concat list a title is read through (data/discs/<disc> - <playlist>.ffconcat)."""
    d = os.path.join(config.data_dir(), "discs")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', " ", disc).strip() or "disc"
    path = os.path.join(d, f"{safe} - {title['id']}.ffconcat")
    with open(path, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for name, a, b in title["files"]:
            f.write("file '" + name.replace("\\", "/").replace("'", "'\\''") + "'\n")
            if a is not None:
                f.write(f"inpoint {a:.6f}\n")
            if b is not None:
                f.write(f"outpoint {b:.6f}\n")
    return path


def apply_title(job, title: dict) -> None:
    """The disc's languages onto the task's tracks (by PID / sub-stream id) and its chapter marks as chapters."""
    if job.info is None or job.info.error:
        return
    langs = title.get("langs") or {}
    st = job.meta.setdefault("streams", {})
    pids = {s.index: s.pid for s in job.info.streams}
    if all(p < 0 for p in pids.values()) and title.get("files"):     # the concat list hides the PIDs: 1st clip
        from .probe import probe
        first = probe(title["files"][0][0])
        if not first.error:
            pids = {s.index: s.pid for s in first.streams}
    for s in job.info.streams:
        lang = langs.get(pids.get(s.index, -1))
        if lang and s.kind in ("audio", "subtitle") and (not s.lang or s.lang == "und"):
            st.setdefault(str(s.index), {})["lang"] = lang
    if not st:
        job.meta.pop("streams", None)
    marks = [m for m in title.get("marks") or [] if m < (title.get("duration") or 9e9) - 1]
    if len(marks) > 1 and job.chapters is None:
        job.chapters = [[round(m, 3), f"Chapter {i + 1}"] for i, m in enumerate(marks)]
