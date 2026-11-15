"""1.7: the log window — the history of conversions on the left (search, only errors), the chosen log on the right
with its sections, results and errors coloured; find (Ctrl+F, F3), jump to the next error, copy, open the folder
or the file in the system's editor. Ctrl+L or Log → History; «Open the log» of a task opens it here."""
from __future__ import annotations

import os
import re
import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QKeySequence, QShortcut, QSyntaxHighlighter,
                           QTextCharFormat, QTextCursor, QTextDocument)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPlainTextEdit, QSplitter, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from . import logs, style
from .i18n import t

ERROR_RE = re.compile(r"(^|\W)(error|failed|invalid|cannot|could not|not found|no such|unable|corrupt|"
                      r"ошибка|не удалось)(\W|$)|ENCODING FAILED|^✗", re.I)
WARN_RE = re.compile(r"^Warning:|^⚠|\bwarning\b|deprecated|discarding|non[- ]monotonic|past duration", re.I)
HEAD_RE = re.compile(r"^(Video general|Audio general|Subtitles|Statistics|Encoder settings[^:]*|Frame statistics|"
                     r"Input file|Joined file|Output file|Output folder|Output format|Profile|Trim|Split|"
                     r"Quality metrics)\s*:", re.I)
PHASE_RE = re.compile(r"^(PASS \d+ OF \d+.*|ENCODING STARTED.*|MAIN PASS.*|"
                      r"ENCODING\b.*STARTED.*|PRE PASS.*|POST PASS.*)$")
RESULT_RE = re.compile(r"^ENCODING (COMPLETED|FAILED|STOPPED)!")
KEY_RE = re.compile(r"^(\s*[A-Za-z][\w /.()#:-]{1,40}?:)(\s)")


class LogHighlighter(QSyntaxHighlighter):
    """The colours of a log: ----- lines, phases, headings, «key:», the result, warnings and errors."""

    def __init__(self, doc: QTextDocument):
        super().__init__(doc)

        def fmt(color: str, bold: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Bold)
            return f
        self.f_line = fmt(style.BORDER if not style.IS_DARK else "#5a5a5a")
        self.f_phase = fmt(style.ACCENT, True)
        self.f_head = fmt(style.STRONG, True)
        self.f_key = fmt(style.MUTED)
        self.f_ok = fmt(style.OK, True)
        self.f_err = fmt(style.ERR, True)
        self.f_warn = fmt(style.WARN)
        self.f_cmd = fmt(style.RUN_TEXT)

    def highlightBlock(self, text: str):
        n = len(text)
        if text.startswith("-----"):
            self.setFormat(0, n, self.f_line)
            return
        m = RESULT_RE.match(text)
        if m:
            self.setFormat(0, n, self.f_ok if m.group(1) == "COMPLETED" else
                           self.f_err if m.group(1) == "FAILED" else self.f_warn)
            return
        if text.startswith(("Error:", "✗")) or ERROR_RE.search(text):
            self.setFormat(0, n, self.f_err)
            return
        if WARN_RE.search(text):
            self.setFormat(0, n, self.f_warn)
            return
        if PHASE_RE.match(text):
            self.setFormat(0, n, self.f_phase)
            return
        if text.startswith(("Command:", "$ ")):
            self.setFormat(0, n, self.f_cmd)
            return
        m = HEAD_RE.match(text)
        if m:
            self.setFormat(0, m.end(), self.f_head)
            return
        m = KEY_RE.match(text)
        if m and not text.lstrip().startswith("["):
            self.setFormat(0, m.end(1), self.f_key)


def is_error_line(text: str) -> bool:
    return bool(text.startswith(("Error:", "✗")) or ERROR_RE.search(text)) and not RESULT_RE.match(text) \
        or text.startswith("ENCODING FAILED")


