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


def crop_spec(rect) -> str:
    """2.1: (x, y, w, h) → mpv's video-crop «WxH+X+Y»; None → "" (no crop: libmpv 0.37 does not always take
    «0x0+0+0» back)."""
    if not rect:
        return ""
    x, y, w, h = (int(v) for v in rect)
    return f"{w}x{h}+{x}+{y}"


def crop_filter(rect) -> str:
    """2.1: (x, y, w, h) → the crop filter (mpv's vf for a libmpv without video-crop, ffmpeg's for the still frames)."""
    x, y, w, h = (int(v) for v in rect)
    return f"crop={w}:{h}:{x}:{y}"


def crop_rect(crop, width: int, height: int):
    """2.1: filters.crop_values (left, top, right, bottom) of a width×height source → (x, y, w, h); None when
    nothing is cut off."""
    if not crop or not width or not height:
        return None
    l, t, r, b = (int(v) for v in crop)
    if not (l or t or r or b):
        return None
    return l, t, width - l - r, height - t - b


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
            self.paints = 0                      # frames drawn (the self-test checks the picture moves)
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
            self.paints += 1
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
        self.seeking = False
        self._steps = 0                          # frame steps asked while a seek still runs
        self.listening = ""                      # 1.7: an outside sound file played with the picture
        self._aid0 = None
        for name in ("time-pos", "duration", "pause", "seeking"):
            self.player.observe_property(name, lambda n, v: self._bridge.prop.emit(n, v))
        self.ctx = None
        self._pending = ""
        self._pending_start = 0.0                # 2.2: where the picture stands once the file is open
        self._start_at = 0.0
        self.gl = VideoWidget(mpvmod, self.player, self)
        self.gl.ready.connect(self._gl_ready)
        lay = QStackedLayout(self)
        lay.addWidget(self.gl)

    def _gl_ready(self):
        self.ctx = self.gl.ctx
        if self._pending:
            QTimer.singleShot(0, lambda: self.load(self._pending, self._pending_start))

    def _on_prop(self, name: str, v):
        if name == "time-pos" and v is not None:
            self.pos = float(v)
            self.position.emit(self.pos)
        elif name == "duration" and v is not None:
            self.dur = float(v)
            self.duration.emit(self.dur)
            if self._start_at > 0:                # the file is open: go to where it was asked to stand
                at, self._start_at = self._start_at, 0.0
                self.seek(at)
        elif name == "seeking":
            self.seeking = bool(v)
            if not self.seeking and self._steps:
                n, self._steps = self._steps, 0
                for _ in range(abs(n)):
                    self.step(1 if n > 0 else -1)
        elif name == "pause" and v is not None:
            self.is_paused = bool(v)
            self.paused.emit(self.is_paused)

    # ── control ───────────────────────────────────────────────
    def load(self, path: str, start: float = 0.0):
        """start (2.2): seconds of the file where the picture stands when it is open (0 = the beginning)."""
        if self.dead:
            return
        if self.ctx is None:                 # the GL context comes when the widget is first shown
            if path != self._pending or start:     # a repeated call for the same file must not lose the start
                self._pending_start = start
            self._pending = path
            return
        self._pending = ""
        if path == self.path:
            return
        self.path = path
        self.pos = 0.0
        self._start_at = max(0.0, float(start or 0.0))
        if self.listening:
            try:
                self.player.audio_delay = 0.0
            except Exception:
                pass
        self.listening, self._aid0 = "", None
        self.player.pause = True
        self.player.play(path)

    def unload(self):
        self._pending = ""
        self._start_at = 0.0
        if self.path and not self.dead:
            self.path = ""
            try:
                self.player.command("stop")
            except Exception:
                pass

    def seek(self, t: float, exact: bool = True):
        if not self.path or self.dead:
            return
        self.seeking = True                      # until mpv says the seek is over (or 1.5 s, whichever first)
        QTimer.singleShot(1500, lambda: self._on_prop("seeking", False) if self.seeking else None)
        try:
            self.player.command("seek", max(0.0, t), "absolute+exact" if exact else "absolute+keyframes")
        except Exception:
            pass

    def step(self, frames: int):
        if not self.path or self.dead:
            return
        if self.seeking:                         # a step during a seek is lost by mpv: do it right after
            self._steps += frames
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

    # ── 2.1: the picture as the result will be ────────────────
    crop = None                                  # (x, y, w, h) of the source shown, None = the whole frame
    crop_by = ""                                 # "video-crop" (libmpv 0.37+) | "vf" (older: a crop filter)

    def set_crop(self, rect):
        """Show only (x, y, w, h) of the source — the same numbers ffmpeg's crop gets — or the whole frame (None).
        libmpv with the video-crop property (0.37+) (drawn by the video output, the GPU decoder stays); older libmpv: a
        crop video filter (the decoding then on the CPU, filters cannot take GPU frames)."""
        if rect:                                 # even: libmpv asserts on odd offsets of 4:2:0 frames
            x, y, w, h = (int(v) for v in rect)
            rect = (x // 2 * 2, y // 2 * 2, w // 2 * 2, h // 2 * 2)
        else:
            rect = None
        if self.dead or rect == self.crop:
            return
        self.crop = rect
        if self.crop_by != "vf":
            try:
                try:
                    self.player["video-crop"] = crop_spec(rect)
                    self._check_crop(3)
                except Exception:
                    if rect or self.crop_by != "video-crop":
                        raise
                    self.player["video-crop"] = "0x0+0+0"   # a libmpv that wants a rectangle to clear it
                self.crop_by = "video-crop"
                return
            except Exception:                    # no such property: an older libmpv
                self.crop_by = "vf"
        try:
            if rect:
                self.player["hwdec"] = "no"
                self.player["vf"] = crop_filter(rect)
            else:
                self.player["vf"] = ""
                self.player["hwdec"] = "auto-safe"
        except Exception:
            pass

    def _check_crop(self, tries: int):
        """libmpv 0.37 sometimes misses a video-crop set while the previous one is still being applied (several
        in a row: the Crop numbers typed in). A moment later: does the picture have the size asked? If not, set it
        once more (through a different value first: setting the same one again is no change to mpv)."""
        try:
            QTimer.singleShot(250, lambda: self._recheck_crop(tries))
        except Exception:
            pass

    def _recheck_crop(self, tries: int):
        if self.dead or self.crop_by != "video-crop" or tries <= 0:
            return
        try:
            o = self.player.video_out_params or {}
            if "crop-w" not in o:
                return
            want = (self.crop[2], self.crop[3]) if self.crop else (o["w"], o["h"])
            if (o["crop-w"], o["crop-h"]) == want:
                return
            if self.crop:
                self.player["video-crop"] = ""
                QTimer.singleShot(60, lambda: self._reset_crop(tries))
            else:                                # «no crop» in its other spelling: a change mpv sees
                self.player["video-crop"] = "0x0+0+0" if not self.player["video-crop"] else ""
                self._check_crop(tries - 1)
        except Exception:
            pass

    def _reset_crop(self, tries: int):
        if self.dead:
            return
        try:
            self.player["video-crop"] = crop_spec(self.crop)
        except Exception:
            return
        self._check_crop(tries - 1)

    # ── 1.7: hear an outside sound track with the picture, at a delay ──
    def listen(self, path: str, delay: float) -> bool:
        """Play the film with <path> as its sound, <delay> seconds later (− earlier) than the picture."""
        if not self.path or self.dead:
            return False
        try:
            if self.listening != path:
                if self._aid0 is None:
                    self._aid0 = self.player.aid
                self.player.command("audio-add", path, "select")
                self.listening = path
            self.player.audio_delay = float(delay)
            self.player.mute = False
            self.player.pause = False
            return True
        except Exception:
            return False

    def set_delay(self, delay: float):
        if self.listening and not self.dead:
            try:
                self.player.audio_delay = float(delay)
            except Exception:
                pass

    def stop_listen(self):
        if not self.listening or self.dead:
            return
        try:
            self.player.pause = True
            self.player.audio_delay = 0.0
            self.player.aid = self._aid0 if self._aid0 not in (None, False) else "auto"
        except Exception:
            pass
        self.listening, self._aid0 = "", None

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
    listening = ""

    def listen(self, path: str, delay: float) -> bool:          # 1.7: stills only: no sound to hear
        return False

    def set_delay(self, delay: float):
        pass

    def stop_listen(self):
        pass

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

    def load(self, path: str, duration: float = 0.0, fps: float = 25.0, start: float = 0.0):
        self.path, self.dur, self.fps = path, duration, fps or 25.0
        self.duration.emit(duration)
        self.seek(max(0.0, float(start or 0.0)))

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
            self._want = (self.path, self.pos, self.crop)
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
            path, at, crop = job
            data = b""
            exe = ff.ffmpeg()
            if exe:
                vf = (crop_filter(crop) + "," if crop else "") + "scale=640:-2"      # 2.1: as the result will be
                try:
                    r = ff.run([exe, "-hide_banner", "-v", "error", "-ss", f"{at:.3f}", *ff.inp(path), "-map", "0:v:0",
                                "-frames:v", "1", "-vf", vf, "-f", "image2pipe", "-c:v", "png", "-"],
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

    crop = None
    crop_by = "ffmpeg"

    def set_crop(self, rect):
        """2.1: the still frames cut as the result will be (x, y, w, h of the source) or whole (None)."""
        rect = tuple(int(v) for v in rect) if rect else None
        if rect != self.crop:
            self.crop = rect
            self.seek(self.pos)

    def shutdown(self):
        pass

