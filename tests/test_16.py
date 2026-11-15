"""1.6: the classic look, the track cards that fold like an accordion, sound and subtitle files dropped or
opened attach to their film, external AC3 cut keeps its codec, and the log in sections."""
import os

import pytest

from conftest import ff as mk, pump
from convert import config
from convert import job as J
from convert import probe as P


@pytest.fixture(scope="module")
def films(media, tmp_path_factory):
    d = str(tmp_path_factory.mktemp("films16"))
    mk("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=4", "-f", "lavfi", "-i", "sine=f=300:d=4",
       "-f", "lavfi", "-i", "sine=f=500:d=4", "-map", "0", "-map", "1", "-map", "2",
       "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "ac3", os.path.join(d, "Film.mkv"))
    mk("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-c:v", "libx264", "-preset", "ultrafast",
       os.path.join(d, "Other.mkv"))
    mk("-f", "lavfi", "-i", "sine=f=900:d=4", "-ac", "2", "-c:a", "ac3", "-b:a", "192k", os.path.join(d, "Film.rus.ac3"))
    mk("-f", "lavfi", "-i", "sine=f=700:d=2", "-c:a", "libmp3lame", os.path.join(d, "Song.mp3"))
    mk("-f", "lavfi", "-i", "sine=f=600:d=4", "-ac", "2", "-c:a", "ac3", os.path.join(d, "Commentary.ac3"))
    for name in ("Film.eng.srt", "Lonely.srt"):
        with open(os.path.join(d, name), "w", encoding="utf-8") as f:
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


def _settle(qapp, w):
    assert pump(qapp, 20, lambda: all(j.status != "probe" for j in w.jobs) and
                all(v is not None for j in w.jobs for v in j.infos.values()))


# ── the look ──────────────────────────────────────────────────
def test_look_xvid_and_modern(qapp):
    from convert import style
    try:
        for theme in ("light", "dark"):
            style.set_look("classic")
            style.set_theme(theme)
            qss = style.stylesheet()
            assert "QTabBar#segTabs::tab:selected" in qss and "border-radius: 2px" in qss
            assert style.CARD_SEL and style.SECTION and "gradient" in style.section_css()
            assert "%DOWN%" not in qss and "%UP%" not in qss
        assert style.CARD_SEL == style.CLASSIC["dark"]["CARD_SEL"]
        style.set_look("modern")
        style.set_theme("dark")
        qss = style.stylesheet()
        assert "QTabBar#segTabs::tab:first" not in qss and style.CARD_SEL == style.ACCENT
        assert style.set_look("bogus") == "classic"
        cfg = config.load()
        assert cfg.get("look") == "classic"
    finally:
        style.set_look("classic")
        style.set_theme("dark")


# ── the accordion ─────────────────────────────────────────────
def test_track_cards_fold(qapp, films, data_dir):
    from PySide6.QtWidgets import QCheckBox, QToolButton
    w = _win(qapp)
    w.add_paths([os.path.join(films, "Film.mkv")])
    _settle(qapp, w)
    j = w.jobs[0]
    w.view.setCurrentItem(w.items[j.uid])
    w.show_side("streams")
    pump(qapp, 0.5)

    def body(idx):
        return [x for x in w.tracks_box.findChildren(QWidget_) if x.property("stream") == idx    # the newest
                and x.objectName() != "section"][-1]

    def check(idx):
        head = [x for x in w.tracks_box.findChildren(QWidget_) if x.property("stream") == idx
                and x.objectName() == "section"][-1]
        return head.findChild(QCheckBox), [b for b in head.findChildren(QToolButton) if b.property("fold")][0]

    from PySide6.QtWidgets import QWidget as QWidget_
    assert body(1).isVisible() and body(2).isVisible()
    cb, _fold = check(1)
    cb.setChecked(False)                                 # unticked: the card closes (slides shut)
    assert pump(qapp, 3, lambda: not body(1).isVisible())
    assert j.tracks["1"]["keep"] is False and body(2).isVisible()
    assert [s.index for s, _c in J.plan(j)["audio"]] == [2]
    _cb, fold = check(1)
    fold.click()                                         # the arrow opens it by hand, still unticked
    assert pump(qapp, 3, lambda: body(1).isVisible())
    assert j.tracks["1"]["keep"] is False
    _cb, fold = check(2)
    fold.click()                                         # and closes a kept one
    assert pump(qapp, 3, lambda: not body(2).isVisible())
    cb, _fold = check(1)
    cb.setChecked(True)                                  # ticked again: open
    assert pump(qapp, 3, lambda: body(1).isVisible())
    w.close()


# ── dropped / opened files ────────────────────────────────────
def test_sound_and_subtitles_attach_to_their_film(qapp, films, data_dir, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    w = _win(qapp)
    f = lambda n: os.path.join(films, n)        # noqa: E731
    # a film with its sound and subtitles in one drop: one task, two external tracks
    n = w.add_incoming([f("Film.mkv"), f("Film.rus.ac3"), f("Film.eng.srt")])
    assert n == 1 and len(w.jobs) == 1
    j = w.jobs[0]
    assert [(os.path.basename(e["path"]), e["kind"], e["lang"]) for e in j.ext] == \
        [("Film.rus.ac3", "audio", "rus"), ("Film.eng.srt", "subtitle", "eng")]
    _settle(qapp, w)
    # the same again: not twice
    w.add_incoming([f("Film.rus.ac3")])
    assert len(j.ext) == 2 and len(w.jobs) == 1
    # an unrelated sound with no film selected: its own task; subtitles alone: nothing
    w.view.clearSelection()
    pump(qapp, 0.2)
    assert w.add_incoming([f("Song.mp3"), f("Lonely.srt")]) == 1
    assert [os.path.basename(x.src) for x in w.jobs] == ["Film.mkv", "Song.mp3"]
    # one film selected: asked — «add to the film»
    w.add_incoming([f("Other.mkv")])
    _settle(qapp, w)
    other = w.jobs[-1]
    w.view.clearSelection()
    w.items[other.uid].setSelected(True)
    pump(qapp, 0.2)
    answers = []

    class Box(QMessageBox):
        pick = "add"

        def exec(self):
            answers.append(self.text())
            want = {"add": QMessageBox.AcceptRole, "task": QMessageBox.NoRole}[Box.pick]
            for b in self.buttons():
                if self.buttonRole(b) == want:
                    self._clicked = b
            return 0

        def clickedButton(self):
            return self._clicked

    monkeypatch.setattr("convert.window.QMessageBox", Box)
    assert w.add_incoming([f("Commentary.ac3")]) == 0
    assert answers and "Other.mkv" in answers[0] and [os.path.basename(e["path"]) for e in other.ext] == ["Commentary.ac3"]
    # … or «a task of its own»
    Box.pick = "task"
    other.ext.clear()
    assert w.add_incoming([f("Commentary.ac3")]) == 1 and not other.ext
    assert os.path.basename(w.jobs[-1].src) == "Commentary.ac3"
    # dropped on a card: onto that film whatever its name
    w.view.clearSelection()
    assert w.add_incoming([f("Song.mp3")], target_uid=other.uid) == 0
    assert [os.path.basename(e["path"]) for e in other.ext] == ["Song.mp3"]
    w.close()


# ── external AC3 cut: still AC3 ───────────────────────────────
def test_external_ac3_cut_keeps_codec(qapp, films, data_dir):
    j = J.Job(src=os.path.join(films, "Film.mkv"), settings=J.merged({"video": {"encoder": "x264"}}),
              info=P.probe(os.path.join(films, "Film.mkv")))
    ac3 = os.path.join(films, "Film.rus.ac3")
    j.ext.append(J.clean_ext({"path": ac3, "kind": "audio", "lang": "rus"}))
    j.infos[ac3] = P.probe(ac3)
    j.trim = [[1.0, 3.0]]
    pl = J.plan(j)
    assert [c for _s, c in pl["audio"]] == ["ac3", "ac3", "ac3"]
    j.ext[0]["codec"] = "aac"                                # chosen by hand: as chosen
    assert [c for _s, c in J.plan(j)["audio"]][-1] == "aac"


# ── the log ───────────────────────────────────────────────────
def test_xlog_pieces():
    from convert import xlog
    assert xlog.dur(6630.656) == "1h 50m 30s 656ms" and xlog.dur(2.5) == "2s 500ms"
    assert xlog.size_text(47171239).startswith("47171239 bytes (44.99 MiB / 47.17 MB)")
    lines = [
        "[out#0/matroska @ 0x1]   Output stream #0:0 (video): 150 frames encoded; 150 packets muxed (602060 bytes); ",
        "[out#0/matroska @ 0x1]   Output stream #0:1 (audio): 173 packets muxed (72306 bytes); ",
        "[out#0/matroska @ 0x1] video:588kB audio:312kB subtitle:0kB other streams:0kB global headers:0kB muxing overhead: 0.71%",
        "[out#1/image2 @ 0x2]   Output stream #1:0 (video): 6 frames encoded; 6 packets muxed (114251 bytes); ",
        "[out#1/image2 @ 0x2] video:112kB audio:0kB subtitle:0kB other streams:0kB global headers:0kB muxing overhead: unknown",
        "[in#0/matroska @ 0x3]   Input stream #0:1 (audio): 206 packets read (86100 bytes); 204 frames decoded; "
        "0 decode errors (313344 samples); ",
        "[libx264 @ 0x4] frame I:1     Avg QP:12.82  size: 14733",
        "frame=  150 fps=112 q=-1.0 size=     907kB time=00:00:05.99 bitrate=1240.7kbits/s speed=4.49x",
    ]
    st = "\n".join(xlog.stats(lines))
    assert "Completed video stream at index: 0: Encoded frames: 150, Written packets: 150 (602060 bytes)" in st
    assert "Completed audio stream at index: 1: Copied, Written packets: 173" in st
    assert "index: 0: Encoded frames: 6" not in st                     # the preview frame is not the result
    assert "Input audio stream #0:1: Read packets: 206 (86100 bytes), Decoded frames: 204 (313344 samples)" in st
    assert "I frames: 1, avg QP = 12.82, avg size = 14733 bytes" in st and "Last position: frame= 150" in st
    assert xlog.sizes(lines)["video"] == 588.0
    assert xlog.NOISE.search("[vist#0:0/h264 @ 0x5] Decoder thread received EOF packet")
    assert not xlog.NOISE.search("[libx264 @ 0x4] frame I:1     Avg QP:12.82  size: 14733")


def test_xlog_of_a_run(qapp, films, data_dir):
    """Two passes, a cut, an external AC3, PSNR: every section of the log, and the AC3 stays AC3."""
    w = _win(qapp, metrics=["psnr"], show_frame=True)
    w.add_incoming([os.path.join(films, "Film.mkv"), os.path.join(films, "Film.rus.ac3")])
    _settle(qapp, w)
    j = w.jobs[0]
    j.settings = J.merged({"video": {"encoder": "x264", "speed": 7, "rc": "bitrate", "bitrate": 600, "passes": 2,
                                     "height": 240}, "subs": "all"})
    j.trim = [[0.5, 3.0]]
    w.start_queue()
    assert pump(qapp, 180, lambda: not w.running and not w._any_running())
    pump(qapp, 0.3)
    assert j.status == "done", j.error
    assert [s.codec for s in P.probe(j.out).of("audio")] == ["ac3", "ac3", "ac3"]
    text = open(j.log_path, encoding="utf-8").read()
    for part in ("ENCODING STARTED ", "Input file: " + j.src, "(External: Film.rus.ac3)", "Output format: MKV",
                 "Trim:          1 piece(s)", "Video general:", "Resolution:  640x360 > ",
                 "Codec:       H.264 > LIBX264 600 kbps 2-pass", "Audio general:", "Stream #ext1: AC-3 2ch 44.1kHz > AC-3",
                 "PASS 1 OF 2 (ANALYSIS) STARTED", "PASS 2 OF 2 (ENCODING) STARTED", "QUALITY METRICS STARTED",
                 "Encoding in progress...", "Completed video stream at index: 0", "Encoder settings (written into the stream):",
                 "cabac = 1", "Quality metrics: PSNR", "ENCODING COMPLETED! ", "Encoding time: ", "Maximum memory usage: ",
                 "Encoded data size: ", "Container data size: ", "Total file size: ", "Encoded file path: " + j.out):
        assert part in text, part
    assert "Decoder thread received EOF" not in text and "Terminating demuxer thread" not in text
    assert "Decoder thread received EOF" not in w.log.toPlainText()
    w.close()
