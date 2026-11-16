"""Settings, «encoders on this PC», update."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon
import os

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QProgressBar, QPushButton, QTextBrowser, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QHeaderView)

from . import APP_TITLE, VERSION, ff, style
from . import encoders as E
from .i18n import t


def _buttons(dlg, ok=True):
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel if ok else QDialogButtonBox.Close)
    for b in bb.buttons():
        b.setIcon(QIcon())
    if ok:
        bb.button(QDialogButtonBox.Ok).setObjectName("primary")
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    return bb


def _head(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet("font-size: 14pt; font-weight: 700; padding-bottom: 6px;")
    return lab


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


class SettingsDialog(QDialog):
    CHECKS = [("grp_start", ("single_window", "keep_queue", "check_updates", "notify")),
              ("grp_encode", ("hw_decode", "overwrite", "auto_ext")),
              ("grp_logs", ("save_logs", "log_next_to_output"))]

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(t("settings") + " — " + APP_TITLE)
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(_head(t("settings")))
        form = QFormLayout()
        self.lang = QComboBox()
        self.lang.addItem("Русский", "ru")
        self.lang.addItem("English", "en")
        self.lang.setCurrentIndex(0 if cfg.get("language") == "ru" else 1)
        form.addRow(t("language"), self.lang)
        self.theme = QComboBox()
        for key in ("dark", "light", "auto"):
            self.theme.addItem(t("theme_" + key), key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(cfg.get("theme", "dark"))))
        form.addRow(t("theme"), self.theme)
        self.look = QComboBox()
        for key in ("classic", "modern"):
            self.look.addItem(t("look_" + key), key)
        self.look.setCurrentIndex(max(0, self.look.findData(cfg.get("look", "classic"))))
        form.addRow(t("look"), self.look)
        self.suffix = QLineEdit(cfg.get("out_suffix", "_convert"))
        self.suffix.setPlaceholderText(t("suffix_empty"))
        form.addRow(t("out_suffix"), self.suffix)
        from . import job as J                            # 1.8: the name from a template
        self.tpl = QComboBox()
        self.tpl.setEditable(True)
        for key, tpl in J.NAME_TEMPLATES:
            self.tpl.addItem(t(key) + (f"  —  {tpl}" if tpl else ""), tpl)
        cur = cfg.get("name_template", "") or ""
        i = self.tpl.findData(cur)
        if i >= 0:
            self.tpl.setCurrentIndex(i)
        else:
            self.tpl.setEditText(cur)
        self.tpl.setToolTip(t("tpl_tip"))
        form.addRow(t("tpl_label"), self.tpl)
        lay.addLayout(form)
        lay.addWidget(_hint(t("suffix_hint")))
        self.checks: dict[str, QCheckBox] = {}
        for title, keys in self.CHECKS:
            box = QGroupBox(t(title))
            v = QVBoxLayout(box)
            for k in keys:
                cb = QCheckBox(t("opt_" + k))
                cb.setChecked(bool(cfg.get(k)))
                self.checks[k] = cb
                v.addWidget(cb)
            if title == "grp_encode":                     # 1.8: encoding in chunks
                from PySide6.QtWidgets import QSpinBox, QDoubleSpinBox
                ch = cfg.get("chunks") or {}
                self.ch_on = QCheckBox(t("opt_chunks"))
                self.ch_on.setChecked(bool(ch.get("on")))
                v.addWidget(self.ch_on)
                row = QHBoxLayout()
                row.setContentsMargins(24, 0, 0, 0)
                row.addWidget(QLabel(t("chk_workers")))
                self.ch_workers = QSpinBox()
                self.ch_workers.setRange(0, 32)
                self.ch_workers.setSpecialValueText(t("chk_auto"))
                self.ch_workers.setValue(int(ch.get("workers", 0) or 0))
                row.addWidget(self.ch_workers)
                row.addSpacing(12)
                row.addWidget(QLabel(t("chk_minutes")))
                self.ch_min = QDoubleSpinBox()
                self.ch_min.setRange(0.5, 30.0)
                self.ch_min.setSingleStep(0.5)
                self.ch_min.setDecimals(1)
                self.ch_min.setSuffix(" " + t("min_short"))
                self.ch_min.setValue(float(ch.get("minutes", 3) or 3))
                row.addWidget(self.ch_min)
                row.addStretch(1)
                v.addLayout(row)
                hint = _hint(t("chk_hint"))
                hint.setContentsMargins(24, 0, 0, 0)
                v.addWidget(hint)
            lay.addWidget(box)
        # 2.0: Explorer's menu, the tray, the watch folders
        from . import winint
        box = QGroupBox(t("grp_windows"))
        v = QVBoxLayout(box)
        self.shell = QCheckBox(t("opt_shell_menu"))
        self.shell.setChecked(bool(cfg.get("shell_menu")))
        self.shell.setToolTip(t("opt_shell_menu_tip"))
        self.shell.setEnabled(winint.IS_WIN)
        v.addWidget(self.shell)
        self.ctx_start = QCheckBox(t("opt_ctx_start"))
        self.ctx_start.setChecked(bool(cfg.get("ctx_start")))
        self.ctx_start.setContentsMargins(24, 0, 0, 0)
        self.ctx_start.setStyleSheet("margin-left: 24px;")
        v.addWidget(self.ctx_start)
        self.tray = QCheckBox(t("opt_tray"))
        self.tray.setChecked(bool(cfg.get("tray")))
        v.addWidget(self.tray)
        row = QHBoxLayout()
        self.b_watch = QPushButton(t("wf_title") + "…")
        self.b_watch.setObjectName("watchButton")
        self.b_watch.clicked.connect(lambda _c=False: self._watch())
        row.addWidget(self.b_watch)
        self.watch_state = QLabel("")
        self.watch_state.setObjectName("hint")
        row.addWidget(self.watch_state, 1)
        v.addLayout(row)
        self._watch_text()
        if not winint.IS_WIN:
            v.addWidget(_hint(t("opt_shell_menu_win")))
        lay.addWidget(box)
        from . import config
        lay.addWidget(_hint(t("data_in", config.data_dir())))
        lay.addWidget(_buttons(self))

    def _watch_text(self):
        from . import watch
        w = watch.settings(self.cfg)
        n = sum(1 for f in w["folders"] if f["on"])
        self.watch_state.setText(t("wf_state", n) if w["on"] and n else t("wf_state_off"))

    def _watch(self):
        p = self.parent()
        if p is not None and hasattr(p, "watch_dialog"):
            p.watch_dialog()
        self._watch_text()

    def accept(self):
        self.cfg["language"] = self.lang.currentData()
        self.cfg["theme"] = self.theme.currentData()
        self.cfg["look"] = self.look.currentData()
        self.cfg["out_suffix"] = self.suffix.text().strip()
        txt = self.tpl.currentText().strip()
        i = self.tpl.findText(txt)
        self.cfg["name_template"] = (self.tpl.itemData(i) or "") if i >= 0 else txt
        for k, cb in self.checks.items():
            self.cfg[k] = cb.isChecked()
        self.cfg["chunks"] = {"on": self.ch_on.isChecked(), "workers": self.ch_workers.value(),
                              "minutes": round(self.ch_min.value(), 1)}
        self.cfg["shell_menu"] = self.shell.isChecked()
        self.cfg["ctx_start"] = self.ctx_start.isChecked()
        self.cfg["tray"] = self.tray.isChecked()
        super().accept()


class WatchDialog(QDialog):
    """2.0: Settings → Watch folders: a row per folder (on, the folder, its preset, where the results go, with the
    folders under it); new complete video files are queued and encoded while the program is open."""

    def __init__(self, cfg: dict, presets: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        from . import watch
        self.cfg = cfg
        self.presets = presets
        self.setWindowTitle(t("wf_title") + " — " + APP_TITLE)
        self.setMinimumWidth(760)
        w = watch.settings(cfg)
        self.folders = [dict(f) for f in w["folders"]]
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(_head(t("wf_title")))
        lay.addWidget(_hint(t("wf_hint")))
        self.on = QCheckBox(t("wf_on"))
        self.on.setChecked(w["on"])
        lay.addWidget(self.on)
        self.tree = QTreeWidget()
        self.tree.setObjectName("watchTree")
        self.tree.setRootIsDecorated(False)
        self.tree.setHeaderLabels([t("wf_col_on"), t("wf_col_folder"), t("wf_col_preset"), t("wf_col_out"),
                                   t("wf_col_sub")])
        self.tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tree.itemDoubleClicked.connect(lambda _it, _c: self.edit())
        self.tree.itemChanged.connect(self._ticked)
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        for key, slot in (("wf_add", self.add), ("wf_edit", self.edit), ("wf_remove", self.remove)):
            b = QPushButton(t(key))
            b.setObjectName(key)
            b.clicked.connect(lambda _c=False, f=slot: f())
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(QLabel(t("wf_stable")))
        from PySide6.QtWidgets import QSpinBox
        self.stable = QSpinBox()
        self.stable.setRange(2, 600)
        self.stable.setSuffix(" " + t("sec_short"))
        self.stable.setValue(w["stable"])
        self.stable.setToolTip(t("wf_stable_tip"))
        row.addWidget(self.stable)
        lay.addLayout(row)
        lay.addWidget(_buttons(self))
        self._fill()

    def _preset_label(self, key: str) -> str:
        if not key:
            return t("wf_preset_default")
        return next((lab for k, lab in self.presets if k == key), key)

    def _fill(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        for f in self.folders:
            it = QTreeWidgetItem(["", f["path"], self._preset_label(f["preset"]), f["out"] or t("wf_out_beside"),
                                  t("yes") if f["sub"] else t("no")])
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked if f["on"] else Qt.Unchecked)
            self.tree.addTopLevelItem(it)
        for c in (0, 2, 3, 4):
            self.tree.resizeColumnToContents(c)
        self.tree.blockSignals(False)

    def _ticked(self, it, col):
        i = self.tree.indexOfTopLevelItem(it)
        if col == 0 and 0 <= i < len(self.folders):
            self.folders[i]["on"] = it.checkState(0) == Qt.Checked

    def add(self, folder: dict | None = None) -> bool:
        from . import watch
        f = FolderDialog(folder or dict(watch.FOLDER_KEYS), self.presets, self)
        self._sub = f
        if folder is not None or f.exec():
            self.folders.append(watch.clean_folder(f.value()))
            self._fill()
            return True
        return False

    def edit(self):
        i = self.tree.indexOfTopLevelItem(self.tree.currentItem()) if self.tree.currentItem() else -1
        if not 0 <= i < len(self.folders):
            return
        from . import watch
        f = FolderDialog(self.folders[i], self.presets, self)
        self._sub = f
        if f.exec():
            self.folders[i] = watch.clean_folder(f.value())
            self._fill()

    def remove(self):
        i = self.tree.indexOfTopLevelItem(self.tree.currentItem()) if self.tree.currentItem() else -1
        if 0 <= i < len(self.folders):
            self.folders.pop(i)
            self._fill()

    def accept(self):
        self.cfg["watch"] = {"on": self.on.isChecked(), "stable": self.stable.value(), "folders": list(self.folders)}
        super().accept()


class FolderDialog(QDialog):
    """One watch folder: the folder, its preset, the result folder (empty = beside the source), with subfolders."""

    def __init__(self, folder: dict, presets: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("wf_folder"))
        self.setMinimumWidth(620)
        self.f = dict(folder)
        lay = QVBoxLayout(self)
        form = QFormLayout()

        def picker(edit: QLineEdit, title: str):
            row = QHBoxLayout()
            row.addWidget(edit, 1)
            b = QPushButton("…")
            b.setFixedWidth(34)

            def pick(_c=False):
                from PySide6.QtWidgets import QFileDialog
                d = QFileDialog.getExistingDirectory(self, title, edit.text())
                if d:
                    edit.setText(os.path.normpath(d))
            b.clicked.connect(pick)
            row.addWidget(b)
            return row
        self.path = QLineEdit(self.f.get("path", ""))
        self.path.setObjectName("wfPath")
        form.addRow(t("wf_col_folder"), picker(self.path, t("wf_col_folder")))
        self.preset = QComboBox()
        self.preset.setObjectName("wfPreset")
        self.preset.addItem(t("wf_preset_default"), "")
        for key, label in presets:
            if key.startswith("#"):
                self.preset.addItem(label, None)
                self.preset.model().item(self.preset.count() - 1).setEnabled(False)
            else:
                self.preset.addItem("   " + label, key)
        self.preset.setMaxVisibleItems(30)
        self.preset.setCurrentIndex(max(0, self.preset.findData(self.f.get("preset", ""))))
        form.addRow(t("wf_col_preset"), self.preset)
        self.out = QLineEdit(self.f.get("out", ""))
        self.out.setObjectName("wfOut")
        self.out.setPlaceholderText(t("wf_out_beside"))
        form.addRow(t("wf_col_out"), picker(self.out, t("wf_col_out")))
        self.sub = QCheckBox(t("wf_sub"))
        self.sub.setChecked(bool(self.f.get("sub", True)))
        form.addRow("", self.sub)
        self.on = QCheckBox(t("wf_col_on"))
        self.on.setChecked(bool(self.f.get("on", True)))
        form.addRow("", self.on)
        lay.addLayout(form)
        lay.addWidget(_hint(t("wf_folder_hint")))
        lay.addWidget(_buttons(self))

    def value(self) -> dict:
        return {"path": self.path.text().strip(), "preset": self.preset.currentData() or "",
                "out": self.out.text().strip(), "sub": self.sub.isChecked(), "on": self.on.isChecked()}

    def accept(self):
        if not self.path.text().strip() or not os.path.isdir(self.path.text().strip()):
            self.path.setFocus()
            self.path.setStyleSheet(f"border: 1px solid {style.ERR};")
            return
        super().accept()


class EncodersDialog(QDialog):
    """What this PC can encode with: every encoder, ✓ / ✗ and why not."""
    recheck = Signal()

    def __init__(self, av: E.Available, ready: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("encoders_here") + " — " + APP_TITLE)
        self.resize(720, 560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(_head(t("encoders_here")))
        self.head = _hint("")
        lay.addWidget(self.head)
        self.view = QTreeWidget()
        self.view.setObjectName("playlist")
        self.view.setColumnCount(3)
        self.view.setHeaderLabels([t("encoder"), "ffmpeg", t("enc_state")])
        self.view.setRootIsDecorated(False)
        h = self.view.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        lay.addWidget(self.view, 1)
        lay.addWidget(_hint(t("enc_dialog_hint")))
        row = QHBoxLayout()
        self.again = QPushButton(t("enc_recheck"))
        self.again.clicked.connect(self._recheck)
        row.addWidget(self.again)
        row.addStretch(1)
        bb = _buttons(self, ok=False)
        row.addWidget(bb)
        lay.addLayout(row)
        self.set_available(av if ready else None)

    def _recheck(self):
        self.again.setEnabled(False)
        self.set_available(None)
        self.recheck.emit()

    def set_available(self, av):
        self.view.clear()
        if av is None:
            self.head.setText(t("enc_checking"))
            return
        self.again.setEnabled(True)
        gpu = av.gpu()
        self.head.setText(t("enc_head", ff.version() or "—", E.VENDOR_LABEL[gpu] if gpu else t("enc_no_gpu")))
        for e in E.VIDEO:
            it = QTreeWidgetItem(self.view)
            it.setText(0, f"{E.FAMILY_LABEL[e.family]} — " +
                       (E.VENDOR_LABEL[e.vendor] if e.hw else e.ffname.replace("lib", "") + f" ({t('cpu')})"))
            it.setText(1, e.ffname)
            ok = av.ok.get(e.id)
            it.setIcon(2, style.icon("fa6s.circle-check" if ok else "fa6s.circle-xmark", style.OK if ok else style.MUTED))
            line = av.errors.get(e.id, "")
            if ok:
                text = t("enc_ok") + f", {max(e.bits)} bit"
            elif not e.hw or e.id not in av.reasons:
                text = t("enc_not_in_build")
            else:
                text = t("why_" + av.reasons[e.id])
            it.setText(2, text)
            it.setToolTip(2, f"{text}\n\nffmpeg: {line}" if line else text)
            it.setToolTip(0, it.toolTip(2))


class UpdateDialog(QDialog):
    """Release notes + download and silent install of the new Setup.exe."""
    progress = Signal(int, int)
    done_ = Signal(str, str)

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        from . import updater
        self.info, self.updater, self.path = info, updater, ""
        self.setWindowTitle(t("upd_title"))
        self.resize(600, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(_head(t("upd_available", info["version"])))
        sub = QLabel(t("upd_text", info["version"], VERSION))
        sub.setStyleSheet(f"color: {style.MUTED};")
        lay.addWidget(sub)
        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info.get("notes") or "")
        notes.setStyleSheet(f"background: {style.PANEL}; border: 1px solid {style.BORDER}; border-radius: 8px; padding: 6px;")
        lay.addWidget(notes, 1)
        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.bar)
        lay.addWidget(_hint(t("upd_restart")))
        bb = QDialogButtonBox()
        self.go = bb.addButton(t("upd_install"), QDialogButtonBox.AcceptRole)
        self.go.setObjectName("primary")
        bb.addButton(t("upd_later"), QDialogButtonBox.RejectRole)
        bb.accepted.connect(self._go)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.progress.connect(self._on_progress)
        self.done_.connect(self._on_done)

    def _go(self):
        import threading
        self.go.setEnabled(False)
        self.bar.setRange(0, max(1, self.info.get("size", 0)))
        self.bar.show()

        def work():
            try:
                p = self.updater.download(self.info["url"], self.info.get("size", 0),
                                          lambda d, n: self.progress.emit(d, n))
                self.done_.emit(p, "")
            except Exception as e:
                self.done_.emit("", str(e))
        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, d: int, n: int):
        if n:
            self.bar.setMaximum(n)
        self.bar.setValue(min(d, self.bar.maximum()))
        self.bar.setFormat(t("upd_downloading", f"{d / 1048576:.1f}", f"{(n or d) / 1048576:.1f}"))

    def _on_done(self, path: str, err: str):
        from PySide6.QtWidgets import QMessageBox
        if err:
            self.go.setEnabled(True)
            QMessageBox.warning(self, t("upd_title"), t("upd_error", err))
            return
        self.path = path
        self.accept()


class BenchDialog(QDialog):
    """1.4, Tools → Speed test: a short piece through every encoder of this PC, one after another (a thread);
    the table fills row by row: frames a second, speed against real time, the size it came out at."""
    row = Signal(object)
    done = Signal()

    def __init__(self, parent, av: E.Available, src: str | None, duration: float, threads: int = 0,
                 only: list[str] | None = None):
        super().__init__(parent)
        from . import bench as B
        self.B = B
        self.av, self.threads = av, threads
        self.encs = [e for e in B.encoders(av) if not only or e.id in only]
        self.src, self.start, self.length = B.piece(src, duration)
        self.results: list[dict] = []
        self._stop = False
        self._thread = None
        self.setWindowTitle(t("bench") + " — " + APP_TITLE)
        self.resize(760, 520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.addWidget(_head(t("bench")))
        import os
        what = (t("bench_from", os.path.basename(src), f"{self.length:g}") if self.src else
                t("bench_test", f"{self.length:g}"))
        lay.addWidget(_hint(what + " " + t("bench_hint")))
        self.view = QTreeWidget()
        self.view.setObjectName("playlist")
        self.view.setColumnCount(5)
        self.view.setHeaderLabels([t("encoder"), "fps", t("bench_speed"), t("st_size").rstrip(":"), t("enc_state")])
        self.view.setRootIsDecorated(False)
        h = self.view.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 5):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.items = {}
        for e in self.encs:
            it = QTreeWidgetItem(self.view, [e.label, "", "", "", t("bench_wait")])
            self.items[e.id] = it
        lay.addWidget(self.view, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, max(1, len(self.encs)))
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        self.go = QPushButton(t("bench_start"))
        self.go.setObjectName("primary")
        self.go.clicked.connect(self.start_test)
        row.addWidget(self.go)
        row.addStretch(1)
        row.addWidget(_buttons(self, ok=False))
        lay.addLayout(row)
        self.row.connect(self._on_row)
        self.done.connect(self._on_done)

    def start_test(self):
        import threading
        if self._thread is not None:
            return
        self.go.setEnabled(False)
        self._stop = False
        self.results = []
        self.bar.setValue(0)
        exe, probe = ff.ffmpeg() or "ffmpeg", ff.ffprobe() or "ffprobe"

        def work():
            for e in self.encs:
                if self._stop:
                    break
                self.row.emit({"id": e.id, "running": True})
                self.row.emit(self.B.run_one(e, exe, probe, self.src, self.start, self.length, self.threads))
            self.done.emit()
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def _on_row(self, r: dict):
        it = self.items.get(r["id"])
        if it is None:
            return
        if r.get("running"):
            it.setText(4, t("bench_running"))
            return
        self.results.append(r)
        self.bar.setValue(len(self.results))
        if r["ok"]:
            from . import probe as P
            it.setText(1, f"{r['fps']:.1f}")
            it.setText(2, f"{r['speed']:.2f}×")
            it.setText(3, P.fmt_size(r["size"]))
            it.setText(4, "✓")
            from PySide6.QtGui import QColor
            it.setForeground(4, QColor(style.OK))
        else:
            it.setText(4, "✗ " + (r.get("error") or "")[:120])
            it.setToolTip(4, r.get("error") or "")
        best = max((x["fps"] for x in self.results if x["ok"]), default=0)
        for x in self.results:
            if x["ok"]:
                f = self.items[x["id"]].font(0)
                f.setBold(x["fps"] == best)
                for c in range(5):
                    self.items[x["id"]].setFont(c, f)

    def _on_done(self):
        self._thread = None
        self.go.setEnabled(True)
        self.go.setText(t("bench_again"))

    def wait(self, timeout: float = 600) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def reject(self):
        self._stop = True
        super().reject()


class DiscDialog(QDialog):
    """1.8: the titles of a Blu-ray / DVD — playlist, length, parts, size, video, the sound and subtitle
    languages; the main film (the longest) is chosen. Several can be taken at once (a task each)."""

    def __init__(self, kind: str, root: str, titles: list[dict], name: str, parent=None):
        super().__init__(parent)
        from . import probe as P
        self.titles = titles
        self.setWindowTitle(("Blu-ray" if kind == "bd" else "DVD") + " — " + name)
        self.setMinimumSize(860, 420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.addWidget(_head(("Blu-ray" if kind == "bd" else "DVD") + ": " + name))
        lay.addWidget(_hint(t("disc_hint")))
        self.view = QTreeWidget()
        self.view.setObjectName("playlist")
        self.view.setRootIsDecorated(False)
        self.view.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.view.setHeaderLabels([t("disc_title"), t("disc_len"), t("disc_parts"), t("disc_size"), t("disc_video"),
                                   t("disc_audio"), t("disc_subs")])
        h = self.view.header()
        for c in range(7):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents if c < 5 else QHeaderView.Stretch)
        for n, ti in enumerate(titles):
            d = ti["duration"]
            audio = ", ".join(f"{c} {lang}".strip() for c, lang in ti["audio"]) or "—"
            subs = ", ".join(x or "?" for x in ti["subs"]) or "—"
            it = QTreeWidgetItem(self.view, [("★ " if ti.get("main") else "") + ti["id"],
                                             f"{int(d // 3600)}:{int(d // 60 % 60):02d}:{int(d % 60):02d}" if d else "?",
                                             str(len(ti["files"])), P.fmt_size(ti["size"]), ", ".join(ti["video"]),
                                             audio, subs])
            it.setData(0, Qt.UserRole, n)
            it.setToolTip(5, audio)
            it.setToolTip(6, subs)
            if ti.get("main"):
                it.setSelected(True)
                f = it.font(0)
                f.setBold(True)
                for c in range(7):
                    it.setFont(c, f)
        self.view.itemDoubleClicked.connect(lambda *_: self.accept())
        lay.addWidget(self.view, 1)
        if not titles:
            lay.addWidget(_hint(t("disc_none")))
        lay.addWidget(_buttons(self))

    def chosen(self) -> list[dict]:
        rows = sorted(self.view.indexOfTopLevelItem(it) for it in self.view.selectedItems())
        return [self.titles[self.view.topLevelItem(r).data(0, Qt.UserRole)] for r in rows]


class DemuxDialog(QDialog):
    """1.8: every track of a task into a file of its own, without encoding (.h264 .hevc .ac3 .dts .thd .srt .sup …),
    the DTS core of DTS-HD and the AC3 core of E-AC3 too, the chapters as a text file.
    1.9: «Stop»; a stopped, failed or closed run leaves no half-written files; the speed and the time left."""

    def __init__(self, job, parent=None):
        super().__init__(parent)
        from . import job as J
        from .runner import Runner
        self.job = job
        self.J = J
        self.setWindowTitle(t("demux"))
        self.setMinimumSize(780, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.addWidget(_head(t("demux")))
        lay.addWidget(_hint(t("demux_hint")))
        row = QHBoxLayout()
        row.addWidget(QLabel(t("demux_to")))
        stem = os.path.splitext(os.path.basename(job.src))[0]
        self.folder = QLineEdit(os.path.join(os.path.dirname(job.src) if not job.src.lower().endswith(".ffconcat")
                                             else os.path.expanduser("~"), f"{stem} [tracks]"))
        row.addWidget(self.folder, 1)
        self.b_pick = QPushButton("…")
        self.b_pick.clicked.connect(lambda: self._pick())
        row.addWidget(self.b_pick)
        lay.addLayout(row)
        self.cores = QCheckBox(t("demux_cores"))
        self.cores.setChecked(True)
        self.cores.toggled.connect(lambda _on: self._fill())
        lay.addWidget(self.cores)
        self.chap = QCheckBox(t("demux_chapters"))
        self.chap.setChecked(True)
        lay.addWidget(self.chap)
        self.view = QTreeWidget()
        self.view.setObjectName("playlist")
        self.view.setRootIsDecorated(False)
        self.view.setHeaderLabels([t("demux_track"), t("demux_file")])
        self.view.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.view.header().setSectionResizeMode(1, QHeaderView.Stretch)
        lay.addWidget(self.view, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.rate = _hint("")                     # 1.9: «412 MB/s · 2.1 of 31.6 GB · 1:12 left»
        self.rate.hide()
        lay.addWidget(self.rate)
        self.state = _hint("")
        self.state.setTextFormat(Qt.PlainText)
        lay.addWidget(self.state)
        bb = QDialogButtonBox()
        self.b_go = bb.addButton(t("demux_go"), QDialogButtonBox.AcceptRole)
        self.b_go.setObjectName("primary")
        self.b_stop = bb.addButton(t("demux_stop"), QDialogButtonBox.ActionRole)
        self.b_stop.setIcon(style.icon("fa6s.stop"))
        self.b_stop.hide()
        self.b_close = bb.addButton(QDialogButtonBox.Close)
        self.b_go.clicked.connect(lambda: self.start())
        self.b_stop.clicked.connect(lambda: self.stop())
        self.b_close.clicked.connect(lambda: self.reject())
        lay.addWidget(bb)
        self.runner = Runner(self)
        self.runner.progress.connect(self._progress)
        self.runner.finished.connect(self._done)
        self.done_items: list[dict] = []
        self.removed: list[str] = []
        self._made_folder = False                 # (self.ok appears when a run ends: True / False)
        self._total = self._src_bytes()
        self._fill()

    @property
    def running(self) -> bool:
        return self.runner.running

    def _src_bytes(self) -> int:
        """How much is read: the file, or every clip of a disc title / joined files."""
        src = self.job.src
        files = [p for p, _a, _b in self.J.ffconcat_entries(src)] if src.lower().endswith(".ffconcat") else [src]
        n = 0
        for f in files:
            try:
                n += os.path.getsize(f)
            except OSError:
                pass
        return n

    def _pick(self):
        from PySide6.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, t("demux_to"), self.folder.text())
        if d:
            self.folder.setText(d)
            self._fill()

    def _fill(self):
        self.items = self.J.demux_items(self.job, self.folder.text().strip(), self.cores.isChecked())
        self.view.clear()
        from PySide6.QtGui import QFont
        for n, it in enumerate(self.items):
            s = it["stream"]
            what = it["what"]
            if it.get("core"):                    # 1.9: its own, plain row: «#3 DTS core ← DTS-HD MA»
                what = t("demux_core_of", what, self.J.P_codec(s))
            row = QTreeWidgetItem(self.view, [f"#{s.index} {what}" + (f" · {s.lang}" if s.lang else ""),
                                              os.path.basename(it["path"])])
            row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
            row.setCheckState(0, Qt.Checked)
            row.setData(0, Qt.UserRole, n)
            if it.get("core"):
                f = QFont(row.font(0))
                f.setBold(True)
                row.setFont(0, f)
                row.setToolTip(0, t("demux_core_tip"))

    def core_rows(self) -> list[str]:
        """The texts of the core rows (tests, screenshots)."""
        return [self.view.topLevelItem(i).text(0) for i in range(self.view.topLevelItemCount())
                if self.items[self.view.topLevelItem(i).data(0, Qt.UserRole)].get("core")]

    def chosen(self) -> list[dict]:
        out = []
        for i in range(self.view.topLevelItemCount()):
            row = self.view.topLevelItem(i)
            if row.checkState(0) == Qt.Checked:
                out.append(self.items[row.data(0, Qt.UserRole)])
        return out

    def _set_running(self, on: bool):
        self.b_go.setEnabled(not on)
        self.b_stop.setVisible(on)
        self.b_stop.setEnabled(on)
        for w in (self.folder, self.b_pick, self.cores, self.chap, self.view):
            w.setEnabled(not on)
        self.rate.setVisible(on)
        if on:
            self.rate.setText(t("demux_reading"))

    def start(self):
        if self.running:
            return
        folder = self.folder.text().strip()
        off = {self.view.topLevelItem(i).data(0, Qt.UserRole) for i in range(self.view.topLevelItemCount())
               if self.view.topLevelItem(i).checkState(0) != Qt.Checked}
        self._fill()                                      # the paths in the folder now typed, the ticks kept
        for i in range(self.view.topLevelItemCount()):
            if self.view.topLevelItem(i).data(0, Qt.UserRole) in off:
                self.view.topLevelItem(i).setCheckState(0, Qt.Unchecked)
        items = self.chosen() if self.view.topLevelItemCount() else []
        if not items or not folder:
            return
        self._made_folder = not os.path.isdir(folder)
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as e:
            self.state.setText("✗ " + str(e))
            return
        self.done_items = items
        self.removed = []
        self.__dict__.pop("ok", None)
        args = self.J.demux_args(self.job, ff.ffmpeg() or "ffmpeg", items)
        self.bar.setValue(0)
        self.bar.show()
        self._set_running(True)
        self.state.setText(t("demux_running"))
        self.runner.start(args, items[0]["path"], self.J.src_duration(self.job), direct=True)

    def stop(self) -> None:
        """«Stop»: ffmpeg is killed; _done takes the half-written files away."""
        if self.running:
            self.b_stop.setEnabled(False)
            self.state.setText(t("demux_stopping"))
            self.runner.cancel()

    def _progress(self, d: dict):
        frac = float(d.get("frac", 0) or 0)
        self.bar.setValue(int(frac * 1000))
        el = float(d.get("elapsed", 0) or 0)
        if frac <= 0.002 or el < 0.8:
            return
        done = frac * self._total
        mbs = done / el / 1e6 if self._total else 0.0
        left = el * (1 - frac) / frac
        from .probe import fmt_size, fmt_time
        parts = []
        if mbs > 0:
            parts.append(t("demux_mbs", f"{mbs:.0f}" if mbs >= 10 else f"{mbs:.1f}"))
            parts.append(t("demux_of", fmt_size(done), fmt_size(self._total)))
        if d.get("speed"):
            parts.append(f"×{float(d['speed']):.0f}" if float(d["speed"]) >= 10 else f"×{float(d['speed']):.1f}")
        parts.append(t("demux_left", fmt_time(left)))
        self.rate.setText(" · ".join(parts))

    def _cleanup(self) -> list[str]:
        """The files of a run that did not finish — each chosen track's (only those, never the source)."""
        keep = {os.path.normcase(os.path.abspath(p)) for p in [self.job.src] + list(self.job.joins)}
        gone = []
        from .runner import _remove
        for it in self.done_items:
            p = it["path"]
            if os.path.normcase(os.path.abspath(p)) in keep or not os.path.exists(p):
                continue
            _remove(p)
            if not os.path.exists(p):
                gone.append(p)
        folder = self.folder.text().strip()
        if self._made_folder and os.path.isdir(folder):
            try:
                os.rmdir(folder)                          # only when empty
            except OSError:
                pass
        return gone

    def _done(self, ok: bool, err: str):
        self._set_running(False)
        self.bar.setValue(1000 if ok else 0)
        self.ok = ok
        if not ok:
            self.removed = self._cleanup()
            n = len(self.removed)
            if self.runner.cancelled:
                self.state.setText("■ " + t("demux_stopped", n))
            else:
                self.state.setText("✗ " + (err or t("s_stop")) + "\n" + t("demux_failed_clean", n))
            return
        folder = self.folder.text().strip()
        if self.chap.isChecked():                          # the chapters only after a clean run
            txt = self.J.chapters_text(self.job)
            if txt:
                stem = os.path.splitext(os.path.basename(self.job.src))[0]
                try:
                    with open(os.path.join(folder, f"{stem} - chapters.txt"), "w", encoding="utf-8") as f:
                        f.write(txt)
                except OSError:
                    pass
        made = [it for it in self.done_items if os.path.isfile(it["path"]) and os.path.getsize(it["path"]) > 0]
        empty = [it for it in self.done_items if it not in made]
        msg = "✓ " + t("demux_done", len(made), folder)
        if empty:                                          # 1.9: say which one came out empty (a core, say)
            msg += "\n⚠ " + t("demux_empty", ", ".join(os.path.basename(it["path"]) for it in empty))
            for it in empty:
                try:
                    os.remove(it["path"])
                except OSError:
                    pass
        self.state.setText(msg)

    def _ask_stop(self) -> bool:
        from PySide6.QtWidgets import QMessageBox
        return QMessageBox.question(self, t("demux"), t("demux_ask_stop")) == QMessageBox.Yes

    def reject(self):
        if self.running:
            if not self._ask_stop():
                return
            self._kill_and_clean()
        super().reject()

    def _kill_and_clean(self):
        """Closing while it runs: ffmpeg is stopped and waited for, so its files can be deleted at once."""
        if not self.running:
            return
        proc = self.runner.proc
        self.runner.cancel()
        if proc is not None:
            proc.waitForFinished(5000)
        if self.running:                                   # no «finished» came: clean up here
            self.runner.proc = None
            self._done(False, "")


