"""1.9: the demux window (Stop, no half-written files left, speed and time left, the DTS core of a real DTS-HD MA
track in a Blu-ray m2ts), the task's menu (remove all, join all, encode first, open the source, compare presets),
the quality metrics through libvmaf (PSNR-HVS, MS-SSIM, CIEDE2000, CAMBI) with what each one means.

tests/data/dtshd_ma_51.dts: a few frames of real DTS-HD Master Audio 5.1 (48 kHz / 24 bit, 768 kbit/s core + XLL)
from the dcadec test suite (github.com/foo86/dcadec-samples, public domain)."""
import json
import os
import signal
import subprocess
import sys

import pytest

from conftest import ff as mk, pump
from convert import config
from convert import encoders as E
from convert import ff
from convert import job as J
from convert import probe as P

HERE = os.path.dirname(os.path.abspath(__file__))
DTSHD = os.path.join(HERE, "data", "dtshd_ma_51.dts")


def _win(qapp, **over):
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False, keep_queue=False)
    cfg.update(over)
    w = MainWindow(cfg)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    return w


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def bdav_stream_type(path: str, old: int, new: int) -> int:
    """FFmpeg writes DTS into m2ts as stream type 0x82; a Blu-ray has DTS-HD MA as 0x86. Rewrite the PMT (and its
    CRC) the way a disc has it. Returns how many PMTs changed."""
    d = bytearray(open(path, "rb").read())
    n = 0
    for off in range(4, len(d) - 187, 192):
        p = d[off:off + 188]
        if p[0] != 0x47 or not p[1] & 0x40:
            continue
        i = 4 + (1 + p[4] if (p[3] >> 4) & 2 else 0)
        i += 1 + p[i]
        if p[i] != 0x02:                                  # a PMT
            continue
        end = i + 3 + (((p[i + 1] & 0x0F) << 8) | p[i + 2]) - 4
        j = i + 12 + (((p[i + 10] & 0x0F) << 8) | p[i + 11])
        while j < end:
            if p[j] == old:
                d[off + j] = new
                n += 1
            j += 5 + (((p[j + 3] & 0x0F) << 8) | p[j + 4])
        d[off + end: off + end + 4] = _crc32_mpeg(bytes(d[off + i: off + end])).to_bytes(4, "big")
    with open(path, "wb") as f:
        f.write(d)
    return n


@pytest.fixture(scope="module")
def bd(media, tmp_path_factory):
    """A BDAV m2ts like a Blu-ray's: H.264 + AC3 (rus) + DTS-HD MA 5.1 (eng, stream type 0x86), ~19 s; a disc title
    reading it twice (concat list); a slow-to-read file for Stop."""
    d = str(tmp_path_factory.mktemp("bd19"))
    raw = os.path.join(d, "ma.dts")
    with open(DTSHD, "rb") as f:
        frames = f.read()
    with open(raw, "wb") as f:
        f.write(frames * 300)                              # 300 × 64 ms
    m2ts = os.path.join(d, "00800.m2ts")
    mk("-f", "lavfi", "-i", "testsrc2=s=640x360:r=24000/1001", "-f", "lavfi", "-i", "sine=f=500", "-i", raw,
       "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a:0", "ac3",
       "-c:a:1", "copy", "-metadata:s:a:0", "language=rus", "-metadata:s:a:1", "language=eng", "-shortest",
       "-f", "mpegts", "-mpegts_m2ts_mode", "1", m2ts)
    assert bdav_stream_type(m2ts, 0x82, 0x86) > 0
    title = os.path.join(d, "Tenet - 00800.ffconcat")
    with open(title, "w", encoding="utf-8") as f:
        f.write(f"ffconcat version 1.0\nfile '{m2ts}'\nfile '{m2ts}'\n")
    return {"dir": d, "m2ts": m2ts, "title": title}


