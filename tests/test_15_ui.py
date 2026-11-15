"""1.5 in the window: the grouped preset list with the devices, the preset of the format (create, copy, rename,
delete, export, import, use), the container options in Format options, the advanced encoder settings, the
comparison window, «Copy the ffmpeg command», the queue and presets keeping all of it — real encodes, results
checked by ffprobe."""
import json
import os

import pytest

from conftest import ff as mk, pump
from convert import config
from convert import formats as FM
from convert import job as J
from convert import probe as P


@pytest.fixture(scope="module")
def film(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("f15"))
    mk("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=3", "-f", "lavfi", "-i", "sine=f=500:d=3",
       "-filter_complex", "[1]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[a]", "-map", "0:v", "-map", "[a]",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3", os.path.join(d, "Film.mkv"))
    return os.path.join(d, "Film.mkv")


def _win(qapp, **over):
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False, keep_queue=False, out_mode="source")
    cfg.update(over)
    w = MainWindow(cfg)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    return w


def _add(qapp, w, path):
    assert w.add_paths([path]) == 1
    assert pump(qapp, 15, lambda: all(j.status != "probe" for j in w.jobs))
    j = w.jobs[-1]
    w.view.clearSelection()
    w.items[j.uid].setSelected(True)
    pump(qapp, 0.2)
    return j


def _run(qapp, w, secs=240):
    w.start_queue()
    assert pump(qapp, secs, lambda: not w.running and not w._any_running())
    pump(qapp, 0.3)


def test_grouped_presets_and_devices(qapp, film, data_dir):
    config.write("presets.json", {"Мой старый": {"container": "mkv", "video": {"encoder": "x264", "height": 240}}})
    w = _win(qapp)
    w.user_presets = config.read("presets.json")
    w._fill_presets()
    box = w.preset_box
    heads = [box.itemText(i) for i in range(box.count()) if box.itemData(i) is None]
    from convert.i18n import t
    assert heads == [t("pg_" + g) for g in ("general", "apple", "android", "tv", "console", "web", "disc", "own")]
    assert all(not box.model().item(i).isEnabled() for i in range(box.count()) if box.itemData(i) is None)
    keys = [box.itemData(i) for i in range(box.count()) if box.itemData(i)]
    for k in ("iphone_hevc", "android", "tv_samsung", "tv_lg", "tv_sony", "ps5", "xbox", "youtube", "telegram",
              "discord10", "discord50", "bluray", "dvd_pal", "dvd_ntsc", "Мой старый"):
        assert k in keys
    j = _add(qapp, w, film)
    box.setCurrentIndex(box.findData("dvd_pal"))
    w._preset_picked()
    assert j.settings["container"] == "mpg" and j.settings["fmt"]["name"] == "DVD PAL" and j.preset == "dvd_pal"
    assert w.v_fmt.isVisible() and "DVD PAL" in w.v_fmt.text()
    assert "DVD PAL" in w.items[j.uid].text(3)                          # the profile column names the format
    # the encoder list: what the format doesn't allow is greyed out
    model = w.v_enc.model()
    assert not model.item(w.v_enc.findData("x264")).isEnabled()
    assert model.item(w.v_enc.findData("mpeg2")).isEnabled()
    # an own (old) preset opens, can be renamed
    box.setCurrentIndex(box.findData("Мой старый"))
    w._preset_picked()
    assert j.settings["fmt"] == {} and j.settings["video"]["height"] == 240
    w.rename_preset("Мой старый", "Новое имя")
    assert "Новое имя" in config.read("presets.json") and w.preset_box.findData("Новое имя") >= 0
    _run(qapp, w)
    assert j.status == "done", j.error
    w.close()


