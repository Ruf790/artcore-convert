"""Runs one ffmpeg at a time (QProcess): reads -progress from stdout, keeps the log from stderr, pauses by
suspending the process, cancels by killing it and deleting the half-written file. The result is written to
<name>.part and renamed when ffmpeg finishes cleanly."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from . import winint


class Runner(QObject):
    progress = Signal(dict)          # {frac, out_time, fps, speed, bitrate, size, eta, elapsed, frame}
    log = Signal(str)                # stderr lines
    finished = Signal(bool, str)     # ok, error text ("" when ok or cancelled)

    LOG_KEEP = 4000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QProcess | None = None
        self.duration = 0.0
        self.out = ""
        self.part = ""
        self.paused = False
        self.cancelled = False
        self.started = 0.0
        self.paused_for = 0.0
        self._paused_at = 0.0
        self._buf_out = b""
        self._buf_err = b""
        self._stat: dict = {}
        self.tail: list[str] = []            # the last stderr lines: the error message when it fails
        self.priority = "below"

    @property
    def running(self) -> bool:
        return self.proc is not None

    def start(self, args: list[str], out: str, duration: float, priority: str = "below"):
        """args end with the output path; it is replaced by <out>.part here."""
        self.out = out
        self.part = out + ".part"
        args = list(args[:-1]) + [self.part]
        self.duration = max(0.0, duration)
        self.paused = self.cancelled = False
        self.paused_for = 0.0
        self.started = time.monotonic()
        self._buf_out = self._buf_err = b""
        self._stat = {}
        self.tail = []
        self.priority = priority
        p = QProcess(self)
        p.setProgram(args[0])
        p.setArguments(args[1:])
        p.readyReadStandardOutput.connect(self._read_out)
        p.readyReadStandardError.connect(self._read_err)
        p.finished.connect(self._done)
        p.errorOccurred.connect(self._error)
        self.proc = p
        self.log.emit("$ " + " ".join(_q(a) for a in args))
        p.start()
        QTimer.singleShot(300, self._apply_priority)

    def _apply_priority(self):
        if self.proc is not None and self.proc.processId():
            winint.set_priority(int(self.proc.processId()), self.priority)

    def set_priority(self, level: str):
        self.priority = level
        self._apply_priority()

    def pause(self, on: bool) -> bool:
        if self.proc is None or self.paused == on or not self.proc.processId():
            return False
        if winint.suspend(int(self.proc.processId()), on):
            self.paused = on
            if on:
                self._paused_at = time.monotonic()
            else:
                self.paused_for += time.monotonic() - self._paused_at
            return True
        return False

    def cancel(self):
        if self.proc is None:
            return
        self.cancelled = True
        if self.paused:
            winint.suspend(int(self.proc.processId()), False)
        self.proc.kill()

    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        now = self._paused_at if self.paused else time.monotonic()
        return max(0.0, now - self.started - self.paused_for)

    # ── reading ───────────────────────────────────────────────
    def _read_out(self):
        self._buf_out += bytes(self.proc.readAllStandardOutput())
        *lines, self._buf_out = self._buf_out.split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", "replace").strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            self._stat[k] = v
            if k == "progress":
                self._emit_progress(v == "end")

    def _emit_progress(self, end: bool):
        st = self._stat
        t = 0.0
        for key in ("out_time_us", "out_time_ms"):          # both are microseconds in ffmpeg
            try:
                t = int(st.get(key, "0")) / 1e6
                if t > 0:
                    break
            except ValueError:
                pass
        spd = _f(st.get("speed", "").rstrip("x"))
        frac = 1.0 if end else (min(0.999, t / self.duration) if self.duration > 0 else 0.0)
        eta = (self.duration - t) / spd if spd > 0 and self.duration > t else 0.0
        self.progress.emit({"frac": frac, "out_time": t, "fps": _f(st.get("fps")), "speed": spd,
                            "bitrate": st.get("bitrate", "").strip(), "size": int(_f(st.get("total_size"))),
                            "eta": eta, "elapsed": self.elapsed(), "frame": int(_f(st.get("frame")))})

    def _read_err(self):
        self._buf_err += bytes(self.proc.readAllStandardError())
        parts = self._buf_err.replace(b"\r", b"\n").split(b"\n")
        self._buf_err = parts.pop()
        for raw in parts:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.tail.append(line)
                if len(self.tail) > 40:
                    del self.tail[:-40]
                self.log.emit(line)

    def _error(self, err):
        if err == QProcess.FailedToStart and self.proc is not None:
            self.tail.append(f"ffmpeg did not start: {self.proc.errorString()}")
            self._done(-1, QProcess.CrashExit)

    def _done(self, code, status=None):
        p = self.proc
        if p is None:
            return
        if self._buf_err:
            self.tail.append(self._buf_err.decode("utf-8", "replace"))
            self._buf_err = b""
        self.proc = None
        p.deleteLater()
        ok = code == 0 and not self.cancelled
        err = ""
        if ok:
            try:
                os.replace(self.part, self.out)
            except OSError as e:
                ok, err = False, str(e)
        else:
            _remove(self.part)
            if not self.cancelled:
                err = _error_line(self.tail) or f"ffmpeg exit code {code}"
        self.finished.emit(ok, err)


def _error_line(tail: list[str]) -> str:
    """The most telling line of ffmpeg's last words."""
    keys = ("error", "invalid", "not supported", "cannot", "could not", "failed", "unknown", "no such", "unable",
            "unrecognized", "not found")
    skip = ("conversion failed", "error splitting the argument list", "exiting with exit code")
    for line in reversed(tail):
        low = line.lower()
        if any(k in low for k in keys) and not low.startswith(skip):
            return line.strip()
    return tail[-1].strip() if tail else ""


def _remove(path: str):
    for _ in range(10):
        try:
            if os.path.exists(path):
                os.remove(path)
            return
        except OSError:
            time.sleep(0.2)


def _f(v) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _q(a: str) -> str:
    return f'"{a}"' if (" " in a or not a) else a