# ── 1. the DTS core ───────────────────────────────────────────
def test_dts_hd_found_in_bdav(bd):
    info = P.probe(bd["m2ts"])
    dts = info.of("audio")[1]
    assert (dts.codec, dts.profile, dts.channels, dts.tag) == ("dts", "DTS-HD MA", 6, 0x86)
    j = J.Job(src=bd["m2ts"], settings=J.merged({}), info=info)
    assert J.dts_hd(j, dts)
    # what ffprobe may say of a DTS track — and what is decided from it
    S = lambda **k: P.Stream(index=1, kind="audio", codec="dts", **k)       # noqa: E731
    fake = J.Job(src="/nowhere/x.m2ts", settings=J.merged({}))
    for prof, want in (("DTS-HD MA", True), ("DTS-HD HRA", True), ("DTS-HD MA + DTS:X", True),
                       ("DTS-HD MA + DTS:X IMAX", True), ("DTS Express", False), ("DTS", False), ("DTS-ES", False),
                       ("DTS 96/24", False), ("", False)):
        assert J.dts_hd(fake, S(profile=prof)) is want, prof
    assert J.dts_hd(fake, S(profile="DTS", tag=0x86)) and J.dts_hd(fake, S(profile="", tag=0x85))
    assert J.dts_hd(fake, S(bitrate=3_500_000)) and not J.dts_hd(fake, S(profile="DTS Express", tag=0x86))
    # a short probe that saw only «DTS»: the first frames are looked at — the extension substream is there
    dts.profile, dts.tag, dts.bitrate = "DTS", 0, 0
    assert J.dts_hd(j, dts, sniff=False) is False and J.dts_hd(j, dts) is True
    plain = os.path.join(bd["dir"], "plain.mka")
    mk("-f", "lavfi", "-i", "sine=d=1", "-ac", "6", "-c:a", "dca", "-strict", "-2", plain)
    jp = J.Job(src=plain, settings=J.merged({}), info=P.probe(plain))
    assert not J.dts_hd(jp, jp.info.of("audio")[0])


def _run(qapp, d, secs=120):
    d.start()
    assert pump(qapp, secs, lambda: hasattr(d, "ok") and not d.running), d.state.text()


@pytest.mark.parametrize("which", ["m2ts", "title"])
def test_demux_dts_core_of_real_dtshd(qapp, bd, data_dir, tmp_path, which):
    from convert.dialogs import DemuxDialog
    src = bd[which]
    j = J.Job(src=src, settings=J.merged({}), info=P.probe(src))
    d = DemuxDialog(j)
    out = str(tmp_path / "tracks")
    d.folder.setText(out)
    d._fill()
    rows = [d.view.topLevelItem(i).text(0) for i in range(d.view.topLevelItemCount())]
    assert len(d.core_rows()) == 1 and d.core_rows()[0].startswith("#2 DTS core ← из DTS-HD MA")
    assert rows[2].startswith("#2 DTS-HD MA") and rows[3] == d.core_rows()[0]
    assert d.view.topLevelItem(3).font(0).bold()                       # the core row stands out
    _run(qapp, d)
    assert d.ok is True, d.state.text()
    core = next(it["path"] for it in d.done_items if it.get("core"))
    full = next(it["path"] for it in d.done_items if it["stream"].index == 2 and not it.get("core"))
    assert core.endswith(" core.dts") and full.endswith(".dts")
    c, f = P.probe(core).of("audio")[0], P.probe(full).of("audio")[0]
    assert (c.codec, c.profile, c.channels, c.rate) == ("dts", "DTS", 6, 48000) and c.bitrate == 768000
    assert f.profile == "DTS-HD MA"
    assert os.path.getsize(core) < os.path.getsize(full) * 0.4

    def md5(*args):
        r = subprocess.run(["ffmpeg", "-v", "error", *args, "-f", "md5", "-"], capture_output=True, text=True)
        return r.stdout.strip()
    if which == "m2ts":
        # the core cut out = the core the decoder reads inside DTS-HD (bit for bit); the full track = lossless
        assert md5("-core_only", "1", "-i", bd["m2ts"], "-map", "0:2") == md5("-i", core) != ""
        assert md5("-i", full) == md5("-i", bd["m2ts"], "-map", "0:2") != ""
    assert "✓" in d.state.text()


def _slow(monkeypatch, rate="0.5"):
    """ffmpeg reads at half real time, so there is time to stop it."""
    orig = J.demux_args
    monkeypatch.setattr(J, "demux_args", lambda job, exe, items: [exe, "-readrate", rate] + orig(job, exe, items)[1:])


def _writing(d, folder):
    return d.running and os.path.isdir(folder) and any(os.path.getsize(os.path.join(folder, f)) > 0
                                                       for f in os.listdir(folder))


