"""1.7: before / after — the source and the result at the same moment, one over the other with a curtain to drag
(source on the left, result on the right). The result is the finished file, or a 10-second test piece encoded from
the current moment with the task's settings. Both are shown at the result's frame size, the source cropped the
same way, so the picture lines up."""
from __future__ import annotations

import copy
import os
import tempfile
import threading

from PySide6.QtCore import QObject, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from . import ff, style
from . import filters as F
from . import job as J
from .i18n import t

PIECE = 10.0


def _frame(ffmpeg: str, path: str, at: float, vf: str, out_png: str, run=None) -> str:
    """One frame of <path> at <at> s through <vf> into a PNG; "" if it failed."""
    run = run or ff.run
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{max(0.0, at):.3f}", "-i", path,
            "-map", "0:v:0", "-frames:v", "1"] + (["-vf", vf] if vf else []) + ["-update", "1", out_png]
    try:
        r = run(args, 120)
    except Exception:                                     # noqa: BLE001
        return ""
    return out_png if getattr(r, "returncode", 1) == 0 and os.path.isfile(out_png) else ""


def source_vf(job, w: int, h: int) -> str:
    """The source the way the result frames it: deinterlaced, cropped, rotated, scaled to w × h."""
    chain = F.interlace_filters(job) + F.crop_filter(job) + F.rotate_filters(job)
    chain.append(f"scale={w}:{h}:flags=bicubic,setsar=1")
    return ",".join(chain)


def piece_job(job, at: float):
    """A copy of the task that encodes <PIECE> seconds from <at> of the source (one pass, no split, no metrics)."""
    k = copy.copy(job)
    k.settings = copy.deepcopy(job.settings)
    v = k.settings.setdefault("video", {})
    if int(v.get("passes", 1) or 1) == 2:
        v["passes"] = 1
        if v.get("rc") == "size":                         # a size makes no sense for a piece: its bit rate
            v["rc"] = "bitrate"
            dur = J.out_duration(job) or 1.0
            v["bitrate"] = max(100, int(float(v.get("size_mb", 700) or 700) * 8192 / dur) - 128)
    k.settings["split"] = {"mode": "off"}
    k.trim, k.trim_cut, k.joins, k.chapters = [[at, at + PIECE]], False, [], []
    k.metrics, k.parts = {}, []
    k.tracks = {key: dict(val, keep=False) for key, val in job.tracks.items()}
    k.ext = [dict(e) for e in job.ext if e.get("kind") == "subtitle" and e.get("burn")]    # 2.0: burnt in
    from . import hardsub as HS
    HS.pin(job, k)
    return k


class Grabber(QObject):
    """Makes the two frames in a thread: done(source_png, result_png, error)."""
    done = Signal(str, str, str)
    status = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dir = tempfile.mkdtemp(prefix="artcore_compare_")
        self.piece = ""                                    # the test piece made last, and from where
        self.piece_at = -1.0
        self.piece_key = ""
        self.busy = False

    def grab(self, job, av, at: float, use_result: bool = True, hw: bool = True):
        if self.busy:
            return
        self.busy = True
        threading.Thread(target=self._work, args=(job, av, at, use_result, hw), daemon=True).start()

    def _work(self, job, av, at, use_result, hw):
        try:
            self.done.emit(*self.frames(job, av, at, use_result, hw))
        except Exception as e:                            # noqa: BLE001
            self.done.emit("", "", str(e))
        finally:
            self.busy = False

    def frames(self, job, av, at: float, use_result: bool = True, hw: bool = True) -> tuple[str, str, str]:
        exe = ff.ffmpeg() or "ffmpeg"
        enc = J.resolve(job, av) if av is not None else None
        w, h = J.out_size(job, enc)
        if not w or not h:
            return "", "", "no video"
        n = len(os.listdir(self.dir))
        a_png = os.path.join(self.dir, f"src_{n}.png")
        b_png = os.path.join(self.dir, f"out_{n}.png")
        src = _frame(exe, job.src, at, source_vf(job, w, h), a_png)
        files = job.parts or [job.out]
        if use_result and job.status == "done" and len(files) == 1 and files[0] and os.path.isfile(files[0]):
            rt = J.map_time(J.segments(job), at)
            if rt is None:
                return src, "", t("cmp_cut_out")
            path, rel = files[0], rt
        else:
            key = repr((job.settings, job.uid))
            if not (self.piece and os.path.isfile(self.piece) and self.piece_key == key
                    and self.piece_at <= at < self.piece_at + PIECE - 0.1):
                self.status.emit(t("cmp_encoding"))
                k = piece_job(job, at)
                ext = J.out_ext(k.settings)
                out = os.path.join(self.dir, f"piece_{n}" + (ext if ext.startswith(".") else "." + ext))
                args = J.build_args(k, exe, out, av, hw)
                r = ff.run(args, 1800)
                if getattr(r, "returncode", 1) != 0 or not os.path.isfile(out):
                    err = (getattr(r, "stderr", "") or "").strip().splitlines()
                    return src, "", (err[-1] if err else "ffmpeg")
                self.piece, self.piece_at, self.piece_key = out, at, key
            path, rel = self.piece, at - self.piece_at
        res = _frame(exe, path, rel, f"scale={w}:{h}:flags=bicubic,setsar=1", b_png)
        return src, res, "" if res else t("cmp_failed")


