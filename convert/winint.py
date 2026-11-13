"""Windows bits: dark title bar, taskbar id, the installer mutex, a progress bar on the taskbar button
(ITaskbarList3), ffmpeg's process priority, sleep / shut down after the queue. A no-op elsewhere."""
from __future__ import annotations

import os
import subprocess
import sys

IS_WIN = sys.platform == "win32"
MUTEX_NAME = "ArtCoreConvertMutex"     # same as AppMutex in setup.iss
NO_WINDOW = 0x08000000                 # CREATE_NO_WINDOW: ffmpeg / ffprobe without a console flashing up
_mutex = None


def popen_flags() -> dict:
    return {"creationflags": NO_WINDOW} if IS_WIN else {}


def init_process(app_id: str) -> None:
    if not IS_WIN:
        return
    import ctypes
    global _mutex
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass
    try:
        _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    except Exception:
        pass


def release_mutex() -> None:
    """Let the installer run (it waits for AppMutex) — before a self-update."""
    global _mutex
    if IS_WIN and _mutex:
        import ctypes
        try:
            ctypes.windll.kernel32.CloseHandle(_mutex)
        except Exception:
            pass
        _mutex = None


def dark_title_bar(win_id: int, dark: bool = True) -> None:
    """DWMWA_USE_IMMERSIVE_DARK_MODE (20; 19 on early Windows 10 builds)."""
    if not IS_WIN:
        return
    import ctypes
    val = ctypes.c_int(1 if dark else 0)
    for attr in (20, 19):
        try:
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(win_id, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        except Exception:
            pass


def show_in_explorer(path: str) -> None:
    if IS_WIN:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(os.path.normpath(path if os.path.isdir(path) else os.path.dirname(path)))
    else:
        target = path if os.path.isdir(path) else os.path.dirname(path)
        try:
            subprocess.Popen(["xdg-open", target])
        except OSError:
            pass


def set_priority(pid: int, level: str) -> bool:
    """idle | below | normal | above, for the running ffmpeg (psutil; nice values outside Windows)."""
    try:
        import psutil
        p = psutil.Process(pid)
        if IS_WIN:
            p.nice({"idle": psutil.IDLE_PRIORITY_CLASS, "below": psutil.BELOW_NORMAL_PRIORITY_CLASS,
                    "normal": psutil.NORMAL_PRIORITY_CLASS, "above": psutil.ABOVE_NORMAL_PRIORITY_CLASS}[level])
        else:
            want = {"idle": 19, "below": 10, "normal": 0, "above": 0}[level]
            if want >= p.nice():
                p.nice(want)
        return True
    except Exception:
        return False


def suspend(pid: int, on: bool) -> bool:
    """Pause / resume ffmpeg (NtSuspendProcess on Windows, SIGSTOP elsewhere) — the encoders just wait."""
    try:
        import psutil
        p = psutil.Process(pid)
        p.suspend() if on else p.resume()
        return True
    except Exception:
        return False


def power(action: str) -> None:
    """After the queue: sleep or shut down (a minute's warning, cancelled by shutdown /a)."""
    if not IS_WIN:
        return
    if action == "shutdown":
        subprocess.Popen(["shutdown", "/s", "/t", "60", "/c", "ArtCore Convert: the queue is done"], **popen_flags())
    elif action == "sleep":
        import ctypes
        try:
            ctypes.windll.powrprof.SetSuspendState(False, False, False)
        except Exception:
            pass


class TaskbarProgress:
    """ITaskbarList3::SetProgressValue / SetProgressState — the green bar on the taskbar button."""
    NOPROGRESS, INDETERMINATE, NORMAL, ERROR, PAUSED = 0, 1, 2, 4, 8

    def __init__(self):
        self.tb = None
        self._vt = None

    def _create(self) -> bool:
        if self.tb is not None:
            return True
        if not IS_WIN:
            return False
        import ctypes
        import uuid
        try:
            ole = ctypes.windll.ole32
            ole.CoInitialize(None)
            clsid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("56FDF344-FD6D-11d0-958A-006097C9A090").bytes_le)
            iid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF").bytes_le)
            ptr = ctypes.c_void_p()
            hr = ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr))
            if hr != 0 or not ptr.value:
                return False
            self.tb = ptr
            self._vt = ctypes.cast(ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0],
                                   ctypes.POINTER(ctypes.c_void_p))
            self._call(3, [])                       # HrInit
            return True
        except Exception:
            return False

    def _call(self, idx: int, args: list, argtypes: list | None = None) -> int:
        import ctypes
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *(argtypes or []))
        return proto(self._vt[idx])(self.tb, *args)

    def set(self, hwnd: int, frac: float | None, state: int = NORMAL) -> bool:
        """frac None = no bar; 0..1 = the bar in `state` (NORMAL green, PAUSED yellow, ERROR red)."""
        if not self._create():
            return False
        import ctypes
        try:
            h = ctypes.c_void_p(hwnd)
            if frac is None:
                return self._call(10, [h, self.NOPROGRESS], [ctypes.c_void_p, ctypes.c_int]) == 0
            self._call(10, [h, state], [ctypes.c_void_p, ctypes.c_int])                     # SetProgressState
            done = int(max(0.0, min(1.0, frac)) * 10000)
            return self._call(9, [h, ctypes.c_ulonglong(done), ctypes.c_ulonglong(10000)],   # SetProgressValue
                              [ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_ulonglong]) == 0
        except Exception:
            return False