def test_demux_stop_deletes_half_written(qapp, bd, data_dir, tmp_path, monkeypatch):
    from convert.dialogs import DemuxDialog
    from convert.i18n import set_lang
    j = J.Job(src=bd["m2ts"], settings=J.merged({}), info=P.probe(bd["m2ts"]))
    d = DemuxDialog(j)
    d.show()
    folder = str(tmp_path / "new folder")
    d.folder.setText(folder)
    _slow(monkeypatch)
    d.start()
    assert pump(qapp, 20, lambda: _writing(d, folder))
    assert not d.b_go.isEnabled() and d.b_stop.isVisible() and d.b_stop.isEnabled() and not d.folder.isEnabled()
    assert pump(qapp, 10, lambda: "МБ/с" in d.rate.text() and "осталось" in d.rate.text()), d.rate.text()
    d.stop()
    assert pump(qapp, 20, lambda: not d.running)
    assert d.ok is False and len(d.removed) == 4                       # h264, ac3, dts, core
    assert not os.path.exists(folder)                                   # the folder it made, now empty, is gone
    assert d.state.text() == "■ Остановлено. Недописанные файлы удалены: 4"
    assert d.b_go.isEnabled() and not d.b_stop.isVisible() and d.folder.isEnabled()
    assert not os.path.exists(os.path.join(folder, "00800 - chapters.txt"))
    # a folder that was there stays (with what was in it), only the run's files go
    os.makedirs(folder)
    keep = os.path.join(folder, "mine.txt")
    open(keep, "w").close()
    set_lang("en")
    try:
        d.start()
        assert pump(qapp, 20, lambda: _writing(d, folder))
        assert pump(qapp, 10, lambda: "MB/s" in d.rate.text() and "left" in d.rate.text())
        d.stop()
        assert pump(qapp, 20, lambda: not d.running)
        assert os.listdir(folder) == ["mine.txt"] and d.state.text().startswith("■ Stopped. Half-written files deleted")
    finally:
        set_lang("ru")
    d.close()


def test_demux_close_asks_then_cleans(qapp, bd, data_dir, tmp_path, monkeypatch):
    from convert.dialogs import DemuxDialog
    j = J.Job(src=bd["m2ts"], settings=J.merged({}), info=P.probe(bd["m2ts"]))
    d = DemuxDialog(j)
    d.show()
    folder = str(tmp_path / "t")
    d.folder.setText(folder)
    _slow(monkeypatch)
    asked = []
    monkeypatch.setattr(d, "_ask_stop", lambda: asked.append(1) or answer[0])
    answer = [False]
    d.start()
    assert pump(qapp, 20, lambda: _writing(d, folder))
    d.b_close.click()                                    # «No»: it goes on
    assert asked and d.isVisible() and d.running
    d.close()                                            # the window's ✕ asks the same
    assert len(asked) == 2 and d.isVisible() and d.running
    answer[0] = True
    d.b_close.click()                                    # «Yes»: stopped, waited for, cleaned, closed
    assert not d.isVisible() and not d.running and d.ok is False
    assert not os.path.exists(folder)
    # closing when nothing runs does not ask
    asked.clear()
    d.show()
    d.reject()
    assert not asked and not d.isVisible()