class CompareView(QWidget):
    """The two frames with a curtain: drag (or click) to move it; ← → a second back / on; Esc closes."""
    closed = Signal()
    moved = Signal(float)                                  # ask for the frames at another moment (seconds)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.a = QPixmap()
        self.b = QPixmap()
        self.split = 0.5
        self.at = 0.0
        self.note = ""
        self.label_b = t("cmp_result")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        bar.addStretch(1)

        def btn(icon, tip, slot):
            b = QToolButton()
            b.setObjectName("ctl")
            b.setIcon(style.icon(icon, "#ffffff"))
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False: slot())
            b.setStyleSheet("QToolButton { background: rgba(0,0,0,120); border-radius: 4px; padding: 5px; }")
            bar.addWidget(b)
            return b
        self.b_back = btn("fa6s.backward-step", t("cmp_back") + " (←)", lambda: self.moved.emit(max(0.0, self.at - 1)))
        self.b_on = btn("fa6s.forward-step", t("cmp_on") + " (→)", lambda: self.moved.emit(self.at + 1))
        self.b_close = btn("fa6s.xmark", t("cmp_close") + " (Esc)", lambda: self.closed.emit())
        lay.addLayout(bar)
        lay.addStretch(1)
        self.info = QLabel("")
        self.info.setAlignment(Qt.AlignCenter)
        self.info.setStyleSheet("color: #ffffff; background: rgba(0,0,0,140); padding: 4px 10px; border-radius: 4px;")
        lay.addWidget(self.info, 0, Qt.AlignHCenter)

    def set_frames(self, a: str, b: str, at: float, note: str = "", label_b: str = ""):
        self.a = QPixmap(a) if a else QPixmap()
        self.b = QPixmap(b) if b else QPixmap()
        self.at = at
        self.note = note
        if label_b:
            self.label_b = label_b
        self.info.setText(f"{J.fmt_tc(at)}" + (f" — {note}" if note else ""))
        self.update()

    def set_busy(self, text: str):
        self.info.setText(text)

    def _rect(self) -> QRect:
        ref = self.b if not self.b.isNull() else self.a
        if ref.isNull():
            return QRect()
        s = ref.size().scaled(self.size(), Qt.KeepAspectRatio)
        return QRect((self.width() - s.width()) // 2, (self.height() - s.height()) // 2, s.width(), s.height())

    def paintEvent(self, e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        r = self._rect()
        if r.isNull():
            p.end()
            return
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        x = r.left() + int(r.width() * self.split)
        if not self.a.isNull():
            p.save()
            p.setClipRect(QRect(r.left(), r.top(), x - r.left(), r.height()))
            p.drawPixmap(r, self.a)
            p.restore()
        if not self.b.isNull():
            p.save()
            p.setClipRect(QRect(x, r.top(), r.right() - x + 1, r.height()))
            p.drawPixmap(r, self.b)
            p.restore()
        p.setPen(QPen(QColor("#ffffff"), 2))
        p.drawLine(x, r.top(), x, r.bottom())
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(x, r.center().y()), 7, 7)
        f = p.font()
        f.setBold(True)
        p.setFont(f)
        for text, left in ((t("cmp_source"), True), (self.label_b, False)):
            fm = p.fontMetrics()
            tw = fm.horizontalAdvance(text) + 14
            box = QRect(r.left() + 8 if left else r.right() - 8 - tw, r.top() + 8, tw, fm.height() + 6)
            p.fillRect(box, QColor(0, 0, 0, 150))
            p.setPen(QColor("#ffffff"))
            p.drawText(box, Qt.AlignCenter, text)
        p.end()

    def _move_to(self, x: float):
        r = self._rect()
        if r.width() > 0:
            self.split = min(1.0, max(0.0, (x - r.left()) / r.width()))
            self.update()

    def mousePressEvent(self, e):
        self._move_to(e.position().x())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._move_to(e.position().x())

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.closed.emit()
        elif e.key() == Qt.Key_Left:
            self.moved.emit(max(0.0, self.at - 1))
        elif e.key() == Qt.Key_Right:
            self.moved.emit(self.at + 1)
        else:
            super().keyPressEvent(e)
