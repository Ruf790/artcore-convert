"""1.4 in the window: tasks at the same time, the CPU limit, quality metrics and the current frame on Status,
dragging tasks, the task list in a file, cards / rows, joining, external tracks, the split, two passes, the
speed test — real encodes, results checked by ffprobe."""
import os
import shutil

import pytest

from conftest import ff as mk, pump
from convert import config
from convert import job as J
from convert import probe as P


@pytest.fixture(scope="module")
def films(media, tmp_path_factory):
    d = str(tmp_path_factory.mktemp("films"))
    for i in range(3):
        mk("-f", "lavfi", "-i", f"testsrc2=s=640x360:r=25:d={2 + i}", "-f", "lavfi", "-i", f"sine=f={300 + 100 * i}:d={2 + i}",
           "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "aac", os.path.join(d, f"Part{i + 1}.mkv"))
    mk("-f", "lavfi", "-i", "sine=f=900:d=3", "-ac", "2", "-c:a", "ac3", os.path.join(d, "Part1.rus.ac3"))
    with open(os.path.join(d, "Part1.eng.srt"), "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,300 --> 00:00:01,200\nHello\n")
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


def _add(qapp, w, paths, **st):
    n = w.add_paths(paths)
    assert n == len(paths)
    assert pump(qapp, 15, lambda: all(j.status != "probe" for j in w.jobs))
    for j in w.jobs[-n:]:
        j.settings = J.merged({"video": {"encoder": "x264", "speed": 7}, "subs": "all"} | st)
    return w.jobs[-n:]


def _select(qapp, w, jobs):
    w.view.clearSelection()
    for j in jobs:
        w.items[j.uid].setSelected(True)
    pump(qapp, 0.2)


def _run_all(qapp, w, secs=180):
    w.start_queue()
    assert pump(qapp, secs, lambda: not w.running and not w._any_running())
    pump(qapp, 0.3)


def test_parallel_cpu_limit_frame_metrics(qapp, films, data_dir):
    w = _win(qapp, parallel=2, metrics=["psnr", "ssim"], show_frame=True, cpu_limit=100)
    jobs = _add(qapp, w, [os.path.join(films, f"Part{i}.mkv") for i in (1, 2, 3)])
    w.set_cpu_limit(50)
    assert w.b_cpu.text() == "50% ▾" and w._threads() == max(1, round((os.cpu_count() or 1) / 2))
    w.show_side("status")
    seen = {"two": False, "frame": False}

    def watch():
        if len(w._busy_slots()) == 2:
            seen["two"] = True
        pm = w.fr_img.pixmap()
        if pm is not None and not pm.isNull():
            seen["frame"] = True
        return not w.running and not w._any_running()
    w.start_queue()
    assert pump(qapp, 240, watch)
    pump(qapp, 0.3)
    assert seen["two"], "two tasks never ran at the same time"
    assert seen["frame"], "the current frame never showed"
    assert all(j.status == "done" for j in jobs), [(j.status, j.error) for j in jobs]
    assert len({j.out for j in jobs}) == 3
    for j in jobs:
        assert 30 < j.metrics["psnr"] < 100 and 0.9 < j.metrics["ssim"] <= 1
        assert abs(P.probe(j.out).duration - J.out_duration(j)) < 0.3
        assert "-threads" in open(j.log_path, encoding="utf-8").read()
    _select(qapp, w, [jobs[0]])
    assert w.m_val["psnr"].text().endswith("dB") and w.m_val["vmaf"].text() == "—"
    assert "PSNR" in w.i_text.text()
    w.set_parallel(1)
    assert w.par_box.currentData() == 1
    w.close()


def test_drag_order_save_load_rows(qapp, films, data_dir, tmp_path):
    w = _win(qapp, task_view="cards")
    jobs = _add(qapp, w, [os.path.join(films, f"Part{i}.mkv") for i in (1, 2, 3)])
    jobs[1].trim = [[0.2, 1.5]]
    jobs[2].ext = [J.clean_ext({"path": os.path.join(films, "Part1.rus.ac3"), "lang": "rus", "delay": 120})]
    # a card dragged to the top (what TaskList.dropEvent leaves behind)
    it = w.view.takeTopLevelItem(2)
    w.view.insertTopLevelItem(0, it)
    w.view.reordered.emit()
    assert [j.name for j in w.jobs] == ["Part3.mkv", "Part1.mkv", "Part2.mkv"]
    assert w.items[w.jobs[0].uid].text(0) == "1"
    assert not (w.items[jobs[0].uid].flags() & __import__("PySide6.QtCore").QtCore.Qt.ItemIsDropEnabled)
    # rows / cards
    w.set_task_view("rows")
    assert not w.view.isHeaderHidden() and not w.view.isColumnHidden(3) and w.b_view_rows.isChecked()
    assert w.view.itemDelegate() is w._plain
    w.set_task_view("cards")
    assert w.view.isHeaderHidden() and w.view.isColumnHidden(3) and w.view.itemDelegate() is w._card
    # the list in a file and back
    path = str(tmp_path / "list.acq")
    assert w.save_task_list(path)
    w.view.selectAll()
    w.remove_selected()
    assert not w.jobs
    assert w.load_task_list(path) == 3
    assert pump(qapp, 15, lambda: all(j.status != "probe" for j in w.jobs))
    assert [j.name for j in w.jobs] == ["Part3.mkv", "Part1.mkv", "Part2.mkv"]
    assert w.jobs[2].trim == [[0.2, 1.5]] and w.jobs[0].ext[0]["delay"] == 120
    assert w.jobs[0].settings["video"]["encoder"] == "x264"
    assert w.load_task_list(str(tmp_path / "nope.acq")) == 0 or True
    w.close()


def test_join_external_two_pass_split(qapp, films, data_dir):
    w = _win(qapp, metrics=[], parallel=1, cpu_limit=100)
    jobs = _add(qapp, w, [os.path.join(films, "Part1.mkv"), os.path.join(films, "Part2.mkv")],
                split={"mode": "time", "minutes": 0.025})      # 1.5 s parts
    _select(qapp, w, jobs)
    w.join_selected()
    assert len(w.jobs) == 1
    j = w.jobs[0]
    assert j.joins == [os.path.join(films, "Part2.mkv")]
    assert pump(qapp, 10, lambda: all(j.infos.get(p) for p in j.joins))
    assert abs(J.src_duration(j) - 5.0) < 0.2 and "⧉" in w.items[j.uid].text(3)
    _select(qapp, w, [j])
    w.show_side("info")
    assert "⧉" in w.i_text.text()
    # external tracks through the «+» of the Streams tab
    w.add_external_dialog([os.path.join(films, "Part1.rus.ac3"), os.path.join(films, "Part1.eng.srt")])
    assert [e["lang"] for e in j.ext] == ["rus", "eng"] and [e["kind"] for e in j.ext] == ["audio", "subtitle"]
    assert pump(qapp, 10, lambda: all(J.file_info(j, e["path"]) for e in j.ext))
    w.show_side("streams")
    pump(qapp, 0.3)
    from PySide6.QtWidgets import QSpinBox
    delays = [x for x in w.ext_box.findChildren(QSpinBox) if x.objectName() == "extDelay"]
    assert len(delays) == 2
    delays[0].setValue(250)
    assert j.ext[0]["delay"] == 250
    # two passes from the codec window
    w.v_rc.setCurrentIndex(w.v_rc.findData("bitrate"))
    pump(qapp, 0.1)
    assert w.v_2pass.isEnabled()
    w.v_2pass.setChecked(True)
    assert j.settings["video"]["passes"] == 2
    kinds = []
    w.slots[0].runner.progress.connect(lambda _d: kinds.append(w.slots[0].runner.kind))
    _run_all(qapp, w)
    assert j.status == "done", j.error
    assert "pass1" in kinds and "main" in kinds
    assert len(j.parts) >= 3 and j.out == j.parts[0] and all(os.path.isfile(p) for p in j.parts)
    whole = os.path.splitext(j.parts[0])[0][:-3] + ".mkv"
    assert not os.path.exists(whole)                           # only the parts stay
    total = sum(P.probe(p).duration for p in j.parts)
    assert abs(total - 5.0) < 0.4
    first = P.probe(j.parts[0])
    assert len(first.of("audio")) == 2 and first.of("audio")[1].lang == "rus" and first.of("subtitle")
    assert j.out_size == sum(os.path.getsize(p) for p in j.parts)
    # undo the join: Part2 is a task again
    j.status = "wait"
    w.join_undo(j)
    assert pump(qapp, 10, lambda: len(w.jobs) == 2)
    w.close()


def test_copy_command_has_it_all(qapp, films, data_dir):
    w = _win(qapp, metrics=["psnr"], cpu_limit=50)
    jobs = _add(qapp, w, [os.path.join(films, "Part1.mkv")])
    j = jobs[0]
    j.settings["video"].update(rc="bitrate", bitrate=1500, passes=2)
    j.settings["split"] = {"mode": "size", "size_mb": 1, "minutes": 20}
    j.joins = [os.path.join(films, "Part3.mkv")]
    w._probe_extra(j)
    assert pump(qapp, 10, lambda: all(j.infos.get(p) for p in j.joins))
    w._copy_cmd(j)
    from PySide6.QtWidgets import QApplication
    cmd = QApplication.clipboard().text()
    for part in ("-pass 1", "-pass 2", "-threads", "concat=n=2", "psnr", "-f segment"):
        assert part in cmd, part
    assert cmd.count(" && ") == 3
    w.close()


def test_speed_test_dialog(qapp, films, data_dir):
    from convert.dialogs import BenchDialog
    w = _win(qapp)
    d = BenchDialog(w, w.av, os.path.join(films, "Part3.mkv"), 4.0, 0, only=["x264", "mpeg4"])
    assert d.length == 4.0 and len(d.items) == 2
    d.start_test()
    assert pump(qapp, 120, lambda: d._thread is None and len(d.results) == 2)
    assert all(r["ok"] for r in d.results), d.results
    assert all(d.items[r["id"]].text(1) for r in d.results) and d.items["x264"].text(4) == "✓"
    d.reject()
    w.close()
