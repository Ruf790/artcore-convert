"""1.1 in the window: the container list, the Trim tab with the libmpv preview, Metadata, Chapters — and a task
with all of them run through the queue."""
import os

import pytest
from conftest import pump
from PySide6.QtCore import Qt

from convert import config
from convert import encoders as E
from convert import job as J
from convert import mpvview
from convert import probe as P


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


def test_containers_in_the_video_tab(qapp, media, data_dir):
    w = _win(qapp)
    j = _one(qapp, w, os.path.join(media, "Movie (2017).mkv"))
    ids = [w.v_cont.itemData(i) for i in range(w.v_cont.count()) if w.v_cont.itemData(i)]
    assert ids == [c.id for c in E.CONTAINERS]
    w.v_cont.setCurrentIndex(w.v_cont.findData("webm"))
    assert j.settings["video"]["encoder"] == "vp9" and w.v_enc.currentData() == "vp9"
    model = w.v_enc.model()
    x264 = model.item(w.v_enc.findData("x264"))
    assert not x264.isEnabled() and "WEBM" in x264.toolTip()
    aac = w.a_codec.model().item(w.a_codec.findData("aac"))
    assert not aac.isEnabled()
    assert "W" in w.items[j.uid].text(3).upper() and "Opus" not in w.items[j.uid].text(3)
    w.v_cont.setCurrentIndex(w.v_cont.findData("mp3"))
    assert not w.v_enc.isEnabled() and not w.v_mode.isEnabled()
    assert w.items[j.uid].text(3).endswith("MP3")
    w.v_cont.setCurrentIndex(w.v_cont.findData("mov"))
    w.v_enc.setCurrentIndex(w.v_enc.findData("prores"))
    assert not w.rc_b.isEnabled() and "HQ" in w.v_q_hint.text() and w.v_q.value() == 4
    assert "ProRes HQ" in w.items[j.uid].text(3)
    w.v_cont.setCurrentIndex(w.v_cont.findData("gif"))
    assert j.settings["video"]["encoder"] == "gif" and not w.v_q.isEnabled()
    w.close()


def test_trim_meta_chapters(qapp, media, data_dir, tmp_path):
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"))
    j = _one(qapp, w, os.path.join(media, "Movie (2017).mkv"))
    j.settings["video"].update(encoder="x264", speed=1, height=360)
    tr = w.trim_tab
    w.tabs.setCurrentWidget(tr)
    assert pump(qapp, 15, lambda: tr.preview is not None and tr.bar.dur > 0)
    if mpvview.load_mpv() is not None:
        assert isinstance(tr.preview, mpvview.MpvView), mpvview.mpv_error()
        assert pump(qapp, 15, lambda: tr.preview.path == j.src and tr.preview.dur > 2.5)
        tr._seek(1.0)
        assert pump(qapp, 10, lambda: abs(tr.preview.pos - 1.0) < 0.05)              # mpv got there
        tr._step(1)
        assert pump(qapp, 10, lambda: 1.02 < tr.bar.pos < 1.1)                    # one frame at 25 fps
        w.toggle_pause()                                                         # Space plays the preview here
        assert pump(qapp, 10, lambda: not tr.preview.is_paused)
        w.toggle_pause()
        assert pump(qapp, 10, lambda: tr.preview.is_paused)
    else:
        assert isinstance(tr.preview, mpvview.FramePreview)
    # a piece by the marks, the time line knows it
    tr.set_in(1.0)
    tr.set_out(2.0)
    tr.add_piece()
    assert j.trim == [[1.0, 2.0]] and tr.list.topLevelItemCount() == 1
    assert w.items[j.uid].text(3).startswith("✂") and "1.000" not in tr.summary.text()
    tr.r_cut.setChecked(True)                                                    # throw that second away
    assert j.trim_cut and J.segments(j) == [(0.0, 1.0), (2.0, 3.023)] or J.segments(j)[0] == (0.0, 1.0)
    assert len(tr.bar.keep) == 2 and "2" in tr.summary.text()
    # typed marks, a bad piece
    tr.e_in.setText("0:02.5")
    tr.e_out.setText("0:02.0")
    tr.add_piece()
    assert len(j.trim) == 1 and "⚠" in tr.summary.text()
    # metadata: the title, a sound track's language and default flag
    mt = w.meta_tab
    w.tabs.setCurrentIndex(w.tabs.indexOf(w.meta_tab.parentWidget().parentWidget()))
    title = next(mt.tags.topLevelItem(i) for i in range(mt.tags.topLevelItemCount())
                 if mt.tags.topLevelItem(i).text(0) == "title")
    assert title.text(1) == "Test movie"
    title.setText(1, "Фильм")
    assert j.meta["tags"]["title"] == "Фильм"
    rows = {mt.streams.topLevelItem(i).data(0, Qt.UserRole): mt.streams.topLevelItem(i)
            for i in range(mt.streams.topLevelItemCount())}
    assert set(rows) == {0, 1, 2}                                       # 2 pieces encoded: no subtitles
    rows[2].setCheckState(4, Qt.Checked)                                          # English becomes the default …
    assert j.meta["streams"]["2"]["default"] is True and j.meta["streams"]["1"]["default"] is False   # … Russian not
    rows[2].setText(3, "English")
    box = mt.streams.itemWidget(rows[1], 2)
    box.setCurrentIndex(box.findData("ukr"))
    assert j.meta["streams"]["1"]["lang"] == "ukr" and j.meta["streams"]["2"]["title"] == "English"
    # chapters: the source's «Two» (at 1 s) fell into the cut-out second → moved to its end (1.0 of the result)
    ch = w.chap_tab
    w.tabs.setCurrentWidget(ch)
    assert [ch.view.topLevelItem(i).text(2) for i in range(ch.view.topLevelItemCount())] == ["One", "Two"]
    assert ch.view.topLevelItem(1).text(1) == "0:01.000" and j.chapters is None
    ch.view.topLevelItem(1).setText(2, "Вторая")
    assert j.chapters == [[0.0, "One"], [1.0, "Вторая"]]
    ch.every.setValue(1)
    ch.auto()
    assert j.chapters == [[0.0, "Глава 1"]]                                       # a 2 s clip: one chapter
    ch.from_source()
    assert j.chapters is None
    ch.clear()
    assert j.chapters == []
    ch.add()
    assert len(j.chapters) == 1
    ch.view.topLevelItem(0).setText(1, "0:00.5")
    assert j.chapters == [[0.5, j.chapters[0][1]]]
    assert "✎" in w.i_text.text()
    # the task survives the queue file
    k = J.Job.from_dict(j.to_dict())
    assert (k.trim, k.trim_cut, k.meta, k.chapters) == (j.trim, j.trim_cut, j.meta, j.chapters)
    # and runs
    w.start_queue()
    assert pump(qapp, 120, lambda: not w.running)
    assert j.status == "done", j.error
    info = P.probe(j.out)
    assert abs(info.duration - 2.0) < 0.2 and info.tags.get("title") == "Фильм"
    a = info.of("audio")
    assert (a[0].lang, a[0].default, a[1].default, a[1].title) == ("ukr", False, True, "English")
    assert [round(c[0], 1) for c in info.chapter_list] == [0.5]
    # its log
    from convert import logs
    assert os.path.isfile(j.log_path) and os.path.dirname(j.log_path) == logs.logs_dir()
    text = open(j.log_path, encoding="utf-8").read()
    assert "Source:   " + j.src in text and "Status:   done" in text and "\n$ " in text and "concat" in text
    h = logs.history()[0]
    assert h["log"] == j.log_path and h["status"] == "done" and h["out"] == j.out
    w.close()


