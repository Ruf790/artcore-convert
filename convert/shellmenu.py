"""2.0: Explorer's menu «Convert with ArtCore Convert» → the default preset, the general presets, the user's own —
for video files and folders, in HKCU (no administrator rights; on Windows 11 it is under «Show more options»).
Each item runs ArtCoreConvert.exe --ctx [--preset KEY] "file"; a running copy takes the files (one copy only)."""
from __future__ import annotations

import os
import sys

from . import VIDEO_EXT, config, winint
from .i18n import t


def exe_args() -> list[str]:
    """How Explorer starts the program: the exe, or pythonw run.py from the sources."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    py = sys.executable
    w = os.path.join(os.path.dirname(py), "pythonw.exe")
    return [w if os.path.isfile(w) else py, os.path.join(config.app_dir(), "run.py")]


def icon() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}",0'
    return f'"{os.path.join(config.app_dir(), "assets", "icon.ico")}",0'


def items(cfg: dict, user_presets: dict | None = None) -> list[tuple[str, str]]:
    """[(preset key, label)]: the default first ("" = the settings of new files), then the general presets and the
    user's own (the device presets would make the menu longer than a screen)."""
    from . import job as J
    user = user_presets if user_presets is not None else (config.read("presets.json") or {})
    cur = cfg.get("preset", "hevc_hw")
    cur_label = ("★ " + cur) if cur in user else t("p_" + cur)
    out = [("", t("ctx_default", cur_label) if not cfg.get("defaults") else t("ctx_default_own"))]
    out += [(k, t("p_" + k)) for k in J.PRESETS]
    out += [(k, "★ " + k) for k in sorted(user)]
    return out


def entries(cfg: dict, user_presets: dict | None = None) -> dict:
    return winint.shell_entries(items(cfg, user_presets), t("ctx_title"), exe_args(), icon(), VIDEO_EXT)


def apply(cfg: dict, user_presets: dict | None = None) -> str:
    """Write the menu as the settings say (or take it away). "" or the error."""
    on = bool(cfg.get("shell_menu"))
    return winint.shell_menu(on, entries(cfg, user_presets) if on else None)


def parse_args(args: list[str]) -> dict:
    """ArtCoreConvert.exe [--ctx] [--preset KEY] [--shell-menu on|off] [files / folders …]."""
    out = {"ctx": False, "preset": "", "shell": "", "paths": [], "selftest": False}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--ctx":
            out["ctx"] = True
        elif a == "--preset" and i + 1 < len(args):
            out["preset"] = args[i + 1]
            i += 1
        elif a.startswith("--preset="):
            out["preset"] = a.split("=", 1)[1]
        elif a == "--shell-menu" and i + 1 < len(args):
            out["shell"] = args[i + 1].lower()
            i += 1
        elif a == "--selftest":
            out["selftest"] = True
        elif not a.startswith("--"):
            out["paths"].append(a)
        i += 1
    return out


def message(paths: list[str], ctx: bool = False, preset: str = "") -> str:
    """What a second copy sends to the running one: «--ctx», «--preset=KEY», the paths — a line each."""
    lines = (["--ctx"] if ctx else []) + ([f"--preset={preset}"] if preset else [])
    lines += [os.path.abspath(p) for p in paths]
    return "\n".join(lines) if lines else "--show"
