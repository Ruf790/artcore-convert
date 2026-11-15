"""1.7: no «XviD4PSP» anywhere, the crop finds grey / noisy bars and is not undone by a moment without bars, the
log window, settings applied to other tasks, sound / subtitles beside a film picked up, hearing an outside track
at a delay, before / after with a curtain, the NVENC presets for the RTX 40 / 50."""
import glob
import os

import pytest

from conftest import ff as mk, pump
from convert import config, logs
from convert import filters as F
from convert import job as J
from convert import probe as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def films(media, tmp_path_factory):
    d = str(tmp_path_factory.mktemp("films17"))
    # 4 s full frame (a title), then letterboxed with grey noisy bars
    mk("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=3", "-f", "lavfi",
       "-i", "testsrc2=s=640x268:r=25:d=17,pad=640:360:0:46:color=0x1e1e1e,noise=alls=6:allf=t",
       "-filter_complex", "[0][1]concat=n=2:v=1[v]", "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast",
       "-pix_fmt", "yuv420p", os.path.join(d, "Grey.mkv"))
    for name, n in (("A.mkv", 2), ("B.mkv", 3)):
        args = ["-f", "lavfi", "-i", f"testsrc2=s=640x360:r=25:d={n + 2}"]
        for k in range(n):
            args += ["-f", "lavfi", "-i", f"sine=f={300 + 100 * k}:d={n + 2}"]
        args += ["-map", "0"] + sum((["-map", str(k + 1)] for k in range(n)), [])
        mk(*args, "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3", os.path.join(d, name))
    mk("-f", "lavfi", "-i", "sine=f=900:d=4", "-ac", "2", "-c:a", "ac3", os.path.join(d, "A.rus.ac3"))
    with open(os.path.join(d, "A.eng.srt"), "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,300 --> 00:00:01,200\nHello\n")
    with open(os.path.join(d, "Another.srt"), "w", encoding="utf-8") as f:   # «A» + «n…»: not A's
        f.write("1\n00:00:00,300 --> 00:00:01,200\nNo\n")
    return d


def _win(qapp, **over):
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False, keep_queue=False)
    cfg.update(over)
    w = MainWindow(cfg)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    return w


def _settle(qapp, w):
    assert pump(qapp, 20, lambda: all(j.status != "probe" for j in w.jobs) and
                all(v is not None for j in w.jobs for v in j.infos.values()))


def test_no_xvid4psp_anywhere():
    files = glob.glob(os.path.join(ROOT, "convert", "*.py")) + [os.path.join(ROOT, "README.md")]
    for p in files:
        with open(p, encoding="utf-8") as f:
            assert "xvid4psp" not in f.read().lower(), p
    from convert.i18n import STRINGS
    assert "look_classic" in STRINGS and "look_xvid" not in STRINGS
    from convert import style
    assert style.set_look("xvid") == "classic"               # an old setting: the classic look


# ── crop ──────────────────────────────────────────────────────
def test_bars_of_majority():
    W, H = 1280, 720
    letter = (1280, 536, 0, 92)
    # 10 moments: 8 letterboxed, 1 full-frame title, 1 dark scene (looks like wider bars)
    res = [letter] * 8 + [(1280, 720, 0, 0), (1200, 400, 40, 160)]
    assert F.bars_of(res, W, H) == (0, 92, 0, 92)
    assert F.bars_of([(1280, 720, 0, 0)] * 5 + [letter] * 5, W, H) == (0, 0, 0, 0)   # half without: none
    assert F.bars_of([], W, H) == (0, 0, 0, 0)
    assert F.bars_of([(1278, 718, 1, 1)], W, H) == (0, 0, 0, 0)                      # noise is no bar


def test_crop_grey_bars_and_a_title(films):
    from convert import ff
    p = os.path.join(films, "Grey.mkv")
    info = P.probe(p)
    l, t, r, b = F.detect_crop(ff.ffmpeg(), p, info.duration, 640, 360)
    assert (l, r) == (0, 0) and 40 <= t <= 48 and 40 <= b <= 48


