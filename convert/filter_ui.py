"""1.3: the filter rows of the Streams tab (☑ · name · value · ⚙), the small settings window of
each filter, and the frame drawn over the picture (the crop, the logo to remove — drawn with the mouse)."""
from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from . import filters as F
from . import style
from .i18n import t

# the settings window of each row: (key, kind, options); "video.<key>" edits settings["video"][key]
SPECS: dict[str, list] = {
    "crop": [("mode", "combo", ("auto", "manual")), ("l", "int", (0, 4000, 2)), ("t", "int", (0, 4000, 2)),
             ("r", "int", (0, 4000, 2)), ("b", "int", (0, 4000, 2)), ("detect", "button", None)],
    "interlace": [("video.deint", "combo", ("auto", "on", "off")), ("method", "combo", ("bwdif", "yadif")),
                  ("parity", "combo", ("auto", "tff", "bff")), ("double", "check", None), ("ivtc", "check", None)],
    "hdr": [("mode", "combo", ("auto", "on", "off")), ("engine", "combo", ("zscale", "libplacebo")),
            ("algo", "combo", F.TONEMAP_ALGOS), ("peak", "int", (50, 1000, 10))],
    "rotate": [("angle", "combo", (0, 90, 180, 270)), ("hflip", "check", None), ("vflip", "check", None)],
    "deshake": [("method", "combo", ("deshake", "vidstab")), ("strength", "int", (1, 10, 1))],
    "denoise": [("method", "combo", ("hqdn3d", "nlmeans")), ("strength", "int", (1, 10, 1))],
    "grain": [("strength", "int", (1, 60, 1))],
    "delogo": [("x", "int", (0, 8000, 1)), ("y", "int", (0, 8000, 1)), ("w", "int", (0, 8000, 1)),
               ("h", "int", (0, 8000, 1)), ("draw", "button", None)],
    "sharpen": [("amount", "float", (-2.0, 3.0, 0.1))],
    "color": [("brightness", "float", (-1.0, 1.0, 0.02)), ("contrast", "float", (0.0, 3.0, 0.05)),
              ("saturation", "float", (0.0, 3.0, 0.05)), ("gamma", "float", (0.1, 10.0, 0.05)),
              ("temp", "int", (1000, 40000, 100)), ("r", "float", (-1.0, 1.0, 0.05)),
              ("g", "float", (-1.0, 1.0, 0.05)), ("b", "float", (-1.0, 1.0, 0.05)), ("reset", "button", None)],
    "pad": [("l", "int", (0, 4000, 2)), ("t", "int", (0, 4000, 2)), ("r", "int", (0, 4000, 2)),
            ("b", "int", (0, 4000, 2)), ("color", "combo", ("black", "white", "gray"))],
    "aspect": [("dar", "edit_combo", F.DARS)],
    "overlay": [("path", "file", None), ("pos", "combo", ("tl", "tr", "bl", "br", "c")),
                ("margin", "int", (0, 1000, 2)), ("opacity", "int", (0, 100, 5)), ("size", "int", (0, 100, 1))],
    "pixfmt": [("video.bits", "combo", ("auto", "8", "10")), ("chroma", "combo", ("420", "422", "444")),
               ("matrix", "combo", F.MATRICES), ("range", "combo", ("auto", "tv", "pc"))],
    "fps": [("video.fps", "fps", (0, 15, 23.976, 24, 25, 29.97, 30, 48, 50, 59.94, 60)),
            ("mode", "combo", ("auto", "cfr", "vfr")), ("interp", "check", None), ("dedup", "check", None)],
}
NEEDS_PARAMS = {"delogo", "overlay", "rotate", "pad", "color", "aspect"}   # switching on opens the ⚙ window


def get_value(st: dict, name: str, key: str):
    if key.startswith("video."):
        return st.get("video", {}).get(key[6:], {"deint": "auto", "fps": 0, "bits": "auto"}.get(key[6:]))
    return F.fset(st, name).get(key, F.FILTER_DEFAULTS[name].get(key))


