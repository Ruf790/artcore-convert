"""The per-file tabs of 1.1: Trim (pieces of the film with a libmpv preview), Metadata (the file's tags; language,
title and flags of every track) and Chapters. Each one edits a single task; `changed` tells the window."""
from __future__ import annotations
import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QComboBox, QDoubleSpinBox, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QRadioButton, QSizePolicy,
                               QStackedLayout, QStyledItemDelegate, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import encoders as E
from . import job as J
from . import mpvview, style
from .i18n import t
from .timeline import Timeline


def _btn(icon: str, tip: str, slot=None, text: str = "", size: int = 15) -> QToolButton:
    b = QToolButton()
    b.setObjectName("ctlText" if text else "ctl")
    b.setIcon(style.icon(icon, style.ICON))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    if text:
        b.setText("  " + text)
        b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    if slot:
        b.clicked.connect(slot)
    return b


def _hint(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


class _ReadOnly(QStyledItemDelegate):
    def createEditor(self, *_a):
        return None


def _tree(headers: list[str], read_only: tuple = ()) -> QTreeWidget:
    v = QTreeWidget()
    v.setObjectName("playlist")
    v.setColumnCount(len(headers))
    v.setHeaderLabels(headers)
    v.setRootIsDecorated(False)
    v.setUniformRowHeights(True)
    v._ro = _ReadOnly(v)
    for c in read_only:
        v.setItemDelegateForColumn(c, v._ro)
    return v


# ══ Trim ══════════════════════════════════════════════════════
class Stage(QWidget):
    """The black area of the picture; tells the trim controller when it becomes visible."""

    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def showEvent(self, e):
        super().showEvent(e)
        self.owner.stage_shown(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        self.owner.stage_shown(False)


class TrimTab(QWidget):
    changed = Signal()
    media_wanted = Signal(object)        # a new task is shown: its strip and loudness are needed
    position_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.job: J.Job | None = None
        self.editable = False
        self.preview = None                      # MpvView | FramePreview, made when the tab is first shown
        self.fps = 25.0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self.hint = _hint()
        lay.addWidget(self.hint)
        # the picture, the strip under it and the player buttons are placed by the window (the
        # preview in the middle, the buttons in the bottom bar); this widget itself holds the trim controls
        self.stage = Stage(self)
        self.stage.setMinimumHeight(160)
        self.stage.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.stage.setStyleSheet("background: #000;")
        self.stage_lay = QStackedLayout(self.stage)
        self.stage_lay.setContentsMargins(0, 0, 0, 0)
        self.placeholder = QLabel(t("trm_placeholder"))
        self.placeholder.setAlignment(Qt.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet("color: #8a8f99; font-size: 11pt; background: #000;")
        self.stage_lay.addWidget(self.placeholder)
        self.bar = Timeline()
        self.bar.seek.connect(lambda s: self._seek(s, exact=False))
        self.transport = QWidget()
        row = QHBoxLayout(self.transport)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.b_back = _btn("fa6s.backward", t("trm_back5") + " (Shift+←)", lambda: self._jump(-5))
        self.b_prev = _btn("fa6s.backward-step", t("trm_prev_frame") + " (←)", lambda: self._step(-1))
        self.b_play = _btn("fa6s.play", t("trm_play") + " (Space)", self.toggle_play, size=16)
        self.b_next = _btn("fa6s.forward-step", t("trm_next_frame") + " (→)", lambda: self._step(1))
        self.b_fwd = _btn("fa6s.forward", t("trm_fwd5") + " (Shift+→)", lambda: self._jump(5))
        for b in (self.b_back, self.b_prev, self.b_play, self.b_next, self.b_fwd):
            row.addWidget(b)
        row.addSpacing(10)
        self.time = QLabel("0 / 0  0:00.000")
        self.time.setStyleSheet("font-family: Consolas, 'DejaVu Sans Mono', monospace;")
        row.addWidget(self.time)
        row.addStretch(1)
        self.b_mute = _btn("fa6s.volume-high", t("trm_mute"), self._toggle_mute)
        self.muted = False
        row.addWidget(self.b_mute)
        self.b_back.setIcon(style.icon("fa6s.backward-fast", style.ICON))
        # In / Out
        g = QHBoxLayout()
        g.setSpacing(6)
        self.b_in = _btn("fa6s.arrow-right-to-bracket", t("trm_set_in") + " (I)", self.set_in, t("trm_in"))
        self.b_out = _btn("fa6s.arrow-right-from-bracket", t("trm_set_out") + " (O)", self.set_out, t("trm_out"))
        self.e_in = QLineEdit()
        self.e_out = QLineEdit()
        for e in (self.e_in, self.e_out):
            e.setPlaceholderText("0:00.000")
            e.setMinimumWidth(96)
            e.setMaximumWidth(130)
            e.editingFinished.connect(self._marks_typed)
        self.b_add = _btn("fa6s.plus", t("trm_add_tip") + " (Enter)", self.add_piece, t("trm_add"))
        self.b_replace = _btn("fa6s.pen", t("trm_replace_tip"), self.replace_piece, t("trm_replace"))
        for wdg in (self.b_in, self.e_in, self.b_out, self.e_out):
            g.addWidget(wdg)
        g.addStretch(1)
        lay.addLayout(g)
        mode = QHBoxLayout()
        self.r_keep = QRadioButton(t("trm_keep"))
        self.r_cut = QRadioButton(t("trm_cut"))
        self.mode_grp = QButtonGroup(self)
        self.mode_grp.addButton(self.r_keep)
        self.mode_grp.addButton(self.r_cut)
        self.r_keep.setChecked(True)
        self.r_cut.toggled.connect(self._mode_changed)
        acts = QHBoxLayout()
        acts.addWidget(self.b_add)
        acts.addWidget(self.b_replace)
        acts.addStretch(1)
        lay.addLayout(acts)
        mode.addWidget(self.r_keep)
        mode.addWidget(self.r_cut)
        mode.addStretch(1)
        lay.addLayout(mode)
        body = QHBoxLayout()
        self.list = _tree(["#", t("trm_from"), t("trm_to"), t("trm_len")], (0, 1, 2, 3))
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        h = self.list.header()
        for c in range(4):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents if c == 0 else QHeaderView.Stretch)
        self.list.setMinimumHeight(90)
        self.list.itemSelectionChanged.connect(self._piece_picked)
        self.list.itemDoubleClicked.connect(lambda it, _c: self._seek(self._piece(it)[0]))
        body.addWidget(self.list, 1)
        side = QVBoxLayout()
        self.b_del = _btn("fa6s.trash-can", t("trm_del"), self.delete_piece)
        self.b_clear = _btn("fa6s.eraser", t("trm_clear"), self.clear)
        self.b_edl = _btn("fa6s.file-import", t("edl_import") + "…", lambda: self.import_edl())   # 1.8
        self.b_edl_out = _btn("fa6s.file-export", t("edl_export") + "…", lambda: self.export_edl())
        side.addWidget(self.b_del)
        side.addWidget(self.b_clear)
        side.addWidget(self.b_edl)
        side.addWidget(self.b_edl_out)
        side.addStretch(1)
        body.addLayout(side)
        lay.addLayout(body, 2)
        self.summary = _hint()
        lay.addWidget(self.summary)
        self.set_job(None, False)

    # ── the task ──────────────────────────────────────────────
    def set_job(self, job: J.Job | None, editable: bool):
        same = job is self.job
        self.job, self.editable = job, editable
        ok = job is not None and job.info is not None and not job.info.error and J.src_duration(job) > 0
        for w in (self.b_in, self.b_out, self.e_in, self.e_out, self.b_add, self.b_replace, self.r_keep, self.r_cut,
                  self.list, self.b_del, self.b_clear, self.b_edl):
            w.setEnabled(ok and editable)
        self.b_edl_out.setEnabled(ok and bool(job.trim) if job is not None else False)
        for w in (self.b_back, self.b_prev, self.b_play, self.b_next, self.b_fwd, self.bar):
            w.setEnabled(ok)
        if not ok:
            self.hint.setText(t("trm_pick_one_trim"))
            self.bar.dur, self.bar.keep = 0.0, []
            self.list.clear()
            self.summary.setText("")
            if self.preview is not None:
                self.preview.unload()
            self.stage_lay.setCurrentWidget(self.placeholder)
            self.bar.clear_media()
            self._media_for = None
            self.time.setText("0 / 0  0:00.000")
            return
        self.hint.setText(t("trm_hint") + ("" if mpvview.load_mpv() is not None
                                          else "\n⚠ " + t("trm_no_mpv")))
        v = job.info.video
        self.fps = v.fps if v is not None and v.fps else 25.0
        self.bar.dur = J.src_duration(job)
        if not same:
            self.bar.mark_in = self.bar.mark_out = None
            self.e_in.clear()
            self.e_out.clear()
            self.bar.pos = 0.0
        self.r_keep.blockSignals(True)
        self.r_cut.blockSignals(True)
        (self.r_cut if job.trim_cut else self.r_keep).setChecked(True)
        self.r_keep.blockSignals(False)
        self.r_cut.blockSignals(False)
        self._fill()
        if getattr(self, "_media_for", None) is not job:
            self._media_for = job
            self.media_wanted.emit(job)
        if self.stage.isVisible():
            self._load_preview()

    def _fill(self):
        j = self.job
        self.list.blockSignals(True)
        self.list.clear()
        for n, (a, b) in enumerate(sorted((float(x), float(y)) for x, y in j.trim)):
            it = QTreeWidgetItem(self.list, [str(n + 1), J.fmt_tc(a), J.fmt_tc(b), J.fmt_tc(b - a)])
            it.setData(0, Qt.UserRole, (a, b))
        self.list.blockSignals(False)
        segs = J.segments(j)
        self.bar.keep = segs
        self.bar.update()
        if not j.trim:
            self.summary.setText(t("trm_none"))
            return
        total = J.out_duration(j)
        text = t("trm_total", J.fmt_tc(total, False), J.fmt_tc(J.src_duration(j), False), len(segs))
        warn = [t(w) for w in dict.fromkeys(J.plan(j)["warnings"]) if w.startswith("w_trim")]
        self.summary.setText(text + "".join("\n⚠ " + w for w in warn))

    def _piece(self, it) -> tuple[float, float]:
        return tuple(it.data(0, Qt.UserRole))

    def _changed(self):
        self._fill()
        self.changed.emit()

    # ── preview ───────────────────────────────────────────────
    def stage_shown(self, on: bool):
        """The window shows / hides the picture (the Status tab puts the log there)."""
        if on and self.job is not None and self.job.info is not None and not self.job.info.error:
            self._load_preview()
        elif not on and self.preview is not None:
            self.preview.pause()

    def _make_preview(self, frames_only: bool = False):
        if self.preview is not None:
            return
        mod = None if frames_only else mpvview.load_mpv()
        p = None
        if mod is not None:
            try:
                p = mpvview.MpvView(mod, self.stage)
            except Exception as ex:                       # libmpv there but would not start
                mpvview._mpv_error = str(ex)
                p = None
        if p is None:
            p = mpvview.FramePreview(self.stage)
        self.preview = p
        self.stage_lay.addWidget(p)
        self.stage_lay.setCurrentWidget(p)
        p.position.connect(self._on_pos)
        p.duration.connect(lambda d: (setattr(self.bar, "dur", d or self.bar.dur), self._on_pos(self.bar.pos)))
        p.paused.connect(lambda on: self.b_play.setIcon(style.icon("fa6s.play" if on else "fa6s.pause", style.ICON)))
        p.position.connect(self.position_changed)
        if isinstance(p, mpvview.FramePreview):
            for b in (self.b_play, self.b_mute):
                b.setVisible(False)
            if self.job is not None:
                self.hint.setText(t("trm_hint") + "\n⚠ " + t("trm_no_mpv"))

    def _gl_check(self):
        """No OpenGL 3.3 here (old driver, remote desktop): libmpv can't draw — still frames instead."""
        p = self.preview
        if isinstance(p, mpvview.MpvView) and p.ctx is None and self.stage.isVisible():
            p.shutdown()
            p.setParent(None)
            p.deleteLater()
            self.preview = None
            mpvview._mpv_error = "OpenGL"
            self._make_preview(frames_only=True)
            self._load_preview()

    def _load_preview(self):
        self._make_preview()
        self.stage_lay.setCurrentWidget(self.preview)
        if isinstance(self.preview, mpvview.MpvView) and self.preview.ctx is None:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(3000, self._gl_check)
        j = self.job
        if j is None or j.info is None or j.info.error:
            return
        if isinstance(self.preview, mpvview.FramePreview):
            if self.preview.path != J.media_src(j):
                self.preview.load(J.media_src(j), J.src_duration(j), self.fps)
        else:
            self.preview.load(J.preview_src(j))

    def position(self) -> float | None:
        """Where the preview stands (seconds of the source), None if it shows nothing of this task."""
        if self.preview is None or self.job is None or \
                self.preview.path not in (J.preview_src(self.job), J.media_src(self.job)):
            return None
        return self.bar.pos

    def _on_pos(self, s: float):
        self.bar.pos = s
        self.bar.update()
        frame = int(round(s * self.fps)) + 1
        total = max(1, int(round(self.bar.dur * self.fps)))
        self.time.setText(f"{min(frame, total)}/{total}  {J.fmt_tc(s)}")

    def _seek(self, s: float, exact: bool = True):
        if self.preview is None:
            return
        self.bar.pos = s
        self.preview.seek(s, exact)
        self._on_pos(s)

    def _jump(self, d: float):
        self._seek(max(0.0, min(self.bar.dur, self.bar.pos + d)))

    def _step(self, n: int):
        if self.preview is not None:
            self.preview.step(n)

    def toggle_play(self):
        if self.preview is not None:
            self.preview.toggle()

    def _toggle_mute(self):
        self.muted = not self.muted
        if self.preview is not None:
            self.preview.set_mute(self.muted)
        self.b_mute.setIcon(style.icon("fa6s.volume-xmark" if self.muted else "fa6s.volume-high", style.ICON))

    def keyPressEvent(self, e):
        k, mods = e.key(), e.modifiers()
        if k in (Qt.Key_Left, Qt.Key_Right):
            d = -1 if k == Qt.Key_Left else 1
            self._jump(5 * d) if mods & Qt.ShiftModifier else self._step(d)
        elif k == Qt.Key_I and self.b_in.isEnabled():
            self.set_in()
        elif k == Qt.Key_O and self.b_out.isEnabled():
            self.set_out()
        elif k in (Qt.Key_Return, Qt.Key_Enter) and self.b_add.isEnabled():
            self.add_piece()
        elif k == Qt.Key_Delete and self.b_del.isEnabled():
            self.delete_piece()
        else:
            super().keyPressEvent(e)

    # ── pieces ────────────────────────────────────────────────
    def set_in(self, at: float | None = None):
        s = self.bar.pos if at is None else at
        self.bar.mark_in = s
        self.e_in.setText(J.fmt_tc(s))
        if self.bar.mark_out is not None and self.bar.mark_out <= s:
            self.bar.mark_out = None
            self.e_out.clear()
        self.bar.update()

    def set_out(self, at: float | None = None):
        s = self.bar.pos if at is None else at
        self.bar.mark_out = s
        self.e_out.setText(J.fmt_tc(s))
        if self.bar.mark_in is None:
            self.bar.mark_in = 0.0
            self.e_in.setText(J.fmt_tc(0))
        self.bar.update()

    def _marks_typed(self):
        a, b = J.parse_tc(self.e_in.text()), J.parse_tc(self.e_out.text())
        self.bar.mark_in, self.bar.mark_out = a, b
        self.bar.update()
        if self.sender() is self.e_in and a is not None:
            self._seek(a)
        elif self.sender() is self.e_out and b is not None:
            self._seek(b)

    def _marks(self) -> tuple[float, float] | None:
        a = J.parse_tc(self.e_in.text())
        b = J.parse_tc(self.e_out.text())
        dur = J.src_duration(self.job) if self.job and self.job.info else 0.0
        if a is None and b is None:
            return None
        a = a or 0.0
        b = dur if b is None else min(b, dur or b)
        if b - a < 0.04:
            self.summary.setText("⚠ " + t("trm_bad_piece"))
            return None
        return round(a, 3), round(b, 3)

    def add_piece(self):
        m = self._marks()
        if m is None or self.job is None:
            return
        self.job.trim.append(list(m))
        self.bar.mark_in = self.bar.mark_out = None
        self.e_in.clear()
        self.e_out.clear()
        self._changed()

    def replace_piece(self):
        it = self.list.currentItem()
        m = self._marks()
        if it is None or m is None or self.job is None:
            return
        old = list(self._piece(it))
        for i, p in enumerate(self.job.trim):
            if [float(p[0]), float(p[1])] == old:
                self.job.trim[i] = list(m)
                break
        self._changed()

    def delete_piece(self):
        it = self.list.currentItem()
        if it is None or self.job is None:
            return
        old = list(self._piece(it))
        self.job.trim = [p for p in self.job.trim if [float(p[0]), float(p[1])] != old]
        self._changed()

    def clear(self):
        if self.job is not None and self.job.trim:
            self.job.trim = []
            self._changed()

    def _piece_picked(self):
        it = self.list.currentItem()
        if it is None:
            return
        a, b = self._piece(it)
        self.bar.mark_in, self.bar.mark_out = a, b
        self.e_in.setText(J.fmt_tc(a))
        self.e_out.setText(J.fmt_tc(b))
        self._seek(a)

    # ── 1.8: EDL ───────────────────────────────────────────────
    def import_edl(self, path: str | None = None) -> bool:
        """An EDL (Kodi / MPlayer, DaVinci / Premiere CMX 3600, mpv) → the pieces of this task."""
        if self.job is None or not self.editable:
            return False
        if not isinstance(path, str) or not path:
            from PySide6.QtWidgets import QFileDialog
            path, _f = QFileDialog.getOpenFileName(self, t("edl_import"), os.path.dirname(self.job.src),
                                                   "EDL (*.edl *.txt);;" + t("all_files") + " (*)")
            if not path:
                return False
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                pcs, cut = J.parse_edl(f.read(), self.fps or 25.0)
        except (OSError, ValueError):
            self.summary.setText("⚠ " + t("edl_bad"))
            return False
        dur = J.src_duration(self.job)
        pcs = [[a, min(b, dur) if dur else b] for a, b in pcs if not dur or a < dur]
        self.job.trim, self.job.trim_cut = pcs, cut
        self.r_cut.setChecked(cut)
        self.r_keep.setChecked(not cut)
        self._changed()
        return True

    def export_edl(self, path: str | None = None) -> bool:
        if self.job is None or not self.job.trim:
            return False
        if not isinstance(path, str) or not path:
            from PySide6.QtWidgets import QFileDialog
            path, _f = QFileDialog.getSaveFileName(self, t("edl_export"), os.path.splitext(self.job.src)[0] + ".edl",
                                                   "EDL (*.edl)")
            if not path:
                return False
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(J.write_edl(self.job.trim, self.job.trim_cut))
        except OSError:
            return False
        return True

    def _mode_changed(self):
        if self.job is not None and self.editable:
            self.job.trim_cut = self.r_cut.isChecked()
            self._changed()

    def shutdown(self):
        if self.preview is not None:
            self.preview.shutdown()


# ══ Metadata ══════════════════════════════════════════════════
LANGS = ("rus", "eng", "ukr", "bel", "kaz", "bul", "pol", "cze", "deu", "fra", "spa", "ita", "por", "tur", "jpn",
         "chi", "kor", "heb", "ara", "hin", "und")
COMMON_TAGS = ("title", "artist", "date", "genre", "comment", "description", "copyright")


class MetaTab(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.job: J.Job | None = None
        self.editable = False
        self._filling = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        self.hint = _hint()
        lay.addWidget(self.hint)
        box = QGroupBox(t("md_file"))
        v = QVBoxLayout(box)
        self.tags = _tree([t("md_tag"), t("md_value")])
        self.tags.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                                  QAbstractItemView.SelectedClicked)
        self.tags.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tags.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tags.itemChanged.connect(self._tag_edited)
        self.tags.setMinimumHeight(110)
        v.addWidget(self.tags)
        row = QHBoxLayout()
        self.b_tag_add = _btn("fa6s.plus", t("md_add"), self._add_tag, t("md_add"))
        self.b_tag_del = _btn("fa6s.trash-can", t("md_del"), self._del_tag, t("md_del"))
        self.b_tag_src = _btn("fa6s.rotate-left", t("md_from_src"), self._tags_from_source, t("md_from_src"))
        for b in (self.b_tag_add, self.b_tag_del, self.b_tag_src):
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        self.tag_hint = _hint()
        v.addWidget(self.tag_hint)
        lay.addWidget(box, 1)
        box = QGroupBox(t("md_tracks"))
        v = QVBoxLayout(box)
        self.streams = _tree(["#", t("tr_track"), t("md_lang"), t("md_title"), t("md_default"), t("md_forced")],
                             (0, 1, 2, 4, 5))
        self.streams.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                                     QAbstractItemView.SelectedClicked)
        h = self.streams.header()
        h.setMinimumSectionSize(30)
        for c, mode in enumerate((QHeaderView.ResizeToContents, QHeaderView.Stretch, QHeaderView.Fixed,
                                  QHeaderView.Stretch, QHeaderView.ResizeToContents, QHeaderView.ResizeToContents)):
            h.setSectionResizeMode(c, mode)
        self.streams.setColumnWidth(2, 168)
        hi = self.streams.headerItem()
        hi.setToolTip(4, t("md_default_tip"))
        hi.setToolTip(5, t("md_forced_tip"))
        self.streams.itemChanged.connect(self._stream_edited)
        self.streams.setMinimumHeight(120)
        v.addWidget(self.streams)
        v.addWidget(_hint(t("md_tracks_hint")))
        lay.addWidget(box, 1)
        self.set_job(None, False)

    def set_job(self, job: J.Job | None, editable: bool):
        self.job, self.editable = job, editable
        ok = job is not None and job.info is not None and not job.info.error
        for w in (self.tags, self.streams, self.b_tag_add, self.b_tag_del, self.b_tag_src):
            w.setEnabled(ok and editable)
        self._filling = True
        try:
            self.tags.clear()
            self.streams.clear()
            if not ok:
                self.hint.setText(t("pick_one_edit"))
                self.tag_hint.setText("")
                return
            self.hint.setText(t("md_hint"))
            self._fill_tags()
            self._fill_streams()
        finally:
            self._filling = False

    def _tags(self) -> dict:
        j = self.job
        tg = j.meta.get("tags")
        if tg is not None:
            return dict(tg)
        return J.source_tags(j.info) if j.settings.get("meta", True) else {}

    def _fill_tags(self):
        j = self.job
        tags = self._tags()
        keys = list(tags)
        if "title" not in tags:
            keys.insert(0, "title")
        for k in keys:
            it = QTreeWidgetItem(self.tags, [k, str(tags.get(k, ""))])
            it.setFlags(it.flags() | Qt.ItemIsEditable)
        notes = []
        if j.meta.get("tags") is not None:
            notes.append(t("md_tags_edited"))
        if not j.settings.get("meta", True):
            notes.append(t("md_meta_off"))
        self.tag_hint.setText("\n".join(notes))

    def _collect_tags(self) -> dict:
        out = {}
        for i in range(self.tags.topLevelItemCount()):
            it = self.tags.topLevelItem(i)
            k = it.text(0).strip().lower().replace(" ", "_").replace("=", "")
            val = it.text(1)
            if k and (val or k != "title"):
                out[k] = val
        return out

    def _store_tags(self):
        if self.job is None:
            return
        tags = self._collect_tags()
        src = J.source_tags(self.job.info) if self.job.settings.get("meta", True) else {}
        self.job.meta["tags"] = None if tags == src else tags
        if self.job.meta["tags"] is None:
            self.job.meta.pop("tags", None)
        self.changed.emit()
        self._filling = True
        self.tags.clear()
        self._fill_tags()
        self._filling = False

    def _tag_edited(self, _it, _c):
        if not self._filling:
            self._store_tags()

    def _add_tag(self):
        used = {self.tags.topLevelItem(i).text(0) for i in range(self.tags.topLevelItemCount())}
        key = next((k for k in COMMON_TAGS if k not in used), "tag")
        it = QTreeWidgetItem(self.tags, [key, ""])
        it.setFlags(it.flags() | Qt.ItemIsEditable)
        self.tags.setCurrentItem(it)
        self.tags.editItem(it, 1)

    def _del_tag(self):
        it = self.tags.currentItem()
        if it is None:
            return
        self.tags.takeTopLevelItem(self.tags.indexOfTopLevelItem(it))
        self._store_tags()

    def _tags_from_source(self):
        if self.job is not None:
            self.job.meta.pop("tags", None)
            self.changed.emit()
            self.set_job(self.job, self.editable)

    # tracks
    def _fill_streams(self):
        from . import probe as P
        j = self.job
        p = J.plan(j)
        rows = [(s, "video") for s in p["video"]] + [(s, "audio") for s, _ in p["audio"]] + \
            [(s, "subtitle") for s, _ in p["subs"]]
        for s, kind in rows:
            m = j.smeta(s)
            line = {"video": P.video_line, "audio": P.audio_line, "subtitle": P.sub_line}[kind](s)
            it = QTreeWidgetItem(self.streams, [str(s.index), line, "", m.get("title", s.title)])
            it.setData(0, Qt.UserRole, s.index)
            it.setToolTip(1, line)
            it.setIcon(1, style.icon({"video": "fa6s.film", "audio": "fa6s.volume-high",
                                      "subtitle": "fa6s.closed-captioning"}[kind], style.MUTED))
            it.setFlags(it.flags() | Qt.ItemIsEditable | Qt.ItemIsUserCheckable)
            if kind != "video":
                it.setCheckState(4, Qt.Checked if m.get("default", s.default) else Qt.Unchecked)
                it.setCheckState(5, Qt.Checked if m.get("forced", s.forced) else Qt.Unchecked)
            it.setSizeHint(2, QSize(140, 30))
            box = QComboBox()
            box.setMaxVisibleItems(16)
            box.setStyleSheet("QComboBox { padding: 1px 8px; min-height: 0; }")
            for code in LANGS:
                box.addItem(f"{code} — {t('lang_' + code)}", code)
            cur = m.get("lang", s.lang or "und")
            i = box.findData(cur)
            if i < 0:
                box.addItem(cur, cur)
                i = box.count() - 1
            box.setCurrentIndex(i)
            box.setEnabled(self.editable)
            box.currentIndexChanged.connect(lambda _i, b=box, st=s: self._lang_changed(st, b))
            self.streams.setItemWidget(it, 2, box)

    def _stream_of(self, idx: int):
        return next((s for s in self.job.info.streams if s.index == idx), None)

    def _set_smeta(self, s, key: str, value, src_value):
        st = self.job.meta.setdefault("streams", {})
        m = st.setdefault(str(s.index), {})
        if value == src_value and key in ("lang", "title"):
            m.pop(key, None)
        else:
            m[key] = value
        if not m:
            st.pop(str(s.index), None)
        if not st:
            self.job.meta.pop("streams", None)
        self.changed.emit()

    def _lang_changed(self, s, box: QComboBox):
        if self._filling or self.job is None:
            return
        code = str(box.currentData() or "").strip().lower()
        if code:
            self._set_smeta(s, "lang", code, s.lang or "und")

    def _stream_edited(self, it, col):
        if self._filling or self.job is None:
            return
        s = self._stream_of(int(it.data(0, Qt.UserRole)))
        if s is None:
            return
        if col == 3:
            self._set_smeta(s, "title", it.text(3), s.title)
        elif col in (4, 5):
            key = "default" if col == 4 else "forced"
            on = it.checkState(col) == Qt.Checked
            self._set_smeta(s, key, on, s.default if col == 4 else s.forced)
            if col == 4 and on and s.kind == "audio":        # one default sound track: the others lose it
                self._filling = True
                for i in range(self.streams.topLevelItemCount()):
                    other = self.streams.topLevelItem(i)
                    o = self._stream_of(int(other.data(0, Qt.UserRole)))
                    if o is not None and o is not s and o.kind == s.kind and other.checkState(4) == Qt.Checked:
                        other.setCheckState(4, Qt.Unchecked)
                        self._set_smeta(o, "default", False, o.default)
                self._filling = False


# ══ Chapters ══════════════════════════════════════════════════
class ChaptersTab(QWidget):
    changed = Signal()

    def __init__(self, position=None, parent=None):
        super().__init__(parent)
        self.position = position or (lambda: None)      # the Trim preview's moment (source seconds) or None
        self.job: J.Job | None = None
        self.editable = False
        self._filling = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self.hint = _hint()
        lay.addWidget(self.hint)
        self.view = _tree(["#", t("ch_time"), t("ch_name")], (0,))
        self.view.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed |
                                  QAbstractItemView.SelectedClicked)
        h = self.view.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        self.view.itemChanged.connect(self._edited)
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        self.b_add = _btn("fa6s.plus", t("ch_add_tip"), self.add, t("ch_add"))
        self.b_del = _btn("fa6s.trash-can", t("ch_del"), self.delete, t("ch_del"))
        self.b_clear = _btn("fa6s.eraser", t("ch_clear_tip"), self.clear, t("ch_clear"))
        self.b_src = _btn("fa6s.rotate-left", t("ch_from_src_tip"), self.from_source, t("ch_from_src"))
        for b in (self.b_add, self.b_del, self.b_clear, self.b_src):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        row = QHBoxLayout()
        self.b_auto = _btn("fa6s.wand-magic-sparkles", t("ch_auto_tip"), self.auto, t("ch_auto"))
        self.every = QDoubleSpinBox()
        self.every.setRange(1, 120)
        self.every.setDecimals(0)
        self.every.setValue(10)
        self.every.setSuffix(" " + t("ch_min"))
        row.addWidget(self.b_auto)
        row.addWidget(self.every)
        row.addStretch(1)
        lay.addLayout(row)
        self.note = _hint()
        lay.addWidget(self.note)
        self.set_job(None, False)

    def set_job(self, job: J.Job | None, editable: bool):
        self.job, self.editable = job, editable
        ok = job is not None and job.info is not None and not job.info.error
        for w in (self.view, self.b_add, self.b_del, self.b_clear, self.b_src, self.b_auto, self.every):
            w.setEnabled(ok and editable)
        self._filling = True
        self.view.clear()
        self._filling = False
        if not ok:
            self.hint.setText(t("pick_one_edit"))
            self.note.setText("")
            return
        self.hint.setText(t("ch_hint"))
        self._fill()

    def shown(self) -> list[list]:
        """The chapters as the result will have them: [[start, title], …] (seconds of the result)."""
        j = self.job
        if j.chapters is not None:
            return sorted([float(a), str(b)] for a, b in j.chapters)
        oc = J.out_chapters(j)
        if oc is not None:
            return [[a, title] for a, _b, title in oc]
        if not j.settings.get("meta", True):
            return []
        return [[a, title] for a, _b, title in j.info.chapter_list]

    def _fill(self):
        j = self.job
        self._filling = True
        self.view.clear()
        for n, (a, title) in enumerate(self.shown()):
            it = QTreeWidgetItem(self.view, [str(n + 1), J.fmt_tc(a), title])
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            if j.chapters is None:
                it.setForeground(1, QColor(style.MUTED))
        self._filling = False
        c = E.container(j.settings.get("container", "mkv"))
        notes = []
        if not c.chapters:
            notes.append("⚠ " + t("ch_no_support", c.id.upper()))
        if j.chapters is None:
            notes.append(t("ch_from_src_note") if j.info.chapter_list else t("ch_none_src"))
            if J.segments(j) and j.info.chapter_list:
                notes.append(t("ch_moved"))
        else:
            notes.append(t("ch_edited", len(j.chapters)))
        self.note.setText("\n".join(notes))

    def _own(self) -> list[list]:
        """Switch to our own list (a copy of what is shown) before changing it."""
        if self.job.chapters is None:
            self.job.chapters = self.shown()
        return self.job.chapters

    def _done(self):
        self.job.chapters = sorted(([round(float(a), 3), str(b)] for a, b in self.job.chapters), key=lambda c: c[0]) \
            if self.job.chapters is not None else None
        self._fill()
        self.changed.emit()

    def _edited(self, it, col):
        if self._filling or self.job is None:
            return
        row = self.view.indexOfTopLevelItem(it)
        ch = self._own()
        if row >= len(ch):
            return
        if col == 1:
            s = J.parse_tc(it.text(1))
            if s is not None:
                ch[row][0] = s
        elif col == 2:
            ch[row][1] = it.text(2)
        self._done()

    def add(self):
        if self.job is None:
            return
        ch = self._own()
        pos = self.position()
        if pos is not None:
            at = J.map_time(J.segments(self.job), pos)
            at = at if at is not None else J.out_duration(self.job)
        else:
            at = (max(a for a, _ in ch) + 60.0) if ch else 0.0
        at = min(round(at, 3), max(0.0, J.out_duration(self.job) - 1))
        ch.append([at, t("ch_name_n", len(ch) + 1)])
        self._done()

    def delete(self):
        it = self.view.currentItem()
        if it is None or self.job is None:
            return
        row = self.view.indexOfTopLevelItem(it)
        ch = self._own()
        if 0 <= row < len(ch):
            del ch[row]
        self._done()

    def clear(self):
        if self.job is not None:
            self.job.chapters = []
            self._done()

    def from_source(self):
        if self.job is not None:
            self.job.chapters = None
            self._done()

    def auto(self):
        if self.job is not None:
            self.job.chapters = J.auto_chapters(J.out_duration(self.job), self.every.value(), t("ch_name_n", "{0}"))
            self._done()

