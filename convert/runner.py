"""Runs one ffmpeg at a time (QProcess): reads -progress from stdout, keeps the log from stderr, pauses by
suspending the process, cancels by killing it and deleting the half-written file. The result is written to
<name>.part and renamed when ffmpeg finishes cleanly. 1.3: analysis passes (the two-pass stabilisation) can
run first; the progress bar is shared out between the passes. 1.4: passes after it (the quality metrics, on
the finished file — their failure does not fail the task), the first pass of a two-pass encode, a CPU limit
(the cores ffmpeg may use), and direct mode (no .part: the split writes many files of its own)."""
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
        self._main: list[str] = []
        self._pre: list[list[str]] = []
        self.phase = 0
        self.phases = 1
        self._post: list[list[str]] = []
        self.post_failed = False
        self.cores: list[int] | None = None          # 1.4: the CPUs ffmpeg may run on (None = all)
        self.kinds: list[str] = []                   # the name of every pass (pre…, main, post…)
        self.direct = False

    @property
    def running(self) -> bool:
        return self.proc is not None

    def start(self, args: list[str], out: str, duration: float, priority: str = "below",
              pre: list[list[str]] | None = None, post: list[list[str]] | None = None,
              cores: list[int] | None = None, direct: bool = False, kinds: list[str] | None = None):
        """args hold the output path (its last place); it is replaced by <out>.part here (not when direct).
        pre: commands run before it, post: after it, on the finished file (none writes a file of the result)."""
        self.out = out
        self.direct = direct
        self.part = out if direct else out + ".part"
        main = list(args)
        if not direct:
            i = len(main) - 1 - main[::-1].index(out) if out in main else len(main) - 1
            main[i] = self.part
        self._main = main
        self._pre = [list(a) for a in (pre or [])]
        self._post = [list(a) for a in (post or [])]
        self.post_failed = False
        self.phases = len(self._pre) + 1 + len(self._post)
        self.kinds = list(kinds or []) or (["pre"] * len(self._pre) + ["main"] + ["post"] * len(self._post))
        self.cores = list(cores) if cores else None
        self.phase = 0
        self.duration = max(0.0, duration)
        self.paused = self.cancelled = False
        self.paused_for = 0.0
        self.started = time.monotonic()
        self.tail = []
        self.priority = priority
        self._launch(self._pre[0] if self._pre else self._main)

    @property
    def kind(self) -> str:
        return self.kinds[self.phase] if self.phase < len(self.kinds) else "main"

    def _cmd(self, phase: int) -> list[str]:
        n = len(self._pre)
        if phase < n:
            return self._pre[phase]
        if phase == n:
            return self._main
        return self._post[phase - n - 1]

    def _launch(self, args: list[str]):
        self._buf_out = self._buf_err = b""
        self._stat = {}
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
            self._apply_cores()

    def _apply_cores(self):
        if self.proc is None or not self.proc.processId():
            return
        try:
            import psutil
            pr = psutil.Process(int(self.proc.processId()))
            pr.cpu_affinity(self.cores or list(range(psutil.cpu_count() or 1)))
        except Exception:
            pass

    def set_cores(self, cores: list[int] | None):
        """1.4: the CPU limit changed while it runs: the process moves to these cores at once."""
        self.cores = list(cores) if cores else None
        self._apply_cores()

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
        if self.phases > 1:                                    # this pass's share of the bar
            frac = min(0.999, (self.phase + frac) / self.phases) if not (end and self.phase == self.phases - 1) else 1.0
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
        n = len(self._pre)
        if self.phase > n:                                     # a pass on the finished file
            if self.cancelled:
                self.finished.emit(True, "")                   # the file itself is done
                return
            if not ok:
                self.post_failed = True
                self.log.emit("⚠ " + (_error_line(self.tail) or f"ffmpeg exit code {code}"))
            if self.phase < self.phases - 1:
                self.phase += 1
                self._launch(self._cmd(self.phase))
                return
            self.finished.emit(True, "")
            return
        if ok and self.phase < n:                              # an analysis / first pass is done: the next one
            self.phase += 1
            self._launch(self._cmd(self.phase))
            return
        if ok:
            if not self.direct:
                try:
                    os.replace(self.part, self.out)
                except OSError as e:
                    ok, err = False, str(e)
            if ok and self.phase < self.phases - 1:            # the metrics on the finished file
                self.phase += 1
                self.tail = []
                self._launch(self._cmd(self.phase))
                return
        else:
            if not self.direct:
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