def opt_label(name: str, key: str, value) -> str:
    """The text of one choice of a combo box."""
    k = key.split(".")[-1]
    if k == "deint":
        return t("deint_" + str(value))
    if k == "bits":
        return t("bits_" + str(value))
    if k == "angle":
        return f"{value}°" if value else t("f_rot_none")
    if k == "algo":
        return str(value) + ("" if value in F.ZSCALE_ALGOS else " (libplacebo)")
    if k in ("chroma",):
        return {"420": "4:2:0", "422": "4:2:2", "444": "4:4:4"}[str(value)]
    if k == "matrix" and value != "auto":
        return {"bt709": "BT.709", "bt601": "BT.601", "bt2020": "BT.2020"}[value]
    if k == "range" and value != "auto":
        return {"tv": t("f_range_tv"), "pc": t("f_range_pc")}[value]
    if k == "method" and value in ("bwdif", "yadif"):
        return value
    key2 = f"f_{name}_{k}_{value}"
    s = t(key2)
    if s != key2:
        return s
    s = t(f"f_{k}_{value}")
    return s if s != f"f_{k}_{value}" else str(value)


# ── what a row shows ─────────────────────────────────────────
def row_state(name: str, st: dict, job=None, enc=None, bits: int = 8) -> tuple[bool, str]:
    """(the checkbox, the value text) of a filter row: the settings `st` (and the selected task, if one)."""
    f = F.fset(st, name)
    v = st.get("video", {})
    src = job.info.video if job is not None and job.info is not None else None
    if name == "interlace":
        d = v.get("deint", "auto")
        on = d != "off" or bool(f.get("ivtc"))
        if f.get("ivtc"):
            return True, t("f_ivtc_short")
        if not on:
            return False, t("f_off")
        meth = f.get("method", "bwdif") + (" ×2" if f.get("double") else "")
        if d == "auto":
            if src is None:
                return True, t("f_auto") + " · " + meth
            return True, t("f_auto") + " · " + (meth if src.interlaced else t("f_progressive"))
        return True, meth
    if name == "hdr":
        mode = f.get("mode", "auto")
        if mode == "off":
            return False, t("f_off")
        what = f"{f.get('algo', 'hable')} · {'libplacebo' if f.get('engine') == 'libplacebo' else 'zscale'}"
        if job is None or src is None:
            return True, (t("f_auto") + " · " if mode == "auto" else "") + what
        if F.tonemap_on(job, enc, bits):
            return True, f"{src.hdr} → SDR · {what}"
        if mode == "auto":
            return True, t("f_auto") + " · " + (t("f_hdr_kept") if src.hdr in ("HDR10", "HLG") else t("f_not_needed"))
        return True, t("f_not_needed")
    if name == "fps":
        on = F.fps_on(job) if job is not None else (bool(float(v.get("fps", 0) or 0)) or f.get("mode", "auto") != "auto"
                                                    or bool(f.get("interp")) or bool(f.get("dedup")))
        if not on:
            return False, t("as_source")
        fps = float(v.get("fps", 0) or 0)
        bits_ = [f"{fps:g} fps" if fps else t("as_source")]
        if f.get("mode", "auto") != "auto":
            bits_.append(f["mode"].upper())
        if f.get("interp") and fps:
            bits_.append(t("f_interp_short"))
        if f.get("dedup"):
            bits_.append(t("f_dedup_short"))
        return True, " · ".join(bits_)
    if name == "pixfmt":
        b = str(v.get("bits", "auto"))
        on = bool(f.get("on"))
        parts = [t("bits_" + b).split(" (")[0]]
        if not on:
            return False, parts[0]
        if f.get("on"):
            parts.append(opt_label(name, "chroma", str(f.get("chroma", "420"))))
            if f.get("matrix", "auto") != "auto":
                parts.append(opt_label(name, "matrix", f["matrix"]))
            if f.get("range", "auto") != "auto":
                parts.append(opt_label(name, "range", f["range"]))
        return True, " · ".join(parts)
    on = bool(f.get("on"))
    if name == "crop":
        if f.get("mode", "auto") == "auto":
            if job is None:
                return on, t("f_crop_auto")
            found = getattr(job, "crop_found", None)
            if not on:
                return False, t("f_crop_auto")
            if found is None:
                return True, t("f_crop_searching")
            if not any(found):
                return True, t("f_crop_none")
        if not on:
            return False, t("f_off")
        l, tt, r, b = (F.crop_values(job) if job is not None else [f.get(k, 0) for k in ("l", "t", "r", "b")])
        text = f"{l} · {tt} · {r} · {b}"
        if src is not None and src.width:
            text += f"  →  {src.width - l - r}×{src.height - tt - b}"
        return True, (t("f_auto") + ": " if f.get("mode", "auto") == "auto" else "") + text
    if not on:
        return False, t("f_off")
    if name == "rotate":
        a = int(f.get("angle", 0) or 0)
        parts = [f"{a}°"] if a else []
        if f.get("hflip"):
            parts.append(t("f_rotate_hflip_short"))
        if f.get("vflip"):
            parts.append(t("f_rotate_vflip_short"))
        return True, " · ".join(parts) or t("f_rot_none")
    if name == "deshake":
        return True, f"{opt_label(name, 'method', f.get('method'))} · {f.get('strength')}"
    if name == "denoise":
        return True, f"{f.get('method')} · {f.get('strength')}"
    if name == "grain":
        return True, t("f_strength_n", f.get("strength"))
    if name == "delogo":
        if not int(f.get("w", 0) or 0):
            return True, t("f_delogo_draw_hint")
        return True, f"{f.get('w')}×{f.get('h')} @ {f.get('x')},{f.get('y')}"
    if name == "sharpen":
        return True, t("f_strength_n", f"{float(f.get('amount', 0)):g}")
    if name == "color":
        parts = F.color_parts(f)
        return True, (" · ".join(p.split("=", 1)[0] for p in parts)) if parts else t("f_neutral")
    if name == "pad":
        return True, f"{f.get('l')} · {f.get('t')} · {f.get('r')} · {f.get('b')} · {opt_label(name, 'color', f.get('color'))}"
    if name == "aspect":
        return True, "DAR " + str(f.get("dar"))
    if name == "overlay":
        p = f.get("path") or ""
        if not p:
            return True, t("f_overlay_pick")
        miss = "" if os.path.isfile(p) else " ⚠ " + t("f_overlay_missing")
        return True, f"{os.path.basename(p)} · {opt_label(name, 'pos', f.get('pos'))} · {f.get('opacity')}%{miss}"
    return on, ""


