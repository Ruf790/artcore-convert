"""2.1: the delayed start of the queue (the time worked out by next_start — today or tomorrow, across a change of
summer time; the menu beside «Start», the button «Start at 01:00», the countdown in the status bar; «Start» starts at
once, «Stop» cancels; the queue really starts at the moment and encodes; the waiting outlives a restart with the
queue, a moment that passed while the program was closed is dropped and does not start anything) and the preview
with the crop («Show the result»: mpv shows the frame already cut — the same numbers ffmpeg gets, auto and by hand;
the result file has exactly the size shown)."""
import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ff as mk, pump
from convert import config
from convert import delay as DL
from convert import filters as F
from convert import job as J
from convert import mpvview

IS_POSIX = hasattr(time, "tzset")


def _win(qapp, **over):
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False, keep_queue=False)
    cfg.update(over)
    w = MainWindow(cfg)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    return w


def _one(qapp, w, path):
    w.add_paths([path])
    assert pump(qapp, 10, lambda: all(j.status != "probe" for j in w.jobs))
    j = w.jobs[-1]
    w.view.clearSelection()
    w.items[j.uid].setSelected(True)
    pump(qapp, 0.2)
    return j


def wh(path: str) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-of", "json", path], capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    return s["width"], s["height"]


@pytest.fixture(scope="module")
def wide(tmp_path_factory):
    """A 2.20 film in a 1080p frame: black bars of 104 px above and below (as the user's 1920×872 film)."""
    d = tmp_path_factory.mktemp("w21")
    p = str(d / "Wide (2021).mkv")
    mk("-f", "lavfi", "-i", "testsrc2=s=1920x872:r=25:d=2", "-f", "lavfi", "-i", "sine=f=440:d=2",
       "-vf", "pad=1920:1080:0:104:black", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", p)
    return p


@pytest.fixture()
def tz():
    """Local time of the tests: Europe/Sofia (summer time from the last Sunday of March, 03:00 → 04:00)."""
    if not IS_POSIX:
        pytest.skip("time.tzset is POSIX only")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Sofia"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def local(y, mo, d, h, mi, s=0) -> float:
    return time.mktime((y, mo, d, h, mi, s, 0, 0, -1))


# ── next_start and the small helpers ─────────────────────────
def test_next_start_today_or_tomorrow(tz):
    now = local(2026, 11, 17, 12, 0, 30)
    assert DL.next_start(now, "13:30") == local(2026, 11, 17, 13, 30)          # still ahead: today
    assert DL.next_start(now, "01:00") == local(2026, 11, 18, 1, 0)            # passed: tomorrow
    assert DL.next_start(now, "12:00") == local(2026, 11, 18, 12, 0)           # this minute already began
    assert DL.next_start(now, "12:01") == local(2026, 11, 17, 12, 1)
    assert DL.next_start(now, "0:00") == local(2026, 11, 18, 0, 0)
    assert DL.next_start(local(2026, 12, 31, 23, 30), "00:15") == local(2027, 1, 1, 0, 15)   # a new year
    assert DL.next_start(local(2026, 11, 17, 23, 59, 59), "23:59") == local(2026, 11, 18, 23, 59)
    for bad in ("", "25:00", "12:60", "noon", "1200", None):
        with pytest.raises(ValueError):
            DL.next_start(now, bad)


def test_next_start_across_summer_time(tz):
    # 28 → 29 March 2026: the clocks go 03:00 → 04:00 — from 23:00 to 05:00 is 6 h on the wall but 5 h real;
    # the moment is the one the clock shows, not «now + 6 h»
    now = local(2026, 3, 28, 23, 0)
    at = DL.next_start(now, "05:00")
    assert time.localtime(at)[:5] == (2026, 3, 29, 5, 0) and at - now == 5 * 3600
    now = local(2026, 10, 24, 23, 0)                          # 25 October: 04:00 → 03:00, an hour more
    at = DL.next_start(now, "05:00")
    assert time.localtime(at)[:5] == (2026, 10, 25, 5, 0) and at - now == 7 * 3600


def test_helpers():
    assert DL.parse_hhmm("1:05") == (1, 5) and DL.parse_hhmm(" 23.59 ") == (23, 59) and DL.parse_hhmm("24:00") is None
    assert DL.after_minutes(1000.7, 15) == 1900.0
    assert [DL.countdown(x) for x in (0, 0.2, 7, 65, 3600, 3723, -5)] == \
        ["0:00", "0:01", "0:07", "1:05", "1:00:00", "1:02:03", "0:00"]
    now = 1_000_000.0
    assert DL.state_on_open(now + 60, now, True) == now + 60
    assert DL.state_on_open(now + 60, now, False) == 0.0           # the queue does not come back: no waiting
    assert DL.state_on_open(now - 1, now, True) == 0.0             # passed while closed: dropped
    assert DL.state_on_open("junk", now, True) == 0.0 and DL.state_on_open(None, now, True) == 0.0


def test_config_keeps_and_cleans(data_dir):
    cfg = config.load()
    assert cfg["start_at"] == 0 and cfg["start_time"] == "01:00"
    config.save(dict(cfg, start_at=1234567890.5, start_time="02:30"))
    cfg = config.load()
    assert cfg["start_at"] == 1234567890.5 and cfg["start_time"] == "02:30"
    config.save(dict(cfg, start_at="soon", start_time="99:99"))
    cfg = config.load()
    assert cfg["start_at"] == 0 and cfg["start_time"] == "01:00"


# ── the delayed start in the window ──────────────────────────
def test_menu_button_countdown_start_stop(qapp, media, data_dir, tmp_path):
    from convert.i18n import t
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"), overwrite=True)
    j = _one(qapp, w, os.path.join(media, "Movie (2017).mkv"))
    menu = [a.text() for a in w.b_start_at.menu().actions() if a.text()]
    assert menu == [t("dl_at"), t("dl_in_min", 15), t("dl_in_min", 30), t("dl_in_h", 1), t("dl_in_h", 2),
                    t("dl_cancel")]
    assert not w.a_start_cancel.isEnabled() and not w.b_stop.isEnabled()
    w.a_start_in[1].trigger()                                       # «In 30 min»
    want = DL.clock(w.start_at)
    assert 30 * 60 - 2 <= w.start_at - time.time() <= 30 * 60
    assert w.b_start.text().strip() == t("dl_start_at", want) and w.b_stop.isEnabled() and w.a_start_cancel.isEnabled()
    pump(qapp, 1.2)
    assert w.dl_label.isVisible() and want in w.dl_label.text() and ("29:5" in w.dl_label.text()
                                                                     or "30:00" in w.dl_label.text())
    assert config.load()["start_at"] == w.start_at and not w.running and j.status == "wait"
    w.stop_queue()                                                  # «Stop» ends the waiting
    assert w.start_at == 0 and not w.dl_label.isVisible() and w.b_start.text().strip() == t("start")
    assert config.load()["start_at"] == 0
    w.a_start_in[0].trigger()                                       # «In 15 min» … then «Start»: at once
    assert w.start_at
    w.start_queue()
    assert w.start_at == 0 and w.running
    assert pump(qapp, 60, lambda: j.status in ("done", "error")), j.status
    assert j.status == "done" and os.path.isfile(j.out)
    assert pump(qapp, 10, lambda: not w.running and not w._any_running())
    w.a_start_in[0].trigger()
    w.a_start_cancel.trigger()                                      # the menu's «Cancel»
    assert w.start_at == 0 and t("dl_cancelled") in w.statusBar().currentMessage()
    w.close()


def test_the_queue_starts_at_the_moment(qapp, media, data_dir, tmp_path):
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"), overwrite=True)
    j = _one(qapp, w, os.path.join(media, "Movie (2017).mkv"))
    j.settings["video"].update(encoder="x264", speed=1, height=360)
    w.set_delayed(time.time() + 2.5)
    pump(qapp, 1.0)
    assert not w.running and j.status == "wait"
    # files coming meanwhile (a watch folder, Explorer's menu) wait for the moment too
    w._auto_start()
    assert not w.running and w.start_at
    assert pump(qapp, 10, lambda: w.running or j.status != "wait")
    assert w.start_at == 0 and config.load()["start_at"] == 0
    assert pump(qapp, 90, lambda: j.status in ("done", "error")) and j.status == "done", j.error
    assert wh(j.out) == (640, 360)
    w.close()


def test_the_moment_with_nothing_to_do(qapp, data_dir):
    from convert.i18n import t
    w = _win(qapp)
    w.set_delayed(time.time() + 1.2)
    assert pump(qapp, 5, lambda: w.start_at == 0)
    assert not w.running and t("dl_nothing") in w.statusBar().currentMessage()
    w.close()


def test_not_while_the_queue_runs(qapp, media, data_dir, tmp_path):
    from convert.i18n import t
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"), overwrite=True)
    j = _one(qapp, w, os.path.join(media, "Movie (2017).mkv"))
    j.settings["video"].update(encoder="x265", speed=1)
    w.start_queue()
    assert pump(qapp, 10, lambda: w._any_running())
    assert not w.b_start_at.isEnabled()
    w.start_in(15)
    assert w.start_at == 0 and t("dl_busy") in w.statusBar().currentMessage()
    w.stop_queue()
    pump(qapp, 3, lambda: not w._any_running())
    w.close()


def test_start_at_dialog(qapp, data_dir):
    from PySide6.QtCore import QTime, QTimer
    from PySide6.QtWidgets import QTimeEdit
    w = _win(qapp)
    seen = {}

    def answer():
        d = w._dl_dialog
        te = d.findChild(QTimeEdit)
        seen["first"] = te.time().toString("HH:mm")
        te.setTime(QTime(3, 45))
        d.accept()
    QTimer.singleShot(300, answer)
    w.start_at_dialog()
    assert seen["first"] == "01:00"                                       # the last time picked (default)
    assert time.localtime(w.start_at)[3:5] == (3, 45) and 0 < w.start_at - time.time() <= 24 * 3600
    assert w.b_start.text().strip().endswith("03:45") and config.load()["start_time"] == "03:45"
    w.cancel_delayed()
    w.close()


def test_waiting_survives_a_restart(qapp, media, data_dir, tmp_path):
    from convert.i18n import t
    out = str(tmp_path / "out")
    w = _win(qapp, keep_queue=True, out_mode="folder", out_dir=out)
    _one(qapp, w, os.path.join(media, "Movie (2017).mkv"))
    w.set_delayed(time.time() + 3600)
    at = w.start_at
    w.close()
    assert config.load()["start_at"] == at
    w2 = _win(qapp, keep_queue=True, out_mode="folder", out_dir=out)        # opened again
    w2.restore_queue()
    assert w2.start_at == at and len(w2.jobs) == 1 and not w2.running
    assert w2.b_start.text().strip() == t("dl_start_at", DL.clock(at))
    pump(qapp, 1.2)
    assert w2.dl_label.isVisible()
    w2.close()
    # the moment passed while the program was closed: no waiting, nothing started
    cfg = config.load()
    config.save(dict(cfg, start_at=time.time() - 60))
    w3 = _win(qapp, keep_queue=True, out_mode="folder", out_dir=out)
    w3.restore_queue()
    assert w3.start_at == 0 and config.load()["start_at"] == 0 and len(w3.jobs) == 1
    assert pump(qapp, 0.5) and not w3.running and w3.jobs[0].status in ("wait", "probe")
    assert w3.b_start.text().strip() == t("start") and not w3.dl_label.isVisible()
    w3.close()
    # the queue is not kept: neither is the waiting
    config.save(dict(config.load(), start_at=time.time() + 600))
    w4 = _win(qapp, keep_queue=False)
    w4.restore_queue()
    assert w4.start_at == 0 and config.load()["start_at"] == 0
    w4.close()


# ── the preview as the result will be ────────────────────────
def test_crop_helpers():
    assert mpvview.crop_rect((0, 104, 0, 104), 1920, 1080) == (0, 104, 1920, 872)
    assert mpvview.crop_rect((0, 0, 0, 0), 1920, 1080) is None and mpvview.crop_rect(None, 1920, 1080) is None
    assert mpvview.crop_spec((0, 104, 1920, 872)) == "1920x872+0+104" and mpvview.crop_spec(None) == ""
    assert mpvview.crop_filter((10, 104, 1900, 872)) == "crop=1900:872:10:104"


def test_older_libmpv_gets_a_crop_filter():
    """libmpv without video-crop: a vf crop, the decoding on the CPU while it is on, back to auto-safe after."""
    class Player(dict):
        def __setitem__(self, k, v):
            if k == "video-crop":
                raise AttributeError("mpv property does not exist")
            super().__setitem__(k, v)
    v = mpvview.MpvView.__new__(mpvview.MpvView)
    v.player, v.dead = Player(), False
    v.set_crop((0, 104, 1920, 872))
    assert v.crop_by == "vf" and v.player == {"vf": "crop=1920:872:0:104", "hwdec": "no"}
    v.set_crop(None)
    assert v.player == {"vf": "", "hwdec": "auto-safe"}

    class New(dict):
        pass
    n = mpvview.MpvView.__new__(mpvview.MpvView)
    n.player, n.dead = New(), False
    n.set_crop((0, 104, 1920, 872))
    assert n.crop_by == "video-crop" and n.player == {"video-crop": "1920x872+0+104"}
    n.set_crop(None)
    assert n.player["video-crop"] == ""


def test_still_frames_cut_too(qapp, wide):
    p = mpvview.FramePreview()
    p.resize(640, 360)
    p.show()
    p.load(wide, 2.0, 25.0)
    assert pump(qapp, 10, lambda: not p._pm.isNull())
    assert (p._pm.width(), p._pm.height()) == (640, 360)
    p.set_crop((0, 104, 1920, 872))
    assert pump(qapp, 10, lambda: p._pm.height() != 360)
    assert (p._pm.width(), p._pm.height()) == (640, 290)               # 1920×872 scaled to 640 wide
    p.unload()


def _shown(pv) -> tuple[int, int] | None:
    try:
        o = pv.player.video_out_params
        return (o["dw"], o["dh"]) if o else None
    except Exception:
        return None


def test_show_result_auto_and_by_hand(qapp, wide, data_dir, tmp_path):
    from convert.i18n import t
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"), overwrite=True)
    j = _one(qapp, w, wide)
    j.settings["video"].update(encoder="x264", speed=1, height=0)
    w._setf("crop", "on", True)
    w._setf("crop", "mode", "auto")
    w._filters_changed()
    for tab, shown in (("streams", True), ("trim", True), ("info", False), ("status", False)):
        w.show_side(tab)
        pump(qapp, 0.1)
        assert w.view_bar.isVisible() is shown, tab
        assert w.b_as_result.isVisible() is shown
    w.show_side("streams")
    assert pump(qapp, 30, lambda: j.crop_found is not None) and j.crop_found == [0, 104, 0, 104]
    tr = w.trim_tab
    assert pump(qapp, 15, lambda: tr.preview is not None and tr.preview.path)
    assert w.frame_ov.crop == (0, 104, 0, 104)                           # the dashed frame, the whole picture
    w.b_as_result.setChecked(True)                                       # «Show the result»
    assert tr.view_crop == (0, 104, 1920, 872) and tr.preview.crop == (0, 104, 1920, 872)
    assert w.frame_ov.crop is None and w.frame_ov.src[:2] == (1920, 872)
    assert w.cv_note.text() == t("cv_frame", 1920, 872, 1920, 1080)
    with_mpv = isinstance(tr.preview, mpvview.MpvView)
    if with_mpv:
        assert tr.preview.crop_by in ("video-crop", "vf")
        assert pump(qapp, 15, lambda: _shown(tr.preview) == (1920, 872)), _shown(tr.preview)
        shot = str(tmp_path / "shot.png")
        tr.preview.player.command("screenshot-to-file", shot, "video")
        assert wh(shot) == (1920, 872)
    # the same numbers go to ffmpeg: the result has the size the preview shows
    assert F.crop_values(j) == (0, 104, 0, 104)
    w.start_queue()
    assert pump(qapp, 90, lambda: j.status in ("done", "error")) and j.status == "done", j.error
    assert wh(j.out) == (1920, 872)
    assert pump(qapp, 10, lambda: not w.running and not w._any_running())
    w.show_side("streams")                                               # the start went to the Status tab
    # by hand: other numbers, the preview follows
    w._setf("crop", "mode", "manual")
    for k, v in (("l", 240), ("t", 140), ("r", 240), ("b", 140)):
        w._setf("crop", k, v)
    w._filters_changed()
    assert tr.preview.crop == (240, 140, 1440, 800)
    assert w.cv_note.text() == t("cv_frame", 1440, 800, 1920, 1080)
    if with_mpv:
        assert pump(qapp, 15, lambda: _shown(tr.preview) == (1440, 800)), _shown(tr.preview)
    # crop off: the whole frame, said so
    w._setf("crop", "on", False)
    w._filters_changed()
    assert tr.preview.crop is None and w.cv_note.text() == t("cv_crop_off")
    if with_mpv:
        assert pump(qapp, 15, lambda: _shown(tr.preview) == (1920, 1080)), _shown(tr.preview)
    # the button off: the whole frame with the dashed frame again
    w._setf("crop", "on", True)
    w._filters_changed()
    w.b_as_result.setChecked(False)
    assert tr.preview.crop is None and w.frame_ov.crop == (240, 140, 240, 140) and w.cv_note.text() == ""
    if with_mpv:
        assert pump(qapp, 15, lambda: _shown(tr.preview) == (1920, 1080))
    w.close()


def test_logo_drawn_on_the_whole_frame(qapp, wide, data_dir):
    w = _win(qapp)
    _one(qapp, w, wide)
    w._setf("crop", "on", True)
    w._setf("crop", "mode", "manual")
    w._setf("crop", "t", 104)
    w._setf("crop", "b", 104)
    w._filters_changed()
    w.show_side("streams")
    w.b_as_result.setChecked(True)
    assert w.trim_tab.view_crop == (0, 104, 1920, 872)
    w._setf("delogo", "on", True)
    for k, v in (("x", 100), ("y", 200), ("w", 50), ("h", 40)):
        w._setf("delogo", k, v)
    w._filters_changed()
    assert w.frame_ov.logo == (100, 96, 50, 40)                          # where it is in the cut picture
    w._filter_action("delogo", "draw")                                   # drawing: on the whole frame again
    assert not w.b_as_result.isChecked() and w.trim_tab.view_crop is None and w.frame_ov.drawing
    assert w.frame_ov.src[:2] == (1920, 1080)
    w.frame_ov.stop_draw()
    w.close()


def test_strings_both_languages():
    from convert import i18n
    keys = [k for k in i18n.STRINGS if k.startswith(("dl_", "cv_"))]
    assert len(keys) >= 24
    for k in keys:
        ru, en = i18n.STRINGS[k]
        assert ru and en and ru != en


def test_version_210():
    from convert import VERSION, VERSION_TUPLE
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert VERSION == "2.1.0" and VERSION_TUPLE == (2, 1, 0, 0)
    assert '#define AppVersion   "2.1.0"' in open(os.path.join(root, "setup.iss"), encoding="utf-8-sig").read()
    vi = open(os.path.join(root, "version_info.txt"), encoding="utf-8").read()
    assert "(2, 1, 0, 0)" in vi and "'2.1.0'" in vi
    readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
    assert "2.1" in readme and "XviD" + "4PSP" not in readme


# ── 2.1: «Fill the frame (cut the edges)» ────────────────────
@pytest.fixture(scope="module")
def fills(tmp_path_factory):
    """1920×872 (2.20), and 1440×1080 (4:3) whose top / bottom 135 rows are red, the 16:9 middle green."""
    d = tmp_path_factory.mktemp("fill21")
    scope = str(d / "scope.mkv")
    mk("-f", "lavfi", "-i", "testsrc2=s=1920x872:r=25:d=1", "-c:v", "libx264", "-preset", "ultrafast", scope)
    tv = str(d / "tv43.mkv")
    mk("-f", "lavfi", "-i", "color=red:s=1440x1080:r=25:d=1", "-f", "lavfi", "-i", "color=0x00c000:s=1440x810:r=25:d=1",
       "-filter_complex", "[0][1]overlay=0:135", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
       "-crf", "12", tv)
    sides = str(d / "sides.mkv")                       # 1920×872 with red 185-pixel edges: what 16:9 cuts away
    mk("-f", "lavfi", "-i", "color=red:s=1920x872:r=25:d=1", "-f", "lavfi", "-i", "color=0x00c000:s=1550x872:r=25:d=1",
       "-filter_complex", "[0][1]overlay=185:0", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", sides)
    return {"scope": scope, "tv": tv, "sides": sides}


def _job(path, **video):
    from convert import probe as P
    j = J.Job(src=path, settings=J.merged({"container": "mkv", "video": dict({"encoder": "x264", "speed": 1}, **video),
                                           "audio": {"codec": "copy"}}))
    j.info = P.probe(path)
    return j


def _encode(j, out, av):
    args = J.build_args(j, "ffmpeg", out, av, False)
    r = subprocess.run(args, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]
    return out


def rgb_at(path: str, x: int, y: int) -> tuple[int, int, int]:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-frames:v", "1", "-vf",
                        f"crop=4:4:{x}:{y},scale=1:1:flags=area,format=rgb24", "-f", "rawvideo", "-"],
                       capture_output=True, check=True)
    return tuple(r.stdout[:3])


