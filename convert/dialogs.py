"""Settings, «encoders on this PC», update."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QIcon
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
              ("grp_encode", ("hw_decode", "overwrite")),
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
        for key in ("xvid", "modern"):
            self.look.addItem(t("look_" + key), key)
        self.look.setCurrentIndex(max(0, self.look.findData(cfg.get("look", "xvid"))))
        form.addRow(t("look"), self.look)
        self.suffix = QLineEdit(cfg.get("out_suffix", "_convert"))
        self.suffix.setPlaceholderText(t("suffix_empty"))
        form.addRow(t("out_suffix"), self.suffix)
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
            lay.addWidget(box)
        from . import config
        lay.addWidget(_hint(t("data_in", config.data_dir())))
        lay.addWidget(_buttons(self))

    def accept(self):
        self.cfg["language"] = self.lang.currentData()
        self.cfg["theme"] = self.theme.currentData()
        self.cfg["look"] = self.look.currentData()
        self.cfg["out_suffix"] = self.suffix.text().strip()
        for k, cb in self.checks.items():
            self.cfg[k] = cb.isChecked()
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