def test_demux_error_deletes_half_written(qapp, bd, data_dir, tmp_path, monkeypatch):
    """ffmpeg dies on its own (not «Stop»): the files go and the window says so."""
    from convert.dialogs import DemuxDialog
    j = J.Job(src=bd["m2ts"], settings=J.merged({}), info=P.probe(bd["m2ts"]))
    d = DemuxDialog(j)
    folder = str(tmp_path / "t")
    d.folder.setText(folder)
    _slow(monkeypatch)
    d.start()
    assert pump(qapp, 20, lambda: _writing(d, folder))
    os.kill(int(d.runner.proc.processId()), signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    assert pump(qapp, 20, lambda: not d.running)
    assert d.ok is False and not d.runner.cancelled and not os.path.exists(folder)
    assert d.state.text().startswith("✗ ") and "Недописанные файлы удалены: 4" in d.state.text()


def test_demux_whole_and_chapters_only_on_success(qapp, media, data_dir, tmp_path):
    from convert.dialogs import DemuxDialog
    src = os.path.join(media, "Movie (2017).mkv")
    j = J.Job(src=src, settings=J.merged({}), info=P.probe(src))
    d = DemuxDialog(j)
    d.folder.setText(str(tmp_path / "t"))
    d._fill()
    assert d.core_rows() == []                                         # AC3 and AAC have no core
    _run(qapp, d)
    assert d.ok and "Movie (2017) - chapters.txt" in os.listdir(tmp_path / "t")
    assert d.state.text().startswith("✓ Готово: файлов 4")


# ── 2. the task's menu ────────────────────────────────────────
@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("clips19"))
    for n, (s, f) in enumerate((("640x360", 440), ("640x360", 550), ("640x360", 660), ("640x360", 770))):
        mk("-f", "lavfi", "-i", f"testsrc2=s={s}:r=25:d=2", "-f", "lavfi", "-i", f"sine=f={f}:d=2",
           "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", os.path.join(d, f"clip{n + 1}.mkv"))
    mk("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=30", "-f", "lavfi", "-i", "sine=d=30",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", os.path.join(d, "long.mkv"))
    return d


def _queue(qapp, w, clips, names):
    w.add_paths([os.path.join(clips, n) for n in names])
    assert pump(qapp, 20, lambda: len(w.jobs) == len(names) and all(j.status == "wait" for j in w.jobs))
    return list(w.jobs)


def _menu(w, monkeypatch, job=None):
    """The right-click menu of a task: its action texts (the menu is built, not shown)."""
    from PySide6.QtWidgets import QMenu

    class Menu(QMenu):                                   # built, never shown
        def exec(self, *a):
            return None
    monkeypatch.setattr("convert.window.QMenu", Menu)
    if job is not None:
        w.view.clearSelection()
        w.items[job.uid].setSelected(True)
        pos = w.view.visualItemRect(w.items[job.uid]).center()
    else:
        from PySide6.QtCore import QPoint
        pos = QPoint(5, w.view.viewport().height() - 3)
    w._queue_menu(pos)
    return {a.text().split("\t")[0]: a for a in w._menu.actions() if a.text()}


def test_menu_has_the_new_actions(qapp, clips, data_dir, monkeypatch):
    w = _win(qapp)
    jobs = _queue(qapp, w, clips, ["clip1.mkv", "clip2.mkv"])
    acts = _menu(w, monkeypatch, jobs[1])
    for name in ("Открыть исходник", "Кодировать первым", "Сравнить пресеты…", "Склеить все ждущие в одно задание",
                 "Убрать все из очереди…"):
        assert name in acts, sorted(acts)
    acts["Кодировать первым"].trigger()                  # triggered(bool) must not land in a parameter
    assert w.jobs[0] is jobs[1]
    w.clear_queue(ask=False)
    w.close()


def test_clear_queue_keeps_files_and_asks(qapp, clips, data_dir, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QMessageBox
    w = _win(qapp, parallel=1, out_mode="folder", out_dir=str(tmp_path / "out"))
    _queue(qapp, w, clips, ["clip1.mkv", "clip2.mkv", "clip3.mkv"])
    asked = []
    reply = [QMessageBox.No]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or reply.pop(0))
    assert w.clear_queue() is False and len(w.jobs) == 3                # «No»
    assert asked == ["Убрать из очереди все задания (3)?\n\nФайлы на диске не удаляются — ни исходники, "
                     "ни готовые результаты."]
    reply[:] = [QMessageBox.Yes]
    assert w.clear_queue() is True and w.jobs == [] and w.view.topLevelItemCount() == 0
    assert sorted(os.listdir(clips)) == ["clip1.mkv", "clip2.mkv", "clip3.mkv", "clip4.mkv", "long.mkv"]
    # something is being encoded: first «stop it?»
    jobs = _queue(qapp, w, clips, ["long.mkv", "clip1.mkv"])
    jobs[0].settings = J.merged({"video": {"encoder": "x264", "speed": 0}})
    w.start_queue()
    assert pump(qapp, 30, lambda: jobs[0].status == "run" and w.slots[0].runner.running)
    asked.clear()
    reply[:] = [QMessageBox.No]
    assert w.clear_queue() is False and asked[0].startswith("Сейчас идёт кодирование. Остановить его")
    assert jobs[0].status == "run" and len(w.jobs) == 2
    asked.clear()
    reply[:] = [QMessageBox.Yes, QMessageBox.No]                       # stop: yes, then remove all: no
    assert w.clear_queue() is False and len(asked) == 2 and jobs[0].status == "run"
    reply[:] = [QMessageBox.Yes, QMessageBox.Yes]
    out = jobs[0].out
    assert w.clear_queue() is True and w.jobs == [] and not w._any_running() and not w.running
    assert not os.path.exists(out) and not os.path.exists(out + ".part")
    assert os.path.isfile(os.path.join(clips, "long.mkv"))
    w.close()


def test_join_all_in_queue_order(qapp, clips, data_dir, monkeypatch):
    w = _win(qapp)
    jobs = _queue(qapp, w, clips, ["clip1.mkv", "clip2.mkv", "clip3.mkv", "clip4.mkv"])
    w.encode_first(jobs[2])
    jobs[3].status = "skip"                                             # not waiting: stays a task of its own
    w._refresh(jobs[3])
    assert w.join_all() == 3
    assert len(w.jobs) == 2 and w.jobs[0] is jobs[2]
    assert w.jobs[0].joins == [jobs[0].src, jobs[1].src] and w.jobs[1] is jobs[3]
    assert abs(J.src_duration(w.jobs[0]) - 6.0) < 0.2
    assert w.join_all() == 0                                            # one waiting task: nothing to join
    w.clear_queue(ask=False)
    w.close()


def test_encode_first_and_next(qapp, clips, data_dir, monkeypatch, tmp_path):
    w = _win(qapp, parallel=1, out_mode="folder", out_dir=str(tmp_path / "out"))
    jobs = _queue(qapp, w, clips, ["long.mkv", "clip1.mkv", "clip2.mkv", "clip3.mkv"])
    jobs[3].status, jobs[3].error = "error", "x"
    w.encode_first(jobs[3])                                             # not running: to the top, waiting again
    assert w.jobs[0] is jobs[3] and jobs[3].status == "wait" and w.items[jobs[3].uid].text(0) == "1"
    w.encode_first(jobs[0])
    assert [w.jobs.index(j) for j in jobs] == [0, 2, 3, 1]
    jobs[0].settings = J.merged({"video": {"encoder": "x264", "speed": 0}})
    started = []
    orig = w._start
    w._start = lambda slot, j: started.append(j) or orig(slot, j)
    w.start_queue()
    assert pump(qapp, 30, lambda: jobs[0].status == "run")
    w.encode_first(jobs[2])                                             # running: the next one, after the running one
    assert w.jobs[1] is jobs[2] and w.jobs[0] is jobs[0]
    assert "Кодировать следующим" in _menu(w, monkeypatch, jobs[1])      # while the queue runs
    w.stop_queue()
    assert pump(qapp, 20, lambda: not w._any_running())
    jobs[0].status = "skip"
    w.start_queue()
    assert pump(qapp, 60, lambda: len(started) >= 2 and started[1].status in ("done", "run"))
    assert started[1] is jobs[2]
    w.stop_queue()
    assert pump(qapp, 20, lambda: not w._any_running())
    w.clear_queue(ask=False)
    w.close()


def test_open_source_player_or_default(qapp, bd, clips, data_dir, monkeypatch, tmp_path):
    from PySide6.QtGui import QDesktopServices
    w = _win(qapp)
    jobs = _queue(qapp, w, clips, ["clip1.mkv"])
    ran, urls = [], []
    real_popen = subprocess.Popen

    def popen(args, **k):                         # the strip's thumbnails (ffmpeg) run in the background too
        if isinstance(args, (list, tuple)) and args and "ffmpeg" in os.path.basename(str(args[0])).lower():
            return real_popen(args, **k)
        ran.append(args)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda u: urls.append(u.toLocalFile()) or True)
    player = tmp_path / "ArtCorePlayer.exe"
    player.write_bytes(b"")
    monkeypatch.setenv("ARTCORE_PLAYER", str(player))
    assert w.open_source(jobs[0]) == "player" and ran == [[str(player), jobs[0].src]]
    monkeypatch.setenv("ARTCORE_PLAYER", "")                            # not installed
    assert w.open_source(jobs[0]) == "default" and urls == [jobs[0].src]
    t = J.Job(src=bd["title"], settings=J.merged({}))                   # a disc title: its first clip
    assert w.open_source(t) == "default" and urls[-1] == bd["m2ts"]
    acts = _menu(w, monkeypatch, jobs[0])
    acts["Открыть исходник"].trigger()
    assert urls[-1] == jobs[0].src
    w.clear_queue(ask=False)
    w.close()


