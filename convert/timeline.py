"""The strip under the preview (as in XviD4PSP 8): thumbnails of the film over its loudness curve, the trim
pieces, the In / Out marks and the play head; a click or drag seeks. The thumbnails and the curve are made in
the background by ffmpeg (MediaWorker), the newest file wins."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import ff, style

STRIP_N = 8
WAVE_COLS = 1200


class MediaWorker(QObject):
    """ffmpeg in a thread of its own: STRIP_N frames across the film and the loudness peaks of its first
    audio track. Results come back as signals (path, …); a newer request drops an older one."""
    strip = Signal(str, list)            # path, [PNG bytes]
    wave = Signal(str, list)             # path, [0..1 peaks]

    def __init__(self):
        super().__init__()
        self._want: tuple | None = None
        self._busy = False
        self._lock = threading.Lock()
        self.cache: dict[str, tuple[list, list]] = {}

    def request(self, path: str, duration: float, has_video: bool, has_audio: bool):
        if path in self.cache:
            s, w = self.cache[path]
            self.strip.emit(path, s)
            self.wave.emit(path, w)
            return
        with self._lock:
            self._want = (path, duration, has_video, has_audio)
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._run, daemon=True).start()

    def _newer(self, path: str) -> bool:
        with self._lock:
            return self._want is not None and self._want[0] != path

    def _run(self):
        while True:
            with self._lock:
                job, self._want = self._want, None
                if job is None:
                    self._busy = False
                    return
            path, dur, has_v, has_a = job
            exe = ff.ffmpeg()
            frames: list = []
            if exe and has_v and dur > 0:
                for i in range(STRIP_N):
                    if self._newer(path):
                        break
                    at = dur * (i + 0.5) / STRIP_N
                    try:
                        r = ff.run([exe, "-hide_banner", "-v", "error", "-ss", f"{at:.2f}", "-i", path, "-map", "0:v:0",
                                    "-frames:v", "1", "-vf", "scale=-2:72", "-f", "image2pipe", "-c:v", "png", "-"],
                                   30, binary=True)
                        frames.append(r.stdout if r.returncode == 0 else b"")
                    except Exception:
                        frames.append(b"")
                self.strip.emit(path, frames)
            else:
                self.strip.emit(path, [])
            peaks: list = []
            if exe and has_a and dur > 0 and not self._newer(path):
                # the peak of every 1/WAVE_COLS of the film (astats per window, at 8 kHz: fast and no lost peaks)
                n = max(1, int(8000 * dur / WAVE_COLS))
                af = (f"aformat=channel_layouts=mono,aresample=8000,asetnsamples=n={n}:p=0,"
                      "astats=metadata=1:reset=1:measure_overall=Peak_level:measure_perchannel=none,"
                      "ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-")
                try:
                    r = ff.run([exe, "-hide_banner", "-v", "error", "-i", path, "-map", "0:a:0", "-vn", "-sn", "-dn",
                                "-af", af, "-f", "null", "-"], max(60.0, dur / 6))
                    for line in r.stdout.splitlines():
                        if "Peak_level=" in line:
                            v = line.split("=", 1)[1].strip()
                            try:
                                db = float(v)
                                peaks.append(10 ** (db / 20) if db > -120 else 0.0)
                            except ValueError:
                                peaks.append(0.0)
                    top = max(peaks) if peaks else 0.0
                    peaks = [p / top for p in peaks] if top > 0 else []
                except Exception:
                    peaks = []
            self.wave.emit(path, peaks)
            self.cache[path] = (frames, peaks)


class Timeline(QWidget):
    """Thumbnails on top, the loudness below; pieces, marks and the play head over both."""
    seek = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dur = 0.0
        self.pos = 0.0
        self.keep: list[tuple[float, float]] = []      # what goes out (J.segments); [] = all
        self.mark_in: float | None = None
        self.mark_out: float | None = None
        self.frames: list[QPixmap] = []
        self.peaks: list[float] = []
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(128)
        self.setCursor(Qt.PointingHandCursor)

    def set_strip(self, pngs: list):
        self.frames = []
        for data in pngs:
            pm = QPixmap()
            self.frames.append(pm if data and pm.loadFromData(data) else QPixmap())
        self.update()

    def set_wave(self, peaks: list):
        self.peaks = list(peaks)
        self.update()

    def clear_media(self):
        self.frames, self.peaks = [], []
        self.update()

    def _x(self, sec: float) -> float:
        return self.width() * (sec / self.dur if self.dur > 0 else 0.0)

    def _t(self, x: float) -> float:
        return max(0.0, min(self.dur, x / max(1, self.width()) * self.dur)) if self.dur > 0 else 0.0

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor(style.PANEL2))
        strip_h = h * 0.52 if self.frames else 0
        # thumbnails, each scaled to fill its share of the width
        if self.frames:
            cw = w / len(self.frames)
            p.fillRect(QRectF(0, 0, w, strip_h), QColor("#000000"))
            for i, pm in enumerate(self.frames):
                if pm.isNull():
                    continue
                cell = QRectF(i * cw, 0, cw, strip_h)
                sc = pm.scaled(int(cw) + 1, int(strip_h), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                sx = (sc.width() - cell.width()) / 2
                p.drawPixmap(cell, sc, QRectF(sx, (sc.height() - strip_h) / 2, cell.width(), strip_h))
        # the loudness, mirrored around its middle line
        wave = QRectF(0, strip_h, w, h - strip_h)
        if self.peaks:
            mid = wave.center().y()
            amp = wave.height() / 2 - 2
            path = QPainterPath()
            n = len(self.peaks)
            path.moveTo(0, mid)
            for i, v in enumerate(self.peaks):
                path.lineTo(w * i / max(1, n - 1), mid - v * amp)
            for i in range(n - 1, -1, -1):
                path.lineTo(w * i / max(1, n - 1), mid + self.peaks[i] * amp)
            path.closeSubpath()
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#9061d6"))
            p.drawPath(path)
        if self.dur > 0:
            # the cut-out parts dimmed, the pieces outlined
            if self.keep:
                shade = QColor(0, 0, 0, 150)
                pos = 0.0
                for a, b in self.keep + [(self.dur, self.dur)]:
                    if a > pos:
                        p.fillRect(QRectF(self._x(pos), 0, self._x(a) - self._x(pos), h), shade)
                    pos = max(pos, b)
                p.setPen(QPen(QColor(style.ACCENT), 2))
                p.setBrush(Qt.NoBrush)
                for a, b in self.keep:
                    p.drawRect(QRectF(self._x(a) + 1, 1, max(2.0, self._x(b) - self._x(a) - 2), h - 2))
            if self.mark_in is not None and self.mark_out is not None and self.mark_out > self.mark_in:
                c = QColor(style.WARN)
                c.setAlpha(70)
                p.fillRect(QRectF(self._x(self.mark_in), 0, self._x(self.mark_out) - self._x(self.mark_in), h), c)
            for m in (self.mark_in, self.mark_out):
                if m is not None:
                    p.setPen(QPen(QColor(style.WARN), 2))
                    p.drawLine(int(self._x(m)), 0, int(self._x(m)), h)
            x = int(self._x(self.pos))
            p.setPen(QPen(QColor("#ff3b30"), 2))
            p.drawLine(x, 0, x, h)
        p.end()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.dur > 0:
            self.seek.emit(self._t(e.position().x()))

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton and self.dur > 0:
            self.seek.emit(self._t(e.position().x()))