def short_name(name: str) -> str:
    """«Crop — обрезка краёв» → «Crop» (the row; the whole name is the tooltip and the window's title)."""
    return t("f_" + name).split(" — ")[0].split(" / ")[0].split(", ")[0]


# ── the rows ─────────────────────────────────────────────────
class FilterRows(QWidget):
    """☑ · name · value · ⚙ for every video filter."""
    toggled = Signal(str, bool)
    gear = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        g = QGridLayout(self)
        g.setContentsMargins(12, 4, 12, 8)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(3)
        self.checks: dict[str, QCheckBox] = {}
        self.values: dict[str, QLabel] = {}
        self.gears: dict[str, QToolButton] = {}
        for r, name in enumerate(F.VIDEO_ROWS):
            cb = QCheckBox()
            cb.setObjectName("filter_" + name)
            cb.setToolTip(t("f_" + name) + "\n" + t(f"f_{name}_tip"))
            cb.clicked.connect(lambda on, n=name: self.toggled.emit(n, bool(on)))
            lab = QLabel(short_name(name))
            lab.setToolTip(t("f_" + name) + "\n" + t(f"f_{name}_tip"))
            val = QLabel()
            val.setObjectName("hint")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            val.setMinimumWidth(40)
            b = QToolButton()
            b.setObjectName("ctl")
            b.setIcon(style.icon("fa6s.gear", style.ICON))
            b.setIconSize(QSize(13, 13))
            b.setToolTip(t("f_settings", t("f_" + name)))
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _c=False, n=name: self.gear.emit(n))
            g.addWidget(cb, r, 0)
            g.addWidget(lab, r, 1)
            g.addWidget(val, r, 2)
            g.addWidget(b, r, 3)
            self.checks[name], self.values[name], self.gears[name] = cb, val, b
        g.setColumnStretch(2, 1)

    def show_state(self, st: dict, job=None, enc=None, bits: int = 8, enabled: bool = True):
        for name in F.VIDEO_ROWS:
            on, text = row_state(name, st, job, enc, bits)
            cb = self.checks[name]
            cb.blockSignals(True)
            cb.setChecked(on)
            cb.blockSignals(False)
            self.values[name].setText(text)
            self.values[name].setToolTip(text)
            for w in (cb, self.gears[name]):
                w.setEnabled(enabled)


