"""The main window (laid out in ui_main.py): the tasks, their settings, running the queue.
The settings widgets edit the selected tasks (or, with nothing selected, the settings of new ones)."""
from __future__ import annotations

import os
import shutil
import threading
import time

from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QApplication, QFileDialog, QInputDialog, QMainWindow, QMenu, QMessageBox,
                               QSystemTrayIcon, QTreeWidgetItem)

from . import APP_TITLE, AUDIO_EXT, VERSION, VIDEO_EXT, config, ff, style, winint
from . import encoders as E
from . import formats as FM
from . import job as J
from . import filters as F
from . import probe as P
from . import logs
from .filter_ui import NEEDS_PARAMS, FilterDialog, short_name
from .i18n import t
from .runner import Runner
from .ui_main import MainUI

COL_N, COL_FILE, COL_SRC, COL_PROFILE, COL_STATE = range(5)
BUILD_ERRORS = ("no_encoder", "bad_copy", "bad_codec", "nothing_out")
def state_icon(status: str):
    """The status icon of a queue row, in the colours of the theme."""
    name, color = {"wait": ("fa6s.clock", "MUTED"), "probe": ("fa6s.magnifying-glass", "MUTED"),
                   "run": ("fa6s.gear", "RUN"), "pause": ("fa6s.pause", "WARN"), "done": ("fa6s.circle-check", "OK"),
                   "error": ("fa6s.circle-xmark", "ERR"), "stop": ("fa6s.ban", "MUTED"),
                   "skip": ("fa6s.forward", "MUTED")}.get(status, ("fa6s.clock", "MUTED"))
    return style.icon(name, getattr(style, color))
EDITABLE = ("wait", "error", "stop", "skip", "done")


class Bridge(QObject):
    """Results of worker threads, delivered in the GUI thread."""
    probed = Signal(int, object)          # uid, MediaInfo
    thumb = Signal(int, bytes)            # uid, PNG
    encoders = Signal(object)             # Available
    update = Signal(object, str)
    gpu = Signal(dict)
    crop = Signal(int, object)            # uid, (l, t, r, b) | None — the automatic crop
    extra = Signal(int, str, object)      # 1.4: uid, path, MediaInfo of a joined / external file
    split = Signal(int, object)           # 1.4: slot number, the cut times (by size) or an error text


