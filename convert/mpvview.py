"""The picture of the Trim tab: libmpv (as in ArtCore Player) renders into a QOpenGLWidget, so frames can be
stepped one by one and any moment shown exactly. Without libmpv-2.dll the tab falls back to still frames
from ffmpeg (FramePreview)."""
from __future__ import annotations

import locale
import os
import sys
import threading

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QOpenGLContext, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QStackedLayout, QWidget

from . import ff
from .winint import IS_WIN

_mpv = None
_mpv_error = ""


def prepare_libmpv() -> list[str]:
    """Make libmpv-2.dll findable: next to the exe, in PyInstaller's _internal, ./libmpv of the source tree."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirs = []
    for d in (os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else "", getattr(sys, "_MEIPASS", ""),
              root, os.path.join(root, "libmpv")):
        if d and os.path.isdir(d) and d not in dirs:
            dirs.append(d)
    if IS_WIN:
        os.environ["PATH"] = os.pathsep.join(dirs + [os.environ.get("PATH", "")])
        for d in dirs:
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
    return dirs


def load_mpv():
    """The python-mpv module, or None (the reason in mpv_error())."""
    global _mpv, _mpv_error
    if _mpv is not None or _mpv_error:
        return _mpv
    if os.environ.get("ARTCORE_NO_MPV"):
        _mpv_error = "disabled"
        return None
    try:
        prepare_libmpv()
        locale.setlocale(locale.LC_NUMERIC, "C")            # libmpv refuses to start with a non-C LC_NUMERIC
        import mpv
        _mpv = mpv
    except Exception as e:                                   # OSError: no libmpv-2.dll; ImportError: no python-mpv
        _mpv_error = str(e) or e.__class__.__name__
    return _mpv


def mpv_error() -> str:
    return _mpv_error


class _Bridge(QObject):
    prop = Signal(str, object)


def _video_widget_class():
    """ArtCore Player's video surface (player/video.py), without its mouse handling: libmpv's render API into
    a QOpenGLWidget, frames reported as swapped for smooth timing."""
    from PySide6.QtOpenGLWidgets import QOpenGLWidget

    class VideoWidget(QOpenGLWidget):
        frame = Signal()                     # mpv has a new frame (emitted from mpv's thread)
        ready = Signal()                     # the render context exists: files may be loaded now

        def __init__(self, mpv_module, player, parent=None):
            super().__init__(parent)
            self._mpv = mpv_module
            self.player = player
            self.ctx = None
            self._gpa = None
            self.setFocusPolicy(Qt.NoFocus)
            self.setAttribute(Qt.WA_OpaquePaintEvent)
            self.frame.connect(self.update, Qt.QueuedConnection)
            self.frameSwapped.connect(self._swapped)

        def initializeGL(self):
            ctx = QOpenGLContext.currentContext()

            def get_proc_address(_, name):
                addr = ctx.getProcAddress(name.decode() if isinstance(name, bytes) else name)
                return int(addr) if addr else 0
            self._gpa = self._mpv.MpvGlGetProcAddressFn(get_proc_address)
            self.ctx = self._mpv.MpvRenderContext(self.player, "opengl",
                                                  opengl_init_params={"get_proc_address": self._gpa})
            self.ctx.update_cb = self._on_update
            self.ready.emit()

        def _on_update(self):
            try:
                self.frame.emit()
            except RuntimeError:           # widget already gone (shutdown)
                pass

        def paintGL(self):
            if self.ctx is None:
                f = self.context().functions()
                f.glClearColor(0, 0, 0, 1)
                f.glClear(0x4000)          # GL_COLOR_BUFFER_BIT
                return
            r = self.devicePixelRatioF()
            self.ctx.render(flip_y=True, opengl_fbo={"w": int(self.width() * r), "h": int(self.height() * r),
                                                     "fbo": self.defaultFramebufferObject()})

        def _swapped(self):
            if self.ctx is not None:
                try:
                    self.ctx.report_swap()
                except Exception:
                    pass

        def shutdown(self):
            """Free the render context before mpv itself is terminated (with the GL context current)."""
            if self.ctx is not None:
                self.makeCurrent()
                try:
                    self.ctx.update_cb = None
                    self.ctx.free()
                except Exception:
                    pass
                self.ctx = None
                self.doneCurrent()
    return VideoWidget


def VideoWidget(mpv_module, player, parent=None):        # noqa: N802 — built on first use (QtOpenGLWidgets)
    global _VW
    if _VW is None:
        _VW = _video_widget_class()
    return _VW(mpv_module, player, parent)


_VW = None


class MpvView(QWidget):
    """The libmpv picture + the player itself. pos / duration in seconds; signals in the GUI thread."""
    position = Signal(float)
    duration = Signal(float)
    paused = Signal(bool)
    failed = Signal(str)

    def __init__(self, mpvmod, parent=None):
        super().__init__(parent)
        self.mpvmod = mpvmod
        self.pos = 0.0
        self.dur = 0.0
        self.is_paused = True
        self.path = ""
        self._bridge = _Bridge()
        self._bridge.prop.connect(self._on_prop, Qt.QueuedConnection)
        self.dead = False
        extra = {"ao": os.environ["ARTCORE_MPV_AO"]} if os.environ.get("ARTCORE_MPV_AO") else {}  # tests: null
        self.player = mpvmod.MPV(vo="libmpv", hwdec="auto-safe", keep_open="yes", idle="yes", pause=True, osc=False,
                                 input_default_bindings=False, input_vo_keyboard=False, config=False, terminal=False,
                                 ytdl=False, hr_seek="yes", volume=70, audio_display="no", osd_level=0,
                                 sub_auto="no", demuxer_max_bytes="150MiB", demuxer_max_back_bytes="150MiB", **extra)
        for name in ("time-pos", "duration", "pause"):
            self.player.observe_property(name, lambda n, v: self._bridge.prop.emit(n, v))
        self.ctx = None
        self._pending = ""
        self.gl = VideoWidget(mpvmod, self.player, self)
        self.gl.ready.connect(self._gl_ready)
        lay = QStackedLayout(self)
        lay.addWidget(self.gl)

    def _gl_ready(self):
        self.ctx = self.gl.ctx
        if self._pending:
            QTimer.singleShot(0, lambda: self.load(self._pending))

    def _on_prop(self, name: str, v):
        if name == "time-pos" and v is not None:
            self.pos = float(v)
            self.position.emit(self.pos)
        elif name == "duration" and v is not None:
            self.dur = float(v)
            self.duration.emit(self.dur)
        elif name == "pause" and v is not None:
            self.is_paused = bool(v)
            self.paused.emit(self.is_paused)

    # ── control ───────────────────────────────────────────────
    def load(self, path: str):
        if self.dead:
            return
        if self.ctx is None:                 # the GL context comes when the widget is first shown
            self._pending = path
            return
        self._pending = ""
        if path == self.path:
            return
        self.path = path
        self.pos = 0.0
        self.player.pause = True
        self.player.play(path)

    def unload(self):
        self._pending = ""
        if self.path and not self.dead:
            self.path = ""
            try:
                self.player.command("stop")
            except Exception:
                pass

    def seek(self, t: float, exact: bool = True):
        if not self.path or self.dead:
            return
        try:
            self.player.command("seek", max(0.0, t), "absolute+exact" if exact else "absolute+keyframes")
        except Exception:
            pass

    def step(self, frames: int):
        if not self.path or self.dead:
            return
        try:
            self.player.pause = True
            self.player.command("frame-step" if frames > 0 else "frame-back-step")
        except Exception:
            pass

    def toggle(self):
        if self.path and not self.dead:
            self.player.pause = not self.is_paused

    def pause(self):
        if self.path and not self.dead:
            self.player.pause = True

    def set_mute(self, on: bool):
        if not self.dead:
            self.player.mute = bool(on)

    def shutdown(self):
        if self.dead:
            return
        self.dead = True
        try:
            self.gl.shutdown()
            self.ctx = None
            self.player.terminate()
        except Exception:
            pass


class FramePreview(QWidget):
    """No libmpv: a still frame from ffmpeg at the asked moment (the newest request wins)."""
    position = Signal(float)
    duration = Signal(float)
    paused = Signal(bool)
    failed = Signal(str)
    _frame = Signal(float, bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pos = 0.0
        self.dur = 0.0
        self.fps = 25.0
        self.is_paused = True
        self.path = ""
        self.pic = QLabel()
        self.pic.setAlignment(Qt.AlignCenter)
        self.pic.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.pic.setStyleSheet("background: #000;")
        self._pm = QPixmap()
        lay = QStackedLayout(self)
        lay.addWidget(self.pic)
        self._want = None
        self._busy = False
        self._lock = threading.Lock()
        self._frame.connect(self._on_frame, Qt.QueuedConnection)

    def load(self, path: str, duration: float = 0.0, fps: float = 25.0):
        self.path, self.dur, self.fps = path, duration, fps or 25.0
        self.duration.emit(duration)
        self.seek(0.0)

    def unload(self):
        self.path = ""
        self._pm = QPixmap()
        self.pic.clear()

    def seek(self, t: float, exact: bool = True):
        if not self.path:
            return
        self.pos = max(0.0, min(t, self.dur or t))
        self.position.emit(self.pos)
        with self._lock:
            self._want = (self.path, self.pos)
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._grab, daemon=True).start()

    def _grab(self):
        while True:
            with self._lock:
                job, self._want = self._want, None
                if job is None:
                    self._busy = False
                    return
            path, at = job
            data = b""
            exe = ff.ffmpeg()
            if exe:
                try:
                    r = ff.run([exe, "-hide_banner", "-v", "error", "-ss", f"{at:.3f}", "-i", path, "-map", "0:v:0",
                                "-frames:v", "1", "-vf", "scale=640:-2", "-f", "image2pipe", "-c:v", "png", "-"],
                               30, binary=True)
                    data = r.stdout if r.returncode == 0 else b""
                except Exception:
                    data = b""
            self._frame.emit(at, data)

    def _on_frame(self, at: float, data: bytes):
        pm = QPixmap()
        if data and pm.loadFromData(data):
            self._pm = pm
            self._fit()

    def _fit(self):
        if not self._pm.isNull():
            self.pic.setPixmap(self._pm.scaled(self.pic.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def step(self, frames: int):
        self.seek(self.pos + frames / self.fps)

    def toggle(self):
        pass

    def pause(self):
        pass

    def set_mute(self, on: bool):
        pass

    def shutdown(self):
        pass