def test_player_found_in_registry_paths(monkeypatch, tmp_path):
    from convert import winint
    monkeypatch.delenv("ARTCORE_PLAYER", raising=False)
    if not winint.IS_WIN:
        assert winint.artcore_player() == ""
        monkeypatch.setattr(winint, "IS_WIN", True)
    exe = tmp_path / "Programs" / "ArtCore Player" / "ArtCorePlayer.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert os.path.normcase(winint.artcore_player()) == os.path.normcase(str(exe))


def test_compare_from_task_menu(qapp, clips, data_dir, monkeypatch):
    from convert import preset_ui
    w = _win(qapp)
    jobs = _queue(qapp, w, clips, ["clip1.mkv", "clip2.mkv"])
    monkeypatch.setattr(preset_ui.CompareDialog, "exec", lambda self: 0)
    w.view.clearSelection()
    w.items[jobs[0].uid].setSelected(True)
    w.compare_dialog(jobs[1])                                          # the task right-clicked, not the selected
    assert w._cmp.job is jobs[1]
    acts = _menu(w, monkeypatch, jobs[0])
    acts["Сравнить пресеты…"].trigger()
    assert w._cmp.job is jobs[0]
    w.clear_queue(ask=False)
    w.close()


# ── 3. quality metrics ────────────────────────────────────────
def test_metric_verdicts():
    from convert.i18n import set_lang
    assert J.metric_text("vmaf", 95.2) == "VMAF 95.20 — неотличимо от исходника"
    assert [J.metric_level("vmaf", v) for v in (99, 92, 85, 75, 40)] == [0, 1, 2, 3, 4]
    assert [J.metric_level("psnr", v) for v in (50, 42, 36, 31, 20)] == [0, 1, 2, 3, 4]
    assert [J.metric_level("ssim", v) for v in (0.995, 0.985, 0.96, 0.92, 0.8)] == [0, 1, 2, 3, 4]
    assert [J.metric_level("ms_ssim", v) for v in (0.999, 0.995, 0.985, 0.96, 0.9)] == [0, 1, 2, 3, 4]
    # CIEDE2000: libvmaf gives 45 − 20·log10(ΔE)
    assert abs(J.ciede_de(45.0) - 1.0) < 1e-9 and abs(J.ciede_de(25.0) - 10.0) < 1e-9
    assert [J.metric_level("ciede", 45 - 20 * __import__("math").log10(de)) for de in (0.5, 1.5, 3, 4, 8)] \
        == [0, 1, 2, 3, 4]
    assert J.metric_text("ciede", 51.0) == "CIEDE2000 51.00 (ΔE 0.50) — цвета неотличимы"
    assert [J.metric_level("cambi", v) for v in (0.2, 2, 4, 7, 15)] == [0, 1, 2, 3, 4]
    assert J.metric_text("cambi", 6.0) == "CAMBI 6.00 — бандинг мешает"
    assert J.metric_text("psnr_hvs", 41.0) == "PSNR-HVS 41.00 dB — отлично, разница едва заметна"
    set_lang("en")
    try:
        assert J.metric_text("ms_ssim", 0.9) == "MS-SSIM 0.9000 — poor, heavy losses"
        assert J.metric_text("cambi", 0.1) == "CAMBI 0.10 — no banding"
    finally:
        set_lang("ru")