def test_empty_crop_found_is_looked_for_again():
    j = J.Job.from_dict({"src": "/x/a.mkv", "settings": {}, "crop_found": []})
    assert j.crop_found is None
    j = J.Job.from_dict({"src": "/x/a.mkv", "settings": {}, "crop_found": [0, 92, 0, 92]})
    assert j.crop_found == [0, 92, 0, 92]


# ── apply to others ───────────────────────────────────────────
def test_apply_from(films):
    a = J.Job(src=os.path.join(films, "A.mkv"), settings=J.merged({}), info=P.probe(os.path.join(films, "A.mkv")))
    b = J.Job(src=os.path.join(films, "B.mkv"), settings=J.merged({}), info=P.probe(os.path.join(films, "B.mkv")))
    a.settings["video"].update(encoder="x264", quality=21, height=360)
    F.fset(a.settings, "crop")["on"] = True
    a.tracks = {"1": {"keep": False}, "2": {"codec": "aac", "kbps": 192}}       # the 1st audio off, the 2nd AAC
    a.meta["streams"] = {"2": {"lang": "rus"}}
    a.trim = [[1.0, 3.0], [3.5, 10.0]]
    done = J.apply_from(a, b)
    assert done == ["settings", "tracks"]
    assert b.settings["video"]["quality"] == 21 and F.fset(b.settings, "crop")["on"] and b.trim == []
    assert b.tracks == {"1": {"keep": False}, "2": {"codec": "aac", "kbps": 192}} and b.meta["streams"] == {"2": {"lang": "rus"}}
    b.settings["video"]["quality"] = 30
    assert a.settings["video"]["quality"] == 21                                  # copies, not the same dict
    assert J.apply_from(a, b, ["trim"]) == ["trim"]
    assert b.trim == [[1.0, 3.0], [3.5, 5.0]]                                    # cut to B's 5 s
    assert [s.index for s, _c in J.plan(b)["audio"]] == [2, 3]


def test_apply_dialog_and_menu(qapp, films, data_dir, monkeypatch):
    w = _win(qapp)
    w.add_paths([os.path.join(films, n) for n in ("A.mkv", "B.mkv", "Grey.mkv")])
    _settle(qapp, w)
    a, b, g = w.jobs
    a.settings = J.merged({"video": {"encoder": "x264", "quality": 19}})
    a.tracks = {"2": {"codec": "flac"}}
    w.view.clearSelection()
    for j in (a, b):
        w.items[j.uid].setSelected(True)
    w.view.setCurrentItem(w.items[a.uid], 0, __import__("PySide6.QtCore", fromlist=["QItemSelectionModel"])
                          .QItemSelectionModel.NoUpdate)
    pump(qapp, 0.2)
    assert w.apply_dialog(a, ["settings", "tracks"], "sel", ask=False) == 1
    assert b.settings["video"]["quality"] == 19 and b.tracks == {"2": {"codec": "flac"}}
    assert g.settings["video"].get("quality") != 19
    assert w.apply_dialog(a, ["settings"], "all", ask=False) == 1                 # B is the same already: G
    assert g.settings["video"]["quality"] == 19
    # the dialog itself
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.Accepted)
    a.settings["video"]["quality"] = 17
    assert w.apply_dialog(a) == 1 and b.settings["video"]["quality"] == 17
    w.close()


# ── picked up from the folder ─────────────────────────────────
def test_beside_and_auto_pickup(qapp, films, data_dir):
    from convert.window import MainWindow
    found = [os.path.basename(p) for p in MainWindow.beside(os.path.join(films, "A.mkv"))]
    assert found == ["A.eng.srt", "A.rus.ac3"]                                   # not Another.srt
    w = _win(qapp)
    assert w.add_incoming([os.path.join(films, "A.mkv")]) == 1
    j = w.jobs[0]
    assert sorted(os.path.basename(e["path"]) for e in j.ext) == ["A.eng.srt", "A.rus.ac3"]
    w.close()
    w = _win(qapp, auto_ext=False)
    w.add_incoming([os.path.join(films, "A.mkv")])
    assert not w.jobs[0].ext
    w.close()