def red(c) -> bool:
    return c[0] > 150 and c[1] < 80


def green(c) -> bool:
    return c[1] > 120 and c[0] < 80


def test_fill_1920x872_into_1080p(fills, av, tmp_path):
    j = _job(fills["scope"], width=1920, height=1080, fit="fill", upscale=True)
    assert J.out_size(j) == (1920, 1080)
    assert J.scale_filters(j) == ["scale=2378:1080:flags=lanczos", "setsar=1", "crop=1920:1080:228:0", "setsar=1"]
    out = _encode(j, str(tmp_path / "fill1080.mkv"), av)
    assert wh(out) == (1920, 1080)                                       # exactly the frame, no bars
    # without upscaling: no scaling at all, only cut to 16:9 at the source's size
    j = _job(fills["scope"], width=1920, height=1080, fit="fill", upscale=False)
    assert J.out_size(j) == (1550, 872) and J.scale_filters(j) == ["crop=1550:872:184:0", "setsar=1"]
    assert wh(_encode(j, str(tmp_path / "fill_noup.mkv"), av)) == (1550, 872)


def test_16x9_without_bars_cuts_the_sides(fills, av, tmp_path):
    j = _job(fills["sides"], width=0, height=0, fit="fill")
    fp = J.fill_plan(j)
    assert fp["lr"] == 185 and fp["tb"] == 0 and fp["scale"] is None
    out = _encode(j, str(tmp_path / "169.mkv"), av)
    w, h = wh(out)
    assert h == 872 and abs(w - h * 16 / 9) <= 1
    for x, y in ((0, 0), (w - 4, 0), (0, h - 4), (w - 4, h - 4), (w // 2, h // 2)):
        assert green(rgb_at(out, x, y)), (x, y, rgb_at(out, x, y))      # the red edges are gone, nothing else
    from convert.window import fill_note
    assert fill_note(fp).strip() == "Отрезается по краям: 185 + 185 пикселей (19 % ширины)"


def test_4x3_cuts_top_and_bottom(fills, av, tmp_path):
    from convert.window import fill_note
    j = _job(fills["tv"], width=0, height=0, fit="fill")
    fp = J.fill_plan(j)
    assert fp["tb"] == 135 and fp["lr"] == 0 and J.out_size(j) == (1440, 810)
    assert fill_note(fp).strip() == "Отрезается сверху и снизу: 135 + 135 пикселей (25 % высоты)"
    out = _encode(j, str(tmp_path / "tv169.mkv"), av)
    assert wh(out) == (1440, 810)
    for x, y in ((0, 0), (1436, 0), (0, 806), (1436, 806)):
        assert green(rgb_at(out, x, y)), (x, y)
    src = rgb_at(fills["tv"], 0, 0)
    assert red(src)                                                      # the source has them
    j = _job(fills["tv"], width=1280, height=720, fit="fill")            # into 720p: scaled down, then cut
    assert J.scale_filters(j) == ["scale=1280:960:flags=lanczos", "setsar=1", "crop=1280:720:0:120", "setsar=1"]
    out = _encode(j, str(tmp_path / "tv720.mkv"), av)
    assert wh(out) == (1280, 720) and green(rgb_at(out, 0, 4)) and green(rgb_at(out, 1276, 710))   # (scaled: the
    # very edge rows may take a hint of the cut-off red through the lanczos taps)


def test_fill_after_auto_crop(wide, av, tmp_path):
    j = _job(wide, width=0, height=0, fit="fill")
    j.settings["filters"]["crop"] = {"on": True, "mode": "auto"}
    j.crop_found = list(F.detect_crop("ffmpeg", wide, 2.0, 1920, 1080))
    assert j.crop_found == [0, 104, 0, 104]
    assert J.final_crop(j) == (184, 104, 186, 104)                       # even offsets (4:2:0), 1550 kept
    out = _encode(j, str(tmp_path / "wide169.mkv"), av)
    w, h = wh(out)
    assert h == 872 and abs(w - h * 16 / 9) <= 1
    top = subprocess.run(["ffmpeg", "-v", "error", "-i", out, "-frames:v", "1", "-vf",
                          "crop=iw:2:0:0,format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    assert max(top) > 60                                                 # the first row is picture, not a black bar
    from convert import beforeafter as BA
    vf = BA.source_vf(j, w, h)                                           # the curtain cuts the source the same way
    assert "crop=1920:872:0:104" in vf and "crop=1550:872:184:0" in vf


def test_fill_in_the_window(qapp, wide, data_dir, tmp_path):
    from convert.i18n import t
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"), overwrite=True)
    j = _one(qapp, w, wide)
    j.settings["video"].update(encoder="x264", speed=1)
    assert w.v_fit.findData("fill") >= 0 and w.v_fit.itemText(w.v_fit.findData("fill")) == t("fit_fill")
    i = w.v_res.findData("fill169")
    assert i == 1 and w.v_res.itemText(i) == t("res_fill169")
    w.v_res.setCurrentIndex(i)
    v = j.settings["video"]
    assert v["fit"] == "fill" and v["width"] == 0 and v["height"] == 0
    assert w.v_res.currentData() == "fill169" and w.v_fit.currentData() == "fill"
    w._setf("crop", "on", True)
    w._setf("crop", "mode", "auto")
    w.show_side("streams")
    assert pump(qapp, 30, lambda: j.crop_found is not None)
    pump(qapp, 0.3)
    assert w.frame_ov.crop == (184, 104, 186, 104)                       # the frame: Crop + Fill
    assert "185 + 185" in w.v_res_hint.text() and "1550×872" in w.v_res_hint.text()
    w.b_as_result.setChecked(True)
    tr = w.trim_tab
    assert pump(qapp, 15, lambda: tr.preview is not None and tr.preview.path)
    assert tr.preview.crop == (184, 104, 1550, 872) and w.cv_note.text() == t("cv_frame", 1550, 872, 1920, 1080)
    if isinstance(tr.preview, mpvview.MpvView):
        assert pump(qapp, 15, lambda: _shown(tr.preview) == (1550, 872)), _shown(tr.preview)
    w.start_queue()
    assert pump(qapp, 90, lambda: j.status in ("done", "error")) and j.status == "done", j.error
    assert wh(j.out) == (1550, 872)
    assert pump(qapp, 10, lambda: not w.running and not w._any_running())
    w.v_res.setCurrentIndex(w.v_res.findData("0x0"))                     # «As the source»: nothing cut again
    assert v["fit"] == "fit" and J.fill_plan(j) is None and tr.preview.crop == (0, 104, 1920, 872)
    w.b_as_result.setChecked(False)
    assert w.frame_ov.crop == (0, 104, 0, 104)
    w.close()


# ── «Fill» never stretches: one scale for both sides, square pixels, a circle stays a circle ──
CIRCLE = "geq=lum='if(lt(hypot(X-{cx}\\,Y-{cy})\\,{r})\\,235\\,90)':cb=128:cr=128"


def _circle(path, w, h, r, extra=""):
    """A white circle of radius r in the middle of a grey w×h frame (square pixels; grey, so the bars around it are
    the only black the crop finds); extra: filters after it."""
    vf = CIRCLE.format(cx=w // 2, cy=h // 2, r=r) + (("," + extra) if extra else "")
    mk("-f", "lavfi", "-i", f"color=black:s={w}x{h}:r=25:d=1", "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
       "-crf", "10", "-pix_fmt", "yuv420p", path)
    return path


def probe_v(path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,sample_aspect_ratio,display_aspect_ratio", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["streams"][0]


def circle_box(path) -> tuple[int, int]:
    """Width and height of the bright circle in the first frame, in pixels of the stored picture."""
    s = probe_v(path)
    w, h = s["width"], s["height"]
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-frames:v", "1", "-vf", "format=gray", "-f", "rawvideo",
                          "-"], capture_output=True, check=True).stdout
    rows = [y for y in range(h) if max(raw[y * w:(y + 1) * w]) > 128]
    cols = [x for x in range(w) if max(raw[x::w][:h]) > 128]
    return (cols[-1] - cols[0] + 1, rows[-1] - rows[0] + 1) if rows and cols else (0, 0)


def _round(path, sar_ok=True):
    s = probe_v(path)
    assert s.get("sample_aspect_ratio") in ("1:1", None) or not sar_ok, s
    assert s["sample_aspect_ratio"] == "1:1", s
    cw, ch = circle_box(path)
    assert cw > 100 and abs(cw - ch) <= 2, (cw, ch)                     # a circle, not an ellipse
    return cw, ch


@pytest.fixture(scope="module")
def circles(tmp_path_factory):
    d = tmp_path_factory.mktemp("circ21")
    return {
        "wide": _circle(str(d / "wide.mkv"), 1920, 872, 300),
        "tv": _circle(str(d / "tv.mkv"), 1440, 1080, 300),
        # the same 2.20 picture with black bars in a 1080p frame (auto crop first)
        "bars": _circle(str(d / "bars.mkv"), 1920, 872, 300, "pad=1920:1080:0:104:black"),
        # a DVD (4:3, 720×576 stored, SAR 16:15): round on the screen (768×576), squeezed in the stored pixels
        "dvd": _circle(str(d / "dvd.mkv"), 768, 576, 200, "scale=720:576:flags=lanczos,setsar=16/15"),
    }


@pytest.mark.parametrize("name,video,size", [
    ("wide", {"width": 1920, "height": 1080, "fit": "fill", "upscale": True}, (1920, 1080)),
    ("wide", {"width": 1920, "height": 1080, "fit": "fill"}, (1550, 872)),
    ("wide", {"width": 0, "height": 0, "fit": "fill"}, (1550, 872)),
    ("wide", {"width": 1280, "height": 720, "fit": "fill"}, (1280, 720)),
    ("tv", {"width": 0, "height": 0, "fit": "fill"}, (1440, 810)),
    ("tv", {"width": 1920, "height": 1080, "fit": "fill", "upscale": True}, (1920, 1080)),
    ("dvd", {"width": 0, "height": 0, "fit": "fill"}, (768, 432)),
    ("dvd", {"width": 1280, "height": 720, "fit": "fill", "upscale": True}, (1280, 720)),
])
def test_fill_keeps_the_shape(circles, av, tmp_path, name, video, size):
    j = _job(circles[name], **video)
    assert J.out_size(j) == size
    out = _encode(j, str(tmp_path / f"{name}_{size[0]}.mkv"), av)
    s = probe_v(out)
    assert (s["width"], s["height"]) == size and s["sample_aspect_ratio"] == "1:1", s
    _round(out)


def test_dvd_source_is_squeezed_but_the_result_round(circles):
    s = probe_v(circles["dvd"])
    assert s["sample_aspect_ratio"] == "16:15"
    cw, ch = circle_box(circles["dvd"])
    assert cw < ch - 10                                                  # stored pixels: narrower than tall


def test_fill_after_auto_crop_keeps_the_shape(circles, av, tmp_path):
    j = _job(circles["bars"], width=0, height=0, fit="fill")
    j.settings["filters"]["crop"] = {"on": True, "mode": "auto"}
    j.crop_found = list(F.detect_crop("ffmpeg", circles["bars"], 1.0, 1920, 1080))
    assert j.crop_found == [0, 104, 0, 104]
    out = _encode(j, str(tmp_path / "bars169.mkv"), av)
    s = probe_v(out)
    assert (s["width"], s["height"]) == (1550, 872) and s["sample_aspect_ratio"] == "1:1"
    assert _round(out) == circle_box(circles["wide"])                    # the very circle of the source, 1:1
