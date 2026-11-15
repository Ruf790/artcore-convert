"""1.2: the layout — task cards, the side tabs, the Streams tab (video copy / off, every track's own
codec and volume, the stream choice), Save as, the format / history windows, the strip under the picture."""
import os

from conftest import pump
from PySide6.QtWidgets import QCheckBox, QComboBox

from convert import config, logs
from convert import job as J
from convert import probe as P
from convert import ui_main


def _win(qapp, **over):
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False, keep_queue=False)
    cfg.update(over)
    w = MainWindow(cfg)
    w.resize(1440, 860)
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


def _block(w, idx):
    """The Streams tab's widgets of one track: (keep checkbox, language box, [the combos of its body],
    [its checkboxes]) — as the tab shows them now."""
    from conftest import pump as _p
    from PySide6.QtWidgets import QApplication
    _p(QApplication.instance(), 0.05)
    lay = w.tracks_lay
    ws = [lay.itemAt(i).widget() for i in range(lay.count())]
    i = next(k for k, x in enumerate(ws) if x.property("stream") == idx)
    head, body = ws[i], ws[i + 1]
    return head.findChild(QCheckBox), head.findChild(QComboBox), body.findChildren(QComboBox), \
        body.findChildren(QCheckBox)


def test_layout_and_streams(qapp, media, data_dir, tmp_path):
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"))
    assert [w.side_bar.tabText(i) for i in range(w.side_bar.count())] == \
        ["Инфо", "Метаданные", "Главы", "Обрезка", "Потоки", "Статус"]
    assert w.side_bar.currentIndex() == 4 and w.center.currentIndex() == 0         # Streams + the picture
    assert w.empty.isVisible()
    j = _one(qapp, w, os.path.join(media, "Movie (2017).mkv"))
    assert not w.empty.isVisible()
    from PySide6.QtWidgets import QStyleOptionViewItem
    assert w.view.itemDelegate().sizeHint(QStyleOptionViewItem(), None).height() == ui_main.TaskCard.H
    w.view.grab()                                                                  # the card paints
    assert pump(qapp, 20, lambda: w.trim_tab.bar.frames and w.trim_tab.bar.peaks)  # strip + loudness
    assert len(w.trim_tab.bar.frames) == 8
    assert "H.264" in w.v_head_label.text() and w.o_path.text().endswith("Movie (2017)_convert.mkv")
    # video: copy from the codec list, then off
    w.v_enc.setCurrentIndex(w.v_enc.findData("copy"))
    assert j.settings["video"]["mode"] == "copy" and not w.v_res.isEnabled()
    w.v_enc.setCurrentIndex(w.v_enc.findData("x264"))
    assert j.settings["video"]["mode"] == "encode" and j.settings["video"]["encoder"] == "x264"
    w.v_rc.setCurrentIndex(w.v_rc.findData("bitrate"))
    w.v_val.setValue(3000)
    assert j.settings["video"]["rc"] == "bitrate" and j.settings["video"]["bitrate"] == 3000 and w.rc_b.isChecked()
    w.v_rc.setCurrentIndex(w.v_rc.findData("quality"))
    w.v_val.setValue(30)
    assert j.settings["video"]["quality"] == 30 and w.v_q.value() == 30
    w.v_speed_box.setCurrentIndex(w.v_speed_box.findData(1))
    assert j.settings["video"]["speed"] == 1 and w.v_speed.value() == 1
    w.v_on.setChecked(False)
    assert j.settings["video"]["mode"] == "none" and not J.plan(j)["video"]
    w.v_on.setChecked(True)
    assert j.settings["video"]["mode"] == "encode"
    # the English track: its own codec, bit rate and loudness; the Russian one renamed in language
    keep, lang, combos, checks = _block(w, 2)
    codec = next(c for c in combos if c.objectName() == "trackCodec")
    codec.setCurrentIndex(codec.findData("opus"))
    keep, lang, combos, checks = _block(w, 2)
    kb = next(c for c in combos if c.findData(96) >= 0)
    kb.setCurrentIndex(kb.findData(96))
    keep, lang, combos, checks = _block(w, 2)
    checks[0].setChecked(True)
    assert j.tracks["2"] == {"codec": "opus", "keep": True, "kbps": 96, "normalize": True}
    _k, lang, _c, _ch = _block(w, 1)
    lang.setCurrentIndex(lang.findData("ukr"))
    assert j.meta["streams"]["1"]["lang"] == "ukr"
    # the subtitles off by their checkbox
    keep, *_ = _block(w, 3)
    keep.setChecked(False)
    assert j.tracks["3"]["keep"] is False and not J.plan(j)["subs"]
    # «Select streams»: only English
    w.s_lang.setCurrentIndex(w.s_lang.findData("eng"))
    assert j.settings["lang"] == "eng"
    w.s_lang.setCurrentIndex(w.s_lang.findData(""))
    # Save as: its own path (the extension follows the container)
    w.o_path.setText(str(tmp_path / "mine" / "Film.mp4"))
    w.o_path.editingFinished.emit()
    assert j.dest.endswith("Film.mp4") and J.out_name(j, w.cfg) == str(tmp_path / "mine" / "Film.mkv")
    os.makedirs(tmp_path / "mine", exist_ok=True)
    w.start_queue()
    assert w.side_bar.currentIndex() == 5 and w.center.currentIndex() == 1          # Status: the log in the middle
    assert pump(qapp, 120, lambda: not w.running)
    assert j.status == "done", j.error
    info = P.probe(j.out)
    a = info.of("audio")
    assert j.out == str(tmp_path / "mine" / "Film.mkv") and info.video.codec == "h264"
    assert [(x.codec, x.lang) for x in a] == [("ac3", "ukr"), ("opus", "eng")] and not info.of("subtitle")
    w.show_side("streams")
    assert w.center.currentIndex() == 0
    w.close()


def test_windows_open(qapp, media, data_dir, monkeypatch):
    shown = []
    monkeypatch.setattr(ui_main.SmallDialog, "exec", lambda self: shown.append(self.windowTitle()) or 0)
    logs.save("/x/a.mkv", "/x/a_convert.mkv", "done", "", 3.0, 10, "HEVC", "ffmpeg -i a", ["ok"])
    w = _win(qapp)
    _one(qapp, w, os.path.join(media, "dts.mkv"))
    w.format_dialog()
    d = w.history_dialog()                                  # 1.7: the log window, not modal
    assert d.isVisible() and d.hist.topLevelItemCount() == 1 and "Source:" in d.view.toPlainText()
    d.close()
    w.codec_dialog.exec()
    w.res_dialog.exec()
    assert shown[:1] == ["MKV (Matroska)"] and len(shown) == 3
    monkeypatch.setattr("convert.window.QFileDialog.getSaveFileName", lambda *a, **k: ("/tmp/zz/out.mkv", ""))
    w.save_as()
    assert w.jobs[0].dest == "/tmp/zz/out.mkv"
    w._toggle_tasks()
    assert not w.split.widget(0).isVisible()
    w._toggle_tasks()
    w.close()
