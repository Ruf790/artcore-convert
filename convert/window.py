"""The main window: the queue on the left; File / Video / Audio / Tracks / Status tabs on the right; output
folder, priority and «when done» at the bottom. Settings tabs edit the selected tasks (or, with nothing
selected, the settings of new ones)."""
from __future__ import annotations

import os
import shutil
import threading
import time

from PySide6.QtCore import QByteArray, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog,
                               QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
                               QLabel, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QRadioButton,
                               QScrollArea, QSlider, QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from . import APP_TITLE, VERSION, VIDEO_EXT, config, ff, style, winint
from . import encoders as E
from . import job as J
from . import probe as P
from .i18n import t
from .runner import Runner

COL_N, COL_FILE, COL_SRC, COL_PROFILE, COL_STATE = range(5)
def state_icon(status: str):
    """The status icon of a queue row, in the colours of the theme."""
    name, color = {"wait": ("fa6s.clock", "MUTED"), "probe": ("fa6s.magnifying-glass", "MUTED"),
                   "run": ("fa6s.gear", "RUN"), "pause": ("fa6s.pause", "WARN"), "done": ("fa6s.circle-check", "OK"),
                   "error": ("fa6s.circle-xmark", "ERR"), "stop": ("fa6s.ban", "MUTED"),
                   "skip": ("fa6s.forward", "MUTED")}.get(status, ("fa6s.clock", "MUTED"))
    return style.icon(name, getattr(style, color))
EDITABLE = ("wait", "error", "stop", "skip", "done")


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


class Bridge(QObject):
    """Results of worker threads, delivered in the GUI thread."""
    probed = Signal(int, object)          # uid, MediaInfo
    thumb = Signal(int, bytes)            # uid, PNG
    encoders = Signal(object)             # Available
    update = Signal(object, str)
    gpu = Signal(dict)


class Worker:
    """One background thread for ffprobe and the preview frames (jobs in order, newest thumbnail wins)."""

    def __init__(self, bridge: Bridge):
        self.bridge = bridge
        self.cond = threading.Condition()
        self.jobs: list[tuple[str, int, str, float]] = []
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def probe(self, uid: int, path: str):
        with self.cond:
            self.jobs.append(("probe", uid, path, 0.0))
            self.cond.notify()

    def thumb(self, uid: int, path: str, at: float):
        with self.cond:
            self.jobs = [j for j in self.jobs if j[0] != "thumb"]
            self.jobs.append(("thumb", uid, path, at))
            self.cond.notify()

    def stop(self):
        with self.cond:
            self.alive = False
            self.cond.notify()

    def _run(self):
        while True:
            with self.cond:
                while self.alive and not self.jobs:
                    self.cond.wait()
                if not self.alive:
                    return
                kind, uid, path, at = self.jobs.pop(0)
            if kind == "probe":
                self.bridge.probed.emit(uid, P.probe(path))
            else:
                data = b""
                exe = ff.ffmpeg()
                if exe:
                    try:
                        r = ff.run([exe, "-hide_banner", "-v", "error", "-ss", f"{at:.2f}", "-i", path, "-map", "0:v:0",
                                    "-frames:v", "1", "-vf", "scale=480:-2", "-f", "image2pipe", "-c:v", "png", "-"],
                                   30, binary=True)
                        data = r.stdout if r.returncode == 0 else b""
                    except Exception:
                        data = b""
                self.bridge.thumb.emit(uid, data)