class Slot:
    """1.4: one place where a task is encoded (Settings: N tasks at the same time) — its ffmpeg, its task,
    its log lines. stage: main (pre passes, the conversion, the metrics) → split (the parts)."""

    def __init__(self, win: "MainWindow", n: int):
        self.n = n
        self.plain = Runner(win)
        from .chunks import ChunkRunner
        self.chunked = ChunkRunner(win)               # 1.8: the same task in chunks, several ffmpeg at a time
        self.runner = self.plain
        self.job: J.Job | None = None
        self.log: list[str] = []
        self.started = 0.0
        self.stat: dict = {}
        self.stage = "main"
        self.kinds: list[tuple[str, float]] = []     # 1.6: what each «$ command» of the log was, and when
        self.peak = 0                                # 1.6: the most memory its ffmpeg took (bytes)
        self._mem_at = 0.0
        for r in (self.plain, self.chunked):
            r.progress.connect(lambda d, s=self: win._on_progress(d, s))
            r.log.connect(lambda line, s=self: win._on_log(line, s))
            r.finished.connect(lambda ok, err, s=self: win._on_finished(ok, err, s))

    @property
    def busy(self) -> bool:
        return self.job is not None


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
                        r = ff.run([exe, "-hide_banner", "-v", "error", "-ss", f"{at:.2f}", *ff.inp(path), "-map", "0:v:0",
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


class MainWindow(MainUI, QMainWindow):
    UPDATE_EVERY = 24 * 3600

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(style.app_icon())
        self.setAcceptDrops(True)
        self.resize(1440, 860)
        self.setMinimumSize(1100, 640)
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
        self.bridge.crop.connect(self._on_crop)
        self._crop_busy: set[int] = set()        # tasks whose black bars are being looked for
        self.bridge.extra.connect(self._on_extra)
        self.bridge.split.connect(self._on_split_times)
        self.worker = Worker(self.bridge)
        self.slots: list[Slot] = [Slot(self, 0)]
        self.runner = self.slots[0].runner       # the first place (older code, tests)
        self.running = False                     # the queue is being worked through
        self.taskbar = winint.TaskbarProgress()
        self.update_info = None
        self._loading = False                    # settings widgets are being filled: ignore their signals
        self._quitting = False
        self._last_stat: dict = {}
        self._gpu: dict = {}
        self._gpu_busy = False
        self._frame_mtime = 0.0

        self._build_ui()

        self.sys_timer = QTimer(self)
        self.sys_timer.setInterval(1000)
        self.sys_timer.timeout.connect(self._sys_tick)
        self.sys_timer.timeout.connect(self._frame_tick)
        self.sys_timer.start()
        self._cpu_label()
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(800)
        self.save_timer.timeout.connect(self.save_queue)
        self._select_changed()
        self._update_buttons()
        self.detect_encoders()

    # ── 1.4: the places where tasks are encoded ──────────────
    @property
    def current(self) -> J.Job | None:
        """The task shown on the Status tab: the selected one if it runs, else the first one running."""
        s = self._shown_slot()
        return s.job if s is not None else None

    def _busy(self, j: J.Job | None) -> bool:
        return j is not None and any(s.job is j for s in self.slots)

    def _busy_slots(self) -> list[Slot]:
        return [s for s in self.slots if s.busy]

    def _shown_slot(self) -> Slot | None:
        busy = self._busy_slots()
        if not busy:
            return None
        sel = self.selected_jobs() if hasattr(self, "view") else []
        for s in busy:
            if sel and s.job is sel[0]:
                return s
        return busy[0]

    def _any_running(self) -> bool:
        return any(s.runner.running or s.busy for s in self.slots)

    def _all_paused(self) -> bool:
        run = [s for s in self.slots if s.runner.running]
        return bool(run) and all(s.runner.paused for s in run)

    def _threads(self) -> int:
        pct = int(self.cfg.get("cpu_limit", 100))
        if pct >= 100:
            return 0
        return max(1, round((os.cpu_count() or 1) * pct / 100))

    def _cores(self, slot: Slot) -> list[int] | None:
        n = self._threads()
        total = os.cpu_count() or 1
        if not n:
            return None
        start = (slot.n * n) % total
        return [(start + i) % total for i in range(n)]

    def _cpu_label(self):
        pct = int(self.cfg.get("cpu_limit", 100))
        n = self._threads() or (os.cpu_count() or 1)
        self.b_cpu.setText(f"{pct}% ▾")
        self.b_cpu.setToolTip(t("cpu_limit_tip") + "\n" + t("cpu_threads", n, os.cpu_count() or 1))

    def set_cpu_limit(self, pct: int):
        """«100% ▾»: how much of the processor ffmpeg may take. Running tasks move to fewer cores at once;
        the thread counts apply from the next task."""
        self.cfg["cpu_limit"] = int(pct)
        config.save(self.cfg)
        self._cpu_label()
        for s in self.slots:
            if s.runner.running:
                s.runner.set_cores(self._cores(s))

    def set_parallel(self, n: int):
        self.cfg["parallel"] = max(1, min(4, int(n or 1)))
        config.save(self.cfg)
        if self.par_box.currentData() != self.cfg["parallel"]:
            self.par_box.setCurrentIndex(self.cfg["parallel"] - 1)
        if self.running:
            QTimer.singleShot(0, self._next)

    def _metrics_changed(self):
        self.cfg["metrics"] = [k for k, cb in self.m_check.items() if cb.isChecked()]
        config.save(self.cfg)

    def _frame_toggled(self, on: bool):
        self.cfg["show_frame"] = bool(on)
        config.save(self.cfg)
        if not on:
            self.fr_img.clear()
            self.fr_img.setText(t("st_frame_off"))

    def _frame_tick(self):
        """The frame being encoded (ffmpeg writes it once a second of the film) on the Status tab."""
        if self.side_bar.currentIndex() != 5 or not self.fr_on.isChecked():
            return
        s = self._shown_slot()
        if s is None:
            return
        path = J.work_file(s.job, "frame", None, "png")
        try:
            m = os.path.getmtime(path)
        except OSError:
            return
        if m == self._frame_mtime and self.fr_img.pixmap() is not None and not self.fr_img.pixmap().isNull():
            return
        pm = QPixmap()
        if pm.load(path) and not pm.isNull():
            self._frame_mtime = m
            w = max(160, self.fr_img.width() - 28)
            self.fr_img.setPixmap(pm.scaled(min(w, 400), 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _show_metrics(self, j: J.Job | None):
        av_vmaf = "libvmaf" in self.av.filters
        self.m_check["vmaf"].setEnabled(av_vmaf or not self.av_ready)
        self.m_check["vmaf"].setToolTip(t("m_tip_vmaf") if av_vmaf or not self.av_ready else t("m_no_vmaf"))
        m = j.metrics if j is not None else {}
        for k, lab in self.m_val.items():
            v = m.get(k)
            lab.setText("—" if v is None else f"{v:.2f} dB" if k == "psnr" else f"{v:.4f}" if k == "ssim"
                        else f"{v:.2f}")
        if self.av_ready and not av_vmaf:
            self.m_val["vmaf"].setText(t("m_no_vmaf_short"))

    # ── presets ───────────────────────────────────────────────
    def preset_entries(self) -> list[tuple[str, str, dict]]:
        """1.5: every preset in the order of the list, with a «#group» entry before each group:
        [(key, label, settings)] (the comparison window takes the same list)."""
        out, last = [], None
        for g, key in J.all_presets(self.user_presets):
            if g != last:
                out.append(("#" + g, t("pg_" + g), {}))
                last = g
            label = ("★ " + key) if g == "own" else t("p_" + key)
            out.append((key, label, J.preset_settings(key, self.user_presets)))
        return out

    def _fill_presets(self):
        """The preset list in groups (1.5): general, Apple, Android, TVs, consoles, the web, discs, own."""
        self.preset_box.blockSignals(True)
        self.preset_box.clear()
        self.preset_box.setMaxVisibleItems(32)
        model = self.preset_box.model()
        for key, label, _st in self.preset_entries():
            if key.startswith("#"):
                self.preset_box.addItem(label, None)
                it = model.item(self.preset_box.count() - 1)
                it.setEnabled(False)
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            else:
                self.preset_box.addItem("   " + label, key)
        i = self.preset_box.findData(self.cfg.get("preset", "hevc_hw"))
        if i < 0:
            i = self.preset_box.findData("hevc_hw")
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
        m.addAction(style.icon("fa6s.sliders"), t("fp_title") + "…", self.format_preset_dialog)
        m.addAction(style.icon("fa6s.scale-balanced"), t("cmp_title") + "…", self.compare_dialog)
        m.addSeparator()
        m.addAction(style.icon("fa6s.floppy-disk"), t("preset_save"), self.save_preset)
        if self.user_presets:
            rm = m.addMenu(style.icon("fa6s.pen"), t("preset_rename"))
            dm = m.addMenu(style.icon("fa6s.trash-can"), t("preset_delete"))
            for name in sorted(self.user_presets):
                rm.addAction(name, lambda n=name: self.rename_preset(n))
                dm.addAction(name, lambda n=name: self.delete_preset(n))
        m.addSeparator()
        m.addAction(t("preset_default_now"), self._make_default)
        m.exec(self.b_preset_menu.mapToGlobal(self.b_preset_menu.rect().bottomLeft()))

    def rename_preset(self, name: str, new: str = ""):
        if name not in self.user_presets:
            return
        if not new:
            new, ok = QInputDialog.getText(self, APP_TITLE, t("preset_name"), text=name)
            new = (new or "").strip() if ok else ""
        if not new or new == name or new in self.user_presets:
            return
        self.user_presets[new] = self.user_presets.pop(name)
        config.write("presets.json", self.user_presets)
        if self.cfg.get("preset") == name:
            self.cfg["preset"] = new
        for j in self.jobs:
            if j.preset == name:
                j.preset = new
        self._fill_presets()

    # ── 1.5: the preset of the format, the advanced encoder settings, the comparison ──
    def format_preset_dialog(self):
        from .preset_ui import FormatDialog
        st = self._shown_settings()
        d = FormatDialog(self, FM.of(st), st.get("container", "mkv"))
        d.applied.connect(self.apply_format)
        self._fmt_dlg = d
        d.exec()

    def apply_format(self, f: dict | None):
        """Put a preset of the format into the selected tasks (or the settings of new files)."""
        jobs = self.selected_jobs(editable=True)
        targets = [j.settings for j in jobs]
        if not jobs and not self.selected_jobs():
            if not self.cfg.get("defaults"):
                self.cfg["defaults"] = J.merged(self.new_settings())
            targets = [self.cfg["defaults"]]
        for st in targets:
            FM.apply(st, f)
        for j in jobs:
            if j.status in ("done", "error", "stop", "skip") and not self._busy(j) and j.info is not None \
                    and not j.info.error:
                j.status, j.error, j.progress = "wait", "", 0.0
            self._refresh(j)
        if jobs:
            self._queue_changed()
        else:
            config.save(self.cfg)
        self._load_settings()
        self._edit_tabs()
        self._fill_tracks()
        js = self.selected_jobs()
        self._show_info(js[0] if js else None)
        self._show_out_path()

    def _adv_encoder(self) -> E.VEnc | None:
        v = self._shown_settings().get("video", {})
        if v.get("mode", "encode") != "encode":
            return None
        eid = v.get("encoder", "auto_hevc")
        return self.av.resolve(eid) if self.av_ready else E.BY_ID.get(eid)

    def advanced_dialog(self):
        from .preset_ui import AdvancedDialog
        e = self._adv_encoder()
        if e is None:
            self._toast(t("adv_no_encoder"))
            return
        st = self._shown_settings()
        d = AdvancedDialog(self, e, (st.get("venc") or {}).get(e.id, {}), str(st.get("video", {}).get("bits", "auto")))
        d.changed.connect(lambda eid, vals: self._set("venc", eid, vals))
        d.bits.connect(lambda b: self._set("video", "bits", b))
        self._adv_dlg = d
        d.exec()

    def compare_dialog(self):
        from .preset_ui import CompareDialog
        sel = self.selected_jobs()
        j = sel[0] if sel and sel[0].info is not None and not sel[0].info.error else None
        key = self.preset_box.currentData() or self.cfg.get("preset", "hevc_hw")
        d = CompareDialog(self, self.av, self.preset_entries(), j, self._threads(), key)
        self._cmp = d
        d.exec()

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
        aexts = " ".join("*" + e for e in AUDIO_EXT)
        files, _ = QFileDialog.getOpenFileNames(self, t("add_files"), self.cfg.get("last_dir", ""),
                                                f"{t('media_files')} ({exts} {aexts});;{t('video_files')} ({exts});;"
                                                f"{t('audio_files')} ({aexts});;{t('all_files')} (*)")
        if files:
            self.cfg["last_dir"] = os.path.dirname(files[0])
            self.add_incoming(files)

    def add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, t("add_folder"), self.cfg.get("last_dir", ""))
        if d:
            self.cfg["last_dir"] = d
            from . import disc as DS
            if DS.disc_root(d):                           # 1.8: a disc folder: its titles, not its files
                self.open_disc(d)
                return
            self.add_paths([d])

    # ── 1.8: Blu-ray / DVD ────────────────────────────────────
    def open_disc_dialog(self):
        d = QFileDialog.getExistingDirectory(self, t("disc_open"), self.cfg.get("last_dir", ""))
        if d:
            self.cfg["last_dir"] = d
            self.open_disc(d)

    def open_disc(self, path: str, pick=None) -> int:
        """The titles of the disc in a window; the chosen ones become tasks. pick: choose without asking (tests)."""
        from . import disc as DS
        found = DS.disc_root(path)
        if not found:
            self._toast(t("disc_not"))
            return 0
        kind, root = found
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            titles = DS.titles(kind, root)
            name = DS.disc_name(kind, root)
        finally:
            QApplication.restoreOverrideCursor()
        if pick is not None:
            chosen = pick(titles)
        else:
            from .dialogs import DiscDialog
            dlg = DiscDialog(kind, root, titles, name, self)
            self._disc_dlg = dlg
            if not dlg.exec():
                return 0
            chosen = dlg.chosen()
        n = 0
        for ti in chosen:
            src = DS.title_source(ti, name)
            if self.add_paths([src]):
                j = self.jobs[-1]
                self.__dict__.setdefault("_disc_titles", {})[j.uid] = ti
                n += 1
        return n

    @staticmethod
    def expand(paths: list[str]) -> list[str]:
        out = []
        for p in paths:
            if os.path.isdir(p):
                for root, dirs, files in os.walk(p):
                    dirs.sort(key=str.lower)
                    for f in sorted(files, key=str.lower):
                        if f.lower().endswith(VIDEO_EXT + AUDIO_EXT):
                            out.append(os.path.join(root, f))
            elif os.path.isfile(p):
                out.append(p)
        return out

    def add_incoming(self, paths: list[str], target_uid: int = 0, ask: bool = True) -> int:
        from . import disc as DS
        discs = [p for p in paths if os.path.isdir(p) and DS.disc_root(p)]
        if discs:                                          # 1.8: a disc folder dropped: its titles
            n = 0
            for p in discs:
                n += self.open_disc(p)
            rest = [p for p in paths if p not in discs]
            return n + (self.add_incoming(rest, target_uid, ask) if rest else 0)
        return self._add_incoming(paths, target_uid, ask)

    def _add_incoming(self, paths: list[str], target_uid: int = 0, ask: bool = True) -> int:
        """1.6: files dropped or opened (Ctrl+O): a film becomes a task; a sound or a
        subtitle file is attached to a film — to the card (or the picture) it fell on, to the film whose name it
        starts with (film.mkv ← film.rus.ac3, film.eng.srt), else to the selected film after asking — and only
        without a film it becomes a task of its own (sound) or is left out (subtitles). Returns the tasks added."""
        files = self.expand(paths)
        edls = [f for f in files if f.lower().endswith(".edl")]           # 1.8: cut lists → the film's trim
        files = [f for f in files if f not in edls]
        extra_ext = J.EXT_AUDIO + J.EXT_SUBS
        videos = [f for f in files if not f.lower().endswith(extra_ext)]
        extras = [f for f in files if f.lower().endswith(extra_ext)]
        n = self.add_paths(videos) if videos else 0
        new = list(self.jobs[-n:]) if n else []
        if edls:
            QTimer.singleShot(0, lambda e=edls, u=target_uid: self._incoming_edl(e, u))
        try:
            return self._incoming_extras(n, extras, target_uid, ask)
        finally:
            if new and self.cfg.get("auto_ext", True):     # 1.7: film.*.ac3 / .srt … beside each new film
                got = 0
                for j in new:
                    have = {os.path.normcase(e["path"]) for e in j.ext}
                    found = [f for f in self.beside(j.src) if os.path.normcase(f) not in have]
                    if found:
                        self._attach(j, found)
                        got += len(found)
                if got:
                    self._toast(t("ext_found", got))
                    cur = self.selected_jobs()
                    if len(cur) == 1 and cur[0] in new:
                        self._fill_tracks()

    def _incoming_edl(self, edls: list[str], target_uid: int = 0, tries: int = 0):
        """An EDL dropped or opened: onto the card it fell on, the film of the same name, or the selected one."""
        if any(j.status == "probe" for j in self.jobs) and tries < 50:
            QTimer.singleShot(200, lambda: self._incoming_edl(edls, target_uid, tries + 1))
            return
        ok = [j for j in self.jobs if j.status in EDITABLE and not self._busy(j) and j.info is not None
              and not j.info.error]
        sel = [j for j in self.selected_jobs(editable=True) if j in ok]
        done = 0
        for f in edls:
            j = next((x for x in ok if x.uid == target_uid), None) or self._film_for(f, ok) or \
                (sel[0] if len(sel) == 1 else None)
            if j is None:
                continue
            try:
                with open(f, encoding="utf-8", errors="replace") as fh:
                    v = j.info.video
                    pcs, cut = J.parse_edl(fh.read(), v.fps if v is not None and v.fps else 25.0)
            except (OSError, ValueError):
                continue
            j.trim, j.trim_cut = pcs, cut
            if j.status in ("done", "error", "stop", "skip"):
                j.status, j.error, j.progress = "wait", "", 0.0
            self._refresh(j)
            done += 1
        if done:
            self._queue_changed()
            self._select_changed()
            self._toast(t("edl_loaded", done))
        elif edls:
            self._toast(t("edl_bad"))

    def _incoming_extras(self, n: int, extras: list[str], target_uid: int, ask: bool) -> int:
        if not extras:
            return n
        ok = [j for j in self.jobs if j.status in EDITABLE + ("probe",) and not self._busy(j) and
              (j.info is None or (not j.info.error and j.info.video is not None))]
        target = next((j for j in ok if j.uid == target_uid), None) if target_uid else None
        attached: dict[int, list[str]] = {}
        rest = []
        for f in extras:
            j = target or self._film_for(f, ok)
            if j is not None:
                attached.setdefault(j.uid, []).append(f)
            else:
                rest.append(f)
        if rest:
            sel = [j for j in self.selected_jobs(editable=True) if j in ok]
            film = sel[0] if len(sel) == 1 else None
            if film is not None and ask:
                names = "\n".join(os.path.basename(f) for f in rest[:8]) + ("\n…" if len(rest) > 8 else "")
                box = QMessageBox(QMessageBox.Question, APP_TITLE, t("ext_ask", os.path.basename(film.src), names), parent=self)
                b_add = box.addButton(t("ext_ask_add"), QMessageBox.AcceptRole)
                b_task = box.addButton(t("ext_ask_task"), QMessageBox.NoRole)
                box.addButton(QMessageBox.Cancel)
                box.setDefaultButton(b_add)
                box.exec()
                if box.clickedButton() is b_add:
                    attached.setdefault(film.uid, []).extend(rest)
                    rest = []
                elif box.clickedButton() is not b_task:
                    rest = []
        for uid, fs in attached.items():
            j = next(x for x in self.jobs if x.uid == uid)
            self._attach(j, fs)
        if attached:
            self._toast(t("ext_attached", sum(len(v) for v in attached.values())))
            cur = self.selected_jobs()
            if len(cur) == 1 and cur[0].uid in attached:
                self._fill_tracks()
                self._show_info(cur[0])
        tasks = [f for f in rest if not f.lower().endswith(J.EXT_SUBS)]
        if len(tasks) < len(rest):
            self._toast(t("ext_subs_alone"))
        return n + (self.add_paths(tasks) if tasks else 0)

    @staticmethod
    def beside(src: str) -> list[str]:
        """1.7: the sound and subtitle files beside a film whose names start with the film's (film.rus.ac3,
        film_eng.srt, «film [Commentary].mka»), sorted."""
        d = os.path.dirname(os.path.abspath(src))
        base = os.path.splitext(os.path.basename(src))[0].lower()
        try:
            names = sorted(os.listdir(d), key=str.lower)
        except OSError:
            return []
        out = []
        for f in names:
            low = f.lower()
            if low.endswith(J.EXT_AUDIO + J.EXT_SUBS) and low.startswith(base) and len(low) > len(base) \
                    and low[len(base)] in "._- [(" and os.path.isfile(os.path.join(d, f)):
                out.append(os.path.join(d, f))
        return out

    def _film_for(self, path: str, jobs: list) -> J.Job | None:
        """The film whose file name the extra file starts with, in its folder (the longest name wins)."""
        d = os.path.normcase(os.path.dirname(os.path.abspath(path)))
        stem = os.path.basename(path).lower()
        best, size = None, 0
        for j in jobs:
            if os.path.normcase(os.path.dirname(os.path.abspath(j.src))) != d:
                continue
            base = os.path.splitext(os.path.basename(j.src))[0].lower()
            if stem.startswith(base) and len(stem) > len(base) and stem[len(base)] in "._- [(" and len(base) > size:
                best, size = j, len(base)
        return best

    def _attach(self, j: J.Job, files: list[str]):
        have = {os.path.normcase(e["path"]) for e in j.ext}
        for f in files:
            if not os.path.isfile(f) or os.path.normcase(os.path.abspath(f)) in have:
                continue
            j.ext.append(J.clean_ext({"path": os.path.abspath(f), "kind": J.ext_kind(f), "lang": _lang_from_name(f, j.src)}))
        if j.status in ("done", "error", "stop", "skip") and j.info is not None and not j.info.error:
            j.status, j.error, j.progress = "wait", "", 0.0
        self._probe_extra(j)
        self._refresh(j)
        self._queue_changed()

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
        it.setFlags(it.flags() & ~Qt.ItemIsDropEnabled)          # 1.4: dropped between tasks, not into one
        self.items[j.uid] = it
        self.view.addTopLevelItem(it)
        self._probe_extra(j)
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
        ti = self.__dict__.get("_disc_titles", {}).pop(uid, None)
        if ti is not None and not info.error:              # 1.8: the disc's languages and chapters
            from . import disc as DS
            DS.apply_title(j, ti)
        if j.status == "probe":
            j.status = "wait"
        self._show_thumb(j)
        if info.error:
            j.status, j.error = "error", info.error
        elif info.video is None and j.settings.get("video", {}).get("mode") != "copy" and not info.of("audio"):
            j.status, j.error = "error", t("no_streams")
        self._refresh(j)
        sel = self.selected_jobs()
        if sel and sel[0] is j:
            self._select_changed()
        self._probe_extra(j)
        if self.running:
            QTimer.singleShot(0, self._next)

    def _probe_extra(self, j: J.Job):
        """1.4: ffprobe the joined / external files of a task in the background."""
        todo = [p for p in list(j.joins) + [e["path"] for e in j.ext] if p not in j.infos]
        if not todo:
            return
        for p in todo:
            j.infos[p] = None
        uid = j.uid

        def work():
            for p in todo:
                self.bridge.extra.emit(uid, p, P.probe(p))
        threading.Thread(target=work, daemon=True).start()

    def _on_extra(self, uid: int, path: str, info):
        j = next((x for x in self.jobs if x.uid == uid), None)
        if j is None:
            return
        j.infos[path] = info
        self._refresh(j)
        sel = self.selected_jobs()
        if sel and sel[0] is j:
            self._show_info(j)
            self._fill_tracks()
            self._edit_tabs()

    def _on_thumb(self, uid: int, data: bytes):
        pm = QPixmap()
        if data and pm.loadFromData(data):
            self.thumbs[uid] = pm
        it = self.items.get(uid)
        if it is not None:
            self.view.viewport().update()
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
        it.setText(COL_PROFILE, ("✂ " if J.segments(j) else "") + (f"⧉{len(j.joins) + 1} " if j.joins else "") +
                   (f"＋{len(j.ext)} " if j.ext else "") +
                   J.describe(j.settings, self.av if self.av_ready else None, t))
        it.setToolTip(COL_PROFILE, it.text(COL_PROFILE))
        it.setIcon(COL_STATE, state_icon(j.status))
        if j.status in ("run", "pause"):
            text = f"{j.progress * 100:.1f}%"
        elif j.status == "done":
            text = (P.fmt_size(j.out_size) if j.out_size else t("s_done")) + \
                (f" · {t('parts_n', len(j.parts))}" if len(j.parts) > 1 else "")
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
        jobs = [j for j in self.selected_jobs() if not self._busy(j)]
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
            if j.status in ("error", "stop", "skip", "done") and not self._busy(j):
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
            ed = [x for x in jobs if x.status in EDITABLE and not self._busy(x)]
            if len(ed) > 1:
                m.addAction(style.icon("fa6s.object-group"), t("join_selected"), self.join_selected)
            if j.info is not None and not j.info.error and len(self.jobs) > 1:
                m.addAction(style.icon("fa6s.clone"), t("apply_to") + "…", lambda: self.apply_dialog(j))
            if j.info is not None and not j.info.error:
                m.addAction(style.icon("fa6s.file-export"), t("demux") + "…", lambda: self.demux_dialog(j))
                if j.info.video is not None and j.status in EDITABLE and not self._busy(j):
                    m.addAction(style.icon("fa6s.scale-balanced"), t("est_menu") + "…", lambda: self.estimate_dialog(j))
            if len(jobs) == 1 and j in ed:
                m.addAction(style.icon("fa6s.link"), t("join_add") + "…", lambda: self.join_add_dialog())
                if j.joins:
                    m.addAction(style.icon("fa6s.link-slash"), t("join_split"), lambda: self.join_undo(j))
                m.addAction(style.icon("fa6s.plus"), t("ext_add") + "…", lambda: self.add_external_dialog())
            if j.log_path and os.path.isfile(j.log_path):
                m.addAction(style.icon("fa6s.file-lines"), t("open_log"), lambda: self.open_log(j))
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
            hw = bool(self.cfg.get("hw_decode", True))
            out = J.out_name(j, self.cfg)
            exe = ff.ffmpeg() or "ffmpeg"
            n = self._threads()
            cmds = J.pre_passes(j, exe, out, self.av, hw, threads=n) + \
                [J.build_args(j, exe, out, self.av, hw, threads=n)] + \
                J.post_passes(j, exe, out, self.av, self.cfg.get("metrics", []), threads=n)
            if J.split_on(j):
                sp = j.settings.get("split") or {}
                times = None
                if sp.get("mode") == "size":                    # cut times are known only after encoding:
                    times = J.estimate_size_times(j, self.av)    # here from the planned bit rate
                cmds.append(J.split_args(j, exe, out, times))
        except (RuntimeError, OSError) as e:
            self._toast(t(str(e)))
            return
        QApplication.clipboard().setText(" && ".join(" ".join(f'"{a}"' if (" " in a or ";" in a or "'" in a) else a
                                                             for a in args) for args in cmds))
        self._toast(t("cmd_copied"))

    def _open_result(self, j: J.Job | None):
        if j is None or j.status != "done" or not os.path.isfile(j.out):
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(j.out))

    # ── 1.4: the order with the mouse, the list in a file ─────
    def _on_reordered(self):
        """The cards were dragged: the queue takes the order they now stand in."""
        order = [self.view.topLevelItem(i).data(0, Qt.UserRole) for i in range(self.view.topLevelItemCount())]
        by = {j.uid: j for j in self.jobs}
        jobs = [by[u] for u in order if u in by]
        jobs += [j for j in self.jobs if j not in jobs]
        self.jobs = jobs
        self._renumber()
        self._queue_changed()

    TASK_FILTER = "ArtCore Convert (*.acq);;JSON (*.json)"

    def save_task_list(self, path: str = ""):
        """File → Save the task list: the tasks with all their settings (trim, filters, tracks …) in a file."""
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, t("q_save"), os.path.join(self.cfg.get("last_dir", ""),
                                                                                    "tasks.acq"), self.TASK_FILTER)
        if not path:
            return False
        import json
        data = {"app": APP_TITLE, "version": VERSION, "jobs": [j.to_dict() for j in self.jobs]}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, str(e))
            return False
        self._toast(t("q_saved", len(self.jobs)))
        return True

    def load_task_list(self, path: str = ""):
        """File → Load a task list: its tasks are added to the queue (finished ones come back as done)."""
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, t("q_load"), self.cfg.get("last_dir", ""), self.TASK_FILTER)
        if not path:
            return 0
        import json
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_TITLE, f"{t('q_load_bad')}\n{e}")
            return 0
        n = 0
        for d in (data.get("jobs") if isinstance(data, dict) else None) or []:
            if not isinstance(d, dict) or not d.get("src"):
                continue
            j = J.Job.from_dict(d)
            if j.status in ("run", "pause", "probe"):
                j.status = "wait"
            if not os.path.isfile(j.src):
                j.status, j.error = "error", t("missing_file")
            self._append(j)
            n += 1
        self._queue_changed()
        self._toast(t("q_loaded", n))
        return n

    # ── 1.4: joining files, external tracks ───────────────────
    def estimate_dialog(self, j: J.Job | None = None):
        """1.8: how big the result will be (samples encoded with the task's settings)."""
        if j is None:
            sel = self.selected_jobs()
            j = sel[0] if len(sel) == 1 else None
        if j is None or j.info is None or j.info.error or j.info.video is None or not self.av_ready:
            self._toast(t("tr_pick_one"))
            return None
        from .dialogs import EstimateDialog

        def picked(job, q):
            if job.status in ("done", "error", "stop", "skip"):
                job.status, job.error, job.progress = "wait", "", 0.0
            self._refresh(job)
            self._queue_changed()
            self._select_changed()
        d = EstimateDialog(j, self.av, bool(self.cfg.get("hw_decode", True)), self, picked)
        self._est_dlg = d
        d.show()
        return d

    def demux_dialog(self, j: J.Job | None = None):
        """1.8: the tracks of a task into files."""
        if j is None:
            sel = self.selected_jobs()
            j = sel[0] if len(sel) == 1 else None
        if j is None or j.info is None or j.info.error:
            self._toast(t("tr_pick_one"))
            return None
        from .dialogs import DemuxDialog
        d = DemuxDialog(j, self)
        self._demux_dlg = d
        d.show()
        return d

    def apply_dialog(self, src: J.Job | None = None, parts=None, scope: str = "", ask: bool = True) -> int:
        """1.7: this task's settings onto the other selected tasks (or every waiting one): the profile and filters,
        the choice of tracks, the trim. Returns how many tasks changed."""
        if src is None:
            cur = self._job_of(self.view.currentItem())
            sel = self.selected_jobs()
            src = cur if cur in sel else (sel[0] if sel else None)
        if src is None or src.info is None or src.info.error:
            self._toast(t("tr_pick_one"))
            return 0
        sel = [x for x in self.selected_jobs(editable=True) if x is not src and not self._busy(x)]
        every = [x for x in self.jobs if x is not src and x.status in EDITABLE and not self._busy(x)]
        if ask:
            from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QLabel, QRadioButton, QVBoxLayout
            d = QDialog(self)
            d.setWindowTitle(t("apply_to"))
            lay = QVBoxLayout(d)
            lay.addWidget(QLabel(t("apply_from", src.name)))
            boxes = {}
            for key in J.APPLY_PARTS:
                cb = QCheckBox(t("apply_" + key))
                cb.setChecked(key != "trim")
                lay.addWidget(cb)
                boxes[key] = cb
            r_sel = QRadioButton(t("apply_sel", len(sel)))
            r_all = QRadioButton(t("apply_all", len(every)))
            r_sel.setEnabled(bool(sel))
            (r_sel if sel else r_all).setChecked(True)
            lay.addSpacing(6)
            lay.addWidget(r_sel)
            lay.addWidget(r_all)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(d.accept)
            bb.rejected.connect(d.reject)
            lay.addWidget(bb)
            self._apply_dlg = d
            if d.exec() != QDialog.Accepted:
                return 0
            parts = [k for k, cb in boxes.items() if cb.isChecked()]
            scope = "sel" if r_sel.isChecked() else "all"
        parts = parts or ["settings", "tracks"]
        targets = sel if (scope or "sel") == "sel" and sel else every
        n = 0
        for x in targets:
            if J.apply_from(src, x, parts):
                n += 1
                if x.status in ("done", "error", "stop", "skip") and x.info is not None and not x.info.error:
                    x.status, x.error, x.progress = "wait", "", 0.0
                self._refresh(x)
        if n:
            self._queue_changed()
            self._select_changed()
        self._toast(t("applied_n", n))
        return n

    def join_selected(self):
        """The selected tasks become one: the first one takes the others' files after its own."""
        jobs = [j for j in self.selected_jobs(editable=True) if not self._busy(j)]
        if len(jobs) < 2:
            self._toast(t("join_need_two"))
            return
        head = jobs[0]
        for j in jobs[1:]:
            head.joins += [j.src] + list(j.joins)
            for k, v in j.infos.items():
                head.infos.setdefault(k, v)
            if j.info is not None:
                head.infos[j.src] = j.info
            it = self.items.pop(j.uid, None)
            if it is not None:
                self.view.takeTopLevelItem(self.view.indexOfTopLevelItem(it))
            self.jobs.remove(j)
            self.thumbs.pop(j.uid, None)
        head.trim = []
        head.status = "wait" if head.info is not None else head.status
        self._probe_extra(head)
        self._renumber()
        self._refresh(head)
        self.view.setCurrentItem(self.items[head.uid])
        self._queue_changed()
        self._select_changed()
        self._toast(t("joined_n", len(head.joins) + 1))

    def join_add_dialog(self, files: list[str] | None = None):
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1 or self._busy(jobs[0]):
            self._toast(t("tr_pick_one"))
            return
        j = jobs[0]
        if not isinstance(files, (list, tuple)):        # a menu's triggered(bool) must not pass for «files»
            files = None
        if files is None:
            exts = " ".join("*" + e for e in VIDEO_EXT + AUDIO_EXT)
            files, _ = QFileDialog.getOpenFileNames(self, t("join_add"), os.path.dirname(j.src),
                                                    f"{t('media_files')} ({exts});;{t('all_files')} (*)")
        files = [os.path.abspath(f) for f in files or [] if os.path.isfile(f)]
        if not files:
            return
        j.joins += files
        j.trim = []
        if j.status in ("done", "error", "stop", "skip") and j.info is not None and not j.info.error:
            j.status, j.error, j.progress = "wait", "", 0.0
        self._probe_extra(j)
        self._refresh(j)
        self._queue_changed()
        self._select_changed()

    def join_undo(self, j: J.Job):
        """The joined files become tasks of their own again."""
        if self._busy(j) or not j.joins:
            return
        files, j.joins, j.trim = list(j.joins), [], []
        self._refresh(j)
        self.add_paths(files)
        self._select_changed()

    def add_external_dialog(self, files: list[str] | None = None):
        """«+» on the Streams tab: an outside sound or subtitle file goes into the task's result."""
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1 or self._busy(jobs[0]):
            self._toast(t("tr_pick_one"))
            return
        j = jobs[0]
        if not isinstance(files, (list, tuple)):        # a button's clicked(bool) must not pass for «files»
            files = None
        if files is None:
            a = " ".join("*" + e for e in J.EXT_AUDIO)
            sub = " ".join("*" + e for e in J.EXT_SUBS)
            files, _ = QFileDialog.getOpenFileNames(self, t("ext_add"), os.path.dirname(j.src),
                                                    f"{t('ext_files')} ({a} {sub});;{t('audio_files')} ({a});;"
                                                    f"{t('subtitles')} ({sub});;{t('all_files')} (*)")
        for f in files or []:
            if not os.path.isfile(f):
                continue
            kind = J.ext_kind(f)
            lang = _lang_from_name(f, j.src)
            j.ext.append(J.clean_ext({"path": os.path.abspath(f), "kind": kind, "lang": lang}))
        if j.status in ("done", "error", "stop", "skip") and j.info is not None and not j.info.error:
            j.status, j.error, j.progress = "wait", "", 0.0
        self._probe_extra(j)
        self._refresh(j)
        self._queue_changed()
        self._fill_tracks()
        self._show_info(j)

    def _ext_opt(self, n: int, key: str, value):
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1 or n >= len(jobs[0].ext):
            return
        j = jobs[0]
        j.ext[n][key] = value
        if j.status in ("done", "error", "stop", "skip") and j.info is not None and not j.info.error:
            j.status, j.error, j.progress = "wait", "", 0.0
        self._refresh(j)
        self._queue_changed()
        self._show_info(j)
        if key in ("codec", "default"):
            QTimer.singleShot(0, self._fill_tracks)

    def _ext_remove(self, n: int):
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1 or n >= len(jobs[0].ext):
            return
        j = jobs[0]
        j.ext.pop(n)
        self._refresh(j)
        self._queue_changed()
        self._show_info(j)
        QTimer.singleShot(0, self._fill_tracks)

    def bench_dialog(self):
        from .dialogs import BenchDialog
        sel = self.selected_jobs()
        j = sel[0] if sel and sel[0].info is not None and not sel[0].info.error else None
        d = BenchDialog(self, self.av, j.src if j else None, j.info.duration if j else 0.0, self._threads())
        self._bench = d
        d.exec()

    # ── settings tabs ─────────────────────────────────────────
    def _select_changed(self):
        if getattr(self, "cmp_view", None) is not None and self.b_compare.isChecked():
            cur = self.selected_jobs()
            if len(cur) != 1 or cur[0].uid != getattr(self, "_cmp", (0,))[0]:
                self.toggle_compare(False)                 # 1.7: another task: back to its picture
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
            if j.status == "done" or not self._busy_slots():
                self._show_metrics(j)
            nxt = self._shown_slot()
            if nxt is not None and nxt.job is j:
                self._show_slot(nxt)
        else:
            self.scope.setText(t("scope_new"))
            self._show_info(None)
        self._fill_tracks()
        self._edit_tabs()
        for j in self.selected_jobs()[:1]:
            self._want_crop(j)
        self._update_frame_overlay()
        self._gain_preview()
        self._show_out_path()
        self._update_buttons()
    def _one_job(self) -> tuple:
        jobs = self.selected_jobs()
        j = jobs[0] if len(jobs) == 1 else None
        return j, j is not None and j.status in EDITABLE and not self._busy(j)

    def _edit_tabs(self):
        """Trim / Metadata / Chapters show the one selected task."""
        jobs = self.selected_jobs()
        j = jobs[0] if len(jobs) == 1 else None
        ed = j is not None and j.status in EDITABLE and not self._busy(j)
        for tab in (self.trim_tab, self.meta_tab, self.chap_tab):
            tab.set_job(j, ed)

    def _job_edited(self):
        """A per-file tab changed its task."""
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1:
            return
        j = jobs[0]
        if j.status in ("done", "error", "stop", "skip") and j.info is not None and not j.info.error:
            j.status, j.error, j.progress = "wait", "", 0.0
        self._refresh(j)
        self._queue_changed()
        self._show_info(j)
        tab = self.sender()
        for other in (self.trim_tab, self.meta_tab, self.chap_tab):
            if other is not tab:
                other.set_job(j, True)
        self._fill_tracks()
        self._show_out_path()
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
            if key == "container":
                J.fit_container(st)
        for j in jobs:
            if j.status in ("done", "error", "stop", "skip") and not self._busy(j):
                j.status, j.error, j.progress = ("wait" if j.info is not None and not j.info.error else j.status), \
                    (j.error if j.info is not None and j.info.error else ""), 0.0
            self._refresh(j)
        if jobs:
            self._queue_changed()
        else:
            config.save(self.cfg)
        if (section, key) in (("video", "encoder"), ("video", "rc"), ("video", "mode"), (None, "container"),
                              ("video", "width"), ("video", "height"), ("video", "fit"), ("video", "upscale"),
                              ("video", "quality"), ("video", "bitrate"), ("video", "size_mb"), ("video", "speed")):
            self._load_settings()
        if key in ("deint", "fps", "bits") or section in ("venc", "mux"):
            self._load_settings()
        if section == "audio" and key == "gain":
            self._gain_preview()
        if key in ("container", "subs", "lang", "tracks", "codec", "channels", "normalize", "kbps", "mode",
                   "gain", "rate", "abits"):
            if key == "container":
                self._edit_tabs()
            self._fill_tracks()
            self.meta_tab.set_job(*self._one_job())
            j = self.selected_jobs()
            self._show_info(j[0] if j else None)
            self._show_out_path()

    # ── 1.3: the filter rows ──────────────────────────────────
    def _setf(self, name: str, key: str, value):
        """A filter setting changed (the 1.0 keys video.deint / fps / bits go through _set)."""
        if self._loading:
            return
        if key.startswith("video."):
            self._set("video", key[6:], value)
            self._filters_changed()
            return
        jobs = self.selected_jobs(editable=True)
        targets = [j.settings for j in jobs]
        if not jobs and not self.selected_jobs():
            if not self.cfg.get("defaults"):
                self.cfg["defaults"] = J.merged(self.new_settings())
            targets = [self.cfg["defaults"]]
        for st in targets:
            fl = st.setdefault("filters", F.defaults())
            fl.setdefault(name, dict(F.FILTER_DEFAULTS[name]))[key] = value
        for j in jobs:
            if j.status in ("done", "error", "stop", "skip") and not self._busy(j) and j.info is not None \
                    and not j.info.error:
                j.status, j.error, j.progress = "wait", "", 0.0
            self._refresh(j)
        if jobs:
            self._queue_changed()
        else:
            config.save(self.cfg)
        self._filters_changed()

    def _filters_changed(self):
        for j in self.selected_jobs():
            self._want_crop(j)
        self._load_settings()
        self._update_frame_overlay()
        js = self.selected_jobs()
        self._show_info(js[0] if js else None)

    def _filter_toggled(self, name: str, on: bool):
        st = self._shown_settings()
        if name == "interlace":
            if on:
                self._set("video", "deint", "auto")
            else:
                self._set("video", "deint", "off")
                self._setf(name, "ivtc", False)
            self._filters_changed()
            return
        if name == "hdr":
            self._setf(name, "mode", "auto" if on else "off")
            return
        if name == "fps":
            if not on:
                self._set("video", "fps", 0)
                for k in ("interp", "dedup"):
                    self._setf(name, k, False)
                self._setf(name, "mode", "auto")
            else:
                self._filters_changed()
                self._filter_gear(name)
            return
        if name == "pixfmt":
            self._setf(name, "on", bool(on))
            if on:
                self._filter_gear(name)
            return
        self._setf(name, "on", bool(on))
        f = F.fset(st, name)
        if on and name == "delogo" and not int(f.get("w", 0) or 0):
            self._filter_gear(name)
            self._filter_action(name, "draw")
        elif on and name == "overlay" and not f.get("path"):
            self._filter_gear(name)
        elif on and name in NEEDS_PARAMS and not F.is_on(self._first_job() or _Shim(st), name):
            self._filter_gear(name)

    def _filters_on(self, j: J.Job) -> list[str]:
        """The names of the video filters that will run on j (the Info tab)."""
        v = j.settings.get("video", {})
        if v.get("mode", "encode") != "encode" or j.info is None or j.info.video is None:
            return []
        enc = self.av.resolve(v.get("encoder", "auto_hevc")) if self.av_ready else E.BY_ID.get(v.get("encoder", ""))
        bits = J.out_bits(j, enc)
        out = []
        for name in F.VIDEO_ROWS:
            if F.is_on(j, name, enc, bits):
                out.append(short_name(name))
        return out

    def _first_job(self):
        js = self.selected_jobs()
        return js[0] if js and js[0].info is not None and not js[0].info.error else None

    def _filter_gear(self, name: str):
        d = self.f_dialogs.get(name)
        if d is None:
            d = FilterDialog(name, self)
            d.edited.connect(self._setf)
            d.action.connect(self._filter_action)
            d.finished.connect(lambda _r, n=name: self._dialog_closed(n))
            self.f_dialogs[name] = d
        self._fill_filter_dialog(name)
        d.show()
        d.raise_()
        d.activateWindow()

    def _dialog_closed(self, name: str):
        if name == "delogo":
            self.frame_ov.stop_draw()
            self._update_frame_overlay()

    def _fill_filter_dialog(self, name: str):
        d = self.f_dialogs.get(name)
        if d is None:
            return
        st = self._shown_settings()
        j = self._first_job()
        v = st.get("video", {})
        enc = self.av.resolve(v.get("encoder", "auto_hevc")) if self.av_ready else E.BY_ID.get(v.get("encoder", ""))
        note, en = "", {}
        f = F.fset(st, name)
        if name == "crop":
            manual = f.get("mode", "auto") == "manual"
            en = {k: manual for k in ("l", "t", "r", "b")}
            en["detect"] = j is not None and j.info.video is not None
            if j is not None and j.crop_found is not None and j.info.video is not None:
                l, tt, r, b = j.crop_found or (0, 0, 0, 0)
                vs = j.info.video
                note = t("f_crop_found", l, tt, r, b, vs.width - l - r, vs.height - tt - b) if any((l, tt, r, b)) \
                    else t("f_crop_nothing")
            elif j is not None and j.uid in self._crop_busy:
                note = t("f_detecting")
            if not manual and j is not None and j.crop_found:              # auto: the numbers it found
                import copy
                st = copy.deepcopy(st)
                st.setdefault("filters", F.defaults())["crop"].update(zip(("l", "t", "r", "b"), j.crop_found))
        elif name == "hdr" and f.get("engine") == "libplacebo":
            note = t("f_note_libplacebo")
        elif name == "deshake" and f.get("method") == "vidstab":
            note = t("f_note_vidstab")
        elif name == "fps" and f.get("interp"):
            note = t("f_note_interp")
        elif name == "pixfmt" and enc is not None:
            can = F.CHROMAS.get(enc.id, ("420",))
            if len(can) < 3:
                note = t("f_note_chroma", E.FAMILY_LABEL.get(enc.family, enc.ffname) + f" ({enc.ffname})",
                         " / ".join({"420": "4:2:0", "422": "4:2:2", "444": "4:4:4"}[c] for c in can))
        d.load(st, note, en)

    def _filter_action(self, name: str, act: str):
        j = self._first_job()
        if act == "detect" and j is not None:
            j.crop_found = None
            self._want_crop(j, force=True)
            self._fill_filter_dialog(name)
        elif act == "draw":
            if j is None or j.info.video is None:
                return
            self.show_side("streams")
            self._update_frame_overlay()
            self.frame_ov.start_draw()
        elif act == "reset" and name == "color":
            for k, val in F.FILTER_DEFAULTS["color"].items():
                if k != "on":
                    self._setf(name, k, val)

    def _delogo_drawn(self, x: int, y: int, w: int, h: int):
        for k, val in (("x", x), ("y", y), ("w", w), ("h", h)):
            self._setf("delogo", k, val)
        self._setf("delogo", "on", True)

    def _update_frame_overlay(self):
        """The crop frame and the logo rectangle of the selected task over its picture."""
        j = self._first_job()
        if j is None or j.info.video is None or self.trim_tab.job is not j:
            self.frame_ov.set_state((0, 0, 1.0), None, None)
            return
        vs = j.info.video
        sar = J._ratio(vs.sar) if vs.sar and vs.sar not in ("0:1", "1:1") else 1.0
        crop = F.crop_values(j) if F.is_on(j, "crop") else None
        lg = F.fset(j.settings, "delogo")
        logo = tuple(int(lg.get(k, 0) or 0) for k in ("x", "y", "w", "h")) if lg.get("on") else None
        self.frame_ov.set_state((vs.width, vs.height, sar), crop, logo)

    def _gain_preview(self):
        """The Volume of the first audio track, shown on the loudness curve."""
        j = self._first_job()
        db = 0.0
        if j is not None and j.info.of("audio"):
            db = float(j.acfg(j.info.of("audio")[0]).get("gain", 0) or 0)
        self.trim_tab.bar.set_gain(db)

    # the automatic crop: cropdetect in a thread, once per file
    def _want_crop(self, j: J.Job, force: bool = False) -> bool:
        """True while the bars of j are still being looked for."""
        f = F.fset(j.settings, "crop")
        if not (f.get("on") and f.get("mode", "auto") == "auto") or j.info is None or j.info.error \
                or j.info.video is None:
            return False
        if j.crop_found is not None and not force:
            return False
        if j.uid in self._crop_busy:
            return True
        self._crop_busy.add(j.uid)
        exe, src, dur, w, h, uid = ff.ffmpeg(), j.src, j.info.duration, j.info.video.width, j.info.video.height, j.uid

        def work():
            res = F.detect_crop(exe, src, dur, w, h) if exe else None
            self.bridge.crop.emit(uid, res)
        threading.Thread(target=work, daemon=True).start()
        return True

    def _on_crop(self, uid: int, res):
        self._crop_busy.discard(uid)
        j = next((x for x in self.jobs if x.uid == uid), None)
        if j is None:
            return
        j.crop_found = list(res) if res else []
        self._refresh(j)
        self._queue_changed()
        if j in self.selected_jobs():
            self._load_settings()
            self._update_frame_overlay()
            self._show_info(j)
        if self.running:
            QTimer.singleShot(0, self._next)

    def _load_settings(self):
        st = self._shown_settings()
        v, a = st.get("video", {}), st.get("audio", {})
        self._loading = True
        try:
            self._fill_encoders()
            sel = lambda box, val: box.setCurrentIndex(max(0, box.findData(val)))   # noqa: E731
            cont = E.container(st.get("container", "mkv"))
            mode = v.get("mode", "encode")
            sel(self.v_cont, st.get("container", "mkv"))
            sel(self.v_enc, "copy" if mode == "copy" else v.get("encoder", "auto_hevc"))
            enc = self.av.resolve(v.get("encoder", "auto_hevc")) if self.av_ready else \
                E.BY_ID.get(v.get("encoder", "")) or E.BY_ID["x265"]
            enc = enc or E.BY_ID.get(v.get("encoder", ""), E.BY_ID["x265"])
            lo, hi = enc.q_range
            self.v_q.setRange(lo, hi)
            self.v_q.setValue(int(v.get("quality") or enc.q_default))
            if enc.id == "prores":
                self.v_q_hint.setText(t("q_hint_prores"))
            elif enc.id == "gif":
                self.v_q_hint.setText(t("q_hint_gif"))
            else:
                qn = "CRF" if not enc.hw and enc.family not in ("mpeg4", "mpeg2video") else \
                    "CQ" if enc.vendor == "nvidia" else "QP" if enc.hw else "qscale"
                self.v_q_hint.setText(t("q_hint", qn, enc.q_default))
            self.v_br.setValue(int(v.get("bitrate", 8000)))
            self.v_size.setValue(int(v.get("size_mb", 4000)))
            rc = v.get("rc", "quality") if enc.rc else "quality"
            {"quality": self.rc_q, "bitrate": self.rc_b, "size": self.rc_s}.get(rc, self.rc_q).setChecked(True)
            sel(self.v_rc, rc)
            if rc == "quality":
                self.v_val.setRange(lo, hi)
                self.v_val.setSuffix("")
                self.v_val.setSingleStep(1)
                self.v_val.setValue(self.v_q.value())
            elif rc == "bitrate":
                self.v_val.setRange(200, 200000)
                self.v_val.setSuffix(" k")
                self.v_val.setSingleStep(500)
                self.v_val.setValue(self.v_br.value())
            else:
                self.v_val.setRange(10, 500000)
                self.v_val.setSuffix(" MB")
                self.v_val.setSingleStep(100)
                self.v_val.setValue(self.v_size.value())
            self.v_q.setEnabled(rc == "quality")
            self.v_br.setEnabled(rc == "bitrate")
            self.v_size.setEnabled(rc == "size")
            self.v_speed.setValue(int(v.get("speed", 5)))
            sel(self.v_speed_box, int(v.get("speed", 5)))
            two = self.av.two_pass(enc) or enc.vendor == "nvidia"            # 1.4
            self.v_2pass.setChecked(int(v.get("passes", 1) or 1) == 2)
            self.v_2pass.setEnabled(rc != "quality" and enc.rc and (two or not self.av_ready))
            self.v_2pass_hint.setText(t("two_pass_q") if rc == "quality" else
                                      t("two_pass_nv") if enc.vendor == "nvidia" else
                                      t("two_pass_none") if not two and self.av_ready else t("two_pass_hint"))
            self._fill_res(v)
            self.v_w.setValue(int(v.get("width", 0) or 0))
            self.v_hh.setValue(int(v.get("height", 0) or 0))
            sel(self.v_fit, v.get("fit", "fit"))
            self.v_up.setChecked(bool(v.get("upscale")))
            self.v_on.setChecked(mode != "none" and not cont.audio_only)
            self.v_on.setEnabled(not cont.audio_only and cont.id != "gif")
            enc_on = mode == "encode" and not cont.audio_only
            self.v_body.setEnabled(not cont.audio_only and mode != "none")
            self.v_enc.setEnabled(not cont.audio_only and mode != "none")
            for wdg in (self.rc_q, self.rc_b, self.rc_s, self.v_speed, self.v_res, self.v_w,
                        self.v_hh, self.v_fit, self.v_up, self.v_rc, self.v_val, self.v_speed_box):
                wdg.setEnabled(enc_on)
            if enc_on and not enc.rc:
                for wdg in (self.rc_b, self.rc_s, self.v_br, self.v_size, self.v_rc):
                    wdg.setEnabled(False)
                self.v_q.setEnabled(enc.id != "gif")
                self.v_val.setEnabled(enc.id != "gif")
            if enc_on and (enc.id in ("prores", "gif", "mpeg4", "mpeg2")):
                self.v_speed_box.setEnabled(False)
            if not enc_on:
                for wdg in (self.v_q, self.v_br, self.v_size):
                    wdg.setEnabled(False)
            amodel = self.a_codec.model()
            for i in range(self.a_codec.count()):
                cid = self.a_codec.itemData(i)
                item = amodel.item(i)
                if item is not None and cid not in ("auto", "copy"):
                    fits = cont.takes_audio(E.A_CODEC.get(cid, cid))
                    in_fmt = FM.takes_audio(st, cid)
                    item.setEnabled(fits and in_fmt)
                    item.setToolTip("" if fits and in_fmt else t("not_in_container", cont.id.upper()) if not fits
                                    else t("not_in_format", FM.of(st).get("name", "")))
            self._speed_label()
            self._enc_hint(enc if enc_on else None, v, cont)
            from . import advanced as ADV                                  # 1.5: what is set in ⚙ Advanced
            adv = ADV.summary(enc, (st.get("venc") or {}).get(enc.id, {})) if enc_on else ""
            self.v_adv.setText(("⚙ " + adv) if adv else "")
            self.v_adv.setVisible(bool(adv))
            self.b_adv.setEnabled(enc_on)
            f = FM.of(st)
            self.v_fmt.setText(t("fp_in_use", f["name"]) if f.get("name") else "")
            self.v_fmt.setVisible(bool(f.get("name")))
            jobs = self.selected_jobs()
            jr = jobs[0] if jobs and jobs[0].info is not None and jobs[0].info.video is not None else None
            if jr is not None:
                vs = jr.info.video
                self.v_head_label.setText(f"#{vs.index} " + P.video_line(vs))
                c = vs.color
                self.v_color.setText(" ".join(x for x in (vs.hdr, c.get("color_space", ""), c.get("color_transfer", ""),
                                                          c.get("color_primaries", "")) if x) or "—")
            else:
                self.v_head_label.setText(t("video"))
                self.v_color.setText("—")
            if jr is not None and enc_on:
                ow, oh = J.out_size(jr, enc)
                self.v_res_hint.setText(t("res_result", jr.info.video.width, jr.info.video.height, ow, oh))
            else:
                self.v_res_hint.setText("")
            dur = J.out_duration(jobs[0]) if jobs and jobs[0].info else 0.0
            self.v_size_hint.setText(t("size_hint", E.target_kbps(float(v.get("size_mb", 0)), dur, 0)) if dur else "")
            sel(self.a_codec, a.get("codec", "auto"))
            sel(self.a_kbps, int(a.get("kbps", 0) or 0))
            sel(self.a_ch, a.get("channels", "keep"))
            self.a_gain.setValue(float(a.get("gain", 0) or 0))
            sel(self.a_rate, int(a.get("rate", 0) or 0))
            sel(self.a_abits, int(a.get("abits", 0) or 0))
            fj = jobs[0] if len(jobs) >= 1 and jobs[0].info is not None and not jobs[0].info.error else None
            self.f_rows.show_state(st, fj, enc if enc_on else None, J.out_bits(fj, enc) if fj and enc_on else 8,
                                   enc_on)
            self.f_head.setEnabled(enc_on)
            for name in list(self.f_dialogs):
                self._fill_filter_dialog(name)
            sel(self.a_tracks, a.get("tracks", "all"))
            self.a_norm.setChecked(bool(a.get("normalize")))
            sel(self.s_mode, st.get("subs", "all"))
            sel(self.s_lang, st.get("lang", ""))
            locked = bool(jobs) and not self.selected_jobs(editable=True)
            for wdg in (self.side.widget(4), self.v_cont, self.preset_box):         # Streams + the format
                wdg.setEnabled(not locked)
        finally:
            self._loading = False
    def _fill_res(self, v: dict):
        """The resolution list: as the source, the usual frames, the current own size, «own size…»."""
        w, h = int(v.get("width", 0) or 0), int(v.get("height", 0) or 0)
        if h and not w:
            w = J._even(h * 16 / 9)
        cur = f"{w}x{h}" if w and h else "0x0"
        b = self.v_res
        b.blockSignals(True)
        b.clear()
        b.addItem(t("as_source"), "0x0")
        b.insertSeparator(b.count())
        for rw, rh in J.RESOLUTIONS:
            b.addItem(t(f"res_{rw}x{rh}"), f"{rw}x{rh}")
        if b.findData(cur) < 0:
            b.addItem(f"{w}×{h}", cur)
        b.insertSeparator(b.count())
        b.addItem(t("res_own"), "custom")
        b.setCurrentIndex(max(0, b.findData(cur)))
        b.blockSignals(False)

    def _res_picked(self):
        data = self.v_res.currentData()
        if self._loading or not data:
            return
        if data == "custom":
            self._load_settings()                     # back to the current entry until a size is typed
            self.v_w.setFocus()
            self.v_w.selectAll()
            return
        w, h = (int(x) for x in data.split("x"))
        self._put("video", "width", w)
        self._set("video", "height", h)

    def _res_typed(self):
        if self._loading:
            return
        self._put("video", "width", int(self.v_w.value()))
        self._set("video", "height", int(self.v_hh.value()))

    def _put(self, section: str, key: str, value):
        """Write one value like _set, without the refresh (the next _set does it)."""
        if self._loading:
            return
        jobs = self.selected_jobs(editable=True)
        targets = [j.settings for j in jobs]
        if not jobs and not self.selected_jobs():
            if not self.cfg.get("defaults"):
                self.cfg["defaults"] = J.merged(self.new_settings())
            targets = [self.cfg["defaults"]]
        for st in targets:
            st.setdefault(section, {})[key] = value

    def _fill_encoders(self):
        cur = self.v_enc.currentData()
        st = self._shown_settings()
        cont = E.container(st.get("container", "mkv"))
        fmt = FM.of(st)
        self.v_enc.blockSignals(True)
        self.v_enc.clear()
        self.v_enc.setMaxVisibleItems(24)
        model = self.v_enc.model()

        def off(why: str):
            item = model.item(self.v_enc.count() - 1)
            item.setEnabled(False)
            item.setToolTip(why)
        self.v_enc.addItem(t("v_copy_long"), "copy")
        self.v_enc.insertSeparator(self.v_enc.count())
        for fam in E.FAMILIES:
            e = self.av.resolve("auto_" + fam) if self.av_ready else None
            name = E.FAMILY_LABEL[fam] + " — " + t("enc_auto") + (f" ({self._vendor(e)})" if e else "")
            self.v_enc.addItem(name, "auto_" + fam)
            if not cont.takes_video(fam):
                off(t("not_in_container", cont.id.upper()))
            elif not FM.takes_video(st, fam):
                off(t("not_in_format", fmt.get("name", "")))
        self.v_enc.insertSeparator(self.v_enc.count())
        last = ""
        for e in E.VIDEO:
            if e.family in ("vp9", "mpeg4") and last not in ("vp9", "mpeg4"):
                self.v_enc.insertSeparator(self.v_enc.count())
            last = e.family
            self.v_enc.addItem(f"{E.FAMILY_LABEL[e.family]} — {self._vendor(e)}", e.id)
            if self.av_ready and not self.av.ok.get(e.id):
                off(t("enc_missing") + (": " + self.av.errors[e.id] if e.id in self.av.errors else ""))
            elif not cont.takes_video(e.family):
                off(t("not_in_container", cont.id.upper()))
            elif not FM.takes_video(st, e.family):
                off(t("not_in_format", fmt.get("name", "")))
        i = self.v_enc.findData(cur)
        self.v_enc.setCurrentIndex(max(0, i))
        self.v_enc.blockSignals(False)

    @staticmethod
    def _vendor(e: E.VEnc) -> str:
        return E.VENDOR_LABEL[e.vendor] if e.hw else E.CPU_LABEL.get(e.id, e.ffname) + " (" + t("cpu") + ")"

    def _enc_changed(self):
        eid = self.v_enc.currentData()
        if not eid or self._loading:
            return
        if eid == "copy":
            self._set("video", "mode", "copy")
            return
        self._put("video", "mode", "encode")
        self._put("video", "quality", 0)                  # the new encoder's own default
        self._set("video", "encoder", eid)
    def _enc_hint(self, enc: E.VEnc | None, v: dict, cont: E.Container | None = None):
        self.codec_hint.setText("")
        if cont is not None and cont.audio_only:
            self.v_enc_hint.setText(t("audio_only_hint"))
            return
        if enc is None:
            self.v_enc_hint.setText(t("copy_hint") if v.get("mode") == "copy" else
                                    t("v_none_hint") if v.get("mode") == "none" else "")
            return
        if not self.av_ready:
            self.v_enc_hint.setText(t("enc_checking"))
            return
        if self.av.resolve(v.get("encoder", "auto_hevc")) is None:
            self.v_enc_hint.setText("⚠ " + t("enc_none_here"))
            return
        txt = t("enc_gpu") if enc.hw else t("enc_cpu")
        if enc.id in ("gif", "prores", "xvid", "mpeg4", "mpeg2", "vp9"):
            txt = t("enc_hint_" + enc.id)
        jobs = self.selected_jobs()
        src = jobs[0].info.video if jobs and jobs[0].info else None
        if src is not None and src.hdr and 10 not in enc.bits:
            txt += "\n⚠ " + t("w_hdr_8bit")
        elif src is not None and src.hdr == "Dolby Vision" and enc.id not in ("x265", "svtav1"):
            txt += "\n⚠ " + t("w_dv_hw" if enc.hw else "w_dv")
        self.v_enc_hint.setText(txt)
        self.codec_hint.setText(txt)

    def _speed_label(self):
        x = self.v_speed.value()
        self.v_speed_lab.setText(t("speed_" + ("fast" if x <= 2 else "slow" if x >= 6 else "mid")) + f" ({x})")

    def _show_info(self, j: J.Job | None):
        if j is None:
            self.i_name.setText(t("info_none"))
            self.i_text.setText(t("info_drop"))
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
        if j.joins:                                                   # 1.4
            rows.append(f"<b>{t('i_joined')}</b>: {len(j.joins) + 1} · {P.fmt_time(J.src_duration(j))}")
            for pth, inf in J.sources(j):
                d = "…" if inf is None else ("⚠ " + _esc(inf.error)) if inf.error else P.fmt_time(inf.duration)
                rows.append(f"&nbsp;&nbsp;⧉ {_esc(os.path.basename(pth))} <span style='color:{style.MUTED}'>· "
                            f"{d}</span>")
        segs = J.segments(j)
        if segs:
            rows.append(f"<b>{t('i_trim')}</b>: " + t("trm_total", J.fmt_tc(J.out_duration(j), False),
                                                     P.fmt_time(J.src_duration(j)), len(segs)))
        if j.ext:
            rows.append(f"<b>{t('ext_tracks')}</b>: " + ", ".join(
                _esc(os.path.basename(e["path"])) + (f" ({e['lang']})" if e.get("lang") else "") +
                (f" {int(e['delay']):+d} {t('ms')}" if e.get("delay") else "") for e in j.ext))
        edits = [t(k) for k, on in (("i_meta_edited", j.meta.get("tags") is not None or bool(j.meta.get("streams"))),
                                    ("i_chap_edited", j.chapters is not None)) if on]
        if edits:
            rows.append(f"<span style='color:{style.MUTED}'>✎ " + ", ".join(edits) + "</span>")
        fl = self._filters_on(j)
        if fl:
            rows.append(f"<b>{t('f_section')}</b>: " + _esc(", ".join(fl)))
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
            if len(j.parts) > 1:
                rows.append(t("parts_n", len(j.parts)) + ": " + ", ".join(_esc(os.path.basename(x)) for x in j.parts))
            if j.metrics:
                rows.append(f"<b>{t('st_metrics')}</b>: " + " · ".join(
                    f"{k.upper()} {v:.2f}" + (" dB" if k == "psnr" else "") if k != "ssim" else f"SSIM {v:.4f}"
                    for k, v in j.metrics.items()))
        elif j.status == "error" and j.error:
            rows.append(f"<br><span style='color:{style.ERR}'>{_esc(j.error)}</span>")
        self.i_text.setText("<br>".join(rows))
        self._show_thumb(j)

    def _show_thumb(self, j: J.Job):
        """The card's picture: asked once per task (a frame at 12 % of the film)."""
        if j.uid not in self.thumbs and j.info is not None and j.info.video is not None and not j.info.error:
            self.thumbs[j.uid] = QPixmap()
            self.worker.thumb(j.uid, j.src, max(0.0, j.info.duration * 0.12))
    # ── output / priority ─────────────────────────────────────
    def _out_mode_changed(self):
        mode = self.o_mode.currentData()
        self.cfg["out_mode"] = mode
        if mode == "folder" and not self.cfg.get("out_dir"):
            if not self._pick_out_dir():
                self.o_mode.setCurrentIndex(0)
                return
        d = self.cfg.get("out_dir", "")
        self.o_dir.setText(_elide_path(d, 60) if mode == "folder" else "")
        self.o_dir.setVisible(mode == "folder")
        self.o_dir.setToolTip(d)
        self.o_pick.setVisible(mode == "folder")
        config.save(self.cfg)
        self._show_out_path()

    def _show_out_path(self):
        """The result's path of the selected task (Streams → Save files)."""
        if not hasattr(self, "o_path"):
            return
        jobs = self.selected_jobs()
        if len(jobs) == 1:
            j = jobs[0]
            p = j.out if j.status in ("done", "run", "pause") and j.out else J.out_name(j, self.cfg)
            self.o_path.setText(p)
            self.o_path.setEnabled(j.status in EDITABLE and not self._busy(j))
        else:
            self.o_path.setText("")
            self.o_path.setEnabled(False)

    def _out_path_typed(self):
        jobs = self.selected_jobs(editable=True)
        text = self.o_path.text().strip()
        if len(jobs) != 1 or not text:
            return
        j = jobs[0]
        if os.path.normcase(text) != os.path.normcase(J.out_name(j, self.cfg)):
            j.dest = text
            self._refresh(j)
            self._queue_changed()
        self._show_out_path()

    def save_as(self):
        """«Save as»: where the selected task's result goes (its extension follows the container)."""
        jobs = self.selected_jobs(editable=True)
        if len(jobs) != 1:
            self._toast(t("pick_one_edit"))
            return
        j = jobs[0]
        c = E.container(j.settings.get("container", "mkv"))
        path, _ = QFileDialog.getSaveFileName(self, t("save_as"), J.out_name(j, self.cfg),
                                              f"{c.label} (*.{J.out_ext(j.settings)});;{t('all_files')} (*)")
        if not path:
            return
        j.dest = path
        self._refresh(j)
        self._queue_changed()
        self._show_out_path()
    def _pick_out_dir(self) -> bool:
        d = QFileDialog.getExistingDirectory(self, t("out_pick"), self.cfg.get("out_dir") or self.cfg.get("last_dir", ""))
        if not d:
            return False
        self.cfg["out_dir"] = d
        self.o_dir.setText(_elide_path(d, 60))
        self.o_dir.setToolTip(d)
        config.save(self.cfg)
        self._show_out_path()
        return True
    def _prio_changed(self):
        self.cfg["priority"] = self.prio.currentData()
        for s in self.slots:
            if s.runner.running:
                s.runner.set_priority(self.cfg["priority"])

    # ── running ───────────────────────────────────────────────
    def start_queue(self):
        if self._any_running():
            if self._all_paused():
                self.toggle_queue_pause()
            if not self.running:
                self.running = True
                self._next()
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
        self.show_side("status")
        self._next()

    def _free_slot(self) -> Slot | None:
        n = int(self.cfg.get("parallel", 1))
        while len(self.slots) < n:
            self.slots.append(Slot(self, len(self.slots)))
        return next((s for s in self.slots[:n] if not s.busy and not s.runner.running), None)

    def _next(self):
        """Fill every free place (Settings: N tasks at the same time) with the next waiting task."""
        while self.running:
            slot = self._free_slot()
            if slot is None:
                return
            j = next((x for x in self.jobs if x.status == "wait" and not self._busy(x)), None)
            if j is None:
                if any(x.status == "probe" for x in self.jobs) or self._busy_slots():
                    return                                # _on_probed / _on_finished call us again
                self._queue_done()
                return
            if any(j.infos.get(p, 0) is None for p in list(j.joins) + [e["path"] for e in j.ext]):
                return                                    # its other files are being probed: _on_extra
            if self._want_crop(j):
                self.st_name.setText(f"{j.name}: {t('f_detecting')}")
                return                                    # _on_crop calls us again
            if not self._start(slot, j):
                continue

    def _start(self, slot: Slot, j: J.Job) -> bool:
        out = J.out_name(j, self.cfg, taken={s.job.out for s in self._busy_slots() if s.job.out})
        exe = ff.ffmpeg() or "ffmpeg"
        hw = bool(self.cfg.get("hw_decode", True))
        n = self._threads()
        frame = J.work_file(j, "frame", None, "png") if self.cfg.get("show_frame", True) else ""
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            args = J.build_args(j, exe, out, self.av, hw, threads=n, preview=frame)
            pre = J.pre_passes(j, exe, out, self.av, hw, threads=n, preview=frame)
            post = J.post_passes(j, exe, out, self.av, self.cfg.get("metrics", []), threads=n)
        except (RuntimeError, OSError) as e:
            j.status, j.error = "error", t(str(e)) if str(e) in BUILD_ERRORS else str(e)
            self._refresh(j)
            return False
        if not ff.ffmpeg():
            self.running = False
            QMessageBox.critical(self, APP_TITLE, t("no_ffmpeg"))
            return True
        try:
            os.remove(frame)
        except OSError:
            pass
        slot.job, slot.stage, slot.stat, slot.log = j, "main", {}, []
        slot.kinds, slot.peak, slot._mem_at = [], 0, 0.0
        slot.started = time.time()
        j.status, j.out, j.progress, j.error, j.out_size, j.metrics, j.parts = "run", out, 0.0, "", 0, {}, []
        self._refresh(j)
        if self._shown_slot() is slot:
            self._show_slot(slot)
        kinds = list(getattr(j, "pre_kinds", None) or [])           # 1.8: as pre_passes named them
        if len(kinds) != len(pre):
            kinds = (["vidstab"] if J.needs_vidstab(j) else [])
            kinds += ["pass1"] * (len(pre) - len(kinds))
        kinds += ["main"] + ["metrics"] * len(post)
        ch = self.cfg.get("chunks") or {}
        from . import chunks as CH
        why = CH.why_not(j, self.av) if ch.get("on") else "off"
        if not why and J.out_duration(j) > float(ch.get("minutes", 3) or 3) * 60 * 1.5:
            slot.runner = slot.chunked                     # 1.8: in chunks, several at a time
            slot.chunked.start_job(j, exe, out, self.av, hw, workers=int(ch.get("workers", 0) or 0),
                                   minutes=float(ch.get("minutes", 3) or 3), priority=self.cfg.get("priority", "below"),
                                   post=post, cores=self._cores(slot), threads=n)
        else:
            if why not in ("", "off"):
                self.log.appendPlainText("ℹ " + t("chk_not", t(why)))
            slot.runner = slot.plain
            slot.runner.start(args, out, J.out_duration(j), self.cfg.get("priority", "below"), pre, post,
                              self._cores(slot), kinds=kinds)
        self._update_buttons()
        self._queue_changed()
        return True

    def _show_slot(self, slot: Slot):
        j = slot.job
        if j is None:
            return
        self.st_name.setText(f"{j.name}  →  {os.path.basename(j.out)}" +
                             (f"  ·  {t('st_' + slot.runner.kind)}" if slot.runner.kind != "main" else ""))
        self.st["encoder"].setText(self._enc_text(j))
        self._frame_mtime = 0.0
        if slot.stat:
            self._on_progress(slot.stat, slot)

    def _enc_text(self, j: J.Job) -> str:
        if j.enc_used == "copy":
            return t("v_copy")
        e = E.BY_ID.get(j.enc_used)
        return f"{e.ffname} · {self._vendor(e)}" if e else "—"

    def _on_progress(self, d: dict, slot: Slot | None = None):
        slot = slot or self.slots[0]
        j = slot.job
        now = time.monotonic()
        if now - slot._mem_at >= 1.0:                     # 1.6: the peak memory of ffmpeg, for the log
            slot._mem_at = now
            rss = 0
            for pid in _pids(slot.runner):                # 1.8: in chunks, all its ffmpeg together
                try:
                    import psutil
                    rss += psutil.Process(int(pid)).memory_info().rss
                except Exception:                         # noqa: BLE001
                    pass
            slot.peak = max(slot.peak, rss)
        if j is None:
            return
        slot.stat = d
        self._last_stat = d
        if slot.stage == "main":
            j.progress = d["frac"] * (0.97 if J.split_on(j) else 1.0)
            j.out_size = d["size"] if slot.runner.kind == "main" or d.get("chunks") else j.out_size
        else:
            j.progress = 0.97 + 0.03 * d["frac"]
        self._refresh(j)
        if self._shown_slot() is not slot:
            self._update_total()
            return
        kind = slot.runner.kind if slot.stage == "main" else "split"
        name = f"{j.name}  →  {os.path.basename(j.out)}" + (f"  ·  {t('st_' + kind)}" if kind != "main" else "")
        if d.get("chunks"):                               # 1.8: «chunks 7 / 20 · 4 at a time»
            name = f"{j.name}  →  {os.path.basename(j.out)}  ·  {t('chk_progress', d['chunks'], d.get('workers', 0))}"
        if self.st_name.text() != name:
            self.st_name.setText(name)
        self.st_bar.setValue(int(d["frac"] * 1000))
        self.st_bar.setFormat(f"{d['frac'] * 100:.1f}%  ·  {P.fmt_time(d['out_time'])} / "
                              f"{P.fmt_time(J.out_duration(j))}")
        self.st["elapsed"].setText(P.fmt_time(d["elapsed"]))
        self.st["eta"].setText(P.fmt_time(d["eta"]) if d["eta"] else "—")
        self.st["speed"].setText(f"{d['speed']:.2f}×" if d["speed"] else "—")
        self.st["fps"].setText(f"{d['fps']:.1f}" if d["fps"] else "—")
        br = d["bitrate"]
        self.st["bitrate"].setText(br.replace("kbits/s", " kbit/s") if br and br != "N/A" else "—")
        if d["size"] and (kind == "main" or d.get("chunks")):
            est = d["size"] / d["frac"] if d["frac"] > 0.02 else 0
            self.st["size"].setText(P.fmt_size(d["size"]) + (f"  (≈ {P.fmt_size(est)})" if est else ""))
        self._update_total()

    def _on_log(self, line: str, slot: Slot | None = None):
        slot = slot or self.slots[0]
        from .xlog import NOISE
        if NOISE.search(line):                               # 1.6: verbose chatter goes to the log file only
            slot.log.append(line)
            return
        many = len(self._busy_slots()) > 1
        if many and slot.job is not None:
            self.log.appendPlainText(f"[{self.jobs.index(slot.job) + 1 if slot.job in self.jobs else '?'}] {line}")
        else:
            self.log.appendPlainText(line)
        if line.startswith("$ "):
            slot.kinds.append((slot.runner.kind, time.time()))
        slot.log.append(line)

    def _on_finished(self, ok: bool, err: str, slot: Slot | None = None):
        slot = slot or self.slots[0]
        j = slot.job
        if j is None:
            return
        if slot.stage == "main":
            if ok:
                j.metrics = J.parse_metrics(slot.log)
                if slot.runner.post_failed and self.cfg.get("metrics"):
                    self.log.appendPlainText("⚠ " + t("m_failed"))
            if ok and J.split_on(j) and os.path.isfile(j.out):
                self._split(slot)
                return
        elif slot.stage == "split":
            if ok:
                parts = J.split_parts(j.out)
                if parts:
                    try:
                        os.remove(j.out)
                    except OSError:
                        pass
                    j.parts = parts
                    j.out = parts[0]
            else:
                for f in J.split_parts(j.out):                # a broken split: the whole file stays
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                self.log.appendPlainText("⚠ " + t("split_failed") + (": " + err if err else ""))
                ok, err = True, ""
        J.cleanup(j)
        slot.job = None
        j.elapsed = time.time() - slot.started if slot.started else slot.runner.elapsed()
        if ok:
            j.status, j.progress = "done", 1.0
            files = j.parts or [j.out]
            j.out_size = sum(os.path.getsize(f) for f in files if os.path.isfile(f))
            extra = "".join(f", {k.upper()} {v:.4g}" for k, v in j.metrics.items())
            self.log.appendPlainText(f"✓ {j.out} — {P.fmt_size(j.out_size)}, {P.fmt_time(j.elapsed)}" + extra +
                                     (f" · {t('parts_n', len(j.parts))}" if len(j.parts) > 1 else ""))
        elif slot.runner.cancelled:
            j.status, j.progress, j.out = "stop", 0.0, ""
        else:
            j.status, j.error, j.out = "error", err, ""
            self.log.appendPlainText("✗ " + err)
        self._save_log(j, slot)
        self._refresh(j)
        sel = self.selected_jobs()
        if sel and sel[0] is j:
            self._select_changed()
        self._show_metrics(j)
        nxt = self._shown_slot()
        if nxt is not None:
            self._show_slot(nxt)
        else:
            self.st_bar.setValue(1000 if ok else 0)
        self._update_buttons()
        self._queue_changed()
        if self.running:
            QTimer.singleShot(200, self._next)

    def _split(self, slot: Slot):
        """1.4: the finished file into parts — by time at once, by size after reading its packets."""
        j = slot.job
        slot.stage = "split"
        sp = j.settings.get("split") or {}
        self.log.appendPlainText("✂ " + t("split_running"))
        if sp.get("mode") != "size":
            self._on_split_times(slot.n, None)
            return
        path, mb = j.out, float(sp.get("size_mb", 700) or 700)

        def work():
            try:
                self.bridge.split.emit(slot.n, J.size_times(ff.ffprobe() or "ffprobe", path, mb))
            except Exception as e:                          # noqa: BLE001
                self.bridge.split.emit(slot.n, str(e))
        threading.Thread(target=work, daemon=True).start()

    def _on_split_times(self, n: int, times):
        slot = self.slots[n]
        j = slot.job
        if j is None or slot.stage != "split":
            return
        if isinstance(times, str):
            self._on_finished(False, times, slot)
            return
        if isinstance(times, list) and not times:            # small enough already: one part
            self._on_finished(True, "", slot)
            return
        args = J.split_args(j, ff.ffmpeg() or "ffmpeg", j.out, times)
        slot.runner = slot.plain
        slot.runner.start(args, J.split_pattern(j.out), J.out_duration(j), self.cfg.get("priority", "below"),
                          cores=self._cores(slot), direct=True, kinds=["split"])

    def _save_log(self, j: J.Job, slot: Slot | None = None):
        """The conversion's log file (Settings → keep logs): what, into what, the result, all of ffmpeg."""
        lines = slot.log if slot is not None else []
        if slot is not None:
            slot.log = []
        if not self.cfg.get("save_logs", True):
            return
        cmd = next((x[2:] for x in reversed(lines) if x.startswith("$ ")), "")
        profile = J.describe(j.settings, self.av if self.av_ready else None, t)
        started = (slot.started if slot else 0) or (time.time() - (j.elapsed or 0))
        text = ""
        try:                                              # 1.6: the log in sections
            from . import xlog
            text = xlog.build(j, J, lines, slot.kinds if slot else [], started=started, ended=time.time(),
                              profile=profile, peak_mem=slot.peak if slot else 0,
                              out_path=slot.runner.out if slot else "", tr=t)
        except Exception as e:                            # noqa: BLE001 — fall back to the plain log
            import traceback
            self.log.appendPlainText(f"⚠ log: {e}")
            if os.environ.get("ARTCORE_LOG_DEBUG"):
                traceback.print_exc()
        path = logs.save(j.src, j.out, j.status, j.error, j.elapsed, j.out_size, profile, cmd,
                         lines, j.settings, bool(self.cfg.get("log_next_to_output")), started, text=text)
        if path:
            j.log_path = path
            lw = getattr(self, "_logwin", None)
            try:
                if lw is not None and lw.isVisible():
                    lw.reload()
            except RuntimeError:
                pass

    def open_log(self, j: J.Job | None):
        """1.7: in the log window, not in Notepad."""
        if j is None or not j.log_path or not os.path.isfile(j.log_path):
            return
        self.history_dialog(j.log_path)

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
        """Space: the preview plays / stops while it is shown, otherwise the encoding pauses."""
        if self.center.currentIndex() == 0 and self.trim_tab.preview is not None and self.trim_tab.job is not None:
            self.trim_tab.toggle_play()
            return
        self.toggle_queue_pause()

    def toggle_queue_pause(self):
        run = [s for s in self.slots if s.runner.running]
        if not run:
            return
        on = not self._all_paused()
        for s in run:
            if s.runner.pause(on) and s.job is not None:
                s.job.status = "pause" if on else "run"
                self._refresh(s.job)
        self._update_buttons()
        self._update_total()

    def stop_queue(self):
        self.running = False
        for s in self.slots:
            if s.runner.running:
                s.runner.cancel()
        self._update_buttons()

    def _update_buttons(self):
        busy = self._any_running()
        paused = self._all_paused()
        self.b_start.setEnabled(not busy or paused or not self.running)
        self.b_start.setText("  " + (t("resume") if busy and paused else t("start")))
        self.b_pause.setEnabled(busy)
        self.b_pause.setIcon(style.icon("fa6s.play" if paused else "fa6s.pause", style.ICON))
        self.b_pause.setToolTip((t("resume") if paused else t("pause")) + " (Space)")
        self.b_stop.setEnabled(busy or self.running)
        has_sel = bool(self.view.selectedItems())
        for b in (self.b_remove, self.b_up, self.b_down):
            b.setEnabled(has_sel)
        self.b_clean.setEnabled(any(j.status in ("done", "stop", "skip") for j in self.jobs))

    def _update_total(self):
        todo = [j for j in self.jobs if j.status in ("wait", "probe", "run", "pause", "done") and j.info is not None]
        if not todo or not (self.running or self._any_running()):
            self.total.setValue(0)
            self.total.setFormat("")
            self.setWindowTitle(APP_TITLE)
            return
        w = [max(1.0, J.out_duration(j)) for j in todo]
        done = sum(wi * (j.progress if j.status in ("run", "pause", "done") else 0.0) for wi, j in zip(w, todo))
        frac = done / sum(w)
        n_done = sum(1 for j in todo if j.status == "done")
        self.total.setValue(int(frac * 1000))
        self.total.setFormat(t("total_fmt", n_done + len(self._busy_slots()), len(todo), f"{frac * 100:.0f}"))
        paused = self._all_paused()
        self.taskbar.set(int(self.winId()), frac, winint.TaskbarProgress.PAUSED if paused else winint.TaskbarProgress.NORMAL)
        self.setWindowTitle(f"{frac * 100:.0f}% — {APP_TITLE}" + (f" ({t('s_pause')})" if paused else ""))

    # ── system usage ──────────────────────────────────────────
    def _sys_tick(self):
        if self.side_bar.currentIndex() != 5 or not self.isVisible():
            return
        try:
            import psutil
            self.st["cpu"].setText(f"{psutil.cpu_percent(None):.0f}%")
            vm = psutil.virtual_memory()
            self.st["ram"].setText(f"{P.fmt_size(vm.used)} / {P.fmt_size(vm.total)}")
            rss = 0
            for sl in self.slots:
                for pid in _pids(sl.runner):
                    try:
                        rss += psutil.Process(int(pid)).memory_info().rss
                    except Exception:
                        pass
            self.st["ff_mem"].setText(P.fmt_size(rss) if rss else "—")
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
        if not self._gpu_busy and (self._any_running() or not self._gpu):
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
        self._show_metrics(None)
        if self.running:
            self._next()

    # ── menu, settings, updates ───────────────────────────────
    def settings_dialog(self):
        from .dialogs import SettingsDialog
        lang, theme = self.cfg.get("language"), (self.cfg.get("theme", "dark"), self.cfg.get("look", "classic"))
        if SettingsDialog(self.cfg, self).exec():
            config.save(self.cfg)
            for j in self.jobs:
                self._refresh(j)
            if lang != self.cfg.get("language") or theme != (self.cfg.get("theme", "dark"), self.cfg.get("look", "classic")):
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
        if self._any_running() and QMessageBox.question(self, APP_TITLE, t("upd_busy")) != QMessageBox.Yes:
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
        if isinstance(sp, list) and len(sp) == 3 and all(isinstance(x, int) and x > 100 for x in sp):
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
            uid = 0                                    # 1.6: dropped on the picture → onto the shown film
            pv = self.center.widget(0)
            at = pv.mapFrom(self, e.position().toPoint())
            if self.center.currentIndex() == 0 and pv.rect().contains(at):
                sel = self.selected_jobs()
                uid = sel[0].uid if len(sel) == 1 else 0
            before = sum(len(j.ext) for j in self.jobs)
            n = self.add_incoming(paths, uid)
            if n or sum(len(j.ext) for j in self.jobs) == before:
                self._toast(t("added_n", n) if n else t("added_none"))

    def closeEvent(self, e):
        if self._any_running() and not getattr(self, "_installer", "") and not self._quitting:
            if QMessageBox.question(self, APP_TITLE, t("ask_quit_busy")) != QMessageBox.Yes:
                e.ignore()
                return
        self._quitting = True
        self.running = False
        if self._any_running():
            for sl in self.slots:
                if sl.runner.running:
                    sl.runner.cancel()
            t0 = time.monotonic()
            while any(sl.runner.running for sl in self.slots) and time.monotonic() - t0 < 5:
                QApplication.processEvents()
                time.sleep(0.02)
        self.save_queue()
        self.cfg["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.cfg["splitter"] = self.split.sizes()
        config.save(self.cfg)
        self.worker.stop()
        self.sys_timer.stop()
        self.trim_tab.shutdown()
        self.taskbar.set(int(self.winId()), None)
        installer = getattr(self, "_installer", "")
        if installer:
            from . import updater
            winint.release_mutex()
            updater.run_installer(installer)
        super().closeEvent(e)
        QApplication.instance().quit()


class _Shim:
    """Settings without a task, where a filter function wants a job."""

    def __init__(self, st: dict):
        self.settings, self.info, self.crop_found = st, None, None


def _pids(runner) -> list[int]:
    """The ffmpeg processes of a runner (1.8: a task in chunks has several)."""
    if hasattr(runner, "pids"):
        return runner.pids()
    p = runner.proc
    return [int(p.processId())] if p is not None and p.processId() else []


def _lang_from_name(path: str, src: str) -> str:
    """«Film.rus.ac3», «Film_eng.srt», «Film.Russian.srt» → the language code (ISO 639-2), else ""."""
    import re as _re
    from .edit_tabs import LANGS
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    base = os.path.splitext(os.path.basename(src))[0].lower()
    if stem.startswith(base):
        stem = stem[len(base):]
    words = [w for w in _re.split(r"[^a-zа-яё]+", stem) if w]
    names = {"russian": "rus", "english": "eng", "bulgarian": "bul", "german": "deu", "french": "fra",
             "ukrainian": "ukr", "spanish": "spa", "italian": "ita", "japanese": "jpn", "русский": "rus",
             "английский": "eng", "ru": "rus", "en": "eng", "bg": "bul", "de": "deu", "fr": "fra", "uk": "ukr"}
    for w in reversed(words):
        if w in LANGS and w != "und":
            return w
        if w in names:
            return names[w]
    return ""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _elide_path(p: str, n: int) -> str:
    return p if len(p) <= n else "…" + p[-(n - 1):]