def test_parse_metrics_lines_and_json(tmp_path):
    log = tmp_path / "v.json"
    log.write_text(json.dumps({"pooled_metrics": {
        "vmaf": {"mean": 93.5}, "psnr_hvs": {"mean": 41.25}, "float_ms_ssim": {"mean": 0.9955},
        "ciede2000": {"mean": 52.2}, "cambi": {"mean": 0.42}, "integer_adm2": {"mean": 0.9}}}))
    lines = ["[Parsed_psnr_3 @ 0x1] PSNR y:41.2 u:43.0 v:43.6 average:41.92 min:39.10 max:44.26",
             "[Parsed_ssim_4 @ 0x2] SSIM Y:0.98 (17.2) U:0.99 (20.1) V:0.99 (20.3) All:0.985123 (18.27)",
             "[Parsed_libvmaf_5 @ 0x3] VMAF score: 95.120000"]
    m = J.parse_metrics(lines, str(log))
    assert m == {"psnr": 41.92, "ssim": 0.985123, "vmaf": 95.12, "psnr_hvs": 41.25, "ms_ssim": 0.9955,
                 "ciede": 52.2, "cambi": 0.42}
    assert list(m) == list(J.METRICS)                                   # always in this order
    assert J.parse_metrics(lines, str(log), ["cambi", "psnr"]) == {"psnr": 41.92, "cambi": 0.42}
    assert J.parse_metrics(lines[:2], str(tmp_path / "none.json")) == {"psnr": 41.92, "ssim": 0.985123}
    (tmp_path / "bad.json").write_text("{")
    assert J.parse_metrics([], str(tmp_path / "bad.json")) == {}


def _av(feats=True, vmaf=True):
    av = E.Available({"x264": True})
    av.filters = {"psnr", "ssim"} | ({"libvmaf"} if vmaf else set())
    av.vmaf_feats = set(E.VMAF_FEATURES) if feats and vmaf else set()
    return av


