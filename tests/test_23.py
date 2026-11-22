"""2.3: in place of mkvmerge — attachments (fonts) kept / added / dropped, MP4 warns; the result cut at its
chapters (every N, «Name_01 Title»); the Metadata tab's edits written into the MKV by mkvpropedit."""
import os
import shutil
import subprocess

import pytest

from conftest import ff as mk, pump
from convert import encoders as E
from convert import job as J
from convert import probe as P

AV = E.Available({"x264": True})


@pytest.fixture(scope="module")
def att(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("att23"))
    font = os.path.join(d, "Font One.ttf")
    otf = os.path.join(d, "two.otf")
    for p in (font, otf):
        with open(p, "wb") as f:
            f.write(b"\x00\x01\x00\x00" + os.urandom(4092))
    ass = os.path.join(d, "s.ass")
    with open(ass, "w", encoding="utf-8") as f:
        f.write("[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize\n"
                "Style: Default,Font One,20\n\n[Events]\nFormat: Layer, Start, End, Style, Text\n"
                "Dialogue: 0,0:00:00.50,0:00:01.50,Default,Hi\n")
    ch = os.path.join(d, "ch.txt")
    with open(ch, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\ntitle=Old title\n" + "".join(
            f"[CHAPTER]\nTIMEBASE=1/1000\nSTART={i * 1000}\nEND={(i + 1) * 1000}\ntitle={n}\n"
            for i, n in enumerate(("Intro", "Part: two", "Three", "End"))))
    src = os.path.join(d, "Anime.mkv")
    mk("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=4", "-f", "lavfi", "-i", "sine=d=4", "-i", ass, "-i", ch,
       "-map", "0", "-map", "1", "-map", "2", "-map_metadata", "3", "-map_chapters", "3", "-c:v", "libx264",
       "-g", "25", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "copy", "-metadata:s:a:0", "language=jpn",
       "-attach", font, "-metadata:s:t:0", "mimetype=font/ttf", src)
    return {"dir": d, "src": src, "font": font, "otf": otf}


def _job(src, **st):
    j = J.Job(src=src, settings=J.merged({"container": "mkv", "audio": {"codec": "copy"}} | st))
    j.info = P.probe(src)
    return j


def _atts(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "t", "-show_entries",
                        "stream_tags=filename,mimetype", "-of", "csv=p=0", path], capture_output=True, text=True)
    return sorted(tuple(x.split(",")) for x in r.stdout.splitlines() if x)