def test_format_preset_dialog(qapp, film, data_dir, tmp_path):
    from convert.preset_ui import FormatDialog
    w = _win(qapp)
    j = _add(qapp, w, film)
    j.settings = J.merged({"container": "mkv", "video": {"encoder": "x264", "speed": 1}})
    d = FormatDialog(w, {}, "mkv")
    d.applied.connect(w.apply_format)                                   # as format_preset_dialog does
    d.show()
    pump(qapp, 0.1)
    names = [d.list.item(i).text().strip() for i in range(d.list.count())]
    assert "Blu-ray (H.264)" in names and "DVD PAL" in names
    assert not d.f_cont.isEnabled() and d.ro_hint.isVisible() is True     # built-in: read only
    row = next(i for i in range(d.list.count()) if d.list.item(i).text().strip() == "MP4 — H.264 High@4.1")
    d.list.setCurrentRow(row)
    d.copy("Мой MP4")
    assert d.chosen() == ("own", "Мой MP4") and d.f_cont.isEnabled()
    d.prof["h264"].setCurrentIndex(d.prof["h264"].findData("main"))
    d.lev["h264"].setCurrentIndex(d.lev["h264"].findData("3.1"))
    d.gop_max.setValue(20)
    d.gop_closed.setChecked(True)
    d.f_ext.setText("m4v")
    d.f_ext.editingFinished.emit()
    saved = FM.user_formats()["Мой MP4"]
    assert saved["profile"]["h264"] == "main" and saved["level"]["h264"] == "3.1" and saved["gop_max"] == 20
    assert saved["ext"] == "m4v" and saved["container"] == "mp4"
    d.rename("Мой MP4 2")
    assert "Мой MP4 2" in FM.user_formats() and "Мой MP4" not in FM.user_formats()
    path = str(tmp_path / "x.acformat.json")
    d.export(path)
    d.delete(ask=False)
    assert "Мой MP4 2" not in FM.user_formats()
    d.import_(path)
    assert "Мой MP4 2" in FM.user_formats() and d.chosen() == ("own", "Мой MP4 2")
    d.use()
    assert j.settings["fmt"]["name"] == "Мой MP4 2" and j.settings["container"] == "mp4"
    assert j.settings["video"]["encoder"] in ("x264", "auto_h264")
    # «Copy the ffmpeg command» has the format in it
    w._copy_cmd(j)
    from PySide6.QtWidgets import QApplication
    cmd = QApplication.clipboard().text()
    assert "-profile:v main" in cmd and "-level 3.1" in cmd and "-g 20" in cmd and cmd.rstrip().endswith('.m4v')
    _run(qapp, w)
    assert j.status == "done", j.error and j.out.endswith(".m4v")
    info = P.probe(j.out)
    assert info.video.codec == "h264"
    import subprocess
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=profile,level",
                        "-of", "csv=p=0", j.out], capture_output=True, text=True)
    assert r.stdout.strip() == "Main,31"
    # no preset of the format again
    j.status = "wait"
    d2 = FormatDialog(w, j.settings["fmt"], "mp4")
    d2.applied.connect(w.apply_format)
    assert d2.chosen() == ("own", "Мой MP4 2")
    d2.use_none()
    assert j.settings["fmt"] == {}
    w.close()


def test_container_options_in_format_dialog(qapp, film, data_dir):
    from PySide6.QtWidgets import QCheckBox, QComboBox, QGroupBox, QSpinBox
    w = _win(qapp)
    j = _add(qapp, w, film)
    j.settings = J.merged({"container": "mkv", "video": {"mode": "copy"}, "audio": {"codec": "copy"}})
    w._load_settings()
    from PySide6.QtCore import QTimer
    seen = {}

    def look():
        d = w._format_dlg
        seen["box"] = d.findChild(QGroupBox, "muxBox") is not None
        d.findChild(QCheckBox, "muxCues").setChecked(True)
        d.findChild(QSpinBox, "muxClusterMs").setValue(1000)
        dm = d.findChild(QComboBox, "muxDefault")
        dm.setCurrentIndex(dm.findData("infer"))
        d.accept()
    QTimer.singleShot(200, look)
    w.format_dialog()
    assert seen["box"]
    m = j.settings["mux"]
    assert m["mkv_cues_front"] and m["mkv_cluster_ms"] == 1000 and m["mkv_default"] == "infer"
    # MP4: faststart / fragmented
    w._set(None, "container", "mp4")

    def look2():
        d = w._format_dlg
        assert d.findChild(QCheckBox, "muxCues") is None
        d.findChild(QCheckBox, "muxFrag").setChecked(True)
        d.accept()
    QTimer.singleShot(200, look2)
    w.format_dialog()
    assert j.settings["mux"]["mp4_frag"] is True
    args = J.build_args(j, "ffmpeg", "/tmp/o.mp4", w.av)
    assert args[args.index("-movflags") + 1] == "+frag_keyframe+empty_moov+default_base_moof"
    w.close()


