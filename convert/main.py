"""Entry point: one window; files sent from Explorer («Convert with ArtCore Convert») go to the running copy.
ArtCoreConvert.exe [files / folders …]"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget

from . import APP_ID, APP_TITLE, VERSION, config, winint
from .i18n import set_lang

SERVER_NAME = "ArtCoreConvert-" + (os.environ.get("USERNAME") or os.environ.get("USER") or "user")


def _send_to_running(paths: list[str]) -> bool:
    s = QLocalSocket()
    s.connectToServer(SERVER_NAME)
    if not s.waitForConnected(400):
        return False
    s.write(("\n".join(os.path.abspath(p) for p in paths) if paths else "--show").encode("utf-8"))
    s.waitForBytesWritten(1000)
    s.disconnectFromServer()
    return True


def _surface_format() -> None:
    """libmpv's OpenGL renderer (the Trim preview) wants a 3.3+ core context, as in ArtCore Player."""
    f = QSurfaceFormat()
    f.setVersion(3, 3)
    f.setProfile(QSurfaceFormat.CoreProfile)
    f.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(f)


class TitleBars(QObject):
    """Every window and dialog gets a title bar in the colours of the theme when it is first shown."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow() and \
                not obj.property("acTitled"):
            obj.setProperty("acTitled", True)
            from . import style
            winint.dark_title_bar(int(obj.winId()), style.IS_DARK)
        return False


def selftest() -> int:
    """CI: the built exe finds its ffmpeg / ffprobe and libmpv, checks the encoders, builds the window and
    converts a short test clip through the real queue: x264 + AAC to MP4, x265 10 bit + copy to MKV, and (1.1)
    a trimmed VP9 WebM with edited tags and chapters, Xvid AVI, GIF and MP3. Result in %TEMP%."""
    import tempfile
    import time
    import traceback
    out = os.path.join(tempfile.gettempdir(), "artcore_convert_selftest.txt")
    lines = []
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        tmp = os.path.join(tempfile.gettempdir(), "artcore_convert_selftest")
        os.environ.setdefault("ARTCORE_CONVERT_DATA", os.path.join(tmp, "data"))
        app = QApplication(sys.argv)
        from . import encoders as E, ff, probe, style
        app.setStyle("Fusion")
        app.setStyleSheet(style.stylesheet())
        lines.append(f"OK {VERSION}; ffmpeg {ff.version()} at {ff.ffmpeg()}; ffprobe {ff.ffprobe()}")
        if not ff.ffmpeg() or not ff.ffprobe():
            raise RuntimeError("ffmpeg / ffprobe not found")
        av = E.detect()
        lines.append("encoders: " + ", ".join(f"{e.ffname}={'ok' if av.ok.get(e.id) else 'no'}" for e in E.VIDEO))
        need = {"libx264", "libx265", "libsvtav1", "aac", "ac3", "eac3", "libopus", "flac", "libmp3lame",
                "h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_amf", "hevc_amf", "h264_qsv", "hevc_qsv",
                "libvpx-vp9", "mpeg4", "mpeg2video", "prores_ks", "gif", "libvorbis", "mp2", "alac", "pcm_s16le",
                "pcm_s24le"}
        missing = need - av.audio
        if missing:
            raise RuntimeError(f"this ffmpeg build lacks {sorted(missing)}")
        problems = [p for e in E.VIDEO for p in E.check_options(e)]   # GPU arguments vs this ffmpeg, no GPU needed
        lines.append(f"options of {len(E.VIDEO)} encoders checked: " + ("ok" if not problems else "; ".join(problems)))
        if problems:
            raise RuntimeError("encoder options: " + "; ".join(problems))
        src = os.path.join(tmp, "clip.mkv")
        os.makedirs(tmp, exist_ok=True)
        r = ff.run([ff.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=640x360:r=25:d=2", "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "ac3", "-ac", "6", src], 60)
        if r.returncode != 0:
            raise RuntimeError("making the test clip failed: " + r.stderr[-300:])
        lines.append("libxvid: " + ("yes" if "libxvid" in av.audio else "no (FFmpeg's mpeg4 is used for Xvid)"))
        # libmpv: loads and plays (no picture needed here: the offscreen platform has no OpenGL)
        from . import mpvview
        mod = mpvview.load_mpv()
        if mod is None:
            raise RuntimeError("libmpv: " + mpvview.mpv_error())
        m = mod.MPV(vo="null", ao="null", pause=True, config=False, terminal=False, ytdl=False, hr_seek="yes")
        lines.append(f"libmpv {m.mpv_version}, ffmpeg {m.ffmpeg_version}")
        m.play(src)
        t0 = time.monotonic()
        while not m.duration and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        m.command("seek", 1.0, "absolute+exact")
        t0 = time.monotonic()
        while abs((m.time_pos or 0) - 1.0) > 0.05 and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        lines.append(f"  mpv: duration {m.duration}, after seek {m.time_pos}")
        if not m.duration or abs((m.time_pos or 0) - 1.0) > 0.05:
            raise RuntimeError("libmpv did not open / seek the clip")
        m.terminate()
        cfg = config.load()
        cfg.update(check_updates=False, keep_queue=False, out_mode="folder", out_dir=os.path.join(tmp, "out"),
                   overwrite=True, on_done="nothing", notify=False)
        from . import job as J
        from .window import MainWindow
        win = MainWindow(cfg)
        win.show()
        t0 = time.monotonic()
        while not win.av_ready and time.monotonic() - t0 < 60:
            app.processEvents()
            time.sleep(0.02)
        win.add_paths([src])
        win.add_paths([src])                      # the same file again is skipped …
        j1 = win.jobs[0]
        j1.settings = J.merged({"container": "mp4", "video": {"encoder": "x264"}, "audio": {"codec": "aac"}})
        j2 = J.Job(src=src, settings=J.merged({"container": "mkv", "video": {"encoder": "x265", "bits": "10"},
                                                "audio": {"codec": "copy"}}))
        win._append(j2)                          # … so add the second task directly
        j3 = J.Job(src=src, settings=J.fit_container(J.merged({"container": "webm", "video": {"speed": 1}})))
        j3.trim = [[0.2, 0.8], [1.2, 1.8]]
        j3.meta = {"tags": {"title": "Selftest"}, "streams": {"1": {"lang": "rus"}}}
        j3.chapters = [[0.0, "A"], [0.6, "B"]]
        jobs_11 = [j3] + [J.Job(src=src, settings=J.fit_container(J.merged(st))) for st in (
            {"container": "avi", "video": {"encoder": "auto_mpeg4"}},
            {"container": "gif"},
            {"container": "mp3"},
            {"container": "mov", "video": {"encoder": "prores", "quality": 1}})]
        for j in jobs_11:
            win._append(j)
        win._queue_changed()
        win.view.setCurrentItem(win.items[j3.uid])
        t0 = time.monotonic()
        while any(j.status == "probe" for j in win.jobs) and time.monotonic() - t0 < 30:
            app.processEvents()
            time.sleep(0.02)
        for i in range(win.side_bar.count()):                    # every side tab shows the task (Trim, Metadata …)
            win.side_bar.setCurrentIndex(i)
            app.processEvents()
        if win.trim_tab.job is not j3 or not win.meta_tab.tags.topLevelItemCount():
            raise RuntimeError("the Trim / Metadata tabs did not take the task")
        win.show_side("streams")
        t0 = time.monotonic()
        while not (win.trim_tab.bar.frames and win.trim_tab.bar.peaks) and time.monotonic() - t0 < 30:
            app.processEvents()
            time.sleep(0.02)
        lines.append(f"strip: {len(win.trim_tab.bar.frames)} frames, loudness: {len(win.trim_tab.bar.peaks)} points")
        if not win.trim_tab.bar.peaks:
            raise RuntimeError("no loudness curve under the picture")
        win.start_queue()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120 and any(j.status not in ("done", "error") for j in win.jobs):
            app.processEvents()
            time.sleep(0.02)
        for j in win.jobs:
            lines.append(f"{j.status}: {j.out} {j.error}")
            if j.status != "done":
                raise RuntimeError(f"conversion failed: {j.error}")
            info = probe.probe(j.out)
            lines.append("  " + probe.summary(info) + (" · " + probe.video_line(info.video) if info.video else ""))
        if probe.probe(j2.out).video.bits != 10:
            raise RuntimeError("x265 10 bit came out 8 bit")
        r3 = probe.probe(j3.out)
        want = {"video": "vp9", "dur": 1.2, "title": "Selftest", "chapters": ["A", "B"], "lang": "rus"}
        got = {"video": r3.video.codec, "dur": round(r3.duration, 1), "title": r3.tags.get("title"),
               "chapters": [c[2] for c in r3.chapter_list], "lang": r3.of("audio")[0].lang}
        if abs(got["dur"] - want["dur"]) <= 0.2:
            got["dur"] = want["dur"]
        if got != want:
            raise RuntimeError(f"the trimmed WebM: {got} != {want}")
        kinds = [(probe.probe(j.out).video.codec if probe.probe(j.out).video else "-") for j in jobs_11[1:]]
        if kinds != ["mpeg4", "gif", "-", "prores"]:
            raise RuntimeError(f"AVI / GIF / MP3 / MOV: {kinds}")
        win.close()
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return 0
    except Exception:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\nFAIL\n" + traceback.format_exc())
        return 1


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    paths = [a for a in args if not a.startswith("--")]
    cfg = config.load()
    set_lang(cfg.get("language", "ru"))
    winint.init_process(APP_ID)
    _surface_format()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")
    from . import style
    style.set_theme(cfg.get("theme", "dark"))
    app.setStyleSheet(style.stylesheet())
    app.setWindowIcon(style.app_icon())
    title_bars = TitleBars(app)
    app.installEventFilter(title_bars)
    if cfg.get("language", "ru") == "ru":
        from PySide6.QtCore import QLibraryInfo, QTranslator
        tr = QTranslator(app)
        if tr.load("qtbase_ru", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(tr)
    if cfg.get("single_window", True) and _send_to_running(paths):
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)
    from .window import MainWindow
    win = MainWindow(cfg)
    win.restore_geometry()
    win.show()

    def on_conn():
        s = server.nextPendingConnection()

        def read():
            data = bytes(s.readAll()).decode("utf-8", "replace")
            got = [ln for ln in data.splitlines() if ln and not ln.startswith("--")]
            if win.isMinimized():
                win.showNormal()
            win.raise_()
            win.activateWindow()
            if got:
                win.add_paths(got)
        s.readyRead.connect(read)
    server.newConnection.connect(on_conn)

    def start():
        win.restore_queue()
        if paths:
            win.add_paths(paths)
    QTimer.singleShot(0, start)
    QTimer.singleShot(4000, win.maybe_check_updates)
    code = app.exec()
    server.close()
    return code