def gpu_stats() -> dict:
    """NVIDIA: load, encoder load, temperature (nvidia-smi); {} elsewhere."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {}
    try:
        r = ff.run([exe, "--query-gpu=name,utilization.gpu,utilization.encoder,temperature.gpu,memory.used",
                    "--format=csv,noheader,nounits"], 5)
        name, util, enc, temp, mem = [x.strip() for x in r.stdout.splitlines()[0].split(",")]
        return {"name": name, "util": util, "enc": enc, "temp": temp, "mem": mem}
    except Exception:
        return {}


class MainWindow(QMainWindow):
    UPDATE_EVERY = 24 * 3600

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(style.app_icon())
        self.setAcceptDrops(True)
        self.resize(1320, 820)
        self.setMinimumSize(980, 620)
        self.jobs: list[J.Job] = []
        self.items: dict[int, QTreeWidgetItem] = {}
        self.thumbs: dict[int, QPixmap] = {}
        self.user_presets: dict = config.read("presets.json") or {}
        self.av = E.Available()
        self.av_ready = False
        self.bridge = Bridge()
        self.bridge.probed.connect(self._on_probed)
        self.bridge.thumb.connect(self._on_thumb)
        self.bridge.encoders.connect(self._on_encoders)
        self.bridge.update.connect(self._on_update_result)
        self.bridge.gpu.connect(self._on_gpu)
        self.worker = Worker(self.bridge)
        self.runner = Runner(self)
        self.runner.progress.connect(self._on_progress)
        self.runner.log.connect(self._on_log)
        self.runner.finished.connect(self._on_finished)
        self.current: J.Job | None = None
        self.running = False                     # the queue is being worked through
        self.taskbar = winint.TaskbarProgress()
        self.update_info = None
        self._loading = False                    # settings widgets are being filled: ignore their signals
        self._quitting = False
        self._last_stat: dict = {}
        self._gpu: dict = {}
        self._gpu_busy = False

        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_toolbar())
        self.split = QSplitter(Qt.Horizontal)
        self.split.setChildrenCollapsible(False)
        self.split.addWidget(self._build_queue())
        self.split.addWidget(self._build_tabs())
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 0)
        self.split.setSizes([740, 600])
        v.addWidget(self.split, 1)
        v.addWidget(self._build_bottom())
        self._shortcuts()

        self.sys_timer = QTimer(self)
        self.sys_timer.setInterval(1000)
        self.sys_timer.timeout.connect(self._sys_tick)
        self.sys_timer.start()
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(800)
        self.save_timer.timeout.connect(self.save_queue)
        self._select_changed()
        self._update_buttons()
        self.detect_encoders()

    # ── building ──────────────────────────────────────────────
    def _build_toolbar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("transport")
        w.setAttribute(Qt.WA_StyledBackground)
        w.setStyleSheet(f"QWidget#transport {{ border-top: none; border-bottom: 1px solid {style.BORDER}; }}")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(4)
        lay.addWidget(text_button("fa6s.file-circle-plus", t("add_files"), self.add_files_dialog))
        lay.addWidget(text_button("fa6s.folder-plus", t("add_folder"), self.add_folder_dialog))
        lay.addSpacing(6)
        self.b_remove = ctl_button("fa6s.trash-can", t("remove") + " (Del)", 16, self.remove_selected)
        self.b_up = ctl_button("fa6s.arrow-up", t("move_up") + " (Ctrl+↑)", 15, lambda: self.move_selected(-1))
        self.b_down = ctl_button("fa6s.arrow-down", t("move_down") + " (Ctrl+↓)", 15, lambda: self.move_selected(1))
        self.b_clean = ctl_button("fa6s.broom", t("clear_done"), 16, self.clear_done)
        for b in (self.b_remove, self.b_up, self.b_down, self.b_clean):
            lay.addWidget(b)
        lay.addSpacing(18)
        lab = QLabel(t("preset"))
        lab.setObjectName("hint")
        lay.addWidget(lab)
        self.preset_box = QComboBox()
        self.preset_box.setMinimumWidth(300)
        self.preset_box.activated.connect(self._preset_picked)
        lay.addWidget(self.preset_box)
        self.b_preset_menu = ctl_button("fa6s.ellipsis", t("presets_menu"), 15, self._preset_menu)
        lay.addWidget(self.b_preset_menu)
        lay.addStretch(1)
        self.b_start = text_button("fa6s.play", t("start"), self.start_queue, "primaryBtn")
        self.b_pause = ctl_button("fa6s.pause", t("pause") + " (Space)", 16, self.toggle_pause)
        self.b_stop = ctl_button("fa6s.stop", t("stop"), 16, self.stop_queue)
        lay.addWidget(self.b_start)
        lay.addWidget(self.b_pause)
        lay.addWidget(self.b_stop)
        lay.addSpacing(6)
        lay.addWidget(ctl_button("fa6s.bars", t("menu"), 17, self._main_menu))
        self._fill_presets()
        return w

    def _build_queue(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(14, 12, 6, 10)
        lay.setSpacing(6)
        top = QHBoxLayout()
        self.q_title = QLabel(t("queue"))
        self.q_title.setStyleSheet("font-size: 12pt; font-weight: 700;")
        top.addWidget(self.q_title)
        self.q_count = QLabel()
        self.q_count.setObjectName("hint")
        top.addWidget(self.q_count)
        top.addStretch(1)
        lay.addLayout(top)
        self.view = QTreeWidget()
        self.view.setObjectName("playlist")
        self.view.setColumnCount(5)
        self.view.setHeaderLabels(["#", t("col_file"), t("col_src"), t("col_profile"), t("col_state")])
        self.view.setRootIsDecorated(False)
        self.view.setUniformRowHeights(True)
        self.view.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.view.setAlternatingRowColors(False)
        self.view.setIconSize(QSize(15, 15))
        h = self.view.header()
        h.setStretchLastSection(False)
        h.setSectionResizeMode(COL_N, QHeaderView.Fixed)
        h.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        h.setSectionResizeMode(COL_SRC, QHeaderView.Interactive)
        h.setSectionResizeMode(COL_PROFILE, QHeaderView.Interactive)
        h.setSectionResizeMode(COL_STATE, QHeaderView.Fixed)
        self.view.setColumnWidth(COL_N, 34)
        self.view.setColumnWidth(COL_SRC, 200)
        self.view.setColumnWidth(COL_PROFILE, 190)
        self.view.setColumnWidth(COL_STATE, 96)
        self.view.itemSelectionChanged.connect(self._select_changed)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._queue_menu)
        self.view.itemDoubleClicked.connect(lambda it, _c: self._open_result(self._job_of(it)))
        lay.addWidget(self.view, 1)
        self.empty = QLabel(t("empty"), self.view.viewport())
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {style.MUTED}; font-size: 11pt;")
        self.empty.setAttribute(Qt.WA_TransparentForMouseEvents)
        return w

    def _build_tabs(self) -> QWidget:
        w = QWidget()
        w.setMinimumWidth(520)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 12, 14, 10)
        lay.setSpacing(6)
        self.scope = QLabel()
        self.scope.setObjectName("hint")
        self.scope.setWordWrap(True)
        lay.addWidget(self.scope)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_info(), t("tab_info"))
        self.tabs.addTab(self._scroll(self._tab_video()), t("tab_video"))
        self.tabs.addTab(self._scroll(self._tab_audio()), t("tab_audio"))
        self.tabs.addTab(self._tab_tracks(), t("tab_tracks"))
        self.tabs.addTab(self._tab_status(), t("tab_status"))
        lay.addWidget(self.tabs, 1)
        return w

    @staticmethod
    def _scroll(inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sa.setWidget(inner)
        sa.setStyleSheet("QScrollArea { background: transparent; } QScrollArea > QWidget > QWidget { background: transparent; }")
        return sa

    # File / Info
    def _tab_info(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self.i_thumb = QLabel()
        self.i_thumb.setAlignment(Qt.AlignCenter)
        self.i_thumb.setMinimumHeight(200)
        self.i_thumb.setStyleSheet(f"background: {style.PANEL}; border: 1px solid {style.BORDER}; border-radius: 10px;")
        lay.addWidget(self.i_thumb)
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
        sa = self._scroll(self.i_text)
        lay.addWidget(sa, 1)
        return w

    # Video
    def _tab_video(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        box = QGroupBox(t("grp_format"))
        f = QFormLayout(box)
        self.v_cont = QComboBox()
        self.v_cont.addItem("MKV (Matroska)", "mkv")
        self.v_cont.addItem("MP4", "mp4")
        self.v_cont.currentIndexChanged.connect(lambda: self._set(None, "container", self.v_cont.currentData()))
        f.addRow(t("container"), self.v_cont)
        self.v_mode = QComboBox()
        self.v_mode.addItem(t("v_encode"), "encode")
        self.v_mode.addItem(t("v_copy_long"), "copy")
        self.v_mode.currentIndexChanged.connect(lambda: self._set("video", "mode", self.v_mode.currentData()))
        f.addRow(t("video"), self.v_mode)
        self.v_enc = QComboBox()
        self.v_enc.currentIndexChanged.connect(self._enc_changed)
        f.addRow(t("encoder"), self.v_enc)
        self.v_bits = QComboBox()
        for key in ("auto", "8", "10"):
            self.v_bits.addItem(t("bits_" + key), key)
        self.v_bits.currentIndexChanged.connect(lambda: self._set("video", "bits", self.v_bits.currentData()))
        f.addRow(t("bit_depth"), self.v_bits)
        self.v_enc_hint = QLabel()
        self.v_enc_hint.setObjectName("hint")
        self.v_enc_hint.setWordWrap(True)
        f.addRow("", self.v_enc_hint)
        lay.addWidget(box)

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
        self.v_q_hint = QLabel()
        self.v_q_hint.setObjectName("hint")
        self.v_q_hint.setWordWrap(True)
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
        self.v_size_hint = QLabel()
        self.v_size_hint.setObjectName("hint")
        g.addWidget(self.v_q, 0, 1)
        g.addWidget(self.v_q_hint, 0, 2)
        g.addWidget(self.v_br, 1, 1)
        g.addWidget(self.v_size, 2, 1)
        g.addWidget(self.v_size_hint, 2, 2)
        g.setColumnStretch(2, 1)
        sp = QHBoxLayout()
        a = QLabel(t("faster"))
        a.setObjectName("hint")
        b = QLabel(t("better"))
        b.setObjectName("hint")
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
        lay.addWidget(box)

        box = QGroupBox(t("grp_picture"))
        f = QFormLayout(box)
        self.v_h = QComboBox()
        self.v_h.addItem(t("as_source"), 0)
        for h in (2160, 1440, 1080, 720, 576, 480):
            self.v_h.addItem(f"{h}p", h)
        self.v_h.currentIndexChanged.connect(lambda: self._set("video", "height", self.v_h.currentData()))
        f.addRow(t("resolution"), self.v_h)
        self.v_fps = QComboBox()
        self.v_fps.addItem(t("as_source"), 0)
        for x in (60, 50, 30, 25, 24):
            self.v_fps.addItem(f"{x} fps", x)
        self.v_fps.currentIndexChanged.connect(lambda: self._set("video", "fps", self.v_fps.currentData()))
        f.addRow(t("fps"), self.v_fps)
        self.v_deint = QComboBox()
        for key in ("auto", "on", "off"):
            self.v_deint.addItem(t("deint_" + key), key)
        self.v_deint.currentIndexChanged.connect(lambda: self._set("video", "deint", self.v_deint.currentData()))
        f.addRow(t("deint"), self.v_deint)
        self.v_meta = QCheckBox(t("keep_meta"))
        self.v_meta.toggled.connect(lambda on: self._set(None, "meta", bool(on)))
        f.addRow("", self.v_meta)
        lay.addWidget(box)
        lay.addStretch(1)
        return w

    # Audio
    def _tab_audio(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(10)
        box = QGroupBox(t("grp_audio"))
        f = QFormLayout(box)
        self.a_codec = QComboBox()
        self.a_codec.addItem(t("a_auto"), "auto")
        self.a_codec.addItem(t("a_copy"), "copy")
        for a in E.AUDIO:
            self.a_codec.addItem(t("a_name_" + a.id), a.id)
        self.a_codec.currentIndexChanged.connect(lambda: self._set("audio", "codec", self.a_codec.currentData()))
        f.addRow(t("codec"), self.a_codec)
        self.a_kbps = QComboBox()
        self.a_kbps.addItem(t("auto"), 0)
        for k in (96, 128, 160, 192, 224, 256, 320, 384, 448, 512, 640):
            self.a_kbps.addItem(f"{k} kbit/s", k)
        self.a_kbps.currentIndexChanged.connect(lambda: self._set("audio", "kbps", self.a_kbps.currentData()))
        f.addRow(t("bitrate"), self.a_kbps)
        self.a_ch = QComboBox()
        for key in ("keep", "stereo", "5.1"):
            self.a_ch.addItem(t("ch_" + key.replace(".", "")), key)
        self.a_ch.currentIndexChanged.connect(lambda: self._set("audio", "channels", self.a_ch.currentData()))
        f.addRow(t("channels"), self.a_ch)
        self.a_tracks = QComboBox()
        self.a_tracks.addItem(t("tracks_all"), "all")
        self.a_tracks.addItem(t("tracks_first"), "first")
        self.a_tracks.currentIndexChanged.connect(lambda: self._set("audio", "tracks", self.a_tracks.currentData()))
        f.addRow(t("audio_tracks"), self.a_tracks)
        self.a_norm = QCheckBox(t("normalize"))
        self.a_norm.toggled.connect(lambda on: self._set("audio", "normalize", bool(on)))
        f.addRow("", self.a_norm)
        hint = QLabel(t("audio_hint"))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        f.addRow("", hint)
        lay.addWidget(box)
        box = QGroupBox(t("grp_subs"))
        f = QFormLayout(box)
        self.s_mode = QComboBox()
        for key in ("all", "first", "none"):
            self.s_mode.addItem(t("subs_" + key), key)
        self.s_mode.currentIndexChanged.connect(lambda: self._set(None, "subs", self.s_mode.currentData()))
        f.addRow(t("subtitles"), self.s_mode)
        hint = QLabel(t("subs_hint"))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        f.addRow("", hint)
        lay.addWidget(box)
        lay.addStretch(1)
        return w

    # Tracks
    def _tab_tracks(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self.tr_hint = QLabel()
        self.tr_hint.setObjectName("hint")
        self.tr_hint.setWordWrap(True)
        lay.addWidget(self.tr_hint)
        self.tr_view = QTreeWidget()
        self.tr_view.setObjectName("playlist")
        self.tr_view.setColumnCount(4)
        self.tr_view.setHeaderLabels(["#", t("tr_track"), t("tr_lang"), t("tr_action")])
        self.tr_view.setRootIsDecorated(False)
        h = self.tr_view.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.tr_view.itemChanged.connect(self._track_toggled)
        lay.addWidget(self.tr_view, 1)
        return w

    # Status
    def _tab_status(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)
        self.st_name = QLabel(t("st_idle"))
        self.st_name.setStyleSheet("font-weight: 700; font-size: 11pt;")
        self.st_name.setWordWrap(True)
        lay.addWidget(self.st_name)
        self.st_bar = QProgressBar()
        self.st_bar.setRange(0, 1000)
        self.st_bar.setTextVisible(True)
        self.st_bar.setFormat("")
        lay.addWidget(self.st_bar)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(5)
        self.st: dict[str, QLabel] = {}
        left = ("elapsed", "eta", "speed", "fps", "bitrate", "size", "encoder")
        right = ("cpu", "ram", "ff_mem", "disk", "gpu", "gpu_enc", "gpu_temp")
        for col, keys in ((0, left), (2, right)):
            for row, k in enumerate(keys):
                lab = QLabel(t("st_" + k))
                lab.setObjectName("hint")
                val = QLabel("—")
                val.setTextInteractionFlags(Qt.TextSelectableByMouse)
                grid.addWidget(lab, row, col)
                grid.addWidget(val, row, col + 1)
                self.st[k] = val
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        lay.addLayout(grid)
        top = QHBoxLayout()
        lab = QLabel(t("log"))
        lab.setStyleSheet("font-weight: 600;")
        top.addWidget(lab)
        top.addStretch(1)
        top.addWidget(ctl_button("fa6s.copy", t("log_copy"), 13,
                                 lambda: QApplication.clipboard().setText(self.log.toPlainText())))
        top.addWidget(ctl_button("fa6s.eraser", t("log_clear"), 13, lambda: self.log.clear()))
        lay.addLayout(top)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(Runner.LOG_KEEP)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log.setStyleSheet(f"background: {style.PANEL}; border: 1px solid {style.BORDER}; border-radius: 8px;"
                               "font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 8.5pt;")
        lay.addWidget(self.log, 1)
        return w

    def _build_bottom(self) -> QWidget:
        w = QWidget()
        w.setObjectName("transport")
        w.setAttribute(Qt.WA_StyledBackground)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(14, 8, 14, 10)
        lay.setSpacing(8)
        lab = QLabel(t("out_to"))
        lab.setObjectName("hint")
        lay.addWidget(lab)
        self.o_mode = QComboBox()
        self.o_mode.addItem(t("out_source"), "source")
        self.o_mode.addItem(t("out_folder"), "folder")
        self.o_mode.setCurrentIndex(1 if self.cfg.get("out_mode") == "folder" else 0)
        self.o_mode.currentIndexChanged.connect(self._out_mode_changed)
        lay.addWidget(self.o_mode)
        self.o_dir = QLabel()
        self.o_dir.setObjectName("hint")
        self.o_dir.setMaximumWidth(300)
        lay.addWidget(self.o_dir)
        self.o_pick = ctl_button("fa6s.folder-open", t("out_pick"), 15, self._pick_out_dir)
        lay.addWidget(self.o_pick)
        lay.addSpacing(12)
        lab = QLabel(t("priority"))
        lab.setObjectName("hint")
        lay.addWidget(lab)
        self.prio = QComboBox()
        for key in config.PRIORITIES:
            self.prio.addItem(t("prio_" + key), key)
        self.prio.setCurrentIndex(config.PRIORITIES.index(self.cfg.get("priority", "below")))
        self.prio.currentIndexChanged.connect(self._prio_changed)
        lay.addWidget(self.prio)
        lab = QLabel(t("on_done"))
        lab.setObjectName("hint")
        lay.addWidget(lab)
        self.done_box = QComboBox()
        for key in config.ON_DONE:
            self.done_box.addItem(t("done_" + key), key)
        self.done_box.setCurrentIndex(config.ON_DONE.index(self.cfg.get("on_done", "nothing")))
        self.done_box.currentIndexChanged.connect(lambda: self.cfg.__setitem__("on_done", self.done_box.currentData()))
        lay.addWidget(self.done_box)
        lay.addStretch(1)
        self.total = QProgressBar()
        self.total.setRange(0, 1000)
        self.total.setFixedWidth(230)
        self.total.setFormat("")
        lay.addWidget(self.total)
        self._out_mode_changed()
        return w

    def _shortcuts(self):
        def sc(keys, fn):
            for k in (keys if isinstance(keys, (list, tuple)) else [keys]):
                s = QShortcut(QKeySequence(k), self)
                s.setContext(Qt.WindowShortcut)
                s.activated.connect(fn)
        sc("Ctrl+O", self.add_files_dialog)
        sc("Ctrl+Shift+O", self.add_folder_dialog)
        sc("Delete", self.remove_selected)
        sc("Ctrl+Up", lambda: self.move_selected(-1))
        sc("Ctrl+Down", lambda: self.move_selected(1))
        sc("Ctrl+A", self.view.selectAll)
        sc("Ctrl+Return", self.start_queue)
        sc("Space", self.toggle_pause)
        sc("Escape", self.view.clearSelection)
        sc("Ctrl+,", self.settings_dialog)

    # ── presets ───────────────────────────────────────────────
    def _fill_presets(self):
        self.preset_box.blockSignals(True)
        self.preset_box.clear()
        for key in J.PRESETS:
            self.preset_box.addItem(t("p_" + key), key)
        if self.user_presets:
            self.preset_box.insertSeparator(self.preset_box.count())
            for name in sorted(self.user_presets):
                self.preset_box.addItem("★ " + name, name)
        i = self.preset_box.findData(self.cfg.get("preset", "hevc_hw"))
        self.preset_box.setCurrentIndex(max(0, i))
        self.preset_box.blockSignals(False)

    def _preset_picked(self):
        key = self.preset_box.currentData()
        if not key:
            return
        st = J.preset_settings(key, self.user_presets)
        jobs = self.selected_jobs(editable=True)
        if jobs:
            for j in jobs:
                j.settings = J.merged(st)
                j.preset = key
                self._refresh(j)
            self._queue_changed()
        else:
            self.cfg["preset"] = key
            self.cfg["defaults"] = {}
            config.save(self.cfg)
        self._load_settings()

    def _preset_menu(self):
        m = QMenu(self)
        m.addAction(style.icon("fa6s.floppy-disk"), t("preset_save"), self.save_preset)
        if self.user_presets:
            dm = m.addMenu(style.icon("fa6s.trash-can"), t("preset_delete"))
            for name in sorted(self.user_presets):
                dm.addAction(name, lambda n=name: self.delete_preset(n))
        m.addSeparator()
        m.addAction(t("preset_default_now"), self._make_default)
        m.exec(self.b_preset_menu.mapToGlobal(self.b_preset_menu.rect().bottomLeft()))

    def save_preset(self):
        name, ok = QInputDialog.getText(self, APP_TITLE, t("preset_name"))
        name = (name or "").strip()
        if not ok or not name:
            return
        self.user_presets[name] = J.merged(self._shown_settings())
        config.write("presets.json", self.user_presets)
        self._fill_presets()
        self.preset_box.setCurrentIndex(self.preset_box.findData(name))

    def delete_preset(self, name: str):
        self.user_presets.pop(name, None)
        config.write("presets.json", self.user_presets)
        if self.cfg.get("preset") == name:
            self.cfg["preset"] = "hevc_hw"
        self._fill_presets()

    def _make_default(self):
        """The settings shown now become the settings of new files."""
        self.cfg["defaults"] = J.merged(self._shown_settings())
        config.save(self.cfg)
        self._toast(t("defaults_saved"))

    def new_settings(self) -> dict:
        if self.cfg.get("defaults"):
            return J.merged(self.cfg["defaults"])
        return J.preset_settings(self.cfg.get("preset", "hevc_hw"), self.user_presets)

    # ── queue ─────────────────────────────────────────────────
    def add_files_dialog(self):
        exts = " ".join("*" + e for e in VIDEO_EXT)
        files, _ = QFileDialog.getOpenFileNames(self, t("add_files"), self.cfg.get("last_dir", ""),
                                                f"{t('video_files')} ({exts});;{t('all_files')} (*)")
        if files:
            self.cfg["last_dir"] = os.path.dirname(files[0])
            self.add_paths(files)

    def add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, t("add_folder"), self.cfg.get("last_dir", ""))
        if d:
            self.cfg["last_dir"] = d
            self.add_paths([d])

    @staticmethod
    def expand(paths: list[str]) -> list[str]:
        out = []
        for p in paths:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):
                    dirs.sort(key=str.lower)
                    for f in sorted(files, key=str.lower):
                        if f.lower().endswith(VIDEO_EXT):
                            out.append(os.path.join(root, f))
            elif os.path.isfile(p):
                out.append(p)
        return out

    def add_paths(self, paths: list[str]) -> int:
        have = {os.path.normcase(os.path.abspath(j.src)) for j in self.jobs if j.status in ("wait", "probe", "run", "pause")}
        n = 0
        for p in self.expand(paths):
            key = os.path.normcase(os.path.abspath(p))
            if key in have:
                continue
            have.add(key)
            j = J.Job(src=os.path.abspath(p), settings=self.new_settings(), preset=self.cfg.get("preset", ""))
            self._append(j)
            n += 1
        if n:
            self._queue_changed()
            if not self.view.selectedItems():
                self.view.setCurrentItem(self.items[self.jobs[-n].uid])
        return n

    def _append(self, j: J.Job):
        self.jobs.append(j)
        it = QTreeWidgetItem()
        it.setData(0, Qt.UserRole, j.uid)
        self.items[j.uid] = it
        self.view.addTopLevelItem(it)
        if j.info is None:
            if j.status == "wait":
                j.status = "probe"
            self.worker.probe(j.uid, j.src)
        self._refresh(j)

    def _job_of(self, it: QTreeWidgetItem | None) -> J.Job | None:
        if it is None:
            return None
        uid = it.data(0, Qt.UserRole)
        return next((j for j in self.jobs if j.uid == uid), None)

    def selected_jobs(self, editable: bool = False) -> list[J.Job]:
        out = [self._job_of(it) for it in self.view.selectedItems()]
        out = [j for j in out if j is not None]
        out.sort(key=lambda j: self.jobs.index(j))
        if editable:
            out = [j for j in out if j.status in EDITABLE]
        return out

    def _on_probed(self, uid: int, info):
        j = next((x for x in self.jobs if x.uid == uid), None)
        if j is None:
            return
        j.info = info
        if j.status == "probe":
            j.status = "wait"
        if info.error:
            j.status, j.error = "error", info.error
        elif info.video is None and j.settings.get("video", {}).get("mode") != "copy" and not info.of("audio"):
            j.status, j.error = "error", t("no_streams")
        self._refresh(j)
        sel = self.selected_jobs()
        if sel and sel[0] is j:
            self._select_changed()
        if self.running and self.current is None:
            QTimer.singleShot(0, self._next)

    def _on_thumb(self, uid: int, data: bytes):
        pm = QPixmap()
        if data and pm.loadFromData(data):
            self.thumbs[uid] = pm
        sel = self.selected_jobs()
        if sel and sel[0].uid == uid:
            self._show_thumb(sel[0])

    def _refresh(self, j: J.Job):
        it = self.items.get(j.uid)
        if it is None:
            return
        n = self.jobs.index(j) + 1
        it.setText(COL_N, str(n))
        it.setText(COL_FILE, j.name)
        it.setToolTip(COL_FILE, j.src + (f"\n→ {j.out}" if j.out else ""))
        it.setText(COL_SRC, P.summary(j.info) if j.info is not None else "…")
        it.setToolTip(COL_SRC, it.text(COL_SRC))
        it.setText(COL_PROFILE, J.describe(j.settings, self.av if self.av_ready else None, t))
        it.setToolTip(COL_PROFILE, it.text(COL_PROFILE))
        it.setIcon(COL_STATE, state_icon(j.status))
        if j.status in ("run", "pause"):
            text = f"{j.progress * 100:.1f}%"
        elif j.status == "done":
            text = P.fmt_size(j.out_size) if j.out_size else t("s_done")
        else:
            text = t("s_" + j.status)
        it.setText(COL_STATE, text)
        tip = j.error if j.status == "error" else (j.out if j.status == "done" else "")
        it.setToolTip(COL_STATE, tip)
        bold = j.status in ("run", "pause")
        f = it.font(COL_FILE)
        f.setBold(bold)
        for c in range(5):
            it.setFont(c, f)
            it.setForeground(c, QColor(style.RUN_TEXT) if bold else QColor(style.MUTED if j.status in ("done", "stop", "skip")
                                                                       else style.TEXT))

    def _renumber(self):
        for j in self.jobs:
            it = self.items.get(j.uid)
            if it is not None:
                it.setText(COL_N, str(self.jobs.index(j) + 1))

    def _queue_changed(self):
        waiting = sum(1 for j in self.jobs if j.status in ("wait", "probe"))
        done = sum(1 for j in self.jobs if j.status == "done")
        self.q_count.setText(t("q_count", len(self.jobs), waiting, done) if self.jobs else "")
        self.empty.setVisible(not self.jobs)
        self._update_buttons()
        self._update_total()
        self.save_timer.start()

    def remove_selected(self):
        if self.view.hasFocus() is False and not self.view.selectedItems():
            return
        jobs = [j for j in self.selected_jobs() if j is not self.current]
        if not jobs:
            return
        for j in jobs:
            it = self.items.pop(j.uid, None)
            if it is not None:
                self.view.takeTopLevelItem(self.view.indexOfTopLevelItem(it))
            self.jobs.remove(j)
            self.thumbs.pop(j.uid, None)
        self._renumber()
        self._queue_changed()

    def clear_done(self):
        for j in [j for j in self.jobs if j.status in ("done", "stop", "skip")]:
            it = self.items.pop(j.uid, None)
            if it is not None:
                self.view.takeTopLevelItem(self.view.indexOfTopLevelItem(it))
            self.jobs.remove(j)
        self._renumber()
        self._queue_changed()

    def move_selected(self, d: int):
        jobs = self.selected_jobs()
        if not jobs:
            return
        idx = [self.jobs.index(j) for j in jobs]
        if (d < 0 and min(idx) == 0) or (d > 0 and max(idx) == len(self.jobs) - 1):
            return
        for j in (jobs if d < 0 else reversed(jobs)):
            i = self.jobs.index(j)
            self.jobs.insert(i + d, self.jobs.pop(i))
        self.view.blockSignals(True)
        sel = {j.uid for j in jobs}
        while self.view.topLevelItemCount():
            self.view.takeTopLevelItem(0)
        for j in self.jobs:
            self.view.addTopLevelItem(self.items[j.uid])
            self.items[j.uid].setSelected(j.uid in sel)
        self.view.blockSignals(False)
        self._renumber()
        self._queue_changed()

    def retry(self, jobs: list[J.Job]):
        for j in jobs:
            if j.status in ("error", "stop", "skip", "done") and j is not self.current:
                j.status, j.error, j.progress = ("wait" if j.info is not None else "probe"), "", 0.0
                if j.info is None or j.info.error:
                    j.info = None
                    j.status = "probe"
                    self.worker.probe(j.uid, j.src)
                self._refresh(j)
        self._queue_changed()

    def _queue_menu(self, pos):
        it = self.view.itemAt(pos)
        j = self._job_of(it)
        m = QMenu(self)
        if j is not None:
            if j.status == "done" and j.out:
                m.addAction(style.icon("fa6s.play"), t("open_result"), lambda: self._open_result(j))
                m.addAction(style.icon("fa6s.folder-open"), t("show_result"), lambda: winint.show_in_explorer(j.out))
            m.addAction(style.icon("fa6s.folder"), t("show_source"), lambda: winint.show_in_explorer(j.src))
            m.addSeparator()
            jobs = self.selected_jobs()
            if any(x.status in ("error", "stop", "skip", "done") for x in jobs):
                m.addAction(style.icon("fa6s.rotate-right"), t("retry"), lambda: self.retry(jobs))
            if any(x.status in ("wait", "probe") for x in jobs):
                m.addAction(style.icon("fa6s.forward"), t("skip"), lambda: self._skip(jobs))
            m.addAction(style.icon("fa6s.terminal"), t("copy_cmd"), lambda: self._copy_cmd(j))
            m.addSeparator()
            m.addAction(style.icon("fa6s.trash-can"), t("remove") + "\tDel", self.remove_selected)
        m.addAction(style.icon("fa6s.broom"), t("clear_done"), self.clear_done)
        m.exec(self.view.viewport().mapToGlobal(pos))

    def _skip(self, jobs):
        for j in jobs:
            if j.status in ("wait", "probe"):
                j.status = "skip"
                self._refresh(j)
        self._queue_changed()

    def _copy_cmd(self, j: J.Job):
        if j.info is None:
            return
        try:
            args = J.build_args(j, ff.ffmpeg() or "ffmpeg", J.out_name(j, self.cfg), self.av,
                                bool(self.cfg.get("hw_decode", True)))
        except RuntimeError as e:
            self._toast(t(str(e)))
            return
        QApplication.clipboard().setText(" ".join(f'"{a}"' if " " in a else a for a in args))
        self._toast(t("cmd_copied"))

    def _open_result(self, j: J.Job | None):
        if j is None or j.status != "done" or not os.path.isfile(j.out):
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(j.out))

    # ── settings tabs ─────────────────────────────────────────
    def _select_changed(self):
        jobs = self.selected_jobs()
        self._load_settings()
        if jobs:
            j = jobs[0]
            ed = [x for x in jobs if x.status in EDITABLE]
            if len(jobs) == 1:
                self.scope.setText(t("scope_one", j.name) if ed else t("scope_locked"))
            else:
                self.scope.setText(t("scope_many", len(ed)) if ed else t("scope_locked"))
            self._show_info(j)
        else:
            self.scope.setText(t("scope_new"))
            self._show_info(None)
        self._fill_tracks()
        self._update_buttons()

    def _shown_settings(self) -> dict:
        jobs = self.selected_jobs()
        return jobs[0].settings if jobs else self.new_settings()

    def _set(self, section: str | None, key: str, value):
        """A settings widget changed: write that one value into every selected editable job (or the defaults)."""
        if self._loading:
            return
        jobs = self.selected_jobs(editable=True)
        targets = [j.settings for j in jobs]
        if not jobs and not self.selected_jobs():
            if not self.cfg.get("defaults"):
                self.cfg["defaults"] = J.merged(self.new_settings())
            targets = [self.cfg["defaults"]]
        for st in targets:
            if section:
                st.setdefault(section, {})[key] = value
            else:
                st[key] = value
        for j in jobs:
            if j.status in ("done", "error", "stop", "skip") and j is not self.current:
                j.status, j.error, j.progress = ("wait" if j.info is not None and not j.info.error else j.status), \
                    (j.error if j.info is not None and j.info.error else ""), 0.0
            self._refresh(j)
        if jobs:
            self._queue_changed()
        else:
            config.save(self.cfg)
        if (section, key) in (("video", "encoder"), ("video", "rc"), ("video", "mode"), (None, "container")):
            self._load_settings()
            if key == "container":
                self._fill_tracks()
                j = self.selected_jobs()
                self._show_info(j[0] if j else None)

    def _load_settings(self):
        st = self._shown_settings()
        v, a = st.get("video", {}), st.get("audio", {})
        self._loading = True
        try:
            self._fill_encoders()
            sel = lambda box, val: box.setCurrentIndex(max(0, box.findData(val)))   # noqa: E731
            sel(self.v_cont, st.get("container", "mkv"))
            sel(self.v_mode, v.get("mode", "encode"))
            sel(self.v_enc, v.get("encoder", "auto_hevc"))
            sel(self.v_bits, str(v.get("bits", "auto")))
            enc = self.av.resolve(v.get("encoder", "auto_hevc")) if self.av_ready else \
                E.BY_ID.get(v.get("encoder", "")) or E.BY_ID["x265"]
            enc = enc or E.BY_ID.get(v.get("encoder", ""), E.BY_ID["x265"])
            lo, hi = enc.q_range
            self.v_q.setRange(lo, hi)
            self.v_q.setValue(int(v.get("quality") or enc.q_default))
            self.v_q_hint.setText(t("q_hint", "CRF" if not enc.hw else "CQ" if enc.vendor == "nvidia" else "QP",
                                    enc.q_default))
            self.v_br.setValue(int(v.get("bitrate", 8000)))
            self.v_size.setValue(int(v.get("size_mb", 4000)))
            rc = v.get("rc", "quality")
            {"quality": self.rc_q, "bitrate": self.rc_b, "size": self.rc_s}.get(rc, self.rc_q).setChecked(True)
            self.v_q.setEnabled(rc == "quality")
            self.v_br.setEnabled(rc == "bitrate")
            self.v_size.setEnabled(rc == "size")
            self.v_speed.setValue(int(v.get("speed", 5)))
            sel(self.v_h, int(v.get("height", 0) or 0))
            sel(self.v_fps, int(v.get("fps", 0) or 0))
            sel(self.v_deint, v.get("deint", "auto"))
            self.v_meta.setChecked(bool(st.get("meta", True)))
            enc_on = v.get("mode", "encode") != "copy"
            for wdg in (self.v_enc, self.v_bits, self.rc_q, self.rc_b, self.rc_s, self.v_speed, self.v_h,
                        self.v_fps, self.v_deint):
                wdg.setEnabled(enc_on)
            if not enc_on:
                for wdg in (self.v_q, self.v_br, self.v_size):
                    wdg.setEnabled(False)
            self._speed_label()
            self._enc_hint(enc if enc_on else None, v)
            jobs = self.selected_jobs()
            dur = jobs[0].info.duration if jobs and jobs[0].info else 0.0
            self.v_size_hint.setText(t("size_hint", E.target_kbps(float(v.get("size_mb", 0)), dur, 0)) if dur else "")
            sel(self.a_codec, a.get("codec", "auto"))
            sel(self.a_kbps, int(a.get("kbps", 0) or 0))
            sel(self.a_ch, a.get("channels", "keep"))
            sel(self.a_tracks, a.get("tracks", "all"))
            self.a_norm.setChecked(bool(a.get("normalize")))
            sel(self.s_mode, st.get("subs", "all"))
            jobs_all = self.selected_jobs()
            locked = bool(jobs_all) and not self.selected_jobs(editable=True)
            for tab in range(1, 4):
                self.tabs.widget(tab).setEnabled(not locked)
        finally:
            self._loading = False

    def _fill_encoders(self):
        cur = self.v_enc.currentData()
        self.v_enc.blockSignals(True)
        self.v_enc.clear()
        model = self.v_enc.model()
        for fam in E.FAMILIES:
            e = self.av.resolve("auto_" + fam) if self.av_ready else None
            name = E.FAMILY_LABEL[fam] + " — " + t("enc_auto") + (f" ({self._vendor(e)})" if e else "")
            self.v_enc.addItem(name, "auto_" + fam)
        self.v_enc.insertSeparator(self.v_enc.count())
        for e in E.VIDEO:
            self.v_enc.addItem(f"{E.FAMILY_LABEL[e.family]} — {self._vendor(e)}", e.id)
            if self.av_ready and not self.av.ok.get(e.id):
                item = model.item(self.v_enc.count() - 1)
                item.setEnabled(False)
                item.setToolTip(t("enc_missing") + (": " + self.av.errors[e.id] if e.id in self.av.errors else ""))
        i = self.v_enc.findData(cur)
        self.v_enc.setCurrentIndex(max(0, i))
        self.v_enc.blockSignals(False)

    @staticmethod
    def _vendor(e: E.VEnc) -> str:
        return E.VENDOR_LABEL[e.vendor] if e.hw else {"x264": "x264", "x265": "x265", "svtav1": "SVT-AV1"}[e.id] + \
            " (" + t("cpu") + ")"

    def _enc_changed(self):
        eid = self.v_enc.currentData()
        if eid:
            self._set("video", "encoder", eid)
            if not self._loading:
                self._set("video", "quality", 0)          # the new encoder's own default
                self._load_settings()

    def _enc_hint(self, enc: E.VEnc | None, v: dict):
        if enc is None:
            self.v_enc_hint.setText(t("copy_hint") if v.get("mode") == "copy" else "")
            return
        if not self.av_ready:
            self.v_enc_hint.setText(t("enc_checking"))
            return
        if self.av.resolve(v.get("encoder", "auto_hevc")) is None:
            self.v_enc_hint.setText("⚠ " + t("enc_none_here"))
            return
        txt = t("enc_gpu") if enc.hw else t("enc_cpu")
        jobs = self.selected_jobs()
        src = jobs[0].info.video if jobs and jobs[0].info else None
        if src is not None and src.hdr and 10 not in enc.bits:
            txt += "\n⚠ " + t("w_hdr_8bit")
        elif src is not None and src.hdr == "Dolby Vision":
            txt += "\n⚠ " + t("w_dv")
        self.v_enc_hint.setText(txt)

    def _speed_label(self):
        x = self.v_speed.value()
        self.v_speed_lab.setText(t("speed_" + ("fast" if x <= 2 else "slow" if x >= 6 else "mid")) + f" ({x})")

    def _show_info(self, j: J.Job | None):
        if j is None:
            self.i_name.setText(t("info_none"))
            self.i_text.setText("")
            self.i_thumb.setPixmap(QPixmap())
            self.i_thumb.setText(t("info_drop"))
            return
        self.i_name.setText(j.name)
        info = j.info
        if info is None:
            self.i_text.setText(t("probing"))
            return
        if info.error:
            self.i_text.setText(f"<span style='color:{style.ERR}'>{_esc(info.error)}</span>")
            return
        rows = [f"<b>{t('i_container')}</b>: {_esc(info.format_long or info.format)}",
                f"<b>{t('i_duration')}</b>: {P.fmt_time(info.duration)} · <b>{t('i_size')}</b>: {P.fmt_size(info.size)}"
                + (f" · {P.fmt_rate(info.bitrate)}" if info.bitrate else "")]
        if info.title:
            rows.append(f"<b>{t('i_title')}</b>: {_esc(info.title)}")
        if info.chapters:
            rows.append(f"<b>{t('i_chapters')}</b>: {info.chapters}")
        mut = style.MUTED

        def track(s, line):
            extra = " · ".join(x for x in (s.lang_label(), _esc(s.title), t("i_default") if s.default else "",
                                           t("i_forced") if s.forced else "") if x)
            return f"#{s.index} {_esc(line)}" + (f" <span style='color:{mut}'>· {extra}</span>" if extra else "")
        for kind, title, fn in (("video", "i_video", P.video_line), ("audio", "i_audio", P.audio_line),
                                ("subtitle", "i_subs", P.sub_line)):
            ss = info.of(kind)
            if ss:
                rows.append(f"<br><b>{t(title)}</b> ({len(ss)})")
                rows += [track(s, fn(s)) for s in ss]
        warn = [t(w) for w in dict.fromkeys(J.plan(j)["warnings"])]
        if warn:
            rows.append("<br>" + "<br>".join(f"<span style='color:{style.WARN}'>⚠ {_esc(w)}</span>" for w in warn))
        if j.status == "done" and j.out:
            rows.append(f"<br><b>{t('i_result')}</b>: {_esc(j.out)} ({P.fmt_size(j.out_size)})")
        elif j.status == "error" and j.error:
            rows.append(f"<br><span style='color:{style.ERR}'>{_esc(j.error)}</span>")
        self.i_text.setText("<br>".join(rows))
        self._show_thumb(j)

    def _show_thumb(self, j: J.Job):
        pm = self.thumbs.get(j.uid)
        if pm is None:
            self.i_thumb.setPixmap(QPixmap())
            self.i_thumb.setText("")
            if j.info is not None and j.info.video is not None:
                self.worker.thumb(j.uid, j.src, max(0.0, j.info.duration * 0.12))
            return
        w = max(200, self.i_thumb.width() - 12)
        self.i_thumb.setPixmap(pm.scaled(w, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _fill_tracks(self):
        self.tr_view.blockSignals(True)
        self.tr_view.clear()
        jobs = self.selected_jobs()
        if len(jobs) != 1 or jobs[0].info is None or jobs[0].info.error:
            self.tr_hint.setText(t("tr_pick_one"))
            self.tr_view.blockSignals(False)
            self.tr_view.setEnabled(False)
            return
        j = jobs[0]
        self.tr_view.setEnabled(j.status in EDITABLE)
        self.tr_hint.setText(t("tr_hint"))
        pl = J.plan(j)
        out_audio = {s.index: c for s, c in pl["audio"]}
        out_subs = {s.index for s, _ in pl["subs"]}
        for s in j.info.streams:
            if s.kind not in ("video", "audio", "subtitle") or s.cover:
                continue
            it = QTreeWidgetItem(self.tr_view)
            it.setData(0, Qt.UserRole, s.index)
            icon = {"video": "fa6s.film", "audio": "fa6s.volume-high", "subtitle": "fa6s.closed-captioning"}[s.kind]
            it.setIcon(1, style.icon(icon, style.MUTED))
            it.setText(0, str(s.index))
            line = {"video": P.video_line, "audio": P.audio_line, "subtitle": P.sub_line}[s.kind](s)
            it.setText(1, line + (f" — {s.title}" if s.title else ""))
            it.setToolTip(1, it.text(1))
            it.setText(2, s.lang_label())
            if s.kind == "video":
                it.setText(3, t("v_copy") if j.settings["video"].get("mode") == "copy" else t("tr_encode"))
                continue
            keep = s.index in out_audio if s.kind == "audio" else s.index in out_subs
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked if keep else Qt.Unchecked)
            if s.kind == "audio":
                box = QComboBox()
                box.addItem(t("tr_default") + (f" ({self._a_label(out_audio.get(s.index))})" if keep else ""), "")
                box.addItem(t("a_copy"), "copy")
                for a in E.AUDIO:
                    box.addItem(a.label.split(" (")[0], a.id)
                box.setCurrentIndex(max(0, box.findData(j.track(s).get("codec", ""))))
                box.currentIndexChanged.connect(lambda _i, b=box, idx=s.index: self._track_codec(idx, b.currentData()))
                self.tr_view.setItemWidget(it, 3, box)
            else:
                it.setText(3, ("mov_text" if j.settings.get("container") == "mp4" else t("a_copy")) if keep else "")
        self.tr_view.blockSignals(False)

    @staticmethod
    def _a_label(codec: str | None) -> str:
        if not codec:
            return ""
        return t("a_copy") if codec == "copy" else E.A_BY_ID[codec].label.split(" (")[0]

    def _track_toggled(self, it: QTreeWidgetItem, col: int):
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1 or col != 0:
            return
        j = jobs[0]
        idx = str(it.data(0, Qt.UserRole))
        j.tracks.setdefault(idx, {})["keep"] = it.checkState(0) == Qt.Checked
        self._refresh(j)
        self._queue_changed()
        QTimer.singleShot(0, self._fill_tracks)
        self._show_info(j)

    def _track_codec(self, idx: int, codec: str):
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1:
            return
        j = jobs[0]
        j.tracks.setdefault(str(idx), {})["codec"] = codec or ""
        if codec:
            j.tracks[str(idx)]["keep"] = True
        self._refresh(j)
        self._queue_changed()

    # ── output / priority ─────────────────────────────────────
    def _out_mode_changed(self):
        mode = self.o_mode.currentData()
        self.cfg["out_mode"] = mode
        if mode == "folder" and not self.cfg.get("out_dir"):
            if not self._pick_out_dir():
                self.o_mode.setCurrentIndex(0)
                return
        d = self.cfg.get("out_dir", "")
        self.o_dir.setText(_elide_path(d, 44) if mode == "folder" else "")
        self.o_dir.setToolTip(d)
        self.o_pick.setVisible(mode == "folder")
        config.save(self.cfg)

    def _pick_out_dir(self) -> bool:
        d = QFileDialog.getExistingDirectory(self, t("out_pick"), self.cfg.get("out_dir") or self.cfg.get("last_dir", ""))
        if not d:
            return False
        self.cfg["out_dir"] = d
        self.o_dir.setText(_elide_path(d, 44))
        self.o_dir.setToolTip(d)
        config.save(self.cfg)
        return True

    def _prio_changed(self):
        self.cfg["priority"] = self.prio.currentData()
        if self.runner.running:
            self.runner.set_priority(self.cfg["priority"])

    # ── running ───────────────────────────────────────────────
    def start_queue(self):
        if self.runner.running:
            if self.runner.paused:
                self.toggle_pause()
            return
        if not any(j.status in ("wait", "probe") for j in self.jobs):
            if any(j.status in ("error", "stop") for j in self.jobs) and \
                    QMessageBox.question(self, APP_TITLE, t("ask_retry")) == QMessageBox.Yes:
                self.retry([j for j in self.jobs if j.status in ("error", "stop")])
            else:
                return
        if not self.av_ready:
            self._toast(t("enc_checking"))
            QTimer.singleShot(500, self.start_queue)
            return
        self.running = True
        self.log.clear()
        self.tabs.setCurrentIndex(4)
        self._next()

    def _next(self):
        if not self.running or self.runner.running:
            return
        j = next((x for x in self.jobs if x.status == "wait"), None)
        if j is None:
            if any(x.status == "probe" for x in self.jobs):
                return                                    # _on_probed calls us again
            self._queue_done()
            return
        out = J.out_name(j, self.cfg)
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            args = J.build_args(j, ff.ffmpeg() or "ffmpeg", out, self.av, bool(self.cfg.get("hw_decode", True)))
        except (RuntimeError, OSError) as e:
            j.status, j.error = "error", t(str(e)) if str(e) in ("no_encoder",) else str(e)
            self._refresh(j)
            QTimer.singleShot(0, self._next)
            return
        if not ff.ffmpeg():
            self.running = False
            QMessageBox.critical(self, APP_TITLE, t("no_ffmpeg"))
            return
        self.current = j
        j.status, j.out, j.progress, j.error, j.out_size = "run", out, 0.0, "", 0
        self._refresh(j)
        self.st_name.setText(f"{j.name}  →  {os.path.basename(out)}")
        self.st["encoder"].setText(self._enc_text(j))
        self._last_stat = {}
        self.runner.start(args, out, j.info.duration if j.info else 0.0, self.cfg.get("priority", "below"))
        self._update_buttons()
        self._queue_changed()

    def _enc_text(self, j: J.Job) -> str:
        if j.enc_used == "copy":
            return t("v_copy")
        e = E.BY_ID.get(j.enc_used)
        return f"{e.ffname} · {self._vendor(e)}" if e else "—"

    def _on_progress(self, d: dict):
        j = self.current
        if j is None:
            return
        self._last_stat = d
        j.progress = d["frac"]
        j.out_size = d["size"]
        self._refresh(j)
        self.st_bar.setValue(int(d["frac"] * 1000))
        self.st_bar.setFormat(f"{d['frac'] * 100:.1f}%  ·  {P.fmt_time(d['out_time'])} / "
                              f"{P.fmt_time(j.info.duration if j.info else 0)}")
        self.st["elapsed"].setText(P.fmt_time(d["elapsed"]))
        self.st["eta"].setText(P.fmt_time(d["eta"]) if d["eta"] else "—")
        self.st["speed"].setText(f"{d['speed']:.2f}×" if d["speed"] else "—")
        self.st["fps"].setText(f"{d['fps']:.1f}" if d["fps"] else "—")
        br = d["bitrate"]
        self.st["bitrate"].setText(br.replace("kbits/s", " kbit/s") if br and br != "N/A" else "—")
        if d["size"]:
            est = d["size"] / d["frac"] if d["frac"] > 0.02 else 0
            self.st["size"].setText(P.fmt_size(d["size"]) + (f"  (≈ {P.fmt_size(est)})" if est else ""))
        self._update_total()

    def _on_log(self, line: str):
        self.log.appendPlainText(line)

    def _on_finished(self, ok: bool, err: str):
        j = self.current
        self.current = None
        if j is not None:
            j.elapsed = self.runner.elapsed()
            if ok:
                j.status, j.progress = "done", 1.0
                try:
                    j.out_size = os.path.getsize(j.out)
                except OSError:
                    pass
                self.log.appendPlainText(f"✓ {j.out} — {P.fmt_size(j.out_size)}, {P.fmt_time(j.elapsed)}")
            elif self.runner.cancelled:
                j.status, j.progress, j.out = "stop", 0.0, ""
            else:
                j.status, j.error, j.out = "error", err, ""
                self.log.appendPlainText("✗ " + err)
            self._refresh(j)
            sel = self.selected_jobs()
            if sel and sel[0] is j:
                self._select_changed()
        self.st_bar.setValue(1000 if ok else 0)
        self._update_buttons()
        self._queue_changed()
        if self.running:
            QTimer.singleShot(200, self._next)

    def _queue_done(self):
        self.running = False
        self._update_buttons()
        self.st_name.setText(t("st_all_done"))
        self.st_bar.setFormat("")
        done = [j for j in self.jobs if j.status == "done"]
        errs = [j for j in self.jobs if j.status == "error"]
        self.taskbar.set(int(self.winId()), 1.0 if not errs else 1.0,
                         winint.TaskbarProgress.ERROR if errs else winint.TaskbarProgress.NORMAL)
        QTimer.singleShot(4000, lambda: None if self.running else self.taskbar.set(int(self.winId()), None))
        msg = t("q_done", len(done)) + (t("q_errors", len(errs)) if errs else "")
        self._toast(msg)
        if self.cfg.get("notify", True) and not self.isActiveWindow() and QSystemTrayIcon.isSystemTrayAvailable():
            tray = getattr(self, "_tray", None)
            if tray is None:
                self._tray = tray = QSystemTrayIcon(style.app_icon(), self)
            tray.show()
            tray.showMessage(APP_TITLE, msg, style.app_icon(), 6000)
        action = self.cfg.get("on_done", "nothing")
        if not done:
            return
        if action == "open":
            winint.show_in_explorer(done[-1].out)
        elif action in ("sleep", "shutdown"):
            self.save_queue()
            winint.power(action)
        elif action == "quit":
            QTimer.singleShot(1500, self.close)

    def toggle_pause(self):
        if not self.runner.running:
            return
        on = not self.runner.paused
        if self.runner.pause(on) and self.current is not None:
            self.current.status = "pause" if on else "run"
            self._refresh(self.current)
        self._update_buttons()
        self._update_total()

    def stop_queue(self):
        self.running = False
        if self.runner.running:
            self.runner.cancel()
        self._update_buttons()

    def _update_buttons(self):
        busy = self.runner.running
        self.b_start.setEnabled(not busy or self.runner.paused)
        self.b_start.setText("  " + (t("resume") if busy and self.runner.paused else t("start")))
        self.b_pause.setEnabled(busy)
        self.b_pause.setIcon(style.icon("fa6s.play" if self.runner.paused else "fa6s.pause", style.ICON))
        self.b_pause.setToolTip((t("resume") if self.runner.paused else t("pause")) + " (Space)")
        self.b_stop.setEnabled(busy or self.running)
        has_sel = bool(self.view.selectedItems())
        for b in (self.b_remove, self.b_up, self.b_down):
            b.setEnabled(has_sel)
        self.b_clean.setEnabled(any(j.status in ("done", "stop", "skip") for j in self.jobs))

    def _update_total(self):
        todo = [j for j in self.jobs if j.status in ("wait", "probe", "run", "pause", "done") and j.info is not None]
        if not todo or not (self.running or self.runner.running):
            self.total.setValue(0)
            self.total.setFormat("")
            self.setWindowTitle(APP_TITLE)
            return
        w = [max(1.0, j.info.duration) for j in todo]
        done = sum(wi * (j.progress if j.status in ("run", "pause", "done") else 0.0) for wi, j in zip(w, todo))
        frac = done / sum(w)
        n_done = sum(1 for j in todo if j.status == "done")
        self.total.setValue(int(frac * 1000))
        self.total.setFormat(t("total_fmt", n_done + (1 if self.current else 0), len(todo), f"{frac * 100:.0f}"))
        paused = self.runner.paused
        self.taskbar.set(int(self.winId()), frac, winint.TaskbarProgress.PAUSED if paused else winint.TaskbarProgress.NORMAL)
        self.setWindowTitle(f"{frac * 100:.0f}% — {APP_TITLE}" + (f" ({t('s_pause')})" if paused else ""))

    # ── system usage ──────────────────────────────────────────
    def _sys_tick(self):
        if self.tabs.currentIndex() != 4 or not self.isVisible():
            return
        try:
            import psutil
            self.st["cpu"].setText(f"{psutil.cpu_percent(None):.0f}%")
            vm = psutil.virtual_memory()
            self.st["ram"].setText(f"{P.fmt_size(vm.used)} / {P.fmt_size(vm.total)}")
            pid = self.runner.proc.processId() if self.runner.proc is not None else 0
            self.st["ff_mem"].setText(P.fmt_size(psutil.Process(int(pid)).memory_info().rss) if pid else "—")
        except Exception:
            pass
        d = os.path.dirname(self.current.out) if self.current is not None else \
            (self.cfg.get("out_dir") if self.cfg.get("out_mode") == "folder" else "")
        if not d and self.jobs:
            d = os.path.dirname(self.jobs[0].src)
        try:
            self.st["disk"].setText(P.fmt_size(shutil.disk_usage(d).free) if d else "—")
        except OSError:
            self.st["disk"].setText("—")
        if not self._gpu_busy and (self.runner.running or not self._gpu):
            self._gpu_busy = True

            def work():
                self.bridge.gpu.emit(gpu_stats())
            threading.Thread(target=work, daemon=True).start()

    def _on_gpu(self, g: dict):
        self._gpu_busy = False
        self._gpu = g or {"none": True}
        if not g:
            for k in ("gpu", "gpu_enc", "gpu_temp"):
                self.st[k].setText("—")
            return
        self.st["gpu"].setText(f"{g['util']}% · {g['name']}")
        self.st["gpu_enc"].setText(f"{g['enc']}%")
        self.st["gpu_temp"].setText(f"{g['temp']} °C")

    # ── encoders ──────────────────────────────────────────────
    def detect_encoders(self, force: bool = False):
        self.av_ready = False
        cache = {} if force else self.cfg.get("encoders", {})

        def work():
            self.bridge.encoders.emit(E.detect(cache))
        threading.Thread(target=work, daemon=True).start()

    def _on_encoders(self, av):
        self.av = av
        self.av_ready = True
        self.cfg["encoders"] = E.cache_entry(av)
        config.save(self.cfg)
        for j in self.jobs:
            self._refresh(j)
        self._load_settings()
        gpu = av.gpu()
        self.log.appendPlainText(t("enc_found", E.VENDOR_LABEL.get(gpu, t("enc_no_gpu")) if gpu else t("enc_no_gpu"),
                                   ", ".join(e.ffname for e in E.VIDEO if av.ok.get(e.id)) or "—"))
        if self.running and self.current is None:
            self._next()

    # ── menu, settings, updates ───────────────────────────────
    def _main_menu(self):
        m = QMenu(self)
        m.addAction(style.icon("fa6s.gear"), t("settings") + "\tCtrl+,", self.settings_dialog)
        m.addAction(style.icon("fa6s.microchip"), t("encoders_here"), self.encoders_dialog)
        m.addSeparator()
        from . import updater
        if updater.can_self_update():
            m.addAction(style.icon("fa6s.cloud-arrow-down"), t("upd_check_now"), lambda: self.check_updates(True))
        m.addAction(style.icon("fa6s.circle-info"), t("about"), self.about)
        btn = self.sender()
        if isinstance(btn, QToolButton):
            m.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        else:
            m.exec(self.cursor().pos())

    def settings_dialog(self):
        from .dialogs import SettingsDialog
        lang, theme = self.cfg.get("language"), self.cfg.get("theme", "dark")
        if SettingsDialog(self.cfg, self).exec():
            config.save(self.cfg)
            for j in self.jobs:
                self._refresh(j)
            if lang != self.cfg.get("language") or theme != self.cfg.get("theme", "dark"):
                QMessageBox.information(self, APP_TITLE, t("lang_restart"))

    def encoders_dialog(self):
        from .dialogs import EncodersDialog
        d = EncodersDialog(self.av, self.av_ready, self)
        d.recheck.connect(lambda: self.detect_encoders(force=True))
        self.bridge.encoders.connect(d.set_available)
        d.exec()
        try:
            self.bridge.encoders.disconnect(d.set_available)
        except (RuntimeError, TypeError):
            pass

    def about(self):
        QMessageBox.about(self, t("about"), t("about_text", VERSION, ff.version() or "—", ff.ffmpeg() or "—"))

    def maybe_check_updates(self):
        from . import updater
        if self.cfg.get("check_updates", True) and updater.can_self_update() \
                and time.time() - float(self.cfg.get("last_update_check", 0)) > self.UPDATE_EVERY:
            self.check_updates()

    def check_updates(self, manual: bool = False):
        from . import updater
        self._update_manual = manual
        self.cfg["last_update_check"] = int(time.time())
        config.save(self.cfg)

        def work():
            try:
                self.bridge.update.emit(updater.check(), "")
            except Exception as e:
                self.bridge.update.emit(None, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _on_update_result(self, info, err: str):
        manual = getattr(self, "_update_manual", False)
        if err:
            if manual:
                QMessageBox.warning(self, APP_TITLE, t("upd_error", err))
            return
        if not info:
            if manual:
                QMessageBox.information(self, APP_TITLE, t("upd_none", VERSION))
            return
        self.update_info = info
        if manual or self.cfg.get("update_notified") != info["version"]:
            self.cfg["update_notified"] = info["version"]
            config.save(self.cfg)
            self.show_update()

    def show_update(self):
        info = self.update_info
        if not info:
            return
        from . import updater
        if not updater.can_self_update() or not info.get("url"):
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(info["page"]))
            return
        if self.runner.running and QMessageBox.question(self, APP_TITLE, t("upd_busy")) != QMessageBox.Yes:
            return
        from .dialogs import UpdateDialog
        d = UpdateDialog(info, self)
        if d.exec() and d.path:
            self._installer = d.path
            self.close()

    def _toast(self, text: str):
        self.statusBar().showMessage(text, 4000)

    # ── session ───────────────────────────────────────────────
    def save_queue(self):
        if not self.cfg.get("keep_queue", True):
            config.write("queue.json", {"jobs": []})
            return
        config.write("queue.json", {"jobs": [j.to_dict() for j in self.jobs]})

    def restore_queue(self):
        if not self.cfg.get("keep_queue", True):
            return
        data = config.read("queue.json") or {}
        for d in data.get("jobs") or []:
            if not isinstance(d, dict) or not d.get("src"):
                continue
            j = J.Job.from_dict(d)
            if j.status in ("wait",) and not os.path.isfile(j.src):
                j.status, j.error = "error", t("missing_file")
                self.jobs.append(j)
                it = QTreeWidgetItem()
                it.setData(0, Qt.UserRole, j.uid)
                self.items[j.uid] = it
                self.view.addTopLevelItem(it)
                self._refresh(j)
                continue
            self._append(j)
        self._queue_changed()

    def restore_geometry(self):
        g = self.cfg.get("geometry")
        if g:
            self.restoreGeometry(QByteArray.fromBase64(g.encode()))
        sp = self.cfg.get("splitter")
        if isinstance(sp, list) and len(sp) == 2 and all(isinstance(x, int) and x > 100 for x in sp):
            self.split.setSizes(sp)

    # ── window ────────────────────────────────────────────────
    def showEvent(self, e):
        super().showEvent(e)
        winint.dark_title_bar(int(self.winId()), style.IS_DARK)
        self.statusBar().setStyleSheet(f"color: {style.MUTED};")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.empty.resize(self.view.viewport().size())

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            n = self.add_paths(paths)
            self._toast(t("added_n", n) if n else t("added_none"))

    def closeEvent(self, e):
        if self.runner.running and not getattr(self, "_installer", "") and not self._quitting:
            if QMessageBox.question(self, APP_TITLE, t("ask_quit_busy")) != QMessageBox.Yes:
                e.ignore()
                return
        self._quitting = True
        self.running = False
        if self.runner.running:
            self.runner.cancel()
            t0 = time.monotonic()
            while self.runner.running and time.monotonic() - t0 < 5:
                QApplication.processEvents()
                time.sleep(0.02)
        self.save_queue()
        self.cfg["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.cfg["splitter"] = self.split.sizes()
        config.save(self.cfg)
        self.worker.stop()
        self.sys_timer.stop()
        self.taskbar.set(int(self.winId()), None)
        installer = getattr(self, "_installer", "")
        if installer:
            from . import updater
            winint.release_mutex()
            updater.run_installer(installer)
        super().closeEvent(e)
        QApplication.instance().quit()


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _elide_path(p: str, n: int) -> str:
    return p if len(p) <= n else "…" + p[-(n - 1):]
