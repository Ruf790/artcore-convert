"""1.3 in the window: the filter rows of the Streams tab, their ⚙ windows, the crop frame and the logo drawn
with the mouse over the picture, the sound filters of a track, the Volume shown on the loudness curve, and a
queue run that waits for the automatic crop."""
import os
import subprocess

import pytest

from conftest import pump
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox

from convert import config
from convert import filters as F
from convert import probe as P


@pytest.fixture(scope="module")
def letterbox(tmp_path_factory):
    out = str(tmp_path_factory.mktemp("lb") / "Film 2.20.mkv")
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x872:r=24:d=2",
                    "-f", "lavfi", "-i", "sine=f=440:d=2", "-vf", "pad=1920:1080:0:104:black", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", out], check=True)
    return out


def _win(qapp, **over):
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False)
    cfg.update(over)
    w = MainWindow(cfg)
    w.resize(1500, 950)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    return w


def _pick(qapp, w, path):
    w.add_paths([path])
    j = next(x for x in w.jobs if x.src == path)
    assert pump(qapp, 20, lambda: j.info is not None)
    w.view.clearSelection()
    w.view.setCurrentItem(w.items[j.uid])
    qapp.processEvents()
    return j


def test_filter_rows(qapp, letterbox, data_dir, tmp_path):
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"), overwrite=True, keep_queue=False)
    j = _pick(qapp, w, letterbox)
    w.show_side("streams")
    rows = w.f_rows
    assert list(rows.checks) == list(F.VIDEO_ROWS)
    txt = rows.values["interlace"].text().lower()
    assert rows.checks["interlace"].isChecked() and ("прогрессив" in txt or "progressive" in txt)
    assert not rows.checks["crop"].isChecked()
    # Crop: on → the bars are looked for in the background → the frame on the picture
    rows.checks["crop"].click()
    assert j.settings["filters"]["crop"]["on"] is True
    assert pump(qapp, 30, lambda: j.crop_found is not None)
    assert j.crop_found == [0, 104, 0, 104]
    assert "1920×872" in rows.values["crop"].text()
    assert w.frame_ov.crop == (0, 104, 0, 104) and w.frame_ov.isVisible()
    vr = w.frame_ov.video_rect()
    assert vr.width() > 0 and abs(vr.width() / vr.height() - 1920 / 1080) < 0.01
    # its ⚙ window: by hand
    w._filter_gear("crop")
    d = w.f_dialogs["crop"]
    assert d.isVisible() and "1920×872" in d.note.text()
    d.w["mode"].setCurrentIndex(d.w["mode"].findData("manual"))
    assert j.settings["filters"]["crop"]["mode"] == "manual" and d.w["t"].isEnabled()
    d.w["t"].setValue(100)
    d.w["b"].setValue(100)
    assert j.settings["filters"]["crop"]["t"] == 100 and w.frame_ov.crop == (0, 100, 0, 100)
    d.close()
    # Interlace off / on goes through video.deint (as in 1.0)
    rows.checks["interlace"].click()
    assert j.settings["video"]["deint"] == "off" and not rows.checks["interlace"].isChecked()
    rows.checks["interlace"].click()
    assert j.settings["video"]["deint"] == "auto"
    # Rotation: switching on opens its window (no angle yet)
    rows.checks["rotate"].click()
    assert w.f_dialogs["rotate"].isVisible()
    dr = w.f_dialogs["rotate"]
    dr.w["angle"].setCurrentIndex(dr.w["angle"].findData(90))
    assert j.settings["filters"]["rotate"] == {"on": True, "angle": 90, "hflip": False, "vflip": False}
    assert rows.values["rotate"].text() == "90°"
    dr.close()
    # Framerate: the rate is video.fps
    w._filter_gear("fps")
    df = w.f_dialogs["fps"]
    df.w["video.fps"].setCurrentIndex(df.w["video.fps"].findData(30))
    df.w["mode"].setCurrentIndex(df.w["mode"].findData("cfr"))
    assert j.settings["video"]["fps"] == 30 and j.settings["filters"]["fps"]["mode"] == "cfr"
    assert rows.checks["fps"].isChecked() and "CFR" in rows.values["fps"].text()
    rows.checks["fps"].click()                                             # off: back to the source's rate
    assert j.settings["video"]["fps"] == 0 and j.settings["filters"]["fps"]["mode"] == "auto"
    df.close()
    # Pixel format carries the bit depth of 1.0
    w._filter_gear("pixfmt")
    dp = w.f_dialogs["pixfmt"]
    dp.w["video.bits"].setCurrentIndex(dp.w["video.bits"].findData("10"))
    assert j.settings["video"]["bits"] == "10" and rows.values["pixfmt"].text().startswith(("10 бит", "10 bit"))
    dp.w["chroma"].setCurrentIndex(dp.w["chroma"].findData("444"))
    assert not rows.checks["pixfmt"].isChecked()                         # the chroma needs the row switched on
    rows.checks["pixfmt"].click()
    assert j.settings["filters"]["pixfmt"] == {"on": True, "chroma": "444", "matrix": "auto", "range": "auto"}
    assert "4:4:4" in rows.values["pixfmt"].text()
    dp.close()
    # the info line of the queue / the Info tab follow
    assert j.status == "wait"
    w.close()