# ── listening at a delay ──────────────────────────────────────
def test_listen_ext_follows_delay(qapp, films, data_dir):
    from PySide6.QtWidgets import QSpinBox, QToolButton
    w = _win(qapp)
    w.add_incoming([os.path.join(films, "A.mkv")])
    _settle(qapp, w)
    j = w.jobs[0]
    w.view.setCurrentItem(w.items[j.uid])
    w.show_side("streams")
    pump(qapp, 0.4)

    class FakePreview:                                     # what MpvView is asked to do
        path = j.src
        listening = ""
        calls = []

        def listen(self, path, delay):
            self.listening = path
            self.calls.append(("listen", os.path.basename(path), delay))
            return True

        def set_delay(self, delay):
            self.calls.append(("delay", delay))

        def stop_listen(self):
            self.listening = ""
            self.calls.append(("stop",))

        def pause(self):
            pass
    fake = FakePreview()
    w.trim_tab.preview = fake
    w._fill_tracks()
    pump(qapp, 0.2)
    ac3 = next(e for e in j.ext if e["kind"] == "audio")["path"]
    hear = [b for b in w.ext_box.findChildren(QToolButton) if b.property("listen") == ac3][-1]
    hear.click()
    assert fake.calls[-1] == ("listen", "A.rus.ac3", 0.0)
    dl = [s for s in hear.parentWidget().findChildren(QSpinBox) if s.objectName() == "extDelay"][0]
    dl.setValue(250)
    pump(qapp, 0.3)
    assert ("delay", 0.25) in fake.calls and next(e for e in j.ext if e["path"] == ac3)["delay"] == 250
    hear = [b for b in w.ext_box.findChildren(QToolButton) if b.property("listen") == ac3][-1]
    assert hear.isChecked()                                # the panel was built again: still on
    hear.click()
    assert fake.calls[-1] == ("stop",)
    w.trim_tab.preview = None
    w.close()


def test_mpv_listen_commands():
    from convert import mpvview

    class Player:
        def __init__(self):
            self.cmds, self.aid, self.audio_delay, self.pause, self.mute = [], 1, 0.0, True, True

        def command(self, *a):
            self.cmds.append(a)
    v = mpvview.MpvView.__new__(mpvview.MpvView)
    v.player, v.path, v.dead, v.listening, v._aid0 = Player(), "/x/film.mkv", False, "", None
    assert v.listen("/x/film.rus.ac3", 0.12)
    assert v.player.cmds == [("audio-add", "/x/film.rus.ac3", "select")] and v.player.audio_delay == 0.12
    assert v.player.pause is False
    v.set_delay(-0.3)
    v.listen("/x/film.rus.ac3", -0.3)                      # the same file: not added twice
    assert len(v.player.cmds) == 1 and v.player.audio_delay == -0.3
    v.stop_listen()
    assert v.player.aid == 1 and v.player.audio_delay == 0.0 and v.listening == ""
    assert not mpvview.FramePreview.listen(None, "/x/a.ac3", 0)


# ── before / after ────────────────────────────────────────────
def test_before_after_frames(qapp, films, data_dir):
    from PySide6.QtGui import QImage
    from convert import beforeafter as BA
    from convert import encoders as E
    av = E.detect()
    p = os.path.join(films, "Grey.mkv")
    j = J.Job(src=p, settings=J.merged({"video": {"encoder": "x264", "speed": 7, "height": 240, "rc": "size",
                                                  "size_mb": 5, "passes": 2}}), info=P.probe(p))
    F.fset(j.settings, "crop").update(on=True, mode="manual", t=46, b=46)
    g = BA.Grabber()
    a, b, err = g.frames(j, av, 8.0)
    assert not err and QImage(a).size() == QImage(b).size() and QImage(b).width() == 426 and QImage(b).height() < 200  # cropped
    piece = g.piece
    assert piece.endswith(".mkv") and abs(P.probe(piece).duration - BA.PIECE) < 0.5
    g.frames(j, av, 9.0)
    assert g.piece == piece                                # inside the piece: not encoded again
    k = BA.piece_job(j, 3.0)
    assert k.settings["video"]["passes"] == 1 and k.settings["video"]["rc"] == "bitrate" and k.trim == [[3.0, 13.0]]
    assert j.settings["video"]["passes"] == 2                                     # the task itself untouched


