"""2.5.1: the language of a text subtitle file (SRT / ASS / SSA / VTT / MicroDVD .sub) from its lines.
The script decides first (kana → Japanese, hangul → Korean, Han → Chinese, Hebrew, Arabic, Devanagari);
Cyrillic is told apart by its own letters (і ї є ґ — Ukrainian, ў — Belarusian, ә ғ қ ң ө ұ ү һ — Kazakh,
ы э ё — Russian, ъ without ы/э — Bulgarian); Latin by the most common short words of each language."""
from __future__ import annotations

import os
import re

TEXT_SUBS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".txt")

_WORDS = {
    "eng": "the you and to is it that of what in this i me my no not be have we are don't your it's was know",
    "deu": "ich und die der nicht das du ist ein zu sie es was mit wir den mir auf ja nein sich dich mich haben",
    "fra": "je de pas le la vous est et les tu que un une il ne ce qui en c'est des pour on mais moi bien",
    "spa": "que de no el la es y a en lo por qué un una me se los te para con las está pero eso bien",
    "ita": "che non di il è la e un per mi ti sono io lo cosa ma con una questo ho bene sei hai gli",
    "por": "que não de o a é e um você se eu do da uma para me com isso está os no na tem mais bem",
    "pol": "nie to się i w na jest że co tak ja z do mi ale już jak mnie czy go tylko tu ty",
    "cze": "to je se a na že ne co v jsem si já tak mi ale jak tady jsi bude by pro být",
    "tur": "bir ve bu ne için mi da de değil ben sen o çok var ama evet hayır gibi beni seni",
}
_SETS = {k: set(v.split()) for k, v in _WORDS.items()}
_RE_SRT = re.compile(r"^\s*\d+\s*$|^\s*\d\d:\d\d:\d\d[,.]\d+\s*-->.*$|^WEBVTT.*$", re.M)
_RE_TAGS = re.compile(r"\{[^}]*\}|<[^>]*>|\\[Nnh]")
_RE_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.U)


def read_text(path: str, limit: int = 400_000) -> str:
    """The file's text (BOM / UTF-8 / UTF-16, else cp1251 for Cyrillic, else latin-1)."""
    with open(path, "rb") as f:
        raw = f.read(limit)
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    hi = sum(1 for b in raw if b >= 0xC0)
    return raw.decode("cp1251" if hi > len(raw) * 0.05 else "latin-1", "replace")


def lines_of(text: str, ext: str) -> str:
    """Only what is said: no numbers, time codes, styles, tags."""
    if ext in (".ass", ".ssa"):
        out = []
        for ln in text.splitlines():
            if ln.startswith("Dialogue:"):
                parts = ln.split(",", 9)
                if len(parts) == 10:
                    out.append(parts[9])
        text = "\n".join(out)
    elif ext == ".sub":
        text = re.sub(r"\{\d+\}\{\d+\}", "", text).replace("|", "\n")
    else:
        text = _RE_SRT.sub("", text)
    return _RE_TAGS.sub(" ", text)


def detect(text: str) -> str:
    """The language code (ISO 639-2 as in the program's list) of a text, or "" when unsure."""
    letters = [c for c in text if c.isalpha()]
    n = len(letters)
    if n < 40:
        return ""

    def share(lo: int, hi: int) -> float:
        return sum(1 for c in letters if lo <= ord(c) <= hi) / n
    if share(0x3040, 0x30FF) > 0.05:
        return "jpn"
    if share(0xAC00, 0xD7AF) + share(0x1100, 0x11FF) > 0.3:
        return "kor"
    if share(0x4E00, 0x9FFF) > 0.3:
        return "chi"
    if share(0x0590, 0x05FF) > 0.3:
        return "heb"
    if share(0x0600, 0x06FF) > 0.3:
        return "ara"
    if share(0x0900, 0x097F) > 0.3:
        return "hin"
    if share(0x0400, 0x04FF) > 0.3:
        low = text.lower()
        cnt = {c: low.count(c) for c in "іїєґўыэёъәғқңөұүһ"}
        cyr = sum(1 for c in letters if 0x0400 <= ord(c) <= 0x04FF)
        if sum(cnt[c] for c in "әғқңөұүһ") > cyr * 0.01:
            return "kaz"
        if cnt["ў"] > cyr * 0.002:
            return "bel"
        if cnt["ї"] + cnt["є"] + cnt["ґ"] + cnt["і"] > cyr * 0.01 and cnt["ы"] + cnt["э"] < cyr * 0.002:
            return "ukr"
        if cnt["ы"] + cnt["э"] + cnt["ё"] > cyr * 0.003:
            return "rus"
        if cnt["ъ"] > cyr * 0.005:
            return "bul"
        return "rus"
    if share(0x0041, 0x024F) > 0.6:
        words = [w.lower() for w in _RE_WORD.findall(text)]
        if len(words) < 30:
            return ""
        score = {k: sum(1 for w in words if w in s) / len(words) for k, s in _SETS.items()}
        best = max(score, key=score.get)
        second = sorted(score.values())[-2]
        if score[best] > 0.12 and score[best] > second * 1.4:
            return best
    return ""


def detect_file(path: str) -> str:
    """The language of a text subtitle file, "" for pictures (SUP / IDX) or when unsure."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_SUBS:
        return ""
    try:
        return detect(lines_of(read_text(path), ext))
    except OSError:
        return ""