class LogWindow(QDialog):
    """Not modal: it stays open beside the main window."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(t("log_history"))
        self.resize(1180, 700)
        self.path = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        sp = QSplitter(Qt.Horizontal)
        # left: the history
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText(t("lv_filter"))
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.only_err = QCheckBox(t("lv_only_errors"))
        self.only_err.toggled.connect(self._apply_filter)
        ll.addWidget(self.filter)
        ll.addWidget(self.only_err)
        self.hist = QTreeWidget()
        self.hist.setObjectName("playlist")
        self.hist.setColumnCount(4)
        self.hist.setHeaderLabels([t("log_when"), t("col_file"), t("col_state"), t("lv_took")])
        self.hist.setRootIsDecorated(False)
        hh = self.hist.header()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.hist.currentItemChanged.connect(lambda cur, _p: self.show_log(cur.data(0, Qt.UserRole) if cur else ""))
        ll.addWidget(self.hist, 1)
        sp.addWidget(left)
        # right: the log
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.title = QLabel("")
        self.title.setStyleSheet("font-weight: 600;")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bar.addWidget(self.title, 1)
        self.find = QLineEdit()
        self.find.setPlaceholderText(t("lv_find"))
        self.find.setClearButtonEnabled(True)
        self.find.setMaximumWidth(240)
        self.find.returnPressed.connect(lambda: self.find_next())
        bar.addWidget(self.find)

        def btn(icon: str, tip: str, slot) -> QToolButton:
            b = QToolButton()
            b.setObjectName("ctl")
            b.setIcon(style.icon(icon))
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False: slot())
            bar.addWidget(b)
            return b
        self.b_up = btn("fa6s.arrow-up", t("lv_prev") + " (Shift+F3)", lambda: self.find_next(back=True))
        self.b_down = btn("fa6s.arrow-down", t("lv_next") + " (F3)", lambda: self.find_next())
        self.b_err = btn("fa6s.triangle-exclamation", t("lv_next_error") + " (F8)", lambda: self.next_error())
        self.b_copy = btn("fa6s.copy", t("log_copy"), lambda: QApplication.clipboard().setText(self.view.toPlainText()))
        self.b_dir = btn("fa6s.folder-open", t("lv_folder"), lambda: self._open(os.path.dirname(self.path or logs.logs_dir())))
        self.b_ext = btn("fa6s.up-right-from-square", t("lv_external"), lambda: self.path and self._open(self.path))
        rl.addLayout(bar)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setStyleSheet("font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 9pt;")
        self.hl = LogHighlighter(self.view.document())
        rl.addWidget(self.view, 1)
        self.status = QLabel("")
        self.status.setObjectName("hint")
        rl.addWidget(self.status)
        sp.addWidget(right)
        sp.setSizes([380, 800])
        lay.addWidget(sp)
        QShortcut(QKeySequence.Find, self, activated=lambda: (self.find.setFocus(), self.find.selectAll()))
        QShortcut(QKeySequence("F3"), self, activated=lambda: self.find_next())
        QShortcut(QKeySequence("Shift+F3"), self, activated=lambda: self.find_next(back=True))
        QShortcut(QKeySequence("F8"), self, activated=lambda: self.next_error())
        QShortcut(QKeySequence("F5"), self, activated=lambda: self.reload())
        self.reload()

    @staticmethod
    def _open(path: str):
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def reload(self, select: str = ""):
        keep = select or self.path
        self.hist.clear()
        for h in logs.history():
            el = float(h.get("elapsed", 0) or 0)
            it = QTreeWidgetItem(self.hist, [time.strftime("%d.%m.%y %H:%M", time.localtime(h.get("time", 0))),
                                             os.path.basename(h.get("src", "")), t("s_" + h.get("status", "done")),
                                             f"{int(el // 3600)}:{int(el // 60 % 60):02d}:{int(el % 60):02d}"])
            it.setData(0, Qt.UserRole, h.get("log", ""))
            it.setData(1, Qt.UserRole, (h.get("src", "") + " " + h.get("out", "") + " " + h.get("profile", "")).lower())
            it.setData(2, Qt.UserRole, h.get("status", "done"))
            it.setToolTip(1, h.get("src", "") + ("\n→ " + h["out"] if h.get("out") else "") +
                          ("\n" + h["error"] if h.get("error") else ""))
            it.setForeground(2, QColor(style.OK if h.get("status") == "done" else style.ERR if h.get("status") == "error"
                                       else style.MUTED))
        self._apply_filter()
        self.select(keep)

    def _apply_filter(self):
        text = self.filter.text().strip().lower()
        err = self.only_err.isChecked()
        for i in range(self.hist.topLevelItemCount()):
            it = self.hist.topLevelItem(i)
            hide = (bool(text) and text not in it.data(1, Qt.UserRole)) or (err and it.data(2, Qt.UserRole) != "error")
            it.setHidden(hide)

    def visible_count(self) -> int:
        return sum(1 for i in range(self.hist.topLevelItemCount()) if not self.hist.topLevelItem(i).isHidden())

    def select(self, path: str = ""):
        """Show this log (a file of the history, or any log file); the newest when none is given."""
        for i in range(self.hist.topLevelItemCount()):
            it = self.hist.topLevelItem(i)
            if it.isHidden():
                continue
            if not path or os.path.normcase(it.data(0, Qt.UserRole) or "") == os.path.normcase(path):
                self.hist.setCurrentItem(it)
                return
        if path:
            self.hist.setCurrentItem(None)
            self.show_log(path)

    def show_log(self, path: str):
        self.path = path or ""
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            text = ""
        self.view.setPlainText(text)
        self.title.setText(os.path.basename(self.path) if self.path else "")
        n = sum(1 for ln in text.splitlines() if is_error_line(ln))
        self.status.setText(t("lv_errors_n", n) if n else (t("lv_no_errors") if text else ""))
        self.b_err.setEnabled(n > 0)
        self.b_ext.setEnabled(bool(text))
        if "ENCODING FAILED" in text or n:
            self.view.moveCursor(QTextCursor.Start)
            self.next_error()
        else:
            self.view.moveCursor(QTextCursor.Start)

    def find_next(self, back: bool = False) -> bool:
        word = self.find.text()
        if not word:
            return False
        flags = QTextDocument.FindBackward if back else QTextDocument.FindFlags()
        if self.view.find(word, flags):
            return True
        cur = self.view.textCursor()                 # wrap round
        cur.movePosition(QTextCursor.End if back else QTextCursor.Start)
        self.view.setTextCursor(cur)
        return self.view.find(word, flags)

    def next_error(self) -> bool:
        doc = self.view.document()
        start = self.view.textCursor().blockNumber() + 1
        for k in range(doc.blockCount()):
            b = doc.findBlockByNumber((start + k) % doc.blockCount())
            if is_error_line(b.text()):
                cur = QTextCursor(b)
                cur.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                self.view.setTextCursor(cur)
                self.view.centerCursor()
                return True
        return False