def test_compare_view_in_window(qapp, films, data_dir):
    w = _win(qapp)
    w.add_paths([os.path.join(films, "A.mkv")])
    _settle(qapp, w)
    j = w.jobs[0]
    j.settings = J.merged({"video": {"encoder": "x264", "speed": 7, "height": 240}})
    w.view.setCurrentItem(w.items[j.uid])
    pump(qapp, 0.3)
    w.toggle_compare()
    v = w.cmp_view
    assert w.trim_tab.stage_lay.currentWidget() is v and w.b_compare.isChecked()
    assert pump(qapp, 60, lambda: not v.b.isNull() and not v.a.isNull())
    assert v.label_b and v.at >= 0
    v.split = 0.3
    v.moved.emit(1.0)
    assert pump(qapp, 60, lambda: not w.cmp_grab.busy and abs(v.at - 1.0) < 0.01)
    v.closed.emit()
    assert w.trim_tab.stage_lay.currentWidget() is not v and not w.b_compare.isChecked()
    w.close()


# ── the log window ────────────────────────────────────────────
def test_log_window(qapp, data_dir):
    good = logs.save("/x/good.mkv", "/x/good_c.mkv", "done", "", 3.0, 10, "HEVC", "ffmpeg", ["ok"],
                     text="-" * 60 + "\nENCODING STARTED 2026\nVideo general:\n Codec: H264 > X265\n"
                          "ENCODING COMPLETED! 2026\n")
    bad = logs.save("/x/bad.mkv", "", "error", "boom", 1.0, 0, "HEVC", "ffmpeg", ["x"],
                    text="ENCODING STARTED\nCommand: ffmpeg -i bad\n[h264 @ 0x1] Invalid NAL unit size\n"
                         "Error while decoding stream #0:0\nENCODING FAILED! 2026\nError: boom\n")
    assert good and bad
    from convert.logview import LogWindow, is_error_line
    assert is_error_line("Error while decoding stream #0:0") and is_error_line("ENCODING FAILED! 2026")
    assert not is_error_line("ENCODING COMPLETED! 2026") and not is_error_line(" Input stream #0:0: 0 decode errors")
    d = LogWindow()
    assert d.hist.topLevelItemCount() == 2 and d.path == bad                       # the newest first
    assert "3" in d.status.text() or "Ошибок" in d.status.text() or "Errors" in d.status.text()
    assert "Invalid NAL" in d.view.textCursor().block().text()                    # jumped to the first error
    assert d.next_error() and "Error while decoding" in d.view.textCursor().block().text()
    d.only_err.setChecked(True)
    assert d.visible_count() == 1
    d.only_err.setChecked(False)
    d.filter.setText("good")
    assert d.visible_count() == 1
    d.filter.clear()
    d.select(good)
    assert d.path == good and "COMPLETED" in d.view.toPlainText() and not d.b_err.isEnabled()
    d.find.setText("x265")
    assert d.find_next() and d.view.textCursor().selectedText().lower() == "x265"
    d.close()


# ── presets ───────────────────────────────────────────────────
def test_rtx_presets():
    from convert import advanced as ADV
    from convert import encoders as E
    from convert.i18n import STRINGS
    for k in ("rtx_av1_q", "rtx_av1_fast", "rtx_hevc_q"):
        assert k in J.PRESETS and "p_" + k in STRINGS
    st = J.preset_settings("rtx_av1_fast")
    e = E.BY_ID["nvenc_av1"]
    caps = {"preset": frozenset({"p4", "p7"}), "tune": frozenset({"hq"}), "multipass": frozenset({"qres"}),
            "rc-lookahead": frozenset(), "spatial-aq": frozenset(), "temporal-aq": frozenset(),
            "b_ref_mode": frozenset({"middle"}), "split_encode_mode": frozenset({"auto", "forced"})}
    args, _ = ADV.args(e, ADV.of(st, e), caps)
    assert args[:2] == ["-preset", "p4"] and "-temporal-aq" in args and args[-2:] == ["-split_encode_mode", "forced"]
    caps.pop("split_encode_mode")                                                 # an older ffmpeg: left out
    assert "-split_encode_mode" not in ADV.args(e, ADV.of(st, e), caps)[0]