def _run(j, out):
    r = subprocess.run(J.build_args(j, "ffmpeg", out, AV, False), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return out


def test_probe_sees_the_font(att):
    i = P.probe(att["src"])
    a = i.of("attachment")
    assert [(s.filename, s.mime) for s in a] == [("Font One.ttf", "font/ttf")] and a[0].att_size == 4096


def test_remux_keeps_and_adds(att, tmp_path):
    j = _job(att["src"], video={"mode": "copy"})
    j.attach = [att["otf"]]
    out = _run(j, str(tmp_path / "remux.mkv"))
    assert _atts(out) == [("Font One.ttf", "font/ttf"), ("two.otf", "font/otf")]
    assert not J.plan(j)["warnings"]


def test_reencode_keeps_and_adds(att, tmp_path):
    j = _job(att["src"], video={"encoder": "x264", "speed": 1})
    j.attach = [att["otf"]]
    out = _run(j, str(tmp_path / "enc.mkv"))
    assert _atts(out) == [("Font One.ttf", "font/ttf"), ("two.otf", "font/otf")]
    assert P.probe(out).of("subtitle")[0].codec == "ass"


def test_untick_drops_one(att, tmp_path):
    j = _job(att["src"], video={"mode": "copy"})
    font = j.info.of("attachment")[0]
    j.tracks[str(font.index)] = {"keep": False}
    assert J.kept_attachments(j) == []
    out = _run(j, str(tmp_path / "none.mkv"))
    assert _atts(out) == []


def test_mp4_warns_and_leaves_them(att, tmp_path):
    j = _job(att["src"], container="mp4", video={"mode": "copy"})
    assert "w_attach_lost" in J.plan(j)["warnings"]
    assert "-attach" not in J.build_args(j, "ffmpeg", "/x.mp4", AV, False)
    j.settings["subs"] = "none"                                  # no ASS → no fonts needed, no warning
    assert "w_attach_lost" not in J.plan(j)["warnings"]
    assert J.attach_mime("a.TTF") == "font/ttf" and J.attach_mime("x.bin") == "application/octet-stream"


def test_attachment_survives_save_and_load(att):
    j = _job(att["src"])
    j.attach = [att["otf"]]
    assert J.Job.from_dict(j.to_dict()).attach == [att["otf"]]


# ── by chapters ──
def test_chapter_cuts_and_names():
    chaps = [(0.0, "Intro"), (1.0, "Part: two"), (2.0, "Three"), (3.0, "")]
    assert J.chapter_cuts(chaps, 1) == ([1.0, 2.0, 3.0], ["Intro", "Part: two", "Three", ""])
    assert J.chapter_cuts(chaps, 2) == ([2.0], ["Intro", "Three"])
    assert J.chapter_cuts(chaps, 9) == ([], ["Intro"])
    parts = [f"/d/Film_0{n}.mkv" for n in (1, 2, 3, 4)]
    assert J.part_names(parts, ["Intro", "Part: two", "Three", ""]) == \
        ["/d/Film_01 Intro.mkv", "/d/Film_02 Part_ two.mkv", "/d/Film_03 Three.mkv", "/d/Film_04.mkv"]


def test_split_by_chapters_copy(att, tmp_path):
    out = shutil.copy(att["src"], str(tmp_path / "Film.mkv"))
    j = _job(out, split={"mode": "chapters", "chapters_n": 2})
    assert J.split_on(j) and "2" in J.describe(j.settings, None, __import__("convert.i18n").i18n.t)
    cuts, titles = J.chapter_cuts(J.read_chapters("ffprobe", out), 2)
    assert cuts == [2.0] and titles == ["Intro", "Three"]
    r = subprocess.run(J.split_args(j, "ffmpeg", out, cuts), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    parts = J.split_parts(out)
    assert len(parts) == 2
    assert all(abs(P.probe(p).duration - 2.0) < 0.3 for p in parts)
    assert all(_atts(p) == [("Font One.ttf", "font/ttf")] for p in parts)     # every part keeps the font


def test_keyframes_at_chapters_when_encoding(att):
    j = _job(att["src"], video={"encoder": "x264", "speed": 1}, split={"mode": "chapters", "chapters_n": 1})
    a = J.build_args(j, "ffmpeg", "/x.mkv", AV, False)
    assert a[a.index("-force_key_frames") + 1] == "1.000,2.000,3.000"


def test_no_chapters_warns(media):
    j = _job(os.path.join(media, "hdr.mkv"), split={"mode": "chapters", "chapters_n": 1})
    assert "split_no_chapters" in J.plan(j)["warnings"]


def test_queue_splits_by_chapters_with_titles(qapp, att, data_dir, tmp_path):
    from test_14_ui import _run_all, _win
    src = shutil.copy(att["src"], str(tmp_path / "Show.mkv"))
    w = _win(qapp, metrics=[], parallel=1, cpu_limit=100, out_mode="folder", out_dir=str(tmp_path / "out"))
    os.makedirs(str(tmp_path / "out"), exist_ok=True)
    assert w.add_paths([src]) == 1
    assert pump(qapp, 15, lambda: w.jobs[0].status != "probe")
    j = w.jobs[0]
    j.settings = J.merged({"video": {"mode": "copy"}, "audio": {"codec": "copy"},
                           "split": {"mode": "chapters", "chapters_n": 1, "names": "title"}})
    _run_all(qapp, w)
    assert j.status == "done", j.error
    names = [os.path.basename(p) for p in j.parts]
    assert names == ["Show_convert_01 Intro.mkv", "Show_convert_02 Part_ two.mkv", "Show_convert_03 Three.mkv",
                     "Show_convert_04 End.mkv"], names                 # (the AAC makes the copied video start late)
    assert all(os.path.isfile(p) for p in j.parts) and j.out == j.parts[0]
    w.close()


# ── the Streams tab ──
def test_streams_tab_lists_and_adds(qapp, att, data_dir):
    from test_14_ui import _select, _win
    from PySide6.QtWidgets import QCheckBox
    w = _win(qapp)
    assert w.add_paths([att["src"]]) == 1
    assert pump(qapp, 15, lambda: w.jobs[-1].status != "probe")
    j = w.jobs[-1]
    _select(qapp, w, [j])
    w.show_side("streams")
    pump(qapp, 0.3)
    boxes = [b for b in w.att_box.findChildren(QCheckBox) if b.isVisible()]
    assert len(boxes) == 1 and "Font One.ttf" in boxes[0].text() and "font/ttf" in boxes[0].text()
    boxes[0].setChecked(False)
    assert J.kept_attachments(j) == []
    w.add_attachment_dialog([att["otf"]])
    w.add_attachment_dialog([att["otf"]])                     # the same file once
    assert j.attach == [att["otf"]]
    pump(qapp, 0.3)
    w._att_remove(0)
    assert j.attach == []
    w.close()


# ── mkvpropedit ──
def test_propedit_arguments(att):
    from convert import propedit as PE
    j = _job(att["src"])
    assert PE.can(j) and PE.changes(j) == []
    a = j.info.of("audio")[0]
    s = j.info.of("subtitle")[0]
    j.meta = {"tags": dict(J.source_tags(j.info), title="New title"),
              "streams": {str(a.index): {"lang": "rus", "title": "Дубляж", "default": a.default},
                          str(s.index): {"forced": True}}}
    assert PE.changes(j) == ["--edit", "info", "--set", "title=New title",
                             "--edit", "track:a1", "--set", "language=rus", "--set", "name=Дубляж",
                             "--edit", "track:s1", "--set", "flag-forced=1"]
    assert PE.command(j, "mkvpropedit")[:2] == ["mkvpropedit", att["src"]]
    assert not PE.can(_job(os.path.join(att["dir"], "s.ass")))


@pytest.mark.skipif(not shutil.which("mkvpropedit"), reason="mkvpropedit is not installed")
def test_propedit_writes_the_file(qapp, att, data_dir, tmp_path):
    from convert import propedit as PE
    from test_14_ui import _select, _win
    src = shutil.copy(att["src"], str(tmp_path / "Edit me.mkv"))
    size = os.path.getsize(src)
    PE.find(refresh=True)
    w = _win(qapp)
    w._pe_confirm = False
    assert w.add_paths([src]) == 1
    assert pump(qapp, 15, lambda: w.jobs[-1].status != "probe")
    j = w.jobs[-1]
    _select(qapp, w, [j])
    w.show_side("meta")
    pump(qapp, 0.2)
    a = j.info.of("audio")[0]
    j.meta = {"tags": dict(J.source_tags(j.info), title="Новое название"),
              "streams": {str(a.index): {"lang": "rus", "title": "Дубляж", "forced": True}}}
    w.meta_tab.set_job(j, True)
    assert w.meta_tab.b_inplace.isVisibleTo(w.meta_tab) and w.meta_tab.b_inplace.isEnabled()
    w.apply_inplace()
    assert pump(qapp, 30, lambda: "streams" not in j.meta)
    i = P.probe(src)
    au = i.of("audio")[0]
    assert i.title == "Новое название" and au.lang == "rus" and au.title == "Дубляж" and au.forced
    assert j.info.title == "Новое название" and "tags" not in j.meta
    assert abs(os.path.getsize(src) - size) < 64 * 1024                 # in place: no new file was written
    assert not w.meta_tab.b_inplace.isEnabled()                       # nothing left to write
    w.close()


def test_propedit_missing_hides_the_button(qapp, att, data_dir, monkeypatch):
    from convert import propedit as PE
    from convert.i18n import t
    from test_14_ui import _select, _win
    monkeypatch.setattr(PE, "find", lambda refresh=False: "")
    w = _win(qapp)
    assert w.add_paths([att["src"]]) == 1
    assert pump(qapp, 15, lambda: w.jobs[-1].status != "probe")
    _select(qapp, w, [w.jobs[-1]])
    w.meta_tab.set_job(w.jobs[-1], True)
    assert not w.meta_tab.b_inplace.isVisibleTo(w.meta_tab)
    assert "MKVToolNix" in w.meta_tab.pe_hint.text() and PE.URL in t("pe_missing", PE.URL)
    w.close()


# ── 2.3.1 ──
def test_empty_preview_is_not_black(qapp, data_dir):
    from convert import style
    from convert.edit_tabs import TrimTab
    old = "dark" if style.IS_DARK else "light"
    try:
        style.set_theme("light")
        tt = TrimTab()
        assert tt.stage_lay.currentWidget() is tt.placeholder
        assert "#ffffff" in tt.stage.styleSheet() and "#000" not in tt.stage.styleSheet()
        style.set_theme("dark")
        tt._stage_bg()
        assert style.PANEL in tt.stage.styleSheet()
        from PySide6.QtWidgets import QLabel
        pic = QLabel()
        tt.stage_lay.addWidget(pic)
        tt.stage_lay.setCurrentWidget(pic)                     # a picture: black under it
        assert tt.stage.styleSheet() == "background: #000;"
    finally:
        style.set_theme(old)