def test_delogo_drawn_with_the_mouse(qapp, letterbox, data_dir):
    from PySide6.QtTest import QTest
    w = _win(qapp)
    j = _pick(qapp, w, letterbox)
    w.show_side("streams")
    w.f_rows.checks["delogo"].click()                     # nothing drawn yet: the window + the drawing mode
    assert w.f_dialogs["delogo"].isVisible() and w.frame_ov.drawing
    ov = w.frame_ov
    assert ov.isVisible() and not ov.testAttribute(Qt.WA_TransparentForMouseEvents)
    vr = ov.video_rect()
    # drag from (10 %, 10 %) to (30 %, 20 %) of the picture
    a = QPoint(int(vr.left() + vr.width() * 0.1), int(vr.top() + vr.height() * 0.1))
    b = QPoint(int(vr.left() + vr.width() * 0.3), int(vr.top() + vr.height() * 0.2))
    QTest.mousePress(ov, Qt.LeftButton, Qt.NoModifier, a)
    QTest.mouseMove(ov, b)
    QTest.mouseRelease(ov, Qt.LeftButton, Qt.NoModifier, b)
    f = j.settings["filters"]["delogo"]
    assert f["on"] and abs(f["x"] - 192) <= 4 and abs(f["y"] - 108) <= 4
    assert abs(f["w"] - 384) <= 6 and abs(f["h"] - 108) <= 6
    assert not ov.drawing and ov.logo == (f["x"], f["y"], f["w"], f["h"])
    assert w.f_dialogs["delogo"].w["w"].value() == f["w"]                 # the window shows the numbers
    assert f"{f['w']}×{f['h']}" in w.f_rows.values["delogo"].text()
    w.close()


def test_audio_track_filters_and_gain_preview(qapp, letterbox, data_dir):
    w = _win(qapp)
    j = _pick(qapp, w, letterbox)
    w.show_side("streams")
    a = j.info.of("audio")[0]
    delay = w.tracks_box.findChild(QSpinBox, "trackDelay")
    gain = w.tracks_box.findChild(QDoubleSpinBox, "trackGain")
    assert delay is not None and gain is not None
    delay.setValue(250)
    assert j.tracks[str(a.index)]["delay"] == 250
    pump(qapp, 0.2)
    gain = w.tracks_box.findChild(QDoubleSpinBox, "trackGain")
    gain.setValue(6.0)
    assert j.tracks[str(a.index)]["gain"] == 6.0
    assert w.trim_tab.bar.gain_db == 6.0
    assert pump(qapp, 30, lambda: bool(w.trim_tab.bar.peaks))
    vals, clip = w.trim_tab.bar.shown_peaks()
    top = w.trim_tab.bar.top
    assert 0 < top < 1 and abs(max(vals) - min(1.0, top * 10 ** 0.3)) < 0.02
    # the common gain of every track; 0 on the track = as the common one
    pump(qapp, 0.2)
    w.tracks_box.findChild(QDoubleSpinBox, "trackGain").setValue(0.0)
    assert "gain" not in j.tracks[str(a.index)]
    w.a_gain.setValue(-3.0)
    assert j.settings["audio"]["gain"] == -3.0 and w.trim_tab.bar.gain_db == -3.0
    rate = w.a_rate
    rate.setCurrentIndex(rate.findData(44100))
    assert j.settings["audio"]["rate"] == 44100
    from convert import job as J
    assert J.plan(j)["audio"][0][1] != "copy"               # a filter: encoded
    w.close()


def test_queue_waits_for_auto_crop(qapp, letterbox, data_dir, tmp_path):
    """Start right after switching Crop on: the run waits for the bars, then crops."""
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"), overwrite=True, keep_queue=False)
    w.add_paths([letterbox])
    j = w.jobs[0]
    assert pump(qapp, 20, lambda: j.info is not None)
    j.settings["video"].update(encoder="x264", speed=1)
    j.settings["audio"]["codec"] = "copy"
    j.settings["filters"]["crop"]["on"] = True
    assert j.crop_found is None
    w.start_queue()
    assert pump(qapp, 120, lambda: j.status in ("done", "error"))
    assert j.status == "done", j.error
    v = P.probe(j.out).video
    assert (v.width, v.height) == (1920, 872)
    assert os.path.isfile(j.out)
    w.close()