def test_advanced_dialog_queue_and_preset(qapp, film, data_dir, tmp_path):
    w = _win(qapp)
    j = _add(qapp, w, film)
    j.settings = J.merged({"container": "mkv", "video": {"encoder": "x264", "speed": 5}, "subs": "none"})
    w._load_settings()
    from PySide6.QtCore import QTimer

    def look():
        d = w._adv_dlg
        assert "tune" in d.widgets and "ref" in d.widgets and "params" in d.widgets
        d.set_value("tune", "animation")
        d.set_value("ref", 3)
        d.set_value("bframes", 0)
        d.set_value("params", "me=hex")
        d.accept()
    QTimer.singleShot(200, look)
    w.advanced_dialog()
    assert j.settings["venc"]["x264"] == {"tune": "animation", "ref": 3, "bframes": 0, "params": "me=hex"}
    assert w.v_adv.isVisible() and "tune animation" in w.v_adv.text()
    # kept in the task list file and in an own preset
    path = str(tmp_path / "q.json")
    w.save_task_list(path)
    saved = json.load(open(path, encoding="utf-8"))
    tasks = saved["jobs"]
    assert tasks[0]["settings"]["venc"]["x264"]["tune"] == "animation"
    from PySide6.QtWidgets import QInputDialog
    orig = QInputDialog.getText
    QInputDialog.getText = staticmethod(lambda *a, **k: ("С анимацией", True))
    try:
        w.save_preset()
    finally:
        QInputDialog.getText = orig
    assert config.read("presets.json")["С анимацией"]["venc"]["x264"]["ref"] == 3
    _run(qapp, w)
    assert j.status == "done", j.error
    data = open(j.out, "rb").read()
    opts = data[data.find(b"options:"):][:1500].decode("latin-1")
    assert "ref=3" in opts and "bframes=0" in opts and "me=hex" in opts
    # NVENC's dialog only offers what this build has (no GPU needed to look)
    from convert import advanced as ADV
    from convert import encoders as E
    from convert.preset_ui import AdvancedDialog
    e = E.BY_ID["nvenc_hevc"]
    caps = ADV.caps(e)
    if caps:
        d = AdvancedDialog(w, e, {}, "auto", caps)
        assert set(d.widgets) == {f.key for f in ADV.offered(e, caps)}
        assert d.findChild(type(d.ten), "adv_10bit") is not None
    w.close()


def test_compare_dialog(qapp, film, data_dir):
    w = _win(qapp)
    j = _add(qapp, w, film)
    from convert.preset_ui import CompareDialog
    d = CompareDialog(w, w.av, w.preset_entries(), j, 0, "h264_mp4")
    d.show()
    assert d.a.currentData() == "h264_mp4" and d.b.currentData() and d.b.currentData() != "h264_mp4"
    d.b.setCurrentIndex(d.b.findData("telegram"))
    d.length.setCurrentIndex(0)
    d.start()
    assert pump(qapp, 300, lambda: d._thread is None and len(d.results) == 2)
    assert all(r["ok"] for r in d.results), [r.get("error") for r in d.results]
    assert all(d.items[i].text(2) and d.items[i].text(7).endswith("dB") for i in (0, 1))
    bold = [d.items[i].font(2).bold() for i in (0, 1)]
    assert bold.count(True) == 1                                         # the smaller one is marked
    d.reject()
    w.close()