# ── the ⚙ window of a filter ─────────────────────────────────
class FilterDialog(QDialog):
    """The settings of one filter; every change goes out at once (edited), buttons as action."""
    edited = Signal(str, str, object)      # filter, key ("video.x" for the 1.0 keys), value
    action = Signal(str, str)              # filter, button key

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.setWindowTitle(t("f_" + name))
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        head = QLabel(t("f_" + name))
        head.setAlignment(Qt.AlignCenter)
        head.setStyleSheet("font-size: 14pt; font-weight: 600;")
        lay.addWidget(head)
        tip = QLabel(t(f"f_{name}_tip"))
        tip.setObjectName("hint")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        form = QFormLayout()
        form.setVerticalSpacing(7)
        lay.addLayout(form)
        self.w: dict[str, QWidget] = {}
        self._loading = False
        for key, kind, opts in SPECS[name]:
            label = t(f"f_{name}_{key.split('.')[-1]}")
            if kind == "combo":
                wdg = QComboBox()
                for o in opts:
                    wdg.addItem(opt_label(name, key, o), o)
                wdg.currentIndexChanged.connect(lambda _i, k=key, b=wdg: self._out(k, b.currentData()))
            elif kind == "edit_combo":
                wdg = QComboBox()
                wdg.setEditable(True)
                for o in opts:
                    wdg.addItem(o, o)
                wdg.currentTextChanged.connect(lambda s, k=key: self._out(k, s.strip()))
            elif kind == "fps":
                wdg = QComboBox()
                wdg.setEditable(True)
                for o in opts:
                    wdg.addItem(t("as_source") if not o else f"{o:g}", o)
                wdg.currentTextChanged.connect(lambda s, k=key, b=wdg: self._fps_typed(k, b, s))
            elif kind == "int":
                wdg = QSpinBox()
                wdg.setRange(opts[0], opts[1])
                wdg.setSingleStep(opts[2])
                wdg.setKeyboardTracking(False)
                wdg.valueChanged.connect(lambda x, k=key: self._out(k, int(x)))
            elif kind == "float":
                wdg = QDoubleSpinBox()
                wdg.setRange(opts[0], opts[1])
                wdg.setSingleStep(opts[2])
                wdg.setDecimals(2)
                wdg.setKeyboardTracking(False)
                wdg.valueChanged.connect(lambda x, k=key: self._out(k, round(float(x), 3)))
            elif kind == "check":
                wdg = QCheckBox(label)
                label = ""
                wdg.toggled.connect(lambda on, k=key: self._out(k, bool(on)))
            elif kind == "file":
                wdg = QWidget()
                row = QHBoxLayout(wdg)
                row.setContentsMargins(0, 0, 0, 0)
                ed = QLineEdit()
                ed.setObjectName("path")
                ed.editingFinished.connect(lambda k=key, e=ed: self._out(k, e.text().strip()))
                pick = QToolButton()
                pick.setObjectName("ctl")
                pick.setIcon(style.icon("fa6s.folder-open", style.ICON))
                pick.clicked.connect(lambda _c=False, k=key, e=ed: self._pick(k, e))
                row.addWidget(ed, 1)
                row.addWidget(pick)
                wdg.edit = ed
            else:                                                # button
                wdg = QToolButton()
                wdg.setObjectName("ctlText")
                wdg.setText("  " + label)
                wdg.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
                wdg.setIcon(style.icon({"detect": "fa6s.crop-simple", "draw": "fa6s.pen-to-square",
                                        "reset": "fa6s.rotate-left"}.get(key, "fa6s.play"), style.ICON))
                wdg.clicked.connect(lambda _c=False, k=key: self.action.emit(self.name, k))
                label = ""
            self.w[key] = wdg
            form.addRow(label, wdg)
        self.note = QLabel()
        self.note.setObjectName("hint")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)
        lay.addStretch(1)

    def _out(self, key: str, value):
        if not self._loading:
            self.edited.emit(self.name, key, value)

    def _fps_typed(self, key: str, box: QComboBox, text: str):
        i = box.findText(text)
        if i >= 0 and box.itemData(i) is not None:
            val = box.itemData(i)
        else:
            try:
                val = float(text.replace(",", "."))
            except ValueError:
                return
        self._out(key, float(val) if val else 0)

    def _pick(self, key: str, ed: QLineEdit):
        path, _ = QFileDialog.getOpenFileName(self, t("f_" + self.name), os.path.dirname(ed.text()),
                                              t("f_overlay_files") + " (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)")
        if path:
            ed.setText(path)
            self._out(key, path)

    def load(self, st: dict, note: str = "", enabled: dict | None = None):
        self._loading = True
        try:
            for key, kind, _opts in SPECS[self.name]:
                wdg = self.w[key]
                val = get_value(st, self.name, key)
                if kind == "combo":
                    i = wdg.findData(val)
                    if i < 0 and isinstance(val, str) and val.isdigit():
                        i = wdg.findData(int(val))
                    wdg.setCurrentIndex(max(0, i))
                elif kind == "edit_combo":
                    wdg.setCurrentText(str(val))
                elif kind == "fps":
                    v = float(val or 0)
                    i = wdg.findData(v) if v else 0
                    if i < 0:
                        i = next((n for n in range(wdg.count()) if wdg.itemData(n) and abs(wdg.itemData(n) - v) < 1e-3), -1)
                    if i >= 0:
                        wdg.setCurrentIndex(i)
                    else:
                        wdg.setEditText(f"{v:g}")
                elif kind == "int":
                    wdg.setValue(int(val or 0))
                elif kind == "float":
                    wdg.setValue(float(val or 0))
                elif kind == "check":
                    wdg.setChecked(bool(val))
                elif kind == "file":
                    wdg.edit.setText(str(val or ""))
                if enabled is not None and key in enabled:
                    wdg.setEnabled(enabled[key])
            self.note.setText(note)
            self.note.setVisible(bool(note))
        finally:
            self._loading = False


