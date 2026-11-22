"""The main window's look: a menu; a top bar with the task buttons, Start, the tabs
(Info · Metadata · Chapters · Trim · Streams · Status) and the container with its preset; the tasks as cards
on the left; the picture with the film strip and the loudness in the middle (the log while Status is open);
the open tab on the right; the player buttons, the frame counter and the stream choice at the bottom.
MainWindow (window.py) mixes this in; the queue, running and settings logic stays there."""
from __future__ import annotations

import os

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog,
                               QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMenu, QMenuBar, QPlainTextEdit, QProgressBar, QRadioButton, QScrollArea,
                               QSlider, QSpinBox, QSplitter, QStackedWidget, QStyle, QStyledItemDelegate, QTabBar,
                               QToolButton, QTreeWidget, QVBoxLayout, QWidget)

from . import config, logs, style, winint
from . import encoders as E
from . import job as J
from . import probe as P
from . import filters as F
from . import delay as DL
from .edit_tabs import ChaptersTab, MetaTab, TrimTab
from .filter_ui import FilterRows, FrameOverlay
from .i18n import t
from .runner import Runner
from .timeline import MediaWorker

SIDE = ("info", "meta", "chapters", "trim", "streams", "status")


def ctl_button(icon_name: str, tip: str, size: int = 18, slot=None, obj: str = "ctl") -> QToolButton:
    b = QToolButton()
    b.setObjectName(obj)
    b.setIcon(style.icon(icon_name, "#ffffff" if obj == "primaryBtn" else style.ICON))
    b.setIconSize(QSize(size, size))
    b.setToolTip(tip)
    b.setCursor(Qt.PointingHandCursor)
    b.setFocusPolicy(Qt.NoFocus)
    if slot:
        b.clicked.connect(slot)
    return b


def text_button(icon_name: str, text: str, slot=None, obj: str = "ctlText") -> QToolButton:
    b = ctl_button(icon_name, "", 15, slot, obj)
    b.setText("  " + text)
    b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    return b


def _hint(text: str = "") -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("hint")
    lab.setWordWrap(True)
    return lab


def _gain_box():
    from PySide6.QtWidgets import QDoubleSpinBox
    b = QDoubleSpinBox()
    b.setRange(-30.0, 30.0)
    b.setSingleStep(0.5)
    b.setDecimals(1)
    b.setSuffix(" " + t("db"))
    b.setKeyboardTracking(False)
    b.setToolTip(t("a_gain_tip"))
    return b


def _rate_box(first: str) -> QComboBox:
    b = QComboBox()
    b.addItem(first, 0)
    for r in F.SAMPLE_RATES[1:]:
        b.addItem(f"{r / 1000:g} kHz", r)
    return b


def _abits_box(first: str) -> QComboBox:
    b = QComboBox()
    b.addItem(first, 0)
    for x in (16, 24, 32):
        b.addItem(f"{x} bit", x)
    b.setToolTip(t("a_abits_tip"))
    return b


def _scroll(inner: QWidget) -> QScrollArea:
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.NoFrame)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    sa.setWidget(inner)
    sa.setStyleSheet("QScrollArea { background: transparent; } QScrollArea > QWidget > QWidget { background: transparent; }")
    return sa


class SmallDialog(QDialog):
    """A settings window opened from a ⚙ button (the codec / format options open this way)."""

    def __init__(self, title: str, body: QWidget, parent=None, width: int = 520):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(width)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        head = QLabel(title)
        head.setAlignment(Qt.AlignCenter)
        head.setStyleSheet("font-size: 14pt; font-weight: 600;")
        lay.addWidget(head)
        lay.addWidget(body, 1)


