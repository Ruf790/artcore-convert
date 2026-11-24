"""«Open in ArtCore Sound»: where the exe of ArtCore Sound (the ArtCore audio editor) is, and opening files in it.
Started with paths, ArtCore Sound opens them — in the copy that already runs, if there is one, else in a new one.

Where it looks, in this order: the path the user gave (cfg["sound_exe"]), the registry (the installer's
«Open with» entry Applications\\ArtCoreSound.exe and App Paths, for all users and for one), the usual install
folders. ARTCORE_SOUND_EXE overrides everything (tests)."""
from __future__ import annotations

import os
import subprocess
import sys

EXE = "ArtCoreSound.exe"
APP_DIR = "ArtCore Sound"
DOWNLOAD_URL = "https://github.com/Ruf790/artcore-sound/releases/latest"
KEYS = (r"Software\Classes\Applications\ArtCoreSound.exe\shell\open\command",
        r"Software\Microsoft\Windows\CurrentVersion\App Paths\ArtCoreSound.exe")


def command_exe(cmd: str) -> str:
    """The program of a registry command line: «"C:\\…\\ArtCoreSound.exe" "%1"» → C:\\…\\ArtCoreSound.exe."""
    cmd = (cmd or "").strip()
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        return cmd[1:end] if end > 0 else cmd[1:]
    low = cmd.lower()
    i = low.find(".exe")
    return cmd[:i + 4] if i >= 0 else cmd


def _registry() -> list[str]:
    if sys.platform != "win32":
        return []
    import winreg
    out = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key in KEYS:
            try:
                with winreg.OpenKey(root, key) as k:
                    value, _kind = winreg.QueryValueEx(k, "")
            except OSError:
                continue
            p = command_exe(os.path.expandvars(str(value)))
            if p:
                out.append(p)
    return out


def _folders() -> list[str]:
    out = []
    for var in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            out.append(os.path.join(base, APP_DIR, EXE))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(os.path.join(local, "Programs", APP_DIR, EXE))      # «just me» installs
    return out


def candidates(cfg: dict | None = None) -> list[str]:
    env = os.environ.get("ARTCORE_SOUND_EXE")
    if env:
        return [env]
    first = [(cfg or {}).get("sound_exe") or ""]
    return [p for p in first + _registry() + _folders() if p]


def find_exe(cfg: dict | None = None) -> str:
    """The path of ArtCoreSound.exe, or "" when it is not installed (nor given)."""
    for p in candidates(cfg):
        if os.path.isfile(p):
            return os.path.abspath(p)
    return ""


def is_sound_exe(path: str) -> bool:
    return os.path.isfile(path) and os.path.basename(path).lower() == EXE.lower()


def open_files(paths: list[str], cfg: dict | None = None) -> bool:
    """Start ArtCore Sound with the files (it passes them to its running copy). False: it was not found."""
    exe = find_exe(cfg)
    if not exe:
        return False
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: lives on its own
    subprocess.Popen([exe, *[os.path.abspath(p) for p in paths]], cwd=os.path.dirname(exe), close_fds=True,
                     creationflags=flags)
    return True
