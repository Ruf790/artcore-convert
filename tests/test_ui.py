"""The window: queue, settings of the selected tasks, presets, tracks, running, saving the queue."""
import os

from conftest import pump
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from convert import config


def _win(qapp, **over):
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False)
    cfg.update(over)
    w = MainWindow(cfg)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    return w


def _close_popup_later(ms=300):
    def go():
        m = QApplication.activePopupWidget()
        if m is not None:
            m.close()
    QTimer.singleShot(ms, go)


def test_queue_and_settings(qapp, media, data_dir, tmp_path):
    w = _win(qapp)
    n = w.add_paths([media])                                   # a folder: every video in it
    assert n == 5 and w.add_paths([os.path.join(media, "hdr.mkv")]) == 0       # no duplicates
    assert pump(qapp, 10, lambda: all(j.status != "probe" for j in w.jobs))
    broken = next(j for j in w.jobs if j.name == "broken.mkv")
    assert broken.status == "error" and broken.error
    movie = next(j for j in w.jobs if j.name == "Movie (2017).mkv")
    w.view.clearSelection()
    w.items[movie.uid].setSelected(True)
    pump(qapp, 0.3)
    assert "Movie (2017).mkv" in w.i_name.text() and "Дубляж" in w.i_text.text()
    # a settings widget writes into the selected task only
    w.v_cont.setCurrentIndex(w.v_cont.findData("mp4"))
    w.a_codec.setCurrentIndex(w.a_codec.findData("ac3"))
    assert movie.settings["container"] == "mp4" and movie.settings["audio"]["codec"] == "ac3"
    others = [j for j in w.jobs if j is not movie]
    assert all(j.settings["container"] == "mkv" for j in others)
    assert "MP4" in w.items[movie.uid].text(3)
    # several selected: one change goes to all of them
    for j in others[:2]:
        w.items[j.uid].setSelected(True)
    pump(qapp, 0.2)
    w.v_h.setCurrentIndex(w.v_h.findData(720))
    assert all(j.settings["video"]["height"] == 720 for j in [movie] + others[:2])
    # a preset on the selection
    w.preset_box.setCurrentIndex(w.preset_box.findData("remux_mkv"))
    w._preset_picked()
    assert movie.settings["video"]["mode"] == "copy" and movie.preset == "remux_mkv"
    # nothing selected: the settings of new files
    w.view.clearSelection()
    pump(qapp, 0.2)
    assert "новых" in w.scope.text() or "new" in w.scope.text()
    w.v_q.setValue(19)
    assert w.cfg["defaults"]["video"]["quality"] == 19
    # tracks of one file
    w.view.clearSelection()
    w.items[movie.uid].setSelected(True)
    pump(qapp, 0.2)
    assert w.tr_view.topLevelItemCount() == 4                    # video, 2 audio, subtitles
    rus = w.tr_view.topLevelItem(1)
    rus.setCheckState(0, Qt.Unchecked)
    pump(qapp, 0.2)
    assert movie.tracks["1"]["keep"] is False
    # menus, the encoders dialog
    _close_popup_later()
    w._queue_menu(w.view.visualItemRect(w.items[movie.uid]).center())
    _close_popup_later()
    w._main_menu()
    _close_popup_later()
    w._preset_menu()
    from convert.dialogs import EncodersDialog, SettingsDialog
    EncodersDialog(w.av, True, w).set_available(w.av)
    SettingsDialog(w.cfg, w)
    # moving, removing
    first = w.jobs[0]
    w.view.clearSelection()
    w.items[first.uid].setSelected(True)
    w.move_selected(1)
    assert w.jobs[1] is first and w.items[first.uid].text(0) == "2"
    w.remove_selected()
    assert first not in w.jobs and len(w.jobs) == 4
    w.close()
    # the queue comes back
    w2 = _win(qapp)
    w2.restore_queue()
    assert len(w2.jobs) == 4
    m2 = next(j for j in w2.jobs if j.name == "Movie (2017).mkv")
    assert m2.settings["video"]["mode"] == "copy" and m2.tracks["1"]["keep"] is False
    w2.close()


def test_run_queue(qapp, media, data_dir, tmp_path):
    out = tmp_path / "out"
    w = _win(qapp, out_mode="folder", out_dir=str(out), keep_queue=False)
    w.add_paths([os.path.join(media, "Movie (2017).mkv"), os.path.join(media, "dts.mkv"),
                 os.path.join(media, "broken.mkv")])
    assert pump(qapp, 10, lambda: all(j.status != "probe" for j in w.jobs))
    for j in w.jobs[:2]:
        j.settings["video"].update(encoder="x264", speed=1)
    w.jobs[1].settings["container"] = "mp4"
    w.start_queue()
    assert pump(qapp, 3, lambda: w.current is not None)
    assert w.tabs.currentIndex() == 4 and w.st_name.text()
    assert pump(qapp, 120, lambda: not w.running)
    a, b, c = w.jobs
    assert (a.status, b.status, c.status) == ("done", "done", "error")
    assert os.path.isfile(a.out) and a.out.endswith("Movie (2017)_convert.mkv") and str(out) in a.out
    assert b.out.endswith(".mp4") and b.out_size > 0
    assert "✓" in w.log.toPlainText()
    w.view.clearSelection()
    w.items[a.uid].setSelected(True)
    pump(qapp, 0.2)
    assert "Movie (2017)_convert.mkv" in w.i_text.text()
    # again: the same names get « (2)»
    w.retry([a])
    assert a.status == "wait"
    w.start_queue()
    assert pump(qapp, 120, lambda: not w.running)
    assert a.status == "done" and a.out.endswith("Movie (2017)_convert (2).mkv")
    w.clear_done()
    assert [j.status for j in w.jobs] == ["error"]
    w.close()


def test_stop_and_pause(qapp, media, data_dir, tmp_path):
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "o"), keep_queue=False)
    w.add_paths([os.path.join(media, "Movie (2017).mkv")])
    assert pump(qapp, 10, lambda: w.jobs[0].status == "wait")
    w.jobs[0].settings["video"].update(encoder="x265", speed=7)
    w.start_queue()
    assert pump(qapp, 10, lambda: w.jobs[0].progress > 0.02)
    w.toggle_pause()
    assert w.runner.paused and w.jobs[0].status == "pause"
    w.toggle_pause()
    assert not w.runner.paused and w.jobs[0].status == "run"
    w.stop_queue()
    assert pump(qapp, 10, lambda: not w.runner.running)
    j = w.jobs[0]
    assert j.status == "stop" and not os.listdir(str(tmp_path / "o"))
    w.close()