class TaskCard(QStyledItemDelegate):
    """A task as a card: the thumbnail, the name, duration and size, a progress line, the state and the result
    size (the task list)."""
    H = 96

    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), self.H)

    def paint(self, p: QPainter, option, index):
        win = self.win
        uid = index.siblingAtColumn(0).data(Qt.UserRole)
        j = next((x for x in win.jobs if x.uid == uid), None)
        if j is None:
            return
        r = option.rect.adjusted(0, 0, 0, -1)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        sel = bool(option.state & QStyle.State_Selected)
        if sel:
            p.fillRect(r, QColor(style.CARD_SEL))
        elif option.state & QStyle.State_MouseOver:
            p.fillRect(r, QColor(style.PANEL2))
        tw = int(r.height() * 16 / 9 * 0.62)
        thumb = QRect(r.left(), r.top(), tw, r.height())
        p.fillRect(thumb, QColor("#000000"))
        pm = win.thumbs.get(j.uid)
        if pm is not None and not pm.isNull():
            sc = pm.scaled(thumb.size(), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            sx, sy = (sc.width() - thumb.width()) // 2, (sc.height() - thumb.height()) // 2
            p.drawPixmap(thumb, sc, QRect(sx, sy, thumb.width(), thumb.height()))
        elif j.info is not None and j.info.video is None and not j.info.error:
            p.drawPixmap(thumb.center().x() - 16, thumb.center().y() - 16,
                         style.icon("fa6s.music", style.MUTED).pixmap(32, 32))
        fg = QColor(style.CARD_SEL_FG) if sel else QColor(style.TEXT)
        mut = QColor(style.CARD_SEL_MUT) if sel else QColor(style.MUTED)
        light_sel = sel and QColor(style.CARD_SEL).lightness() > 160      # classic: light blue, dark text
        x0, x1 = thumb.right() + 8, r.right() - 6
        fm = p.fontMetrics()
        f = QFont(option.font)
        f.setBold(j.status in ("run", "pause"))
        p.setFont(f)
        p.setPen(fg)
        p.drawText(QRect(x0, r.top() + 6, x1 - x0, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(j.name, Qt.ElideMiddle, x1 - x0))
        p.setFont(option.font)
        line2 = ""
        if j.info is not None and not j.info.error:
            line2 = f"{J.fmt_tc(J.src_duration(j))} {P.fmt_size(j.info.size)}"
            if j.joins:
                line2 += f"  ⧉{len(j.joins) + 1}"
            if J.segments(j):
                line2 += f"  ✂ {J.fmt_tc(J.out_duration(j), False)}"
            if j.ext:
                line2 += f"  ＋{len(j.ext)}"
        p.setPen(mut)
        p.drawText(QRect(x0, r.top() + 28, x1 - x0, 18), Qt.AlignLeft | Qt.AlignVCenter, line2)
        bar = QRect(x0, r.top() + 52, x1 - x0, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 70) if sel and not light_sel else
                   QColor(255, 255, 255) if light_sel else QColor(style.PANEL2))
        p.drawRoundedRect(bar, 3, 3)
        frac = 1.0 if j.status == "done" else j.progress if j.status in ("run", "pause") else 0.0
        if frac > 0:
            p.setBrush(QColor("#ffffff") if sel and not light_sel else
                       QColor(style.OK if j.status == "done" else style.ACCENT))
            p.drawRoundedRect(QRect(bar.left(), bar.top(), max(4, int(bar.width() * frac)), bar.height()), 3, 3)
        state = {"run": f"{t('s_run').upper()} {j.progress * 100:.1f}%", "pause": t("s_pause").upper(),
                 "error": t("s_error").upper()}.get(j.status, t("s_" + j.status).upper())
        color = fg if sel else QColor({"error": style.ERR, "done": style.OK, "run": style.RUN}.get(j.status, style.MUTED))
        p.setPen(color)
        p.drawText(QRect(x0, r.top() + 66, x1 - x0, 20), Qt.AlignLeft | Qt.AlignVCenter, state + ":")
        right = ""
        if j.status == "done" and j.out_size:
            right = P.fmt_size(j.out_size) + (f" · {len(j.parts)}×" if len(j.parts) > 1 else "")
        elif j.info is not None and j.info.video is not None:
            right = P.resolution_name(j.info.video.width, j.info.video.height)
        elif j.info is not None and j.info.of("audio"):
            right = P.codec_name(j.info.of("audio")[0])
        p.setPen(fg)
        p.drawText(QRect(x0, r.top() + 66, x1 - x0, 20), Qt.AlignRight | Qt.AlignVCenter, right)
        p.setPen(QPen(QColor(style.BORDER), 1))
        p.drawLine(r.left(), r.bottom() + 1, r.right(), r.bottom() + 1)
        p.restore()


class TaskList(QTreeWidget):
    """The task list: tasks are put in order with the mouse (1.4); files dropped on it are added."""
    reordered = Signal()
    files_dropped = Signal(list, int)               # the files, the uid of the card they fell on (0: none)

    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            e.acceptProposedAction()
            it = self.itemAt(e.position().toPoint())
            uid = it.data(0, Qt.UserRole) if it is not None else 0
            if paths:
                self.files_dropped.emit(paths, int(uid or 0))
            return
        super().dropEvent(e)
        self.reordered.emit()


class MainUI:
    """The widgets of MainWindow (a mixin: needs the window's _set, selected_jobs, … )."""

    # ── the whole window ──────────────────────────────────────
    def _compact(self, root: QWidget, chars: int = 14):
        """Combo boxes of a narrow panel shrink with it (their long texts are elided)."""
        for box in root.findChildren(QComboBox):
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(chars)

    def _build_ui(self):
        self.media = MediaWorker()
        self.media.strip.connect(self._on_strip)
        self.media.wave.connect(self._on_wave)
        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.trim_tab = TrimTab()
        self.trim_tab.changed.connect(self._job_edited)
        self.trim_tab.media_wanted.connect(self._want_media)
        self.trim_tab.position_changed.connect(lambda _s: None)
        self.frame_ov = FrameOverlay(self.trim_tab.stage)          # 1.3: the crop / logo frame over the picture
        self.frame_ov.drawn.connect(self._delogo_drawn)
        v.addWidget(self._build_top())
        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(self._build_tasks())
        self.split.addWidget(self._build_center())
        self.split.addWidget(self._build_side())
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setStretchFactor(2, 0)
        self.split.setSizes([330, 620, 500])
        v.addWidget(self.split, 1)
        v.addWidget(self._build_bottom())
        self.setMenuBar(self._build_menu())
        self._build_dialogs()
        self._compact(self.side, 10)
        self._compact(self.bottom_right, 9)
        for box, n in ((self.v_cont, 12), (self.preset_box, 16), (self.v_rc, 5), (self.v_speed_box, 6)):
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(n)
        self._shortcuts()
        self.side_bar.setCurrentIndex(SIDE.index("streams"))

    # ── menu ──────────────────────────────────────────────────
    def _act(self, menu: QMenu, icon: str, text: str, slot, keys: str = "") -> QAction:
        a = menu.addAction(style.icon(icon) if icon else QIcon(), text)
        a.triggered.connect(lambda _c=False: slot())
        if keys:
            a.setShortcut(QKeySequence(keys))
            a.setShortcutContext(Qt.WindowShortcut)
        return a

    def _build_menu(self) -> QMenuBar:
        mb = QMenuBar(self)
        A = self._act                                                          # noqa: N806
        from . import APP_TITLE, updater
        m = mb.addMenu(APP_TITLE)
        A(m, "fa6s.gear", t("settings") + "…", self.settings_dialog, "Ctrl+,")
        A(m, "fa6s.eye", t("wf_title") + "…", self.watch_dialog)
        A(m, "fa6s.microchip", t("encoders_here") + "…", self.encoders_dialog)
        if updater.can_self_update():
            A(m, "fa6s.cloud-arrow-down", t("upd_check_now"), lambda: self.check_updates(True))
        A(m, "fa6s.circle-info", t("about"), self.about)
        m.addSeparator()
        A(m, "", t("quit"), self.close, "Ctrl+Q")
        m = mb.addMenu(t("m_file"))
        A(m, "fa6s.film", t("add_files") + "…", self.add_files_dialog, "Ctrl+O")
        A(m, "fa6s.folder-open", t("add_folder") + "…", self.add_folder_dialog, "Ctrl+Shift+O")
        A(m, "fa6s.compact-disc", t("disc_open") + "…", self.open_disc_dialog, "Ctrl+D")
        A(m, "fa6s.floppy-disk", t("save_as") + "…", self.save_as, "Ctrl+S")
        m.addSeparator()
        A(m, "fa6s.file-export", t("q_save") + "…", self.save_task_list)
        A(m, "fa6s.file-import", t("q_load") + "…", self.load_task_list)
        m.addSeparator()
        A(m, "fa6s.play", t("open_result"), lambda: self._open_result(next(iter(self.selected_jobs()), None)))
        A(m, "fa6s.folder", t("show_source"), lambda: [winint.show_in_explorer(j.src) for j in self.selected_jobs()[:1]])
        m = mb.addMenu(t("m_tasks"))
        A(m, "fa6s.play", t("start"), self.start_queue, "Ctrl+Return")
        A(m, "fa6s.pause", t("pause"), self.toggle_queue_pause)
        A(m, "fa6s.stop", t("stop"), self.stop_queue)
        m.addSeparator()
        A(m, "fa6s.rotate-right", t("retry"), lambda: self.retry(self.selected_jobs()))
        A(m, "fa6s.forward", t("skip"), lambda: self._skip(self.selected_jobs()))
        A(m, "fa6s.arrow-up", t("move_up"), lambda: self.move_selected(-1))
        A(m, "fa6s.arrow-down", t("move_down"), lambda: self.move_selected(1))
        m.addSeparator()
        A(m, "fa6s.object-group", t("join_selected"), self.join_selected)
        A(m, "fa6s.clone", t("apply_to") + "…", lambda: self.apply_dialog())
        A(m, "fa6s.file-export", t("demux") + "…", lambda: self.demux_dialog())
        A(m, "fa6s.scale-balanced", t("est_menu") + "…", lambda: self.estimate_dialog())
        A(m, "fa6s.link", t("join_add") + "…", self.join_add_dialog)
        A(m, "fa6s.plus", t("ext_add") + "…", self.add_external_dialog)
        m.addSeparator()
        A(m, "fa6s.minus", t("remove"), self.remove_selected)
        A(m, "fa6s.broom", t("clear_done"), self.clear_done)
        A(m, "fa6s.terminal", t("copy_cmd"), lambda: self._copy_cmd(next(iter(self.selected_jobs()), None)))
        m = mb.addMenu(t("m_log"))
        A(m, "fa6s.clock-rotate-left", t("log_history") + "…", self.history_dialog, "Ctrl+L")
        A(m, "fa6s.file-lines", t("open_log"), lambda: self.open_log(next(iter(self.selected_jobs()), None)))
        A(m, "fa6s.folder-open", t("logs_folder"), lambda: winint.show_in_explorer(logs.logs_dir()))
        A(m, "fa6s.copy", t("log_copy"), lambda: QApplication.clipboard().setText(self.log.toPlainText()))
        A(m, "fa6s.eraser", t("log_clear"), lambda: self.log.clear())
        m = mb.addMenu(t("m_tools"))
        A(m, "fa6s.floppy-disk", t("preset_save"), self.save_preset)
        self.preset_del_menu = m.addMenu(style.icon("fa6s.trash-can"), t("preset_delete"))
        self.preset_del_menu.aboutToShow.connect(self._fill_preset_del)
        A(m, "", t("preset_default_now"), self._make_default)
        A(m, "fa6s.sliders", t("fp_title") + "…", self.format_preset_dialog)
        A(m, "fa6s.screwdriver-wrench", t("adv_button") + "…", self.advanced_dialog)
        m.addSeparator()
        A(m, "fa6s.microchip", t("encoders_here") + "…", self.encoders_dialog)
        A(m, "fa6s.gauge-high", t("bench") + "…", self.bench_dialog)
        A(m, "fa6s.scale-balanced", t("cmp_title") + "…", self.compare_dialog)
        return mb                        # 2.2.2: no «Help» menu — «About» is in the ArtCore Convert menu

    def _fill_preset_del(self):
        m = self.preset_del_menu
        m.clear()
        for name in sorted(self.user_presets):
            m.addAction(name, lambda n=name: self.delete_preset(n))
        if not self.user_presets:
            m.addAction(t("preset_none")).setEnabled(False)

    # ── top bar ───────────────────────────────────────────────
    def _build_top(self) -> QWidget:
        w = QWidget()
        w.setObjectName("transport")
        w.setAttribute(Qt.WA_StyledBackground)
        w.setStyleSheet(f"QWidget#transport {{ border-top: none; border-bottom: 1px solid {style.BORDER}; }}")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(2)
        self.b_add = ctl_button("fa6s.film", t("add_files") + " (Ctrl+O)", 17, self.add_files_dialog)
        self.b_addfolder = ctl_button("fa6s.folder-open", t("add_folder") + " (Ctrl+Shift+O)", 17, self.add_folder_dialog)
        self.b_remove = ctl_button("fa6s.minus", t("remove") + " (Del)", 17, self.remove_selected)
        self.b_clean = ctl_button("fa6s.trash-can", t("clear_done"), 16, self.clear_done)
        self.b_saveas = ctl_button("fa6s.floppy-disk", t("save_as") + " (Ctrl+S)", 17, self.save_as)
        self.b_up = ctl_button("fa6s.arrow-up", t("move_up") + " (Ctrl+↑)", 15, lambda: self.move_selected(-1))
        self.b_down = ctl_button("fa6s.arrow-down", t("move_down") + " (Ctrl+↓)", 15, lambda: self.move_selected(1))
        for b in (self.b_add, self.b_addfolder, self.b_remove, self.b_clean, self.b_saveas):
            lay.addWidget(b)
        lay.addSpacing(10)
        self.b_start = text_button("fa6s.play", t("start"), self.start_queue, "primaryBtn")
        self.b_pause = ctl_button("fa6s.pause", t("pause"), 16, self.toggle_queue_pause)
        self.b_stop = ctl_button("fa6s.stop", t("stop"), 16, self.stop_queue)
        # 2.1: the delayed start — an arrow beside «Start» with its menu
        self.b_start_at = ctl_button("fa6s.caret-down", t("dl_menu_tip"), 12)
        self.b_start_at.setPopupMode(QToolButton.InstantPopup)
        dm = QMenu(self.b_start_at)
        self.a_start_at = dm.addAction(style.icon("fa6s.clock"), t("dl_at"))
        self.a_start_at.triggered.connect(lambda _c=False: self.start_at_dialog())
        dm.addSeparator()
        self.a_start_in = []
        for mins in DL.IN_MINUTES:
            a = dm.addAction(t("dl_in_min", mins) if mins < 60 else t("dl_in_h", mins // 60))
            a.triggered.connect(lambda _c=False, m=mins: self.start_in(m))
            self.a_start_in.append(a)
        dm.addSeparator()
        self.a_start_cancel = dm.addAction(style.icon("fa6s.xmark"), t("dl_cancel"))
        self.a_start_cancel.triggered.connect(lambda _c=False: self.cancel_delayed(t("dl_cancelled")))
        self.b_start_at.setMenu(dm)
        lay.addWidget(self.b_start)
        lay.addWidget(self.b_start_at)
        lay.addSpacing(4)
        lay.addWidget(self.b_pause)
        lay.addWidget(self.b_stop)
        lay.addStretch(1)
        self.side_bar = QTabBar()
        self.side_bar.setObjectName("segTabs")
        self.side_bar.setDrawBase(False)
        self.side_bar.setExpanding(False)
        for key in SIDE:
            self.side_bar.addTab(t("side_" + key))
        self.side_bar.currentChanged.connect(self._side_changed)
        lay.addWidget(self.side_bar)
        lay.addStretch(1)
        self.top_right = QStackedWidget()
        c = QWidget()
        cl = QHBoxLayout(c)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(4)
        lab = QLabel(t("container") + ":")
        cl.addWidget(lab)
        self.v_cont = QComboBox()
        self.v_cont.setMaxVisibleItems(20)
        for ct in E.CONTAINERS:
            if ct.audio_only and self.v_cont.findData("mka") < 0:
                self.v_cont.insertSeparator(self.v_cont.count())
            self.v_cont.addItem((t("audio_only") + ": " if ct.audio_only else "") + ct.label, ct.id)
        self.v_cont.currentIndexChanged.connect(lambda: self._set(None, "container", self.v_cont.currentData()))
        cl.addWidget(self.v_cont)
        cl.addWidget(ctl_button("fa6s.gear", t("format_options"), 15, lambda: self.format_dialog()))
        self.preset_box = QComboBox()
        self.preset_box.activated.connect(self._preset_picked)
        cl.addSpacing(6)
        cl.addWidget(self.preset_box)
        self.b_preset_menu = ctl_button("fa6s.gear", t("presets_menu"), 15, self._preset_menu)
        cl.addWidget(self.b_preset_menu)
        self.top_right.addWidget(c)
        pr = QWidget()
        pl = QHBoxLayout(pr)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addStretch(1)
        pl.addWidget(QLabel(t("priority") + ":"))
        self.prio = QComboBox()
        for key in config.PRIORITIES:
            self.prio.addItem(t("prio_" + key), key)
        self.prio.setCurrentIndex(config.PRIORITIES.index(self.cfg.get("priority", "below")))
        self.prio.currentIndexChanged.connect(self._prio_changed)
        pl.addWidget(self.prio)
        self.b_cpu = QToolButton()                       # 1.4: «100% ▾»
        self.b_cpu.setObjectName("ctlText")
        self.b_cpu.setPopupMode(QToolButton.InstantPopup)
        self.b_cpu.setToolTip(t("cpu_limit_tip"))
        cm = QMenu(self.b_cpu)
        for pct in config.CPU_LIMITS:
            a = cm.addAction(f"{pct}%")
            a.triggered.connect(lambda _c=False, x=pct: self.set_cpu_limit(x))
        self.b_cpu.setMenu(cm)
        pl.addWidget(self.b_cpu)
        pl.addSpacing(8)
        pl.addWidget(QLabel(t("parallel") + ":"))
        self.par_box = QComboBox()
        self.par_box.setToolTip(t("parallel_tip"))
        for n in range(1, 5):
            self.par_box.addItem(str(n), n)
        self.par_box.setCurrentIndex(int(self.cfg.get("parallel", 1)) - 1)
        self.par_box.currentIndexChanged.connect(lambda: self.set_parallel(self.par_box.currentData()))
        pl.addWidget(self.par_box)
        self.top_right.addWidget(pr)
        lay.addWidget(self.top_right)
        self._fill_presets()
        return w

    # ── tasks (cards) ─────────────────────────────────────────
    def _build_tasks(self) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(240)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.view = TaskList()
        self.view.setObjectName("cards")
        self.view.setColumnCount(5)
        self.view.setHeaderLabels(["№", t("col_file"), t("col_source"), t("col_profile"), t("col_state")])
        self.view.setRootIsDecorated(False)
        self.view.setIndentation(0)
        self.view.setUniformRowHeights(True)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setMouseTracking(True)
        self._card = TaskCard(self)
        self._plain = QStyledItemDelegate(self.view)
        self.view.setStyleSheet(f"QTreeWidget#cards {{ background: {style.PANEL}; border: none; outline: none; }}"
                                "QTreeWidget#cards::item { border: none; padding: 0; }")
        self.view.reordered.connect(self._on_reordered)
        self.view.files_dropped.connect(lambda paths, uid: self.add_incoming(paths, uid))
        self._apply_task_view()
        self.view.itemSelectionChanged.connect(self._select_changed)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._queue_menu)
        self.view.itemDoubleClicked.connect(lambda it, _c: self._open_result(self._job_of(it)))
        lay.addWidget(self.view, 1)
        self.empty = QLabel(t("empty_tasks"), self.view.viewport())
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {style.MUTED}; font-size: 10pt; padding: 16px;")
        self.empty.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.view.viewport().installEventFilter(self)
        self.q_count = QLabel()
        self.q_count.setObjectName("hint")
        self.q_count.setContentsMargins(10, 4, 10, 4)
        lay.addWidget(self.q_count)
        return w

    def _apply_task_view(self):
        """1.4: the task list as cards (thumbnail, progress) or as rows (a table)."""
        rows = self.cfg.get("task_view", "cards") == "rows"
        self.view.setItemDelegate(self._plain if rows else self._card)
        self.view.setHeaderHidden(not rows)
        for c in range(1, 5):
            self.view.setColumnHidden(c, not rows)
        hh = self.view.header()
        if rows:
            hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(1, QHeaderView.Interactive)
            hh.resizeSection(1, 200)
        self.view.setStyleSheet(
            f"QTreeWidget#cards {{ background: {style.PANEL}; border: none; outline: none; }}" +
            ("" if rows else "QTreeWidget#cards::item { border: none; padding: 0; }"))
        for b, key in ((getattr(self, "b_view_cards", None), "cards"), (getattr(self, "b_view_rows", None), "rows")):
            if b is not None:
                b.setChecked(self.cfg.get("task_view", "cards") == key)
        self.view.doItemsLayout()

    def set_task_view(self, mode: str):
        self.cfg["task_view"] = mode
        config.save(self.cfg)
        self._apply_task_view()

    # ── centre: the picture + strip, or the log ───────────────
    def _build_center(self) -> QWidget:
        self.center = QStackedWidget()
        pv = QWidget()
        pl = QVBoxLayout(pv)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(0)
        # 2.1: over the picture of Streams / Trim — «Show the result»: mpv shows the frame already cropped
        self.view_bar = QWidget()
        vb = QHBoxLayout(self.view_bar)
        vb.setContentsMargins(6, 2, 6, 2)
        vb.setSpacing(6)
        self.b_as_result = text_button("fa6s.crop-simple", t("cv_show"), None, "ctlText")
        self.b_as_result.setCheckable(True)
        self.b_as_result.setToolTip(t("cv_tip"))
        self.b_as_result.toggled.connect(self.set_crop_view)
        vb.addWidget(self.b_as_result)
        self.cv_note = QLabel()
        self.cv_note.setObjectName("hint")
        vb.addWidget(self.cv_note, 1)
        pl.addWidget(self.view_bar)
        pl.addWidget(self.trim_tab.stage, 1)
        pl.addWidget(self.trim_tab.bar)
        self.center.addWidget(pv)
        lg = QWidget()
        ll = QVBoxLayout(lg)
        ll.setContentsMargins(8, 8, 8, 8)
        ll.setSpacing(4)
        top = QHBoxLayout()
        lab = QLabel(t("log"))
        lab.setStyleSheet("font-weight: 600;")
        top.addWidget(lab)
        top.addStretch(1)
        top.addWidget(ctl_button("fa6s.clock-rotate-left", t("log_history"), 13, self.history_dialog))
        top.addWidget(ctl_button("fa6s.copy", t("log_copy"), 13,
                                 lambda: QApplication.clipboard().setText(self.log.toPlainText())))
        top.addWidget(ctl_button("fa6s.eraser", t("log_clear"), 13, lambda: self.log.clear()))
        top.addWidget(ctl_button("fa6s.folder-open", t("logs_folder"), 13,
                                 lambda: winint.show_in_explorer(logs.logs_dir())))
        ll.addLayout(top)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(Runner.LOG_KEEP)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log.setPlaceholderText(t("log_empty"))
        self.log.setStyleSheet(f"background: {style.PANEL}; border: 1px solid {style.BORDER}; border-radius: 8px;"
                               "font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 8.5pt;")
        ll.addWidget(self.log, 1)
        self.center.addWidget(lg)
        return self.center

    # ── right: the tabs ───────────────────────────────────────
    def _build_side(self) -> QWidget:
        self.side = QStackedWidget()
        self.side.setMinimumWidth(420)
        self.side.addWidget(_scroll(self._tab_info()))
        self.meta_tab = MetaTab()
        self.meta_tab.changed.connect(self._job_edited)
        self.side.addWidget(_scroll(self.meta_tab))
        self.chap_tab = ChaptersTab(lambda: self.trim_tab.position())
        self.chap_tab.changed.connect(self._job_edited)
        self.side.addWidget(self.chap_tab)
        self.side.addWidget(_scroll(self.trim_tab))
        self.side.addWidget(_scroll(self._tab_streams()))
        self.status_tab = self._tab_status()
        self.side.addWidget(_scroll(self.status_tab))
        return self.side

    def _side_changed(self, i: int):
        self.side.setCurrentIndex(i)
        status = SIDE[i] == "status"
        self.center.setCurrentIndex(1 if status else 0)
        self.top_right.setCurrentIndex(1 if status else 0)
        self.bottom_right.setCurrentIndex(1 if status else 0)
        self.view_bar.setVisible(SIDE[i] in ("streams", "trim"))
        self._update_buttons()

    def show_side(self, key: str):
        self.side_bar.setCurrentIndex(SIDE.index(key))

    def _tab_info(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        self.scope = _hint()
        lay.addWidget(self.scope)
        self.i_name = QLabel()
        self.i_name.setStyleSheet("font-size: 11.5pt; font-weight: 700;")
        self.i_name.setWordWrap(True)
        self.i_name.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.i_name)
        self.i_text = QLabel()
        self.i_text.setWordWrap(True)
        self.i_text.setTextFormat(Qt.RichText)
        self.i_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.i_text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        lay.addWidget(self.i_text)
        lay.addStretch(1)
        return w

    def _tab_status(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QWidget()
        hl = QVBoxLayout(head)
        hl.setContentsMargins(14, 10, 14, 10)
        self.st_name = QLabel(t("st_idle"))
        self.st_name.setStyleSheet("font-weight: 700;")
        self.st_name.setWordWrap(True)
        hl.addWidget(self.st_name)
        self.st_bar = QProgressBar()
        self.st_bar.setRange(0, 1000)
        self.st_bar.setTextVisible(True)
        self.st_bar.setFormat("")
        hl.addWidget(self.st_bar)
        lay.addWidget(head)
        self.st: dict[str, QLabel] = {}
        for title, keys in (("st_grp_encode", ("elapsed", "eta", "speed", "fps", "bitrate", "size", "encoder")),
                            ("st_grp_system", ("cpu", "ram", "ff_mem", "disk", "gpu", "gpu_enc", "gpu_temp"))):
            lay.addWidget(self._section(t(title)))
            box = QWidget()
            g = QGridLayout(box)
            g.setContentsMargins(14, 6, 14, 8)
            g.setVerticalSpacing(5)
            for row, k in enumerate(keys):
                g.addWidget(QLabel(t("st_" + k).rstrip(":") + ":"), row, 0)
                val = QLabel("—")
                val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                val.setTextInteractionFlags(Qt.TextSelectableByMouse)
                g.addWidget(val, row, 1)
                self.st[k] = val
            g.setColumnStretch(1, 1)
            lay.addWidget(box)
        # 1.4: the frame being encoded
        self.fr_on = QCheckBox()
        self.fr_on.setChecked(bool(self.cfg.get("show_frame", True)))
        self.fr_on.setToolTip(t("st_frame_tip"))
        self.fr_on.toggled.connect(self._frame_toggled)
        lay.addWidget(self._section(t("st_frame"), None, self.fr_on))
        self.fr_img = QLabel(t("st_frame_none"))
        self.fr_img.setObjectName("hint")
        self.fr_img.setAlignment(Qt.AlignCenter)
        self.fr_img.setMinimumHeight(150)
        self.fr_img.setContentsMargins(14, 6, 14, 8)
        lay.addWidget(self.fr_img)
        # 1.4: quality metrics of the finished file
        lay.addWidget(self._section(t("st_metrics")))
        mb = QWidget()
        mg = QGridLayout(mb)
        mg.setContentsMargins(14, 6, 14, 8)
        mg.setVerticalSpacing(5)
        self.m_check: dict[str, QCheckBox] = {}
        self.m_val: dict[str, QLabel] = {}
        for row, key in enumerate(J.METRICS):
            cb = QCheckBox(J.METRIC_NAMES.get(key, key.upper()))
            cb.setChecked(key in self.cfg.get("metrics", []))
            cb.setToolTip(t("m_tip_" + key))
            cb.toggled.connect(lambda _on: self._metrics_changed())
            mg.addWidget(cb, row, 0)
            val = QLabel("—")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            mg.addWidget(val, row, 1)
            self.m_check[key] = cb
            self.m_val[key] = val
        self.m_hint = _hint(t("m_hint"))
        mg.addWidget(self.m_hint, len(J.METRICS), 0, 1, 2)
        mg.setColumnStretch(1, 1)
        lay.addWidget(mb)
        lay.addStretch(1)
        return w

    def _section(self, text: str, right: QWidget | None = None, check: QCheckBox | None = None,
                 fold: QWidget | None = None) -> QWidget:
        w = QFrame()
        w.setObjectName("section")
        w.setStyleSheet(style.section_css())
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4 if fold is not None else 10, 5, 10, 5)
        if fold is not None:
            lay.addWidget(fold)
        if check is not None:
            lay.addWidget(check)
        lab = QLabel(text)
        lab.setStyleSheet("font-weight: 600; background: transparent;")
        lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lab.setWordWrap(True)
        lab.setMinimumWidth(60)
        lay.addWidget(lab, 1)
        if right is not None:
            lay.addWidget(right)
        return w

    # ── Streams: where to save; the video; the sound; every track ─────
    def _tab_streams(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        top = QWidget()
        tl = QGridLayout(top)
        tl.setContentsMargins(12, 10, 12, 8)
        tl.setHorizontalSpacing(6)
        tl.addWidget(QLabel(t("save_files") + ":"), 0, 0, Qt.AlignRight)
        self.o_mode = QComboBox()
        self.o_mode.addItem(t("out_source"), "source")
        self.o_mode.addItem(t("out_folder"), "folder")
        self.o_mode.setCurrentIndex(1 if self.cfg.get("out_mode") == "folder" else 0)
        self.o_mode.currentIndexChanged.connect(self._out_mode_changed)
        tl.addWidget(self.o_mode, 0, 1)
        self.o_pick = ctl_button("fa6s.folder-open", t("out_pick"), 15, self._pick_out_dir)
        tl.addWidget(self.o_pick, 0, 2)
        self.o_dir = QLabel()
        self.o_dir.setObjectName("hint")
        tl.addWidget(self.o_dir, 1, 0, 1, 3)
        self.o_path = QLineEdit()
        self.o_path.setPlaceholderText(t("out_path_hint"))
        self.o_path.editingFinished.connect(self._out_path_typed)
        tl.addWidget(self.o_path, 2, 0, 1, 2)
        tl.addWidget(ctl_button("fa6s.floppy-disk", t("save_as") + " (Ctrl+S)", 15, self.save_as), 2, 2)
        tl.setColumnStretch(1, 1)
        lay.addWidget(top)
        # video
        self.v_on = QCheckBox()
        self.v_on.setToolTip(t("v_on_tip"))
        self.v_on.toggled.connect(self._video_toggled)
        self.v_lang = QComboBox()
        self.v_head = self._section(t("video"), None, self.v_on)
        self.v_head_label = self.v_head.findChild(QLabel)
        lay.addWidget(self.v_head)
        self.v_body = QWidget()
        g = QGridLayout(self.v_body)
        g.setContentsMargins(12, 6, 12, 8)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(5)
        self._vrows = g
        self.v_res = QComboBox()
        self.v_res.setMaxVisibleItems(16)
        self.v_res.currentIndexChanged.connect(self._res_picked)
        self.v_color = QLabel("—")
        self.v_color.setObjectName("hint")
        # 1.3: Interlace, Framerate and the bit depth are filter rows below
        rows = ((t("resolution"), self.v_res, lambda: self.res_dialog.exec()), (t("color"), self.v_color, None))
        for r, (name, wdg, gear) in enumerate(rows):
            g.addWidget(QLabel(name), r, 0)
            g.addWidget(wdg, r, 1)
            if gear:
                g.addWidget(ctl_button("fa6s.sliders", name, 14, gear), r, 2)
        r = len(rows)
        g.addWidget(QLabel(t("codec") + ":"), r, 0)
        crow = QHBoxLayout()
        crow.setSpacing(4)
        self.v_enc = QComboBox()
        self.v_enc.currentIndexChanged.connect(self._enc_changed)
        self.v_rc = QComboBox()
        for key in ("quality", "bitrate", "size"):
            self.v_rc.addItem(t("rc_short_" + key), key)
        self.v_rc.currentIndexChanged.connect(lambda: self._set("video", "rc", self.v_rc.currentData()))
        self.v_val = QSpinBox()
        self.v_val.setKeyboardTracking(False)
        self.v_val.setMinimumWidth(70)
        self.v_val.valueChanged.connect(self._val_changed)
        self.v_speed_box = QComboBox()
        for x in range(1, 8):
            self.v_speed_box.addItem(t("speed_n", x), x)
        self.v_speed_box.currentIndexChanged.connect(lambda: self._set("video", "speed", self.v_speed_box.currentData()))
        g.addWidget(self.v_enc, r, 1)
        gears = QHBoxLayout()
        gears.setSpacing(0)
        gears.addWidget(ctl_button("fa6s.gear", t("video_settings"), 15, lambda: self.codec_dialog.exec()))
        self.b_adv = ctl_button("fa6s.screwdriver-wrench", t("adv_button"), 14, lambda: self.advanced_dialog())
        gears.addWidget(self.b_adv)                                  # 1.5: the encoder's advanced settings
        g.addLayout(gears, r, 2)
        crow.addWidget(self.v_rc, 1)
        crow.addWidget(self.v_val, 1)
        crow.addWidget(self.v_speed_box, 1)
        g.addLayout(crow, r + 1, 1)
        self.v_enc_hint = _hint()
        self.v_enc_hint.setMinimumHeight(34)
        g.addWidget(self.v_enc_hint, r + 2, 0, 1, 3)
        self.v_adv = _hint()                                         # 1.5: «⚙ tune film · ref 4»
        self.v_adv.setVisible(False)
        g.addWidget(self.v_adv, r + 3, 0, 1, 3)
        self.v_fmt = _hint()                                         # 1.5: the preset of the format in use
        self.v_fmt.setVisible(False)
        g.addWidget(self.v_fmt, r + 4, 0, 1, 3)
        g.setColumnStretch(1, 1)
        lay.addWidget(self.v_body)
        # 1.3: the video filters (☑ · name · value · ⚙)
        self.f_head = self._section(t("f_section"))
        lay.addWidget(self.f_head)
        self.f_rows = FilterRows()
        self.f_rows.toggled.connect(self._filter_toggled)
        self.f_rows.gear.connect(self._filter_gear)
        lay.addWidget(self.f_rows)
        self.f_dialogs: dict = {}
        # sound: the settings of every track (each track below can have its own)
        lay.addWidget(self._section(t("audio_common")))
        ab = QWidget()
        ag = QGridLayout(ab)
        ag.setContentsMargins(12, 6, 12, 8)
        ag.setHorizontalSpacing(8)
        ag.setVerticalSpacing(5)
        self.a_codec = QComboBox()
        self.a_codec.addItem(t("a_auto"), "auto")
        self.a_codec.addItem(t("a_copy"), "copy")
        for a in E.AUDIO:
            self.a_codec.addItem(t("a_name_" + a.id), a.id)
        self.a_codec.currentIndexChanged.connect(lambda: self._set("audio", "codec", self.a_codec.currentData()))
        self.a_kbps = QComboBox()
        self.a_kbps.addItem(t("auto"), 0)
        for k in (96, 128, 160, 192, 224, 256, 320, 384, 448, 512, 640):
            self.a_kbps.addItem(f"{k} kbit/s", k)
        self.a_kbps.currentIndexChanged.connect(lambda: self._set("audio", "kbps", self.a_kbps.currentData()))
        self.a_ch = QComboBox()
        for key in F.LAYOUTS:
            self.a_ch.addItem(t("ch_" + key.replace(".", "")), key)
        self.a_ch.currentIndexChanged.connect(lambda: self._set("audio", "channels", self.a_ch.currentData()))
        self.a_dm = QComboBox()                                  # 2.0: the 5.1 / 7.1 → 2.0 matrix
        self.a_dm.setObjectName("audioDownmix")
        for key in F.DOWNMIX:
            self.a_dm.addItem(t("dm_" + key), key)
        self.a_dm.setToolTip(t("dm_tip"))
        self.a_dm.currentIndexChanged.connect(lambda: self._set("audio", "downmix", self.a_dm.currentData()))
        self.a_dm_gear = ctl_button("fa6s.sliders", t("dm_title"), 13, lambda _c=False: self.downmix_dialog(None))
        chrow = QHBoxLayout()
        chrow.setSpacing(4)
        chrow.addWidget(self.a_ch, 1)
        chrow.addWidget(self.a_dm, 1)
        chrow.addWidget(self.a_dm_gear)
        self.a_norm = QCheckBox(t("normalize"))
        self.a_norm.toggled.connect(lambda on: self._set("audio", "normalize", bool(on)))
        self.a_gain = _gain_box()
        self.a_gain.valueChanged.connect(lambda x: self._set("audio", "gain", round(float(x), 1)))
        self.a_rate = _rate_box(t("as_source"))
        self.a_rate.currentIndexChanged.connect(lambda: self._set("audio", "rate", self.a_rate.currentData()))
        self.a_abits = _abits_box(t("as_source"))
        self.a_abits.currentIndexChanged.connect(lambda: self._set("audio", "abits", self.a_abits.currentData()))
        vol = QHBoxLayout()
        vol.addWidget(self.a_gain)
        vol.addWidget(self.a_norm, 1)
        for r, (name, wdg) in enumerate(((t("codec"), self.a_codec), (t("bitrate"), self.a_kbps),
                                         (t("channels"), chrow), (t("volume"), vol), (t("a_rate"), self.a_rate),
                                         (t("a_abits"), self.a_abits))):
            if isinstance(wdg, QHBoxLayout):
                ag.addWidget(QLabel(name), r, 0)
                ag.addLayout(wdg, r, 1)
                continue
            ag.addWidget(QLabel(name), r, 0)
            ag.addWidget(wdg, r, 1)
        ag.setColumnStretch(1, 1)
        lay.addWidget(ab)
        # the tracks of the selected file
        self.tracks_box = QWidget()
        self.tracks_lay = QVBoxLayout(self.tracks_box)
        self.tracks_lay.setContentsMargins(0, 0, 0, 0)
        self.tracks_lay.setSpacing(0)
        lay.addWidget(self.tracks_box)
        self.b_ext = ctl_button("fa6s.plus", t("ext_add"), 14, lambda: self.add_external_dialog())
        self.ext_head = self._section(t("ext_tracks"), self.b_ext)
        lay.addWidget(self.ext_head)
        self.ext_box = QWidget()
        self.ext_lay = QVBoxLayout(self.ext_box)
        self.ext_lay.setContentsMargins(0, 0, 0, 0)
        self.ext_lay.setSpacing(0)
        lay.addWidget(self.ext_box)
        self.tr_hint = _hint()
        self.tr_hint.setContentsMargins(12, 8, 12, 8)
        lay.addWidget(self.tr_hint)
        lay.addStretch(1)
        return w

    def _val_changed(self, x: int):
        rc = self.v_rc.currentData()
        self._set("video", {"quality": "quality", "bitrate": "bitrate", "size": "size_mb"}.get(rc, "quality"), int(x))

    def _video_toggled(self, on: bool):
        if self._loading:
            return
        if not on:
            self._set("video", "mode", "none")
        else:
            self._set("video", "mode", "copy" if self.v_enc.currentData() == "copy" else "encode")

    # the tracks of one file: a block per audio / subtitle track (keep, language, its own codec …)
    # 1.6: the cards of the tracks fold like an accordion — an unticked track closes, a ticked one opens;
    # the arrow on the left opens / closes a card by hand
    def _fold_key(self, j, key) -> tuple:
        return (j.uid, key)

    def _fold_open(self, j, key, default: bool) -> bool:
        return self.__dict__.setdefault("_fold_hand", {}).get(self._fold_key(j, key), default)

    def _fold_button(self, j, key, is_open: bool) -> QToolButton:
        b = QToolButton()
        b.setObjectName("ctl")
        b.setProperty("fold", True)
        b.setAutoRaise(True)
        b.setIcon(style.icon("fa6s.chevron-down" if is_open else "fa6s.chevron-right", style.TEXT))
        b.setIconSize(QSize(11, 11))
        b.setFixedSize(20, 20)
        b.setToolTip(t("tr_fold_tip"))
        b.clicked.connect(lambda _c=False, k=key, o=is_open: self._fold_toggle(j, k, not o))
        return b

    def _fold_toggle(self, j, key, on: bool):
        self.__dict__.setdefault("_fold_hand", {})[self._fold_key(j, key)] = on
        self._fill_tracks()

    def _fold_body(self, j, key, body: QWidget, is_open: bool):
        """Show or hide the card's body; if it changed since the last time the panel was built, slide."""
        shown = self.__dict__.setdefault("_fold_shown", {})
        k = self._fold_key(j, key)
        was = shown.get(k)
        shown[k] = is_open
        body.setProperty("foldOpen", is_open)
        if was is None or was == is_open or not self.isVisible():
            body.setVisible(is_open)
            return
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        full = max(1, body.sizeHint().height())
        body.setVisible(True)
        body.setMaximumHeight(0 if is_open else full)
        an = QPropertyAnimation(body, b"maximumHeight", body)
        an.setDuration(160)
        an.setEasingCurve(QEasingCurve.OutCubic)
        an.setStartValue(0 if is_open else full)
        an.setEndValue(full if is_open else 0)

        def done(b=body, o=is_open):
            try:
                b.setMaximumHeight(16777215)
                b.setVisible(o)
            except RuntimeError:                        # the panel was built again meanwhile
                pass
        an.finished.connect(done)
        an.start()

    def _fill_tracks(self):
        while self.tracks_lay.count():
            it = self.tracks_lay.takeAt(0)
            if it.widget() is not None:
                it.widget().hide()                       # gone at once, not when Qt gets round to deleting it
                it.widget().deleteLater()
        while self.ext_lay.count():
            it = self.ext_lay.takeAt(0)
            if it.widget() is not None:
                it.widget().hide()
                it.widget().deleteLater()
        jobs = self.selected_jobs()
        one = len(jobs) == 1 and jobs[0].info is not None and not jobs[0].info.error
        self.ext_head.setVisible(one)
        self.ext_box.setVisible(one)
        if not one:
            self.tr_hint.setText(t("tr_pick_one"))
            return
        j = jobs[0]
        ed = j.status in EDITABLE_ and not self._busy(j)
        self.b_ext.setEnabled(ed)
        self._fill_ext(j, ed)
        self.tr_hint.setText(t("tr_hint"))
        pl = J.plan(j)
        out_audio = {s.index: c for s, c in pl["audio"]}
        out_subs = {s.index: how for s, how in pl["subs"]}
        from . import hardsub as HS
        burn = HS.choice(j)                                       # 2.0: the track burnt into the picture
        c = E.container(j.settings.get("container", "mkv"))
        for s in j.info.of("audio") + j.info.of("subtitle"):
            audio = s.kind == "audio"
            keep = s.index in (out_audio if audio else out_subs)
            cb = QCheckBox()
            cb.setChecked(keep)
            cb.setEnabled(ed)
            cb.setToolTip(t("tr_keep_tip"))
            cb.toggled.connect(lambda on, idx=s.index: self._track_keep(idx, on))
            is_open = self._fold_open(j, s.index, keep or (burn is not None and burn.key() == ("src", s.index)))
            lang = QComboBox()
            lang.setObjectName("trackLang")
            from .edit_tabs import LANGS
            for code in LANGS:
                lang.addItem(f"{code} — {t('lang_' + code)}", code)
            cur = j.smeta(s).get("lang", s.lang or "und")
            if lang.findData(cur) < 0:
                lang.addItem(cur, cur)
            lang.setCurrentIndex(lang.findData(cur))
            lang.setEnabled(ed)
            lang.currentIndexChanged.connect(lambda _i, b=lang, st=s: self._track_lang(st, b.currentData()))
            line = (P.audio_line(s) if audio else P.sub_line(s)) + (f" — {s.title}" if s.title else "")
            head = self._section(f"#{s.index} " + line, lang, cb, self._fold_button(j, s.index, is_open))
            head.setProperty("stream", s.index)
            self.tracks_lay.addWidget(head)
            body = QWidget()
            g = QGridLayout(body)
            g.setContentsMargins(12, 4, 12, 8)
            g.setHorizontalSpacing(8)
            g.setVerticalSpacing(4)
            tr = j.track(s)
            if audio:
                ch = QComboBox()
                ch.addItem(t("tr_default") + f" ({t('ch_' + j.settings['audio'].get('channels', 'keep').replace('.', ''))})", "")
                for key in F.LAYOUTS:
                    ch.addItem(t("ch_" + key.replace(".", "")), key)
                ch.setCurrentIndex(max(0, ch.findData(tr.get("channels", ""))))
                ch.currentIndexChanged.connect(lambda _i, b=ch, idx=s.index: self._track_opt(idx, "channels", b.currentData()))
                acfg = j.acfg(s)                                # 2.0: the 2.0 matrix, when it mixes down to stereo
                dm = QComboBox()
                dm.setObjectName("trackDownmix")
                dm.addItem(t("tr_default") + f" ({t('dm_' + F.downmix_mode(j.settings.get('audio', {})))})", "")
                for key in F.DOWNMIX:
                    dm.addItem(t("dm_" + key), key)
                dm.setCurrentIndex(max(0, dm.findData(tr.get("downmix", ""))))
                dm.setToolTip(t("dm_tip"))
                dm.currentIndexChanged.connect(lambda _i, b=dm, idx=s.index: self._track_opt(idx, "downmix", b.currentData()))
                dmg = ctl_button("fa6s.sliders", t("dm_title"), 13, lambda _c=False, idx=s.index: self.downmix_dialog(idx))
                dmg.setObjectName("ctl")
                stereo = F.layout_channels(acfg.get("channels", "keep"), s.channels, 8) == 2 and (s.channels or 2) > 2
                dm.setVisible(stereo)
                dmg.setVisible(stereo and F.downmix_mode(acfg) == "custom")
                vol = QCheckBox(t("normalize"))
                vol.setTristate(False)
                vol.setChecked(bool(j.acfg(s).get("normalize")))
                vol.toggled.connect(lambda on, idx=s.index: self._track_opt(idx, "normalize", bool(on)))
                codec = QComboBox()
                codec.setObjectName("trackCodec")
                codec.addItem(t("tr_default") + (f" ({self._a_label(out_audio.get(s.index))})" if keep else ""), "")
                codec.addItem(t("a_copy"), "copy")
                for a in E.AUDIO:
                    codec.addItem(a.label.split(" (")[0], a.id)
                    if not c.takes_audio(E.A_CODEC.get(a.id, a.id)):
                        codec.model().item(codec.count() - 1).setEnabled(False)
                codec.setCurrentIndex(max(0, codec.findData(tr.get("codec", ""))))
                codec.currentIndexChanged.connect(lambda _i, b=codec, idx=s.index: self._track_codec(idx, b.currentData()))
                kb = QComboBox()
                kb.addItem(t("tr_default"), 0)
                for k in (96, 128, 160, 192, 224, 256, 320, 384, 448, 640):
                    kb.addItem(f"{k} kbit/s", k)
                kb.setCurrentIndex(max(0, kb.findData(int(tr.get("kbps", 0) or 0))))
                kb.currentIndexChanged.connect(lambda _i, b=kb, idx=s.index: self._track_opt(idx, "kbps", b.currentData()))
                # 1.3: delay, gain, sample rate, bit depth of this track
                dl = QSpinBox()
                dl.setObjectName("trackDelay")
                dl.setRange(-60000, 60000)
                dl.setSingleStep(10)
                dl.setSuffix(" " + t("ms"))
                dl.setKeyboardTracking(False)
                dl.setToolTip(t("a_delay_tip"))
                dl.setValue(int(tr.get("delay", 0) or 0))
                dl.valueChanged.connect(lambda x, idx=s.index: self._track_opt(idx, "delay", int(x)))
                gn = _gain_box()
                gn.setObjectName("trackGain")
                gn.setValue(float(j.acfg(s).get("gain", 0) or 0))
                gn.valueChanged.connect(lambda x, idx=s.index: self._track_opt(idx, "gain", round(float(x), 1)))
                rt = _rate_box(t("tr_default"))
                rt.setObjectName("trackRate")
                rt.setCurrentIndex(max(0, rt.findData(int(tr.get("rate", 0) or 0))))
                rt.currentIndexChanged.connect(lambda _i, b=rt, idx=s.index: self._track_opt(idx, "rate", b.currentData()))
                ab = _abits_box(t("tr_default"))
                ab.setObjectName("trackBits")
                ab.setCurrentIndex(max(0, ab.findData(int(tr.get("abits", 0) or 0))))
                ab.currentIndexChanged.connect(lambda _i, b=ab, idx=s.index: self._track_opt(idx, "abits", b.currentData()))
                g.addWidget(QLabel(t("channels")), 0, 0)
                crow = QHBoxLayout()
                crow.setSpacing(4)
                crow.addWidget(ch, 1)
                crow.addWidget(dm, 1)
                crow.addWidget(dmg)
                g.addLayout(crow, 0, 1)
                g.addWidget(QLabel(t("volume")), 1, 0)
                vrow = QHBoxLayout()
                vrow.addWidget(gn)
                vrow.addWidget(vol, 1)
                g.addLayout(vrow, 1, 1)
                g.addWidget(QLabel(t("a_delay")), 2, 0)
                g.addWidget(dl, 2, 1)
                g.addWidget(QLabel(t("a_rate")), 3, 0)
                rrow = QHBoxLayout()
                rrow.addWidget(rt, 1)
                rrow.addWidget(ab, 1)
                g.addLayout(rrow, 3, 1)
                g.addWidget(QLabel(t("codec") + ":"), 4, 0)
                row = QHBoxLayout()
                row.addWidget(codec, 2)
                row.addWidget(kb, 1)
                g.addLayout(row, 4, 1)
                for wdg in (ch, vol, codec, kb, dl, gn, rt, ab, dm, dmg):
                    wdg.setEnabled(ed and keep)
            else:
                how = out_subs.get(s.index)
                mine = burn is not None and burn.key() == ("src", s.index)
                burnt = mine and not HS.why_not(j, burn)
                text = t("hs_burnt") if burnt else t("sub_drop") if not keep else (
                    t("a_copy") if how == "copy" else how.replace("mov_text", "MP4 Text").replace("webvtt", "WebVTT"))
                g.addWidget(QLabel(t("codec") + ":"), 0, 0)
                g.addWidget(QLabel(text), 0, 1)
                self._burn_row(g, 1, j, s, ("src", s.index), burn if mine else None, ed)
                if burnt:
                    cb.setEnabled(False)
            g.setColumnStretch(1, 1)
            body.setProperty("stream", s.index)
            self.tracks_lay.addWidget(body)
            self._fold_body(j, s.index, body, is_open)
        self._compact(self.tracks_box, 8)

    def _burn_row(self, g: QGridLayout, row: int, j, s, key: tuple, b, ed: bool):
        """2.0: «Burn in: no / into the picture / the forced ones only» of a subtitle track, ⚙ = how text looks."""
        from . import hardsub as HS
        box = QComboBox()
        box.setObjectName("trackBurn")
        box.addItem(t("hs_no"), "")
        box.addItem(t("hs_all"), "all")
        if HS.can_forced(s):
            box.addItem(t("hs_forced"), "forced")
        box.setCurrentIndex(max(0, box.findData(b.mode if b is not None else "")))
        why = HS.why_not(j, HS.Burn(s, None if key[0] == "src" else key[1], "all"))
        box.setEnabled(ed and (not why or b is not None))
        box.setToolTip(t("hs_tip") if not why else t(why))
        box.currentIndexChanged.connect(lambda _i, bx=box, k=key: self._track_burn(k, bx.currentData()))
        gear = ctl_button("fa6s.sliders", t("hs_look"), 13, lambda _c=False: self.hardsub_dialog())
        row_l = QHBoxLayout()
        row_l.setSpacing(4)
        row_l.addWidget(box, 1)
        row_l.addWidget(gear)
        g.addWidget(QLabel(t("hs_burn") + ":"), row, 0)
        g.addLayout(row_l, row, 1)
        note = ""
        if b is not None and b.auto:
            note = t("hs_auto_note")
        elif b is not None and why:
            note = t(why)
        elif b is not None and b.text and not b.ass:
            h = HS.hset(j.settings)
            note = t("hs_text_note", h["font"], h["size"])
        if note:
            g.addWidget(_hint(note), row + 1, 1)

    def _fill_ext(self, j, ed: bool):
        """1.4: the external tracks of the task: language, delay, codec; ✕ takes one away."""
        from .edit_tabs import LANGS
        pl = J.plan(j)
        used = {J.ext_no(s): c for s, c in pl["audio"] + pl["subs"] if J.ext_no(s) is not None}
        c = E.container(j.settings.get("container", "mkv"))
        for n, e in enumerate(j.ext):
            audio = e.get("kind") == "audio"
            info = J.file_info(j, e["path"])
            bad = info is not None and (info.error or not info.of(e.get("kind", "audio")))
            rm = ctl_button("fa6s.xmark", t("ext_remove"), 13, lambda _c=False, k=n: self._ext_remove(k))
            rm.setEnabled(ed)
            lang = QComboBox()
            lang.setObjectName("extLang")
            for code in LANGS:
                lang.addItem(f"{code} — {t('lang_' + code)}", code)
            cur = e.get("lang") or "und"
            if lang.findData(cur) < 0:
                lang.addItem(cur, cur)
            lang.setCurrentIndex(lang.findData(cur))
            lang.setEnabled(ed)
            lang.currentIndexChanged.connect(lambda _i, b=lang, k=n: self._ext_opt(k, "lang", b.currentData()))
            head = QWidget()
            hl = QHBoxLayout(head)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.addWidget(lang)
            hl.addWidget(rm)
            mark = "🔊" if audio else "💬"
            text = f"{mark} {os.path.basename(e['path'])}"
            if info is None:
                text += " …"
            elif bad:
                text += " — " + t("ext_bad")
            elif e.get("kind") == "subtitle" and e.get("burn") and n not in used:
                text += " — " + t("hs_burnt")
            elif n not in used:
                text += " — " + t("sub_drop")
            key = "ext:" + e["path"]
            is_open = self._fold_open(j, key, True)
            self.ext_lay.addWidget(self._section(text, head, None, self._fold_button(j, key, is_open)))
            body = QWidget()
            g = QGridLayout(body)
            g.setContentsMargins(12, 4, 12, 8)
            g.setHorizontalSpacing(8)
            g.setVerticalSpacing(4)
            dl = QSpinBox()
            dl.setObjectName("extDelay")
            dl.setRange(-600000, 600000)
            dl.setSingleStep(10)
            dl.setSuffix(" " + t("ms"))
            dl.setKeyboardTracking(False)
            dl.setToolTip(t("ext_delay_tip"))
            dl.setValue(int(e.get("delay", 0) or 0))
            dl.setEnabled(ed)
            dl.valueChanged.connect(lambda x, k=n: self._ext_opt(k, "delay", int(x)))
            dl.valueChanged.connect(lambda x, p=e["path"]: self._listen_delay(p, int(x)))
            g.addWidget(QLabel(t("a_delay")), 0, 0)
            if audio:                                     # 1.7: hear it with the picture at this delay
                drow = QHBoxLayout()
                drow.addWidget(dl, 1)
                hear = QToolButton()
                hear.setObjectName("ctlText")
                hear.setProperty("listen", e["path"])
                hear.setCheckable(True)
                hear.setIcon(style.icon("fa6s.headphones"))
                hear.setText(" " + t("ext_listen"))
                hear.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
                hear.setToolTip(t("ext_listen_tip"))
                on = self._listening() == e["path"]
                hear.setChecked(on)
                hear.setEnabled(not bad and info is not None)
                hear.toggled.connect(lambda c, p=e["path"], b=dl: self.listen_ext(p, b.value(), c))
                drow.addWidget(hear)
                g.addLayout(drow, 0, 1)
            else:
                g.addWidget(dl, 0, 1)
            if audio:
                codec = QComboBox()
                codec.setObjectName("extCodec")
                codec.addItem(t("tr_default") + (f" ({self._a_label(used.get(n))})" if n in used else ""), "")
                codec.addItem(t("a_copy"), "copy")
                for a in E.AUDIO:
                    codec.addItem(a.label.split(" (")[0], a.id)
                    if not c.takes_audio(E.A_CODEC.get(a.id, a.id)):
                        codec.model().item(codec.count() - 1).setEnabled(False)
                codec.setCurrentIndex(max(0, codec.findData(e.get("codec", ""))))
                codec.setEnabled(ed)
                codec.currentIndexChanged.connect(lambda _i, b=codec, k=n: self._ext_opt(k, "codec", b.currentData()))
                g.addWidget(QLabel(t("codec") + ":"), 1, 0)
                g.addWidget(codec, 1, 1)
            dft = QCheckBox(t("ext_default"))
            dft.setChecked(bool(e.get("default")))
            dft.setEnabled(ed)
            dft.toggled.connect(lambda on, k=n: self._ext_opt(k, "default", bool(on)))
            g.addWidget(dft, 2, 1)
            if not audio and info is not None and not bad:          # 2.0: an outside subtitle burnt in
                from . import hardsub as HS
                b = HS.choice(j)
                st0 = next((x for k2, x in J.ext_streams(j) if k2 == n), None)
                if st0 is not None:
                    self._burn_row(g, 3, j, st0, ("ext", n), b if b is not None and b.key() == ("ext", n) else None, ed)
            g.setColumnStretch(1, 1)
            self.ext_lay.addWidget(body)
            self._fold_body(j, key, body, is_open)
        if not j.ext:
            self.ext_lay.addWidget(_hint(t("ext_hint")))
        self._compact(self.ext_box, 8)

    # 1.7: before / after with a curtain
    def toggle_compare(self, on: bool | None = None, at: float | None = None):
        from . import beforeafter as BA
        tt = self.trim_tab
        view = getattr(self, "cmp_view", None)
        shown = view is not None and tt.stage_lay.currentWidget() is view
        if on is None:
            on = not shown
        if not on:
            if view is not None and shown:
                tt.stage_lay.setCurrentWidget(tt.preview if tt.preview is not None else tt.placeholder)
            self.b_compare.setChecked(False)
            return
        jobs = self.selected_jobs()
        j = jobs[0] if len(jobs) == 1 else None
        if j is None or j.info is None or j.info.error or j.info.video is None:
            self._toast(t("tr_pick_one"))
            self.b_compare.setChecked(False)
            return
        if not self.av_ready:
            self.b_compare.setChecked(False)
            return
        if self.center.currentIndex() != 0:
            self.show_side("streams")
        if view is None:
            view = BA.CompareView(tt.stage)
            tt.stage_lay.addWidget(view)
            self.cmp_view = view
            self.cmp_grab = BA.Grabber(self)
            self.cmp_grab.done.connect(self._cmp_done)
            self.cmp_grab.status.connect(view.set_busy)
            view.closed.connect(lambda: self.toggle_compare(False))
            view.moved.connect(lambda s: self.toggle_compare(True, s))
        if self.cmp_grab.busy:
            return
        if at is None:
            pos = tt.position()
            at = pos if pos is not None else 0.0
        dur = J.src_duration(j)
        at = max(0.0, min(at, max(0.0, dur - 0.1)))
        self._cmp = (j.uid, at)
        done = j.status == "done" and len(j.parts or [j.out]) == 1 and j.out and os.path.isfile(j.out)
        view.label_b = t("cmp_result") if done else t("cmp_piece")
        view.set_busy(t("cmp_grabbing"))
        tt.stage_lay.setCurrentWidget(view)
        view.setFocus()
        self.b_compare.setChecked(True)
        if tt.preview is not None:
            tt.preview.pause()
        self.cmp_grab.grab(j, self.av, at, True, bool(self.cfg.get("hw_decode", True)))

    def _cmp_done(self, a: str, b: str, err: str):
        view = getattr(self, "cmp_view", None)
        if view is None:
            return
        uid, at = getattr(self, "_cmp", (0, 0.0))
        view.set_frames(a, b, at, err)

    # 1.7: an outside sound track heard with the picture
    def _listening(self) -> str:
        pv = getattr(self.trim_tab, "preview", None)
        return getattr(pv, "listening", "") if pv is not None else ""

    def listen_ext(self, path: str, delay_ms: int, on: bool = True) -> bool:
        tt = self.trim_tab
        if not on:
            if tt.preview is not None:
                tt.preview.stop_listen()
            return False
        if self.center.currentIndex() != 0:
            self.show_side("streams")
        if tt.preview is None:
            tt._load_preview()
        ok = tt.preview is not None and tt.preview.listen(path, delay_ms / 1000.0)
        if not ok:
            self._toast(t("ext_listen_no"))
            QTimer.singleShot(0, self._fill_tracks)
        return bool(ok)

    def _listen_delay(self, path: str, delay_ms: int):
        if self._listening() == path:
            self.trim_tab.preview.set_delay(delay_ms / 1000.0)

    def _track_keep(self, idx: int, on: bool):
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1:
            return
        j = jobs[0]
        j.tracks.setdefault(str(idx), {})["keep"] = bool(on)
        self.__dict__.setdefault("_fold_hand", {}).pop(self._fold_key(j, idx), None)   # the tick decides again
        self._track_changed(j)

    def _track_codec(self, idx: int, codec: str):
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1:
            return
        j = jobs[0]
        j.tracks.setdefault(str(idx), {})["codec"] = codec or ""
        if codec:
            j.tracks[str(idx)]["keep"] = True
        self._track_changed(j)

    def _track_opt(self, idx: int, key: str, value):
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1:
            return
        j = jobs[0]
        tr = j.tracks.setdefault(str(idx), {})
        if (value in ("", 0, None) or (key == "gain" and abs(float(value)) < 0.05)) and key != "normalize":
            tr.pop(key, None)
        else:
            tr[key] = value
        self._track_changed(j)

    def _track_lang(self, s, code: str):
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1 or not code:
            return
        j = jobs[0]
        st = j.meta.setdefault("streams", {})
        m = st.setdefault(str(s.index), {})
        if code == (s.lang or "und"):
            m.pop("lang", None)
        else:
            m["lang"] = code
        if not m:
            st.pop(str(s.index), None)
        if not st:
            j.meta.pop("streams", None)
        self._refresh(j)
        self._queue_changed()
        self.meta_tab.set_job(j, True)

    def _track_changed(self, j):
        if j.status in ("done", "error", "stop", "skip") and j.info is not None and not j.info.error:
            j.status, j.error, j.progress = "wait", "", 0.0
        self._refresh(j)
        self._queue_changed()
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._fill_tracks)
        self._show_info(j)
        self.meta_tab.set_job(j, True)
        self._gain_preview()

    @staticmethod
    def _a_label(codec: str | None) -> str:
        if not codec:
            return ""
        return t("a_copy") if codec == "copy" else E.A_BY_ID[codec].label.split(" (")[0]

    # ── bottom bar ────────────────────────────────────────────
    def _build_bottom(self) -> QWidget:
        w = QWidget()
        w.setObjectName("transport")
        w.setAttribute(Qt.WA_StyledBackground)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(10, 4, 10, 6)
        lay.setSpacing(6)
        self.b_tasks = ctl_button("fa6s.table-list", t("show_tasks"), 15, self._toggle_tasks)
        lay.addWidget(self.b_tasks)
        self.b_view_cards = ctl_button("fa6s.grip", t("view_cards"), 14, lambda: self.set_task_view("cards"))
        self.b_view_rows = ctl_button("fa6s.list", t("view_rows"), 14, lambda: self.set_task_view("rows"))
        for b in (self.b_view_cards, self.b_view_rows):
            b.setCheckable(True)
            lay.addWidget(b)
        self.b_view_cards.setChecked(self.cfg.get("task_view", "cards") == "cards")
        self.b_view_rows.setChecked(self.cfg.get("task_view", "cards") == "rows")
        lay.addStretch(1)
        tr = self.trim_tab
        tr.b_mute.setVisible(True)
        lay.addWidget(tr.transport)
        self.b_compare = ctl_button("fa6s.table-columns", t("cmp_tip") + " (Ctrl+B)", 15, lambda: self.toggle_compare())
        self.b_compare.setCheckable(True)
        lay.addWidget(self.b_compare)
        lay.addStretch(1)
        self.bottom_right = QStackedWidget()
        sw = QWidget()
        sl = QHBoxLayout(sw)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        sl.addWidget(QLabel(t("select_streams") + ":"))
        self.a_tracks = QComboBox()
        self.a_tracks.addItem(t("tracks_all"), "all")
        self.a_tracks.addItem(t("tracks_first"), "first")
        self.a_tracks.setToolTip(t("audio_tracks"))
        self.a_tracks.currentIndexChanged.connect(lambda: self._set("audio", "tracks", self.a_tracks.currentData()))
        sl.addWidget(self.a_tracks)
        self.s_mode = QComboBox()
        for key in ("all", "first", "none"):
            self.s_mode.addItem(t("subs_" + key), key)
        self.s_mode.setToolTip(t("subtitles"))
        self.s_mode.currentIndexChanged.connect(lambda: self._set(None, "subs", self.s_mode.currentData()))
        sl.addWidget(self.s_mode)
        self.s_lang = QComboBox()
        self.s_lang.addItem(t("any_lang"), "")
        from .edit_tabs import LANGS
        for code in LANGS[:-1]:
            self.s_lang.addItem(f"{code} — {t('lang_' + code)}", code)
        self.s_lang.currentIndexChanged.connect(lambda: self._set(None, "lang", self.s_lang.currentData()))
        sl.addWidget(self.s_lang)
        self.bottom_right.addWidget(sw)
        dw = QWidget()
        dl = QHBoxLayout(dw)
        dl.setContentsMargins(0, 0, 0, 0)
        self.total = QProgressBar()
        self.total.setRange(0, 1000)
        self.total.setFixedWidth(240)
        self.total.setFormat("")
        dl.addWidget(self.total)
        dl.addSpacing(10)
        dl.addWidget(QLabel(t("on_done") + ":"))
        self.done_box = QComboBox()
        for key in config.ON_DONE:
            self.done_box.addItem(t("done_" + key), key)
        self.done_box.setCurrentIndex(config.ON_DONE.index(self.cfg.get("on_done", "nothing")))
        self.done_box.currentIndexChanged.connect(lambda: self.cfg.__setitem__("on_done", self.done_box.currentData()))
        dl.addWidget(self.done_box)
        self.bottom_right.addWidget(dw)
        lay.addWidget(self.bottom_right)
        self._out_mode_changed()
        return w

    def _toggle_tasks(self):
        w = self.split.widget(0)
        w.setVisible(not w.isVisible())

    # ── small settings windows ────────────────────────────────
    def _build_dialogs(self):
        # resolution
        body = QWidget()
        f = QFormLayout(body)
        row = QHBoxLayout()
        self.v_w = QSpinBox()
        self.v_hh = QSpinBox()
        for sb in (self.v_w, self.v_hh):
            sb.setRange(0, 8192)
            sb.setSingleStep(2)
            sb.setSpecialValueText(t("auto"))
            sb.setKeyboardTracking(False)
            sb.valueChanged.connect(self._res_typed)
        row.addWidget(self.v_w)
        row.addWidget(QLabel("×"))
        row.addWidget(self.v_hh)
        row.addStretch(1)
        f.addRow(t("res_custom"), row)
        self.v_fit = QComboBox()
        for key in ("fit", "pad", "stretch", "fill"):
            self.v_fit.addItem(t("fit_" + key), key)
        self.v_fit.currentIndexChanged.connect(lambda: self._set("video", "fit", self.v_fit.currentData()))
        f.addRow(t("res_fit"), self.v_fit)
        self.v_up = QCheckBox(t("res_upscale"))
        self.v_up.toggled.connect(lambda on: self._set("video", "upscale", bool(on)))
        f.addRow("", self.v_up)
        self.v_res_hint = _hint()
        f.addRow("", self.v_res_hint)
        self.res_dialog = SmallDialog(t("resolution"), body, self)
        # the video codec: rate control, speed, hints
        body = QWidget()
        lay = QVBoxLayout(body)
        box = QGroupBox(t("grp_rate"))
        g = QGridLayout(box)
        g.setVerticalSpacing(8)
        self.rc_group = QButtonGroup(self)
        self.rc_q = QRadioButton(t("rc_quality"))
        self.rc_b = QRadioButton(t("rc_bitrate"))
        self.rc_s = QRadioButton(t("rc_size"))
        for i, (rb, key) in enumerate(((self.rc_q, "quality"), (self.rc_b, "bitrate"), (self.rc_s, "size"))):
            self.rc_group.addButton(rb, i)
            rb.toggled.connect(lambda on, k=key: on and self._set("video", "rc", k))
            g.addWidget(rb, i, 0)
        self.v_q = QSpinBox()
        self.v_q.setRange(0, 63)
        self.v_q.valueChanged.connect(lambda x: self._set("video", "quality", int(x)))
        self.v_q_hint = _hint()
        self.v_br = QSpinBox()
        self.v_br.setRange(200, 200000)
        self.v_br.setSingleStep(500)
        self.v_br.setSuffix(" kbit/s")
        self.v_br.valueChanged.connect(lambda x: self._set("video", "bitrate", int(x)))
        self.v_size = QSpinBox()
        self.v_size.setRange(10, 500000)
        self.v_size.setSingleStep(100)
        self.v_size.setSuffix(" MB")
        self.v_size.valueChanged.connect(lambda x: self._set("video", "size_mb", int(x)))
        self.v_size_hint = _hint()
        g.addWidget(self.v_q, 0, 1)
        g.addWidget(self.v_q_hint, 0, 2)
        g.addWidget(self.v_br, 1, 1)
        g.addWidget(self.v_size, 2, 1)
        g.addWidget(self.v_size_hint, 2, 2)
        g.setColumnStretch(2, 1)
        sp = QHBoxLayout()
        a = _hint(t("faster"))
        b = _hint(t("better"))
        self.v_speed = QSlider(Qt.Horizontal)
        self.v_speed.setRange(1, 7)
        self.v_speed.setPageStep(1)
        self.v_speed.setTickPosition(QSlider.TicksBelow)
        self.v_speed.valueChanged.connect(lambda x: (self._set("video", "speed", int(x)), self._speed_label()))
        self.v_speed_lab = QLabel()
        self.v_speed_lab.setMinimumWidth(90)
        sp.addWidget(a)
        sp.addWidget(self.v_speed, 1)
        sp.addWidget(b)
        sp.addSpacing(10)
        sp.addWidget(self.v_speed_lab)
        g.addWidget(QLabel(t("speed")), 3, 0)
        g.addLayout(sp, 3, 1, 1, 2)
        self.v_2pass = QCheckBox(t("two_pass"))                         # 1.4
        self.v_2pass.toggled.connect(lambda on: self._set("video", "passes", 2 if on else 1))
        self.v_2pass_hint = _hint()
        g.addWidget(self.v_2pass, 4, 0, 1, 2)
        g.addWidget(self.v_2pass_hint, 5, 0, 1, 3)
        lay.addWidget(box)
        self.codec_hint = _hint()
        lay.addWidget(self.codec_hint)
        self.codec_dialog = SmallDialog(t("video_settings"), body, self, 600)

    def format_dialog(self):
        """What the chosen container holds, the subtitles and the metadata (Format options)."""
        st = self._shown_settings()
        c = E.container(st.get("container", "mkv"))
        body = QWidget()
        lay = QVBoxLayout(body)
        fam = ", ".join(E.FAMILY_LABEL.get(x, x.upper()) for x in sorted(c.video)) if c.video else \
            (t("fmt_any") if c.video is None else "—")
        aud = ", ".join(sorted(c.audio)) if c.audio else (t("fmt_any") if c.audio is None else "—")
        lay.addWidget(QLabel(f"<b>{t('video')}</b>: {fam}<br><b>{t('grp_audio')}</b>: {aud}<br>"
                             f"<b>{t('subtitles')}</b>: {t('fmt_subs_' + c.subs)}<br>"
                             f"<b>{t('tab_chapters')}</b>: {t('yes') if c.chapters else t('no')}"))
        fmt = J.FM.of(st)                                              # 1.5: the preset of the format
        frow = QHBoxLayout()
        frow.addWidget(QLabel(f"<b>{t('fp_title')}</b>: " + (fmt.get("name") or t("fp_none_short"))))
        frow.addStretch(1)
        fb = text_button("fa6s.sliders", t("fp_choose") + "…", lambda: (d.accept(), self.format_preset_dialog()))
        frow.addWidget(fb)
        lay.addLayout(frow)
        from .preset_ui import mux_group                              # 1.5: the container options
        mg = mux_group(st, lambda k, val: self._set("mux", k, val))
        if mg is not None:
            lay.addWidget(mg)
        meta = QCheckBox(t("keep_meta"))
        meta.setChecked(bool(st.get("meta", True)))
        meta.toggled.connect(lambda on: self._set(None, "meta", bool(on)))
        lay.addWidget(meta)
        lay.addWidget(_hint(t("fmt_hint")))
        sp = dict(st.get("split") or {})                                # 1.4: the result in parts
        box = QGroupBox(t("split"))
        sg = QGridLayout(box)
        mode = QComboBox()
        mode.setObjectName("splitMode")
        for key in ("off", "size", "time"):
            mode.addItem(t("split_" + key), key)
        mode.setCurrentIndex(max(0, mode.findData(sp.get("mode", "off"))))
        size = QComboBox()
        size.setObjectName("splitSize")
        size.setEditable(True)
        for mb_ in (100, 350, 700, 1400, 2000, 4095, 4480, 8500, 23000):
            size.addItem(f"{mb_} MB", mb_)
        size.setEditText(f"{sp.get('size_mb', 700)} MB")
        mins = QSpinBox()
        mins.setObjectName("splitMinutes")
        mins.setRange(1, 600)
        mins.setSuffix(" " + t("minutes"))
        mins.setValue(int(sp.get("minutes", 20) or 20))

        def upd():
            m = mode.currentData()
            size.setEnabled(m == "size")
            mins.setEnabled(m == "time")
            try:
                mb_v = float(size.currentText().split()[0].replace(",", "."))
            except (ValueError, IndexError):
                mb_v = float(sp.get("size_mb", 700) or 700)
            self._set(None, "split", {"mode": m, "size_mb": max(1, int(mb_v)), "minutes": int(mins.value())})
        mode.currentIndexChanged.connect(lambda _i: upd())
        size.currentTextChanged.connect(lambda _x: upd() if mode.currentData() == "size" else None)
        mins.valueChanged.connect(lambda _x: upd() if mode.currentData() == "time" else None)
        size.setEnabled(mode.currentData() == "size")
        mins.setEnabled(mode.currentData() == "time")
        sg.addWidget(mode, 0, 0, 1, 2)
        sg.addWidget(QLabel(t("split_size")), 1, 0)
        sg.addWidget(size, 1, 1)
        sg.addWidget(QLabel(t("split_time")), 2, 0)
        sg.addWidget(mins, 2, 1)
        sg.addWidget(_hint(t("split_hint")), 3, 0, 1, 2)
        box.setEnabled(c.id != "gif")
        lay.addWidget(box)
        d = SmallDialog(c.label, body, self)
        self._format_dlg = d
        d.exec()

    def history_dialog(self, path: str = ""):
        """1.7: the log window (Log → History, Ctrl+L): the history and the chosen log, coloured, with find."""
        from .logview import LogWindow
        d = getattr(self, "_logwin", None)
        try:
            if d is not None:
                d.isVisible()
        except RuntimeError:
            d = None
        if d is None:
            d = LogWindow(self)
            self._logwin = d
        else:
            d.reload()
        if path:
            d.select(path)
        d.show()
        d.raise_()
        d.activateWindow()
        return d

    # ── strip & loudness under the picture ────────────────────
    def _want_media(self, j):
        self.trim_tab.bar.clear_media()
        if j is None or j.info is None or j.info.error:
            return
        src = J.media_src(j)
        self._media_src = src
        self.media.request(src, J.src_duration(j), j.info.video is not None, bool(j.info.of("audio")))

    def _on_strip(self, path: str, pngs: list):
        if path == getattr(self, "_media_src", ""):
            self.trim_tab.bar.set_strip(pngs)

    def _on_wave(self, path: str, peaks: list):
        if path == getattr(self, "_media_src", ""):
            self.trim_tab.bar.set_wave(peaks, self.media.top.get(path, 1.0))
            self._gain_preview()

    # ── keys ──────────────────────────────────────────────────
    def _shortcuts(self):
        from PySide6.QtGui import QShortcut

        def sc(keys, fn):
            for k in (keys if isinstance(keys, (list, tuple)) else [keys]):
                s = QShortcut(QKeySequence(k), self)
                s.setContext(Qt.WindowShortcut)
                s.activated.connect(fn)
        sc("Delete", self._delete_key)
        sc("Ctrl+Up", lambda: self.move_selected(-1))
        sc("Ctrl+Down", lambda: self.move_selected(1))
        sc("Ctrl+A", self.view.selectAll)
        sc("Space", self.toggle_pause)
        sc("Escape", self.view.clearSelection)
        sc("Ctrl+I", lambda: self.show_side("info"))
        sc("Ctrl+B", lambda: self.toggle_compare())
        sc("Ctrl+T", lambda: self.show_side("trim"))

    def _delete_key(self):
        """Del: a trim piece while the Trim tab has focus, otherwise the selected tasks."""
        fw = QApplication.focusWidget()
        if fw is not None and self.trim_tab.isAncestorOf(fw) and self.trim_tab.b_del.isEnabled():
            self.trim_tab.delete_piece()
            return
        if isinstance(fw, (QLineEdit, QSpinBox)):
            return
        self.remove_selected()

    def eventFilter(self, obj, ev):
        from PySide6.QtCore import QEvent
        if ev.type() == QEvent.Resize and obj is self.view.viewport():
            self.empty.resize(obj.size())
        return False

    def keyPressEvent(self, e):
        """Keys nobody took (← → I O …) go to the player / trim."""
        if self.center.currentIndex() == 0:
            self.trim_tab.keyPressEvent(e)
            if e.isAccepted():
                return
        super().keyPressEvent(e)


EDITABLE_ = ("wait", "error", "stop", "skip", "done")
