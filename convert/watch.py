"""2.0: watch folders — new video files that appear in a folder go into the queue and are encoded with that folder's
preset, into that folder's result folder (with the folders under it, or the folder alone).

A file is taken only when it is complete: its size and time have not changed for N seconds and no other program
has it open (Windows: opening it without sharing must work — a copy or a download in progress holds it). Files
done (or failed) are remembered by path, size and time in watched.json: the same file is not encoded twice, a new
file under the same name is. The results the program writes are remembered too, so a result folder inside the
watched one does not feed itself. Sources are never deleted or moved. The folders are looked at every few seconds
while the program is open (also minimised to the tray)."""
from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal

from . import VIDEO_EXT, config, winint

FOLDER_KEYS = {"path": "", "preset": "", "out": "", "sub": True, "on": True}
DEFAULTS = {"on": True, "stable": 10, "folders": []}
STORE = "watched.json"
SKIP_EXT = (".part", ".tmp", ".crdownload", ".!qb", ".!ut", ".download", ".partial")


def clean_folder(d: dict) -> dict:
    out = {k: type(v)(d.get(k, v)) if d.get(k) is not None else v for k, v in FOLDER_KEYS.items()}
    out["path"] = os.path.normpath(out["path"]) if out["path"] else ""
    out["out"] = os.path.normpath(out["out"]) if out["out"] else ""
    return out


def settings(cfg: dict) -> dict:
    w = cfg.get("watch") if isinstance(cfg.get("watch"), dict) else {}
    out = dict(DEFAULTS)
    out["on"] = bool(w.get("on", True))
    try:
        out["stable"] = max(2, min(600, int(w.get("stable", 10))))
    except (TypeError, ValueError):
        out["stable"] = 10
    out["folders"] = [clean_folder(f) for f in (w.get("folders") or []) if isinstance(f, dict) and f.get("path")]
    return out


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _inside(path: str, folder: str) -> bool:
    try:
        return bool(folder) and os.path.commonpath([_key(path), _key(folder)]) == _key(folder)
    except ValueError:
        return False


class Store:
    """watched.json: {"done": {path: [size, mtime]}} — files taken (encoded or failed) and the results made."""

    def __init__(self):
        data = config.read(STORE) or {}
        self.done: dict = data.get("done") if isinstance(data.get("done"), dict) else {}

    def has(self, path: str, size: int, mtime: float) -> bool:
        v = self.done.get(_key(path))
        return isinstance(v, list) and len(v) == 2 and int(v[0]) == int(size) and abs(float(v[1]) - mtime) < 2.0

    def add(self, path: str) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return
        self.done[_key(path)] = [int(st.st_size), float(st.st_mtime)]
        if len(self.done) > 20000:                        # the oldest go: a store that never stops growing is no use
            for k in list(self.done)[:5000]:
                self.done.pop(k, None)
        config.write(STORE, {"done": self.done})


def candidates(folder: dict, exclude=()) -> list[tuple[str, int, float]]:
    """The video files of a watch folder now: [(path, size, mtime)] — not inside its result folder (unless that is
    the folder itself) or the given folders, not half-written (.part …), not hidden."""
    root = folder.get("path", "")
    if not root or not os.path.isdir(root):
        return []
    skip = [x for x in ([folder.get("out", "")] + list(exclude)) if x and _key(x) != _key(root)]
    out = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = list(os.scandir(d))
        except OSError:
            continue
        for e in it:
            name = e.name
            if name.startswith((".", "~$")):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if folder.get("sub", True) and not any(_inside(e.path, x) for x in skip):
                        stack.append(e.path)
                    continue
                if not name.lower().endswith(VIDEO_EXT) or name.lower().endswith(SKIP_EXT):
                    continue
                st = e.stat()
                out.append((e.path, int(st.st_size), float(st.st_mtime)))
            except OSError:
                continue
    out.sort(key=lambda x: x[0].lower())
    return out


class Watcher(QObject):
    """Looks at the folders every few seconds (in a thread); found(path, folder) for every file ready to encode."""
    found = Signal(str, dict)
    _scanned = Signal(list)

    INTERVAL = 3000

    def __init__(self, cfg: dict, busy_paths=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.busy_paths = busy_paths or (lambda: set())   # files the queue has (not to be taken again)
        self.store = Store()
        self.seen: dict[str, tuple[int, float, float]] = {}   # path → (size, mtime, unchanged since)
        self.sent: set[str] = set()
        self.scanning = False
        self.timer = QTimer(self)
        self.timer.setInterval(self.INTERVAL)
        self.timer.timeout.connect(self.tick)
        self._scanned.connect(self._got)
        self.clock = time.monotonic                       # tests move the clock
        self.apply()

    def apply(self):
        """The settings changed: start, stop, forget what is no longer watched."""
        w = settings(self.cfg)
        on = w["on"] and any(f["on"] for f in w["folders"])
        if on and not self.timer.isActive():
            self.timer.start()
            QTimer.singleShot(200, self.tick)
        elif not on:
            self.timer.stop()

    @property
    def active(self) -> bool:
        return self.timer.isActive()

    def tick(self):
        if self.scanning:
            return
        w = settings(self.cfg)
        folders = [f for f in w["folders"] if f["on"]]
        if not w["on"] or not folders:
            return
        self.scanning = True
        outs = [f["out"] for f in w["folders"] if f["out"]]

        def work():
            res = []
            try:
                for f in folders:
                    for path, size, mtime in candidates(f, outs):
                        res.append((path, size, mtime, f))
            finally:
                self._scanned.emit(res)
        threading.Thread(target=work, daemon=True).start()

    def _got(self, res: list):
        self.scanning = False
        now = self.clock()
        stable = settings(self.cfg)["stable"]
        queued = {_key(p) for p in self.busy_paths()}
        alive = set()
        for path, size, mtime, folder in res:
            k = _key(path)
            alive.add(k)
            if k in self.sent or k in queued or self.store.has(path, size, mtime):
                continue
            was = self.seen.get(k)
            if was is None or was[0] != size or abs(was[1] - mtime) > 1e-6:
                self.seen[k] = (size, mtime, now)               # new or still growing: wait
                continue
            if now - was[2] < stable or size <= 0:
                continue
            if winint.file_busy(path):                          # still being copied / written
                self.seen[k] = (size, mtime, now)
                continue
            self.sent.add(k)
            self.found.emit(path, folder)
        for k in list(self.seen):
            if k not in alive:
                self.seen.pop(k, None)

    def done(self, path: str):
        """A file taken from a watch folder is finished (encoded or failed) — or a result was written."""
        self.store.add(path)
        self.sent.discard(_key(path))