def test_resolution_and_logs(qapp, media, data_dir, tmp_path):
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out"), log_next_to_output=True)
    j = _one(qapp, w, os.path.join(media, "Movie (2017).mkv"))
    j.settings["video"].update(encoder="x264", speed=1)
    w.v_res.setCurrentIndex(w.v_res.findData("720x576"))
    assert (j.settings["video"]["width"], j.settings["video"]["height"]) == (720, 576)
    assert "720×406" in w.v_res_hint.text()                                   # fit keeps 16:9
    w.v_fit.setCurrentIndex(w.v_fit.findData("pad"))
    assert "720×576" in w.v_res_hint.text().split("→")[1]
    w.v_w.setValue(1000)                                                       # an own size
    w.v_hh.setValue(500)
    assert (j.settings["video"]["width"], j.settings["video"]["height"]) == (1000, 500)
    assert w.v_res.currentText() == "1000×500"
    w.v_res.setCurrentIndex(w.v_res.findData("custom"))
    assert w.v_res.currentData() == "1000x500"                                # «own size…» keeps the value
    assert "1000×500" in w.items[j.uid].text(3)
    w.start_queue()
    assert pump(qapp, 120, lambda: not w.running)
    assert j.status == "done", j.error
    v = P.probe(j.out).video
    assert (v.width, v.height) == (1000, 500)
    assert os.path.isfile(os.path.splitext(j.out)[0] + ".log")                 # the copy next to the result
    w.close()
    # logs off: none written
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path / "out2"), save_logs=False)
    j = _one(qapp, w, os.path.join(media, "dts.mkv"))
    j.settings["video"].update(encoder="x264", speed=1)
    w.start_queue()
    assert pump(qapp, 120, lambda: not w.running)
    assert j.status == "done" and not j.log_path
    w.close()


def test_frame_preview_without_mpv(qapp, media):
    p = mpvview.FramePreview()
    p.resize(320, 180)
    p.show()
    got = []
    p.position.connect(got.append)
    p.load(os.path.join(media, "Movie (2017).mkv"), 3.0, 25.0)
    assert pump(qapp, 10, lambda: not p._pm.isNull())
    p.step(5)
    assert abs(got[-1] - 0.2) < 0.001
    p.seek(9.0)
    assert got[-1] == 3.0                                     # never past the end
    p.unload()


def test_tabs_need_one_file(qapp, media, data_dir):
    w = _win(qapp)
    w.add_paths([os.path.join(media, "Movie (2017).mkv"), os.path.join(media, "dts.mkv")])
    assert pump(qapp, 10, lambda: all(j.status != "probe" for j in w.jobs))
    w.view.selectAll()
    pump(qapp, 0.2)
    assert w.trim_tab.job is None and not w.trim_tab.b_add.isEnabled() and not w.meta_tab.tags.isEnabled()
    w.view.clearSelection()
    w.items[w.jobs[1].uid].setSelected(True)
    pump(qapp, 0.2)
    assert w.trim_tab.job is w.jobs[1] and w.trim_tab.b_add.isEnabled()
    w.close()


@pytest.mark.parametrize("name", ["test.mp3"])
def test_audio_files_are_taken(qapp, media, data_dir, tmp_path, name):
    from conftest import ff
    src = str(tmp_path / name)
    ff("-f", "lavfi", "-i", "sine=f=440:d=1", src)
    w = _win(qapp)
    assert w.add_paths([str(tmp_path)]) == 1
    assert pump(qapp, 10, lambda: w.jobs[0].status == "wait")
    w.close()