def test_metrics_one_pass_graph(media):
    src = os.path.join(media, "Movie (2017).mkv")
    j = J.Job(src=src, settings=J.merged({"video": {"encoder": "x264"}}), info=P.probe(src))
    post = J.post_passes(j, "ffmpeg", "/o/out.mkv", _av(), list(J.METRICS))
    assert len(post) == 1                                               # one pass for all seven
    g = post[0][post[0].index("-filter_complex") + 1]
    assert "split=3" in g and g.count("libvmaf=") == 1 and "]psnr[" in g and "]ssim[" in g
    assert "feature=name=psnr_hvs|name=float_ms_ssim|name=ciede|name=cambi" in g and "log_fmt=json" in g
    assert "log_path='" in g and J.vmaf_log(j).replace("\\", "/").replace(":", "\\:") in g
    # VMAF alone: no features, no log; CAMBI alone: libvmaf with only it
    g1 = J.post_passes(j, "ffmpeg", "/o/out.mkv", _av(), ["vmaf"])[0]
    g1 = g1[g1.index("-filter_complex") + 1]
    assert "libvmaf=n_threads=" in g1 and "feature=" not in g1 and "split" not in g1
    g2 = J.post_passes(j, "ffmpeg", "/o/out.mkv", _av(), ["cambi"])[0]
    assert "feature=name=cambi:log_fmt=json" in g2[g2.index("-filter_complex") + 1]
    # this ffmpeg has no libvmaf / an old libvmaf: only what it can
    g3 = J.post_passes(j, "ffmpeg", "/o/out.mkv", _av(vmaf=False), list(J.METRICS))[0]
    assert "libvmaf" not in " ".join(g3) and "split=2" in " ".join(g3)
    assert J.post_passes(j, "ffmpeg", "/o/out.mkv", _av(vmaf=False), ["cambi", "ciede"]) == []
    old = _av()
    old.vmaf_feats = {"psnr_hvs", "float_ms_ssim", "ciede"}
    assert not J.metric_ok("cambi", old) and J.metric_ok("ciede", old) and J.metric_ok("vmaf", old)
    assert "name=cambi" not in " ".join(J.post_passes(j, "ffmpeg", "/o/out.mkv", old, list(J.METRICS))[0])


def test_config_keeps_new_metrics(data_dir):
    cfg = config.load()
    cfg["metrics"] = ["psnr", "cambi", "ms_ssim", "nonsense"]
    config.save(cfg)
    assert config.load()["metrics"] == ["psnr", "cambi", "ms_ssim"]


def _vmaf_dir():
    """An ffmpeg with libvmaf: ARTCORE_VMAF_FFMPEG (CI downloads one), else this one if it has it."""
    d = os.environ.get("ARTCORE_VMAF_FFMPEG", "")
    if d and os.path.isfile(os.path.join(d, "ffmpeg" + (".exe" if sys.platform == "win32" else ""))):
        return d
    if "libvmaf" in E.ffmpeg_filters():
        return os.path.dirname(ff.ffmpeg())
    return ""


@pytest.fixture()
def vmaf_ff(monkeypatch):
    d = _vmaf_dir()
    if not d:
        pytest.skip("no ffmpeg with libvmaf here (CI sets ARTCORE_VMAF_FFMPEG)")
    monkeypatch.setenv("ARTCORE_FFMPEG_DIR", d)
    return d


def test_vmaf_features_found(vmaf_ff):
    feats = E.vmaf_features()
    assert feats == set(E.VMAF_FEATURES), feats
    av = E.detect()
    assert av.vmaf_feats == feats and E.cache_entry(av)[ff.version()]["_vmaf"] == sorted(feats)
    av2 = E.detect(E.cache_entry(av))                                    # from the cache: no new runs
    assert av2.vmaf_feats == feats