# ── over the picture ─────────────────────────────────────────
class FrameOverlay(QWidget):
    """Lies over the preview: the crop frame (the cut-off edges darkened) and the logo rectangle; in draw mode
    a rectangle is drawn with the mouse (in pixels of the source)."""
    drawn = Signal(int, int, int, int)       # x, y, w, h of the source

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.src = (0, 0, 1.0)               # width, height, pixel aspect of the source
        self.crop: tuple | None = None       # l, t, r, b
        self.logo: tuple | None = None       # x, y, w, h
        self.drawing = False
        self._a: QPointF | None = None
        self._b: QPointF | None = None
        parent.installEventFilter(self)
        self.setGeometry(parent.rect())

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if obj is self.parent() and ev.type() in (QEvent.Resize, QEvent.Show):
            self.setGeometry(self.parent().rect())
            self.raise_()
        return False

    def set_state(self, src: tuple, crop: tuple | None, logo: tuple | None):
        self.src, self.crop, self.logo = src, crop, logo
        self.setVisible(bool(src[0]) and (bool(crop) or bool(logo) or self.drawing))
        self.raise_()
        self.update()

    def start_draw(self):
        self.drawing = True
        self._a = self._b = None
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setCursor(Qt.CrossCursor)
        self.setVisible(bool(self.src[0]))
        self.raise_()
        self.update()

    def stop_draw(self):
        self.drawing = False
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.unsetCursor()
        self.update()

    def video_rect(self) -> QRectF:
        """Where the picture is inside the widget (kept to its shape, centred — as mpv draws it)."""
        w, h, sar = self.src
        if not w or not h:
            return QRectF()
        dw, dh = w * sar, float(h)
        k = min(self.width() / dw, self.height() / dh)
        rw, rh = dw * k, dh * k
        return QRectF((self.width() - rw) / 2, (self.height() - rh) / 2, rw, rh)

    def to_widget(self, x: float, y: float, w: float, h: float) -> QRectF:
        vr = self.video_rect()
        sx, sy = vr.width() / self.src[0], vr.height() / self.src[1]
        return QRectF(vr.left() + x * sx, vr.top() + y * sy, w * sx, h * sy)

    def to_source(self, p: QPointF) -> tuple[int, int]:
        vr = self.video_rect()
        x = (p.x() - vr.left()) / max(1e-6, vr.width()) * self.src[0]
        y = (p.y() - vr.top()) / max(1e-6, vr.height()) * self.src[1]
        return int(max(0, min(self.src[0], x))), int(max(0, min(self.src[1], y)))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.src[0], self.src[1]
        if self.crop and w:
            l, tt, r, b = self.crop
            inner = self.to_widget(l, tt, w - l - r, h - tt - b)
            vr = self.video_rect()
            shade = QColor(0, 0, 0, 140)
            p.fillRect(QRectF(vr.left(), vr.top(), vr.width(), inner.top() - vr.top()), shade)
            p.fillRect(QRectF(vr.left(), inner.bottom(), vr.width(), vr.bottom() - inner.bottom()), shade)
            p.fillRect(QRectF(vr.left(), inner.top(), inner.left() - vr.left(), inner.height()), shade)
            p.fillRect(QRectF(inner.right(), inner.top(), vr.right() - inner.right(), inner.height()), shade)
            p.setPen(QPen(QColor(style.ACCENT or "#9061d6"), 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(inner)
        rect = None
        if self.drawing and self._a is not None and self._b is not None:
            rect = QRectF(self._a, self._b).normalized()
        elif self.logo and self.logo[2] and self.logo[3]:
            rect = self.to_widget(*self.logo)
        if rect is not None:
            p.setPen(QPen(QColor("#ff3b30"), 2))
            p.setBrush(QColor(255, 59, 48, 50))
            p.drawRect(rect)
        if self.drawing:
            p.setPen(QColor("#ffffff"))
            p.drawText(self.rect().adjusted(10, 8, -10, -8), Qt.AlignTop | Qt.AlignHCenter, t("f_delogo_drawing"))
        p.end()

    def mousePressEvent(self, e):
        if self.drawing and e.button() == Qt.LeftButton:
            self._a = self._b = e.position()
            self.update()
        elif self.drawing and e.button() == Qt.RightButton:
            self.stop_draw()
            self.set_state(self.src, self.crop, self.logo)

    def mouseMoveEvent(self, e):
        if self.drawing and self._a is not None:
            self._b = e.position()
            self.update()

    def mouseReleaseEvent(self, e):
        if not (self.drawing and self._a is not None and e.button() == Qt.LeftButton):
            return
        self._b = e.position()
        (x0, y0), (x1, y1) = self.to_source(self._a), self.to_source(self._b)
        x, y, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
        self.stop_draw()
        if w >= 4 and h >= 4:
            self.logo = (x, y, w, h)
            self.drawn.emit(x, y, w, h)
        self.set_state(self.src, self.crop, self.logo)