class EstimateDialog(QDialog):
    """1.8: «at CRF 20 it comes to about 4.3 GB» — measured on short samples from all over the film; two steps
    either side on request; a row's quality can be taken for the task."""

    def __init__(self, job, av, hw: bool = True, parent=None, on_pick=None):
        super().__init__(parent)
        from . import job as J
        from .estimate import Estimator
        self.job, self.av, self.hw, self.on_pick = job, av, hw, on_pick
        self.setWindowTitle(t("est_title"))
        self.setMinimumSize(560, 360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.addWidget(_head(t("est_title")))
        self.enc = av.resolve(job.settings.get("video", {}).get("encoder", "auto_hevc")) if av is not None else None
        v = job.settings.get("video", {})
        self.quality_mode = v.get("rc", "quality") == "quality" or (self.enc is not None and not self.enc.rc)
        self.q = int(v.get("quality") or (self.enc.q_default if self.enc else 23))
        lay.addWidget(_hint(t("est_hint", J.describe(job.settings, av, t))))
        self.view = QTreeWidget()
        self.view.setObjectName("playlist")
        self.view.setRootIsDecorated(False)
        self.view.setHeaderLabels([t("est_q"), t("est_size"), t("est_vrate")])
        for c in range(3):
            self.view.header().setSectionResizeMode(c, QHeaderView.Stretch)
        self.view.itemDoubleClicked.connect(lambda it, _c: self._pick(it))
        lay.addWidget(self.view, 1)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.state = _hint("")
        lay.addWidget(self.state)
        row = QHBoxLayout()
        self.b_more = QPushButton(t("est_more"))
        self.b_more.clicked.connect(self.more)
        self.b_more.setEnabled(self.quality_mode and self.enc is not None)
        self.b_take = QPushButton(t("est_take"))
        self.b_take.clicked.connect(lambda: self._pick(self.view.currentItem()))
        self.b_take.setEnabled(False)
        row.addWidget(self.b_more)
        row.addWidget(self.b_take)
        row.addStretch(1)
        close = QPushButton(t("close"))
        close.clicked.connect(self.reject)
        row.addWidget(close)
        lay.addLayout(row)
        self.est = Estimator(self)
        self.est.row.connect(self._row)
        self.est.progress.connect(lambda k, n: self.bar.setValue(int(1000 * k / max(1, n))))
        self.est.done.connect(self._done)
        self.view.currentItemChanged.connect(lambda cur, _p: self.b_take.setEnabled(cur is not None and self.quality_mode))
        self.rows = {}
        self.J = J
        self.start([self.q if self.quality_mode else None])

    def start(self, qualities):
        qualities = [q for q in qualities if q not in self.rows]
        if not qualities:
            return
        self.bar.setValue(0)
        self.state.setText(t("est_running"))
        self.b_more.setEnabled(False)
        self.est.run(self.job, self.av, qualities, self.hw)

    def more(self):
        from .estimate import steps_around
        self.start(steps_around(self.enc, self.q))

    def _row(self, q, size: float, kbps: float):
        from . import probe as P
        name = (("CQ " if self.enc is not None and self.enc.hw else "CRF ") + str(q)) if q is not None else \
            self.J.describe(self.job.settings, self.av, t)
        it = QTreeWidgetItem([name + ("  ★" if q == self.q else ""), "≈ " + P.fmt_size(size), f"≈ {kbps:.0f} kbit/s"])
        it.setData(0, Qt.UserRole, q)
        if q == self.q or q is None:
            f = it.font(0)
            f.setBold(True)
            for c in range(3):
                it.setFont(c, f)
        self.rows[q] = (size, kbps)
        keys = sorted(k for k in self.rows if k is not None)
        self.view.insertTopLevelItem(keys.index(q) if q is not None else 0, it)

    def _done(self, err: str):
        self.bar.setValue(1000 if not err else self.bar.value())
        self.state.setText(("✗ " + err) if err and err != "stopped" else t("est_note"))
        self.b_more.setEnabled(self.quality_mode and self.enc is not None and len(self.rows) < 5)

    def _pick(self, it):
        if it is None or it.data(0, Qt.UserRole) is None:
            return
        q = int(it.data(0, Qt.UserRole))
        self.job.settings.setdefault("video", {})["quality"] = q
        if self.on_pick:
            self.on_pick(self.job, q)
        self.accept()

    def reject(self):
        self.est.stop = True
        super().reject()
