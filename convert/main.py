"""Entry point: one window; files sent from Explorer («Convert with ArtCore Convert») go to the running copy.
ArtCoreConvert.exe [files / folders …]"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

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


def selftest() -> int:
    """CI: the built exe finds its ffmpeg / ffprobe, checks the encoders, builds the window and converts a
    short test clip (x264 + AAC to MP4, x265 10 bit + copy to MKV) through the real queue. Result in %TEMP%."""
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
                "h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_amf", "hevc_amf", "h264_qsv", "hevc_qsv"}
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
        win._queue_changed()
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
            lines.append("  " + probe.summary(info) + " · " + probe.video_line(info.video))
        if probe.probe(j2.out).video.bits != 10:
            raise RuntimeError("x265 10 bit came out 8 bit")
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
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")
    from . import style
    app.setStyleSheet(style.stylesheet())
    app.setWindowIcon(style.app_icon())
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
