"""1.5, the windows of presets and devices: the preset of the format (⚙ at the preset), the container options
(a part of Format options, ⚙ at the container), the advanced settings of an encoder (⚙ at the codec on the
Streams tab) and the comparison of two presets (Tools)."""
from __future__ import annotations

import os
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
                               QScrollArea, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import APP_TITLE, style
from . import advanced as ADV
from . import encoders as E
from . import formats as FM
from .i18n import t


def _hint(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


def _head(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("font-size: 14pt; font-weight: 700; padding-bottom: 6px;")
    return lab


def fam_label(f: str) -> str:
    return E.FAMILY_LABEL.get(f, f.upper())


# ── the preset of the format ─────────────────────────────────
class FormatDialog(QDialog):
    """XviD4PSP's «Preset of the format»: the list of presets (built in, own) on the left, the chosen one on the
    right. Built-in ones are read only (Copy makes an own one); own ones are kept in formats.json and can be
    created, renamed, deleted, exported to a file and imported from one. «Use» puts the shown preset into the
    selected tasks (or the settings of new files)."""
    applied = Signal(object)                    # the preset (dict) or None = no preset of the format

    def __init__(self, parent=None, current: dict | None = None, container: str = "mkv"):
        super().__init__(parent)
        self.setWindowTitle(t("fp_title") + " — " + APP_TITLE)
        self.resize(980, 680)
        self.user = FM.user_formats()
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.addWidget(_head(t("fp_title")))
        sp = QSplitter(Qt.Horizontal)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.setObjectName("playlist")
        self.list.setStyleSheet(f"QListWidget#playlist {{ background: {style.PANEL}; border: 1px solid {style.BORDER};"
                                f" border-radius: 10px; }} QListWidget#playlist::item {{ padding: 3px 8px; }}")
        self.list.currentItemChanged.connect(lambda *_a: self._show())
        ll.addWidget(self.list, 1)
        grid = QGridLayout()
        grid.setSpacing(4)
        self.b_new = QPushButton(t("fp_new"))
        self.b_copy = QPushButton(t("fp_copy"))
        self.b_rename = QPushButton(t("fp_rename"))
        self.b_delete = QPushButton(t("fp_delete"))
        self.b_export = QPushButton(t("fp_export") + "…")
        self.b_import = QPushButton(t("fp_import") + "…")
        for i, (b, fn) in enumerate(((self.b_new, self.new), (self.b_copy, self.copy), (self.b_rename, self.rename),
                                     (self.b_delete, self.delete), (self.b_export, self.export),
                                     (self.b_import, self.import_))):
            b.clicked.connect(lambda _c=False, f=fn: f())
            grid.addWidget(b, i // 2, i % 2)
        ll.addLayout(grid)
        sp.addWidget(left)
        self.form_box = QWidget()
        self._build_form()
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QScrollArea.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(self.form_box)
        sa.setStyleSheet("QScrollArea { background: transparent; } "
                         "QScrollArea > QWidget > QWidget { background: transparent; }")
        sp.addWidget(sa)
        sp.setSizes([300, 680])
        lay.addWidget(sp, 1)
        row = QHBoxLayout()
        self.b_use = QPushButton(t("fp_use"))
        self.b_use.setObjectName("primary")
        self.b_use.clicked.connect(self.use)
        self.b_none = QPushButton(t("fp_none"))
        self.b_none.clicked.connect(self.use_none)
        row.addWidget(self.b_use)
        row.addWidget(self.b_none)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)
        self.cur_name = (current or {}).get("name", "")
        self._fill(self.cur_name, current, container)

    # the list
    def _fill(self, select: str = "", current: dict | None = None, container: str = ""):
        self.list.blockSignals(True)
        self.list.clear()

        def head(text):
            it = QListWidgetItem(text)
            it.setFlags(Qt.NoItemFlags)
            f = it.font()
            f.setBold(True)
            it.setFont(f)
            self.list.addItem(it)
        head(t("fp_builtin"))
        for f in FM.BUILTIN:
            it = QListWidgetItem("  " + f["name"])
            it.setData(Qt.UserRole, ("builtin", f["name"]))
            self.list.addItem(it)
        head(t("fp_own"))
        for name in sorted(self.user, key=str.lower):
            it = QListWidgetItem("  ★ " + name)
            it.setData(Qt.UserRole, ("own", name))
            self.list.addItem(it)
        if current and current.get("name") and not FM.find(current["name"], self.user):
            # the task's own copy (from a queue / another PC): shown so it can be kept
            it = QListWidgetItem("  ◆ " + current["name"])
            it.setData(Qt.UserRole, ("task", current["name"]))
            self._task_fmt = FM.clean(current)
            self.list.addItem(it)
        else:
            self._task_fmt = None
        self.list.blockSignals(False)
        pick = None
        for i in range(self.list.count()):
            d = self.list.item(i).data(Qt.UserRole)
            if d and d[1] == select:
                pick = i
                if d[0] in ("own", "task"):
                    break
        if pick is None:
            pick = next((i for i in range(self.list.count()) if self.list.item(i).data(Qt.UserRole)
                         and (FM.find(self.list.item(i).data(Qt.UserRole)[1]) or {}).get("container") == container),
                        1)
        self.list.setCurrentRow(pick)
        self._show()

    def chosen(self) -> tuple[str, str]:
        it = self.list.currentItem()
        d = it.data(Qt.UserRole) if it is not None else None
        return tuple(d) if d else ("", "")

    def chosen_fmt(self) -> dict | None:
        kind, name = self.chosen()
        if kind == "task":
            return dict(self._task_fmt)
        if kind == "own":
            return FM.clean(self.user.get(name)) | {"name": name}
        if kind == "builtin":
            return FM.find(name)
        return None

    # the form
    def _build_form(self):
        lay = QVBoxLayout(self.form_box)
        lay.setContentsMargins(10, 0, 4, 0)
        self.ro_hint = _hint(t("fp_readonly"))
        lay.addWidget(self.ro_hint)
        top = QFormLayout()
        self.f_cont = QComboBox()
        for c in E.CONTAINERS:
            if not c.audio_only or c.id == "mka":
                self.f_cont.addItem(c.label, c.id)
        self.f_cont.currentIndexChanged.connect(lambda: self._changed(True))
        top.addRow(t("container"), self.f_cont)
        self.f_ext = QLineEdit()
        self.f_ext.setMaxLength(8)
        self.f_ext.setPlaceholderText(t("fp_ext_same"))
        self.f_ext.editingFinished.connect(self._changed)
        top.addRow(t("fp_ext"), self.f_ext)
        lay.addLayout(top)
        # codecs
        box = QGroupBox(t("fp_codecs"))
        g = QGridLayout(box)
        g.addWidget(QLabel(t("video")), 0, 0, Qt.AlignTop)
        self.vchecks: dict[str, QCheckBox] = {}
        vrow = QGridLayout()
        for i, fam in enumerate(FM.VFAMILIES):
            cb = QCheckBox(fam_label(fam))
            cb.toggled.connect(lambda _on: self._changed())
            self.vchecks[fam] = cb
            vrow.addWidget(cb, i // 4, i % 4)
        g.addLayout(vrow, 0, 1)
        g.addWidget(QLabel(t("grp_audio")), 1, 0, Qt.AlignTop)
        self.achecks: dict[str, QCheckBox] = {}
        arow = QGridLayout()
        for i, a in enumerate(E.AUDIO):
            cb = QCheckBox(a.label.split(" (")[0])
            cb.toggled.connect(lambda _on: self._changed())
            self.achecks[a.id] = cb
            arow.addWidget(cb, i // 4, i % 4)
        g.addLayout(arow, 1, 1)
        g.addWidget(_hint(t("fp_codecs_hint")), 2, 0, 1, 2)
        lay.addWidget(box)
        # profiles / levels
        box = QGroupBox(t("fp_profiles"))
        g = QGridLayout(box)
        self.prof: dict[str, QComboBox] = {}
        self.lev: dict[str, QComboBox] = {}
        g.addWidget(QLabel(t("fp_profile")), 0, 1)
        g.addWidget(QLabel(t("fp_level")), 0, 2)
        for r, fam in enumerate(FM.PROFILES, 1):
            g.addWidget(QLabel(fam_label(fam)), r, 0)
            p = QComboBox()
            p.addItem(t("auto"), "")
            for x in FM.PROFILES[fam]:
                p.addItem(t("prof_" + x) if t("prof_" + x) != "prof_" + x else x, x)
            p.currentIndexChanged.connect(lambda _i: self._changed())
            lv = QComboBox()
            lv.addItem(t("auto"), "")
            for x in FM.LEVELS[fam]:
                lv.addItem(x, x)
            lv.currentIndexChanged.connect(lambda _i: self._changed())
            self.prof[fam], self.lev[fam] = p, lv
            g.addWidget(p, r, 1)
            g.addWidget(lv, r, 2)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(2, 1)
        lay.addWidget(box)
        # keyframes, VBV
        box = QGroupBox(t("fp_gop"))
        g = QGridLayout(box)
        self.gop_min = QDoubleSpinBox()
        self.gop_max = QDoubleSpinBox()
        for sb in (self.gop_min, self.gop_max):
            sb.setRange(0, 10000)
            sb.setDecimals(2)
            sb.setSpecialValueText(t("auto"))
            sb.valueChanged.connect(lambda _x: self._changed())
        self.gop_unit = QComboBox()
        self.gop_unit.addItem(t("fp_frames"), "frames")
        self.gop_unit.addItem(t("fp_seconds"), "sec")
        self.gop_unit.currentIndexChanged.connect(lambda _i: self._changed())
        self.gop_closed = QCheckBox(t("fp_closed"))
        self.gop_closed.toggled.connect(lambda _on: self._changed())
        g.addWidget(QLabel(t("fp_gop_min")), 0, 0)
        g.addWidget(self.gop_min, 0, 1)
        g.addWidget(QLabel(t("fp_gop_max")), 0, 2)
        g.addWidget(self.gop_max, 0, 3)
        g.addWidget(self.gop_unit, 0, 4)
        g.addWidget(self.gop_closed, 1, 0, 1, 5)
        self.vbv_buf = QSpinBox()
        self.vbv_max = QSpinBox()
        for sb in (self.vbv_buf, self.vbv_max):
            sb.setRange(0, 500000)
            sb.setSingleStep(500)
            sb.setSpecialValueText(t("fp_none_v"))
            sb.valueChanged.connect(lambda _x: self._changed())
        self.vbv_buf.setSuffix(" kbit")
        self.vbv_max.setSuffix(" kbit/s")
        g.addWidget(QLabel(t("fp_vbv_buf")), 2, 0)
        g.addWidget(self.vbv_buf, 2, 1)
        g.addWidget(QLabel(t("fp_vbv_max")), 2, 2)
        g.addWidget(self.vbv_max, 2, 3, 1, 2)
        lay.addWidget(box)
        # the result
        box = QGroupBox(t("fp_result"))
        g = QGridLayout(box)
        self.w = QSpinBox()
        self.h = QSpinBox()
        for sb in (self.w, self.h):
            sb.setRange(0, 8192)
            sb.setSingleStep(2)
            sb.setSpecialValueText(t("as_source"))
            sb.valueChanged.connect(lambda _x: self._changed())
        self.size_limit = QCheckBox(t("fp_size_limit"))
        self.size_limit.toggled.connect(lambda _on: self._changed())
        g.addWidget(QLabel(t("fp_size")), 0, 0)
        g.addWidget(self.w, 0, 1)
        g.addWidget(QLabel("×"), 0, 2)
        g.addWidget(self.h, 0, 3)
        g.addWidget(self.size_limit, 1, 0, 1, 4)
        self.dur = QSpinBox()
        self.dur.setRange(0, 24 * 3600)
        self.dur.setSuffix(" " + t("sec"))
        self.dur.setSpecialValueText(t("fp_whole"))
        self.dur.valueChanged.connect(lambda _x: self._changed())
        g.addWidget(QLabel(t("fp_dur")), 2, 0)
        g.addWidget(self.dur, 2, 1, 1, 3)
        self.max_audio = QSpinBox()
        self.max_audio.setRange(0, 32)
        self.max_audio.setSpecialValueText(t("fp_any"))
        self.max_audio.valueChanged.connect(lambda _x: self._changed())
        g.addWidget(QLabel(t("fp_max_audio")), 3, 0)
        g.addWidget(self.max_audio, 3, 1, 1, 3)
        self.a_maxch = QComboBox()
        for n, key in ((0, "fp_any"), (1, "ch_mono"), (2, "ch_stereo"), (6, "ch_51"), (8, "ch_71")):
            self.a_maxch.addItem(t(key), n)
        self.a_maxch.currentIndexChanged.connect(lambda _i: self._changed())
        g.addWidget(QLabel(t("fp_maxch")), 4, 0)
        g.addWidget(self.a_maxch, 4, 1, 1, 3)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        lay.addWidget(box)
        lay.addStretch(1)

    def _show(self):
        f = self.chosen_fmt()
        kind, _n = self.chosen()
        self._loading = True
        try:
            f = f or FM.clean({})
            sel = lambda box, val: box.setCurrentIndex(max(0, box.findData(val)))       # noqa: E731
            sel(self.f_cont, f.get("container") or "mkv")
            self.f_ext.setText(f.get("ext", ""))
            for fam, cb in self.vchecks.items():
                cb.setChecked(fam in (f.get("vcodecs") or []))
            for a, cb in self.achecks.items():
                cb.setChecked(a in (f.get("acodecs") or []))
            for fam in FM.PROFILES:
                sel(self.prof[fam], (f.get("profile") or {}).get(fam, ""))
                sel(self.lev[fam], (f.get("level") or {}).get(fam, ""))
            self.gop_min.setValue(float(f.get("gop_min", 0)))
            self.gop_max.setValue(float(f.get("gop_max", 0)))
            sel(self.gop_unit, f.get("gop_unit", "frames"))
            self.gop_closed.setChecked(bool(f.get("gop_closed")))
            self.vbv_buf.setValue(int(f.get("vbv_buf", 0)))
            self.vbv_max.setValue(int(f.get("vbv_max", 0)))
            self.w.setValue(int(f.get("width", 0)))
            self.h.setValue(int(f.get("height", 0)))
            self.size_limit.setChecked(bool(f.get("size_limit")))
            self.dur.setValue(int(round(float(f.get("dur", 0)))))
            self.max_audio.setValue(int(f.get("max_audio", 0)))
            sel(self.a_maxch, int(f.get("a_maxch", 0)))
            self._fit_container()
        finally:
            self._loading = False
        own = kind == "own"
        for wdg in self.form_box.findChildren(QWidget):
            if wdg is not self.ro_hint and isinstance(wdg, (QComboBox, QLineEdit, QCheckBox, QSpinBox,
                                                            QDoubleSpinBox)):
                wdg.setEnabled(own and (not isinstance(wdg, QCheckBox) or wdg.property("fits") is not False))
        self.ro_hint.setVisible(not own)
        self.b_rename.setEnabled(own)
        self.b_delete.setEnabled(own)
        self.b_copy.setEnabled(bool(kind))
        self.b_export.setEnabled(bool(kind))
        self.b_use.setEnabled(bool(kind))

    def _fit_container(self):
        """Only the codecs the chosen container holds can be ticked."""
        c = E.container(self.f_cont.currentData() or "mkv")
        for fam, cb in self.vchecks.items():
            fits = c.takes_video(fam)
            cb.setProperty("fits", fits)
            if not fits:
                cb.setChecked(False)
            cb.setToolTip("" if fits else t("not_in_container", c.id.upper()))
        for a, cb in self.achecks.items():
            fits = c.takes_audio(E.A_CODEC.get(a, a))
            cb.setProperty("fits", fits)
            if not fits:
                cb.setChecked(False)
            cb.setToolTip("" if fits else t("not_in_container", c.id.upper()))

    def form_fmt(self, name: str) -> dict:
        return FM.clean({
            "name": name, "container": self.f_cont.currentData(), "ext": self.f_ext.text(),
            "vcodecs": [k for k, cb in self.vchecks.items() if cb.isChecked()],
            "acodecs": [k for k, cb in self.achecks.items() if cb.isChecked()],
            "profile": {k: b.currentData() for k, b in self.prof.items() if b.currentData()},
            "level": {k: b.currentData() for k, b in self.lev.items() if b.currentData()},
            "gop_min": self.gop_min.value(), "gop_max": self.gop_max.value(), "gop_unit": self.gop_unit.currentData(),
            "gop_closed": self.gop_closed.isChecked(), "vbv_buf": self.vbv_buf.value(),
            "vbv_max": self.vbv_max.value(), "width": self.w.value(), "height": self.h.value(),
            "size_limit": self.size_limit.isChecked(), "dur": self.dur.value(),
            "max_audio": self.max_audio.value(), "a_maxch": self.a_maxch.currentData()})

    def _changed(self, container: bool = False):
        if self._loading:
            return
        kind, name = self.chosen()
        if container:
            self._loading = True
            try:
                self._fit_container()
                for cb in list(self.vchecks.values()) + list(self.achecks.values()):
                    cb.setEnabled(cb.property("fits") is not False)
            finally:
                self._loading = False
        if kind != "own":
            return
        self.user[name] = self.form_fmt(name)
        FM.save_user_formats(self.user)

    # list buttons
    def _ask_name(self, title: str, text: str = "") -> str:
        name, ok = QInputDialog.getText(self, title, t("fp_name"), text=text)
        name = (name or "").strip()
        return name if ok and name else ""

    def _taken(self) -> set:
        return set(self.user) | set(FM.builtin_names())

    def new(self):
        name = self._ask_name(t("fp_new"))
        if not name:
            return
        name = FM.unique_name(name, self._taken())
        self.user[name] = FM.clean({"name": name, "container": self.f_cont.currentData() or "mkv"})
        FM.save_user_formats(self.user)
        self._fill(name)

    def copy(self, name: str = ""):
        f = self.chosen_fmt()
        if f is None:
            return
        name = name or self._ask_name(t("fp_copy"), f["name"] + " — " + t("fp_copy_suffix"))
        if not name:
            return
        name = FM.unique_name(name, self._taken())
        self.user[name] = dict(f, name=name)
        FM.save_user_formats(self.user)
        self._fill(name)

    def rename(self, new: str = ""):
        kind, name = self.chosen()
        if kind != "own":
            return
        new = new or self._ask_name(t("fp_rename"), name)
        if not new or new == name:
            return
        new = FM.unique_name(new, self._taken() - {name})
        self.user[new] = dict(self.user.pop(name), name=new)
        FM.save_user_formats(self.user)
        self._fill(new)

    def delete(self, ask: bool = True):
        kind, name = self.chosen()
        if kind != "own":
            return
        if ask and QMessageBox.question(self, APP_TITLE, t("fp_delete_q", name)) != QMessageBox.Yes:
            return
        self.user.pop(name, None)
        FM.save_user_formats(self.user)
        self._fill()

    def export(self, path: str = ""):
        f = self.chosen_fmt()
        if f is None:
            return
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, t("fp_export"), f["name"] + ".acformat.json",
                                                  f"{t('fp_files')} (*.json)")
        if path:
            FM.export_file(path, [f])

    def import_(self, path: str = ""):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, t("fp_import"), "", f"{t('fp_files')} (*.json);;"
                                                                           f"{t('all_files')} (*)")
        if not path:
            return
        try:
            got = FM.import_file(path)
        except (OSError, ValueError) as ex:
            QMessageBox.warning(self, APP_TITLE, t("fp_import_bad") + f"\n{ex}")
            return
        last = ""
        for f in got:
            last = FM.unique_name(f["name"], self._taken())
            self.user[last] = dict(f, name=last)
        FM.save_user_formats(self.user)
        self._fill(last)

    def use(self):
        f = self.chosen_fmt()
        if f is not None:
            self.applied.emit(f)
            self.accept()

    def use_none(self):
        self.applied.emit(None)
        self.accept()


# ── the container options ────────────────────────────────────
def mux_group(st: dict, set_value) -> QGroupBox | None:
    """The container options of Format options for the chosen container (None: it has none).
    set_value(key, value) writes settings["mux"][key]."""
    cont = st.get("container", "mkv")
    m = FM.mux(st)
    box = QGroupBox(t("mux_title"))
    box.setObjectName("muxBox")
    f = QFormLayout(box)
    if cont in FM.MATROSKA:
        idx = QSpinBox()
        idx.setObjectName("muxIndex")
        idx.setRange(0, 1024 * 1024)
        idx.setSingleStep(50)
        idx.setSuffix(" KB")
        idx.setSpecialValueText(t("mux_none"))
        idx.setValue(int(m["mkv_index_kb"]))
        idx.valueChanged.connect(lambda x: set_value("mkv_index_kb", int(x)))
        f.addRow(t("mux_index"), idx)
        cues = QCheckBox(t("mux_cues_front"))
        cues.setObjectName("muxCues")
        cues.setChecked(bool(m["mkv_cues_front"]))
        cues.toggled.connect(lambda on: set_value("mkv_cues_front", bool(on)))
        f.addRow("", cues)
        cb = QSpinBox()
        cb.setObjectName("muxClusterBytes")
        cb.setRange(0, 1 << 30)
        cb.setSingleStep(65536)
        cb.setSuffix(" " + t("bytes"))
        cb.setSpecialValueText(t("auto"))
        cb.setValue(int(m["mkv_cluster_bytes"]))
        cb.valueChanged.connect(lambda x: set_value("mkv_cluster_bytes", int(x)))
        f.addRow(t("mux_cluster_bytes"), cb)
        cm = QSpinBox()
        cm.setObjectName("muxClusterMs")
        cm.setRange(0, 600000)
        cm.setSingleStep(500)
        cm.setSuffix(" " + t("ms"))
        cm.setSpecialValueText(t("auto"))
        cm.setValue(int(m["mkv_cluster_ms"]))
        cm.valueChanged.connect(lambda x: set_value("mkv_cluster_ms", int(x)))
        f.addRow(t("mux_cluster_ms"), cm)
        crc = QCheckBox(t("mux_crc32"))
        crc.setObjectName("muxCrc")
        crc.setChecked(bool(m["mkv_crc32"]))
        crc.toggled.connect(lambda on: set_value("mkv_crc32", bool(on)))
        f.addRow("", crc)
        dm = QComboBox()
        dm.setObjectName("muxDefault")
        for key in FM.MKV_DEFAULT_MODES:
            dm.addItem(t("mux_def_" + key), key)
        dm.setCurrentIndex(max(0, dm.findData(m["mkv_default"])))
        dm.currentIndexChanged.connect(lambda _i: set_value("mkv_default", dm.currentData()))
        f.addRow(t("mux_default"), dm)
    elif cont in FM.MOV:
        fs = QCheckBox(t("mux_faststart"))
        fs.setObjectName("muxFaststart")
        fs.setChecked(bool(m["mp4_faststart"]))
        fs.toggled.connect(lambda on: set_value("mp4_faststart", bool(on)))
        fr = QCheckBox(t("mux_frag"))
        fr.setObjectName("muxFrag")
        fr.setChecked(bool(m["mp4_frag"]))
        fr.toggled.connect(lambda on: (set_value("mp4_frag", bool(on)), fs.setEnabled(not on)))
        fs.setEnabled(not m["mp4_frag"])
        f.addRow("", fs)
        f.addRow("", fr)
        f.addRow("", _hint(t("mux_frag_hint")))
    elif cont in FM.TS:
        mr = QSpinBox()
        mr.setObjectName("muxRate")
        mr.setRange(0, 200000)
        mr.setSingleStep(1000)
        mr.setSuffix(" kbit/s")
        mr.setSpecialValueText(t("mux_vbr"))
        mr.setValue(int(m["ts_muxrate"]))
        mr.valueChanged.connect(lambda x: set_value("ts_muxrate", int(x)))
        f.addRow(t("mux_muxrate"), mr)
        pcr = QSpinBox()
        pcr.setObjectName("muxPcr")
        pcr.setRange(0, 1000)
        pcr.setSuffix(" " + t("ms"))
        pcr.setSpecialValueText(t("auto"))
        pcr.setValue(int(m["ts_pcr_ms"]))
        pcr.valueChanged.connect(lambda x: set_value("ts_pcr_ms", int(x)))
        f.addRow(t("mux_pcr"), pcr)
    else:
        return None
    return box


# ── the advanced settings of an encoder ──────────────────────
class AdvancedDialog(QDialog):
    """The fields this encoder has in this ffmpeg; «Default» everywhere = the encoder decides."""
    changed = Signal(str, object)               # encoder id, its settings (dict)
    bits = Signal(str)                          # NVENC «10 bit»: video.bits

    def __init__(self, parent, e: E.VEnc, adv: dict, bits: str = "auto", caps: dict | None = None):
        super().__init__(parent)
        self.e = e
        self.setWindowTitle(t("adv_title", e.label) + " — " + APP_TITLE)
        self.setMinimumWidth(560)
        self.values = ADV.clean(e, adv)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.addWidget(_head(t("adv_title", e.label)))
        self.fields = ADV.offered(e, caps)
        form = QFormLayout()
        self.widgets: dict[str, QWidget] = {}
        for f in self.fields:
            w = self._widget(f)
            w.setObjectName("adv_" + f.key)
            self.widgets[f.key] = w
            label = t("adv_" + f.key)
            w.setToolTip(t("adv_tip_" + f.key) if t("adv_tip_" + f.key) != "adv_tip_" + f.key else
                         ("-" + (f.arg or f.opt) if f.how == "opt" and (f.arg or f.opt) else
                          f"-{f.opt} {f.pkey}=…" if f.how == "param" else ""))
            form.addRow(label, w)
        if e.vendor == "nvidia" and 10 in e.bits:
            self.ten = QCheckBox(t("adv_10bit"))
            self.ten.setObjectName("adv_10bit")
            self.ten.setChecked(bits == "10")
            self.ten.toggled.connect(lambda on: self.bits.emit("10" if on else "auto"))
            form.addRow("", self.ten)
        lay.addLayout(form)
        lay.addWidget(_hint(t("adv_hint_" + ("nvidia" if e.vendor == "nvidia" else e.id)) if
                            t("adv_hint_" + ("nvidia" if e.vendor == "nvidia" else e.id)) !=
                            "adv_hint_" + ("nvidia" if e.vendor == "nvidia" else e.id) else t("adv_hint")))
        if not self.fields:
            lay.addWidget(_hint(t("adv_nothing")))
        row = QHBoxLayout()
        reset = QPushButton(t("adv_reset"))
        reset.clicked.connect(self.reset)
        row.addWidget(reset)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)

    def _widget(self, f: ADV.Field) -> QWidget:
        v = self.values.get(f.key)
        if f.kind in ("choice", "bool"):
            b = QComboBox()
            b.addItem(t("adv_default"), "")
            if f.kind == "bool":
                b.addItem(t("on"), True)
                b.addItem(t("off"), False)
                b.setCurrentIndex(0 if v is None else 1 if v else 2)
            else:
                for x in f.values:
                    if x:
                        b.addItem(t(f"advv_{f.key}_{x}") if t(f"advv_{f.key}_{x}") != f"advv_{f.key}_{x}" else x, x)
                b.setCurrentIndex(max(0, b.findData(v if v is not None else "")))
            b.currentIndexChanged.connect(lambda _i, k=f.key, box=b: self._put(k, box.currentData()))
            return b
        if f.kind in ("int", "float"):
            lo, hi, step = f.values
            b = QSpinBox() if f.kind == "int" else QDoubleSpinBox()
            if f.kind == "float":
                b.setDecimals(2)
            b.setRange(lo - step, hi)                       # one step below = «default»
            b.setSingleStep(step)
            b.setSpecialValueText(t("adv_default"))
            b.setValue(v if v is not None else lo - step)
            b.setKeyboardTracking(False)
            b.valueChanged.connect(lambda x, k=f.key, box=b, m=lo - step: self._put(k, None if x <= m else x))
            return b
        b = QLineEdit(str(v or ""))
        b.setPlaceholderText(t("adv_ph_" + f.key) if t("adv_ph_" + f.key) != "adv_ph_" + f.key else
                             t("adv_default"))
        b.editingFinished.connect(lambda k=f.key, box=b: self._put(k, box.text().strip()))
        return b

    def _put(self, key: str, value):
        if value is None or value == "":
            self.values.pop(key, None)
        else:
            self.values[key] = value
        self.values = ADV.clean(self.e, self.values)
        self.changed.emit(self.e.id, dict(self.values))

    def set_value(self, key: str, value):
        """Tests: set a field as if picked."""
        w = self.widgets[key]
        if isinstance(w, QComboBox):
            w.setCurrentIndex(max(0, w.findData(value)))
        elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
            w.setValue(value)
        else:
            w.setText(str(value))
            w.editingFinished.emit()

    def reset(self):
        self.values = {}
        for f in self.fields:
            w = self.widgets[f.key]
            w.blockSignals(True)
            if isinstance(w, QComboBox):
                w.setCurrentIndex(0)
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.setValue(w.minimum())
            else:
                w.setText("")
            w.blockSignals(False)
        self.changed.emit(self.e.id, {})


# ── two presets side by side ─────────────────────────────────
class CompareDialog(QDialog):
    row = Signal(object)
    done = Signal()

    def __init__(self, parent, av: E.Available, presets: list[tuple[str, str, dict]], job=None, threads: int = 0,
                 first: str = ""):
        """presets: [(key, label, settings)] — every preset of the list (built in, devices, own)."""
        super().__init__(parent)
        from . import compare as C
        self.C = C
        self.av, self.threads, self.job = av, threads, job
        self.presets = presets
        self.results: list[dict] = []
        self._stop = False
        self._thread = None
        self.setWindowTitle(t("cmp_title") + " — " + APP_TITLE)
        self.resize(900, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(_head(t("cmp_title")))
        src = job.src if job is not None else ""
        self.what = _hint(t("cmp_from", os.path.basename(src)) if src else t("cmp_test"))
        lay.addWidget(self.what)
        g = QGridLayout()
        self.a = QComboBox()
        self.b = QComboBox()
        for box in (self.a, self.b):
            box.setMaxVisibleItems(30)
            for key, label, _st in presets:
                if key.startswith("#"):
                    box.addItem(label, None)
                    it = box.model().item(box.count() - 1)
                    it.setEnabled(False)
                    f = it.font()
                    f.setBold(True)
                    it.setFont(f)
                else:
                    box.addItem(label, key)
        keys = [k for k, _l, _s in presets if not k.startswith("#")]
        self.a.setCurrentIndex(max(0, self.a.findData(first if first in keys else keys[0] if keys else "")))
        second = next((k for k in keys if k != (self.a.currentData() or "")), "")
        self.b.setCurrentIndex(max(0, self.b.findData(second)))
        g.addWidget(QLabel("A:"), 0, 0)
        g.addWidget(self.a, 0, 1)
        g.addWidget(QLabel("B:"), 1, 0)
        g.addWidget(self.b, 1, 1)
        self.length = QComboBox()
        for s in C.LENGTHS:
            self.length.addItem(f"{s} {t('sec')}", s)
        self.length.setCurrentIndex(1)
        g.addWidget(QLabel(t("cmp_len")), 0, 2)
        g.addWidget(self.length, 0, 3)
        g.setColumnStretch(1, 1)
        lay.addLayout(g)
        self.view = QTreeWidget()
        self.view.setObjectName("playlist")
        cols = ["", t("preset"), t("st_size").rstrip(":"), t("cmp_film"), "fps", t("bench_speed"), "VMAF", "PSNR",
                "SSIM"]
        self.view.setColumnCount(len(cols))
        self.view.setHeaderLabels(cols)
        self.view.setRootIsDecorated(False)
        h = self.view.header()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3, 4, 5, 6, 7, 8):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        lay.addWidget(self.view, 1)
        vm = "libvmaf" in av.filters
        lay.addWidget(_hint(t("cmp_hint") + ("" if vm else " " + t("cmp_no_vmaf"))))
        self.bar = QProgressBar()
        self.bar.setRange(0, 2)
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        self.go = QPushButton(t("bench_start"))
        self.go.setObjectName("primary")
        self.go.clicked.connect(self.start)
        row.addWidget(self.go)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)
        self.row.connect(self._on_row)
        self.done.connect(self._on_done)

    def _settings(self, key: str) -> tuple[str, dict]:
        for k, label, st in self.presets:
            if k == key:
                return label, st
        return key, {}

    def start(self):
        if self._thread is not None:
            return
        ka, kb = self.a.currentData(), self.b.currentData()
        if not ka or not kb:
            return
        self.go.setEnabled(False)
        self._stop = False
        self.results = []
        self.view.clear()
        self.items = []
        for tag, key in (("A", ka), ("B", kb)):
            label, _st = self._settings(key)
            it = QTreeWidgetItem(self.view, [tag, label, "", "", "", "", "", "", t("bench_wait")])
            self.items.append(it)
        self.bar.setValue(0)
        length = float(self.length.currentData() or 10)
        job = self.job

        def work():
            try:
                folder = self.C.work_dir()
                if job is not None and job.info is not None and not job.info.error:
                    src, info = job.src, job.info
                    dur = info.duration
                    n = min(length, dur) if dur > 0 else length
                    start = max(0.0, min(dur * 0.33, dur - n)) if dur > 0 else 0.0
                else:
                    from . import ff, probe as P
                    src = self.C.test_clip(ff.ffmpeg() or "ffmpeg", folder, int(length))
                    info = P.probe(src)
                    start, n = 0.0, length
                self.film = info.duration if info is not None else 0.0
                for i, key in enumerate((ka, kb)):
                    if self._stop:
                        break
                    label, st = self._settings(key)
                    self.row.emit({"i": i, "running": True})
                    r = self.C.run_one(key, label, st, src, info, start, n, self.av, folder, self.threads,
                                       stop=lambda: self._stop, n=i)
                    r["i"] = i
                    self.row.emit(r)
            except Exception as ex:                         # the test clip could not be made …
                self.row.emit({"i": 0, "ok": False, "error": str(ex), "key": ka})
            self.done.emit()
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def _on_row(self, r: dict):
        from . import probe as P
        it = self.items[r["i"]]
        if r.get("running"):
            it.setText(8, t("bench_running"))
            return
        self.results.append(r)
        self.bar.setValue(len(self.results))
        if not r.get("ok"):
            for c in range(2, 9):
                it.setText(c, "")
            it.setText(2, "✗ " + (r.get("error") or "")[:120])
            it.setToolTip(2, r.get("error") or "")
            return
        it.setToolTip(1, r.get("profile", ""))
        it.setText(2, P.fmt_size(r["size"]))
        full = self.C.full_size(r, getattr(self, "film", 0.0))
        it.setText(3, P.fmt_size(full) if full else "—")
        it.setText(4, f"{r['fps']:.1f}" if r.get("fps") else "—")
        it.setText(5, f"{r['speed']:.2f}×")
        it.setText(6, f"{r['vmaf']:.2f}" if "vmaf" in r else "—")
        it.setText(7, f"{r['psnr']:.2f} dB" if "psnr" in r else "—")
        it.setText(8, f"{r['ssim']:.4f}" if "ssim" in r else "—")
        self._mark()

    def _mark(self):
        """The better value of each column in green, bold."""
        win = self.C.best(self.results)
        col = {"size": (2, 3), "speed": (4, 5), "vmaf": (6,), "psnr": (7,), "ssim": (8,)}
        for r in self.results:
            it = self.items[r["i"]]
            for what, cols in col.items():
                good = win.get(what) is not None and win.get(what) == r.get("key") and \
                    sum(1 for x in self.results if x.get("key") == r.get("key")) == 1
                for c in cols:
                    f = it.font(c)
                    f.setBold(good)
                    it.setFont(c, f)
                    it.setForeground(c, QColor(style.OK) if good else QColor(style.TEXT))

    def _on_done(self):
        self._thread = None
        self.go.setEnabled(True)
        self.go.setText(t("bench_again"))

    def wait(self, timeout: float = 900) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def reject(self):
        self._stop = True
        super().reject()