def test_metrics_real_through_window(qapp, media, data_dir, vmaf_ff, monkeypatch, tmp_path):
    """A real encode with all seven ticked: one pass after it, the numbers on Status (coloured, the meaning in the
    tooltip), on Info and in the log with what they mean."""
    w = _win(qapp, metrics=list(J.METRICS), parallel=1, out_mode="folder", out_dir=str(tmp_path / "out"))
    assert all(w.m_check[k].isEnabled() for k in J.METRICS)
    src = os.path.join(media, "Movie (2017).mkv")
    w.add_paths([src])
    assert pump(qapp, 20, lambda: w.jobs and w.jobs[0].status == "wait")
    j = w.jobs[0]
    j.settings = J.merged({"video": {"encoder": "x264", "rc": "quality", "quality": 34, "height": 360}})
    w.start_queue()
    assert pump(qapp, 240, lambda: j.status in ("done", "error")), j.status
    assert j.status == "done", j.error
    m = j.metrics
    assert list(m) == list(J.METRICS), m
    assert 20 < m["psnr"] < 60 and 0.7 < m["ssim"] <= 1 and 0 < m["vmaf"] <= 100
    assert 15 < m["psnr_hvs"] < 70 and 0.7 < m["ms_ssim"] <= 1 and 20 < m["ciede"] < 90 and 0 <= m["cambi"] < 24
    with open(j.log_path, encoding="utf-8") as f:
        text = f.read()
    runs = [ln for ln in text.splitlines() if ln.startswith("Command:") and "libvmaf" in ln]
    assert len(runs) == 1 and "]psnr[" in runs[0] and "]ssim[" in runs[0] and "name=cambi" in runs[0]
    assert not os.path.exists(J.vmaf_log(j))                            # the JSON is cleaned up
    w._show_metrics(j)
    assert w.m_val["vmaf"].text() == f"{m['vmaf']:.2f}" and w.m_val["vmaf"].toolTip() == J.metric_text("vmaf", m["vmaf"])
    assert "(ΔE " in w.m_val["ciede"].text() and "color:" in w.m_val["cambi"].styleSheet()
    w._show_info(j)
    info = w.i_text.text()
    for k in J.METRICS:
        assert J.metric_verdict(k, m[k]) in info and J.METRIC_NAMES[k] in info
    log = w.log.toPlainText()
    assert "    " + J.metric_text("vmaf", m["vmaf"]) in log and "    " + J.metric_text("cambi", m["cambi"]) in log
    assert "Quality metrics: PSNR " in text and "  " + J.metric_text("ms_ssim", m["ms_ssim"]) in text
    w.clear_queue(ask=False)
    w.close()


def test_metrics_unavailable_said_plainly(qapp, media, data_dir, tmp_path):
    w = _win(qapp, metrics=["psnr", "vmaf", "cambi"], parallel=1, out_mode="folder", out_dir=str(tmp_path / "out"))
    w.av.filters.discard("libvmaf")                                      # as in a build without libvmaf
    w.av.vmaf_feats = set()
    w._show_metrics(None)
    for k in ("vmaf", "psnr_hvs", "ms_ssim", "ciede", "cambi"):
        assert not w.m_check[k].isEnabled() and w.m_val[k].text() == "нет в сборке ffmpeg"
    assert w.m_check["psnr"].isEnabled() and w.m_check["ssim"].isEnabled()
    src = os.path.join(media, "Movie (2017).mkv")
    w.add_paths([src])
    assert pump(qapp, 20, lambda: w.jobs and w.jobs[0].status == "wait")
    j = w.jobs[0]
    j.settings = J.merged({"video": {"encoder": "x264", "speed": 0, "height": 360}})
    w.start_queue()
    assert pump(qapp, 240, lambda: j.status in ("done", "error"))
    assert j.status == "done" and list(j.metrics) == ["psnr"]
    assert "ℹ Недоступно в этой сборке FFmpeg: VMAF, CAMBI" in w.log.toPlainText()
    w._show_info(j)
    assert "Недоступно в этой сборке FFmpeg: VMAF, CAMBI" in w.i_text.text()
    # and with an older libvmaf (no CAMBI): VMAF is there, CAMBI is not
    w.av.filters.add("libvmaf")
    w.av.vmaf_feats = {"psnr_hvs", "float_ms_ssim", "ciede"}
    w._show_metrics(None)
    assert w.m_check["vmaf"].isEnabled() and not w.m_check["cambi"].isEnabled()
    assert w.m_check["cambi"].toolTip() == "В libvmaf этой сборки FFmpeg нет CAMBI"
    w.clear_queue(ask=False)
    w.close()


# ── 4. two passes with the sound encoded (FFmpeg 8.1+ took «-pass 2» for the audio encoder too) ──
def test_two_pass_flags_only_for_video(media, tmp_path):
    src = os.path.join(media, "Movie (2017).mkv")
    j = J.Job(src=src, settings=J.merged({"video": {"encoder": "x264", "rc": "bitrate", "bitrate": 800,
                                                    "passes": 2, "height": 360},
                                          "audio": {"codec": "ac3"}}), info=P.probe(src))
    av = E.detect()
    out = str(tmp_path / "o.mkv")
    pre = J.pre_passes(j, ff.ffmpeg(), out, av)
    args = J.build_args(j, ff.ffmpeg(), out, av)
    assert "-pass" not in args and "-pass:v" in args and "-passlogfile:v" in args
    for a in pre + [args]:
        r = subprocess.run(a, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-500:]
    J.cleanup(j)
    assert P.probe(out).of("audio")[0].codec == "ac3"


# ── 5. the version ────────────────────────────────────────────
