"""Settings in %APPDATA%\\ArtCoreConvert\\config.json, the queue in queue.json, own presets in presets.json.
A portable.ini next to the exe keeps everything in .\\Data instead."""
from __future__ import annotations

import copy
import json
import os
import sys

DEFAULTS = {
    "language":      "ru",
    "theme":         "dark",        # dark | light | auto (as Windows apps)
    "look":          "classic",     # 1.6: classic (flat, square, compact) | modern (rounded, 1.0–1.5)
    "single_window": True,          # files opened from Explorer go to the running converter
    "check_updates": True,
    "release":       {"shots": 12, "cols": 4, "singles": True, "sample": 60},   # 2.4: «For a release»
    "last_update_check": 0,
    "update_notified": "",
    "geometry":      "",
    "splitter":      [],
    "last_dir":      "",
    # output
    "out_mode":      "source",      # source = next to the source file | folder = out_dir
    "out_dir":       "",
    "out_suffix":    "_convert",    # name.mkv → name_convert.mkv
    "name_template": "",            # 1.8: «{title} ({year}) {res} {vcodec}» … ("" = name + suffix)
    "overwrite":     False,         # else name_convert (2).mkv
    # the default settings of new tasks
    "preset":        "hevc_hw",
    "defaults":      {},            # job settings (job.DEFAULT_SETTINGS) overridden by the user
    # running
    "priority":      "below",       # idle | below | normal | above — of ffmpeg
    "on_done":       "nothing",     # nothing | open | sleep | shutdown | quit
    "hw_decode":     True,          # decode on the GPU too (-hwaccel auto) when the encoder is a hardware one
    "keep_queue":    True,          # the queue comes back on the next start
    "notify":        True,          # a tray message when the queue is done (window not active)
    "save_logs":     True,          # a log file of every conversion in <data>\logs (the Log tab)
    "log_next_to_output": False,    # and a copy next to the result (name.log)
    "auto_ext":      True,          # 1.7: a film added → its film.*.ac3 / .srt / .ass … beside it attached
    "encoders":      {},            # cache of the hardware test: {ffmpeg version: {encoder: ok}}
    # 1.4
    "cpu_limit":     100,           # % of the processor ffmpeg may use (threads + the cores it runs on)
    "parallel":      1,             # tasks encoded at the same time (1 … 4)
    "chunks":        {"on": False, "workers": 0, "minutes": 3},   # 1.8: a task in chunks (0 workers = auto)
    "metrics":       [],            # psnr / ssim / vmaf (1.9: psnr_hvs ms_ssim ciede cambi) on every finished file
    "show_frame":    True,          # the frame being encoded on the Status tab
    "task_view":     "cards",       # cards | rows
    # 2.0
    "watch":         {"on": True, "stable": 10, "folders": []},   # watch folders (watch.py)
    "tray":          False,         # minimised → into the notification area (watch folders go on working)
    "shell_menu":    False,         # «Convert with ArtCore Convert» with the presets in Explorer's menu (HKCU)
    "ctx_start":     False,         # files sent from that menu start the queue at once
    # 2.1
    "start_at":      0,             # the delayed start of the queue (seconds since the epoch, 0 = none; delay.py)
    "start_time":    "01:00",       # the time last picked in «Start at…»
    # 2.5.2
    "sound_exe":     "",            # ArtCoreSound.exe when it is not where the registry says («Open in ArtCore Sound»)
}

CPU_LIMITS = (100, 75, 50, 25)

PRIORITIES = ("idle", "below", "normal", "above")
ON_DONE = ("nothing", "open", "sleep", "shutdown", "quit")


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_portable() -> bool:
    return os.path.isfile(os.path.join(app_dir(), "portable.ini"))


def data_dir() -> str:
    override = os.environ.get("ARTCORE_CONVERT_DATA")        # tests
    if override:
        path = override
    elif is_portable():
        path = os.path.join(app_dir(), "Data")
    else:
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
        path = os.path.join(base, "ArtCoreConvert")
    os.makedirs(path, exist_ok=True)
    return path


def _path(name: str) -> str:
    return os.path.join(data_dir(), name)


def read(name: str):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write(name: str, data) -> None:
    path = _path(name)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def load() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    saved = read("config.json")
    if isinstance(saved, dict):
        cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    if cfg.get("language") not in ("ru", "en"):
        cfg["language"] = "ru"
    if cfg.get("theme") not in ("dark", "light", "auto"):
        cfg["theme"] = "dark"
    if cfg.get("look") not in ("classic", "modern"):
        cfg["look"] = "classic"
    if cfg.get("out_mode") not in ("source", "folder"):
        cfg["out_mode"] = "source"
    if cfg.get("priority") not in PRIORITIES:
        cfg["priority"] = "below"
    if cfg.get("on_done") not in ON_DONE:
        cfg["on_done"] = "nothing"
    for k in ("defaults", "encoders"):
        if not isinstance(cfg.get(k), dict):
            cfg[k] = {}
    if cfg.get("cpu_limit") not in CPU_LIMITS:
        cfg["cpu_limit"] = 100
    try:
        cfg["parallel"] = max(1, min(4, int(cfg.get("parallel", 1))))
        ch = cfg.get("chunks") if isinstance(cfg.get("chunks"), dict) else {}
        cfg["chunks"] = {"on": bool(ch.get("on")), "workers": max(0, min(32, int(ch.get("workers", 0) or 0))),
                         "minutes": max(0.5, min(30.0, float(ch.get("minutes", 3) or 3)))}
    except (TypeError, ValueError):
        cfg["parallel"] = 1
    if not isinstance(cfg.get("metrics"), list):
        cfg["metrics"] = []
    cfg["metrics"] = [m for m in cfg["metrics"] if m in ("psnr", "ssim", "vmaf", "psnr_hvs", "ms_ssim", "ciede",
                                                          "cambi")]
    if cfg.get("task_view") not in ("cards", "rows"):
        cfg["task_view"] = "cards"
    if not isinstance(cfg.get("out_suffix"), str):
        cfg["out_suffix"] = "_convert"
    from . import watch                                    # 2.0: the watch folders made sane
    w = watch.settings(cfg)
    cfg["watch"] = {"on": w["on"], "stable": w["stable"], "folders": w["folders"]}
    for k in ("tray", "shell_menu", "ctx_start"):
        cfg[k] = bool(cfg.get(k))
    from . import delay                                    # 2.1: the delayed start
    try:
        cfg["start_at"] = max(0.0, float(cfg.get("start_at") or 0))
    except (TypeError, ValueError):
        cfg["start_at"] = 0
    if not isinstance(cfg.get("start_time"), str) or delay.parse_hhmm(cfg["start_time"]) is None:
        cfg["start_time"] = "01:00"
    return cfg


def save(cfg: dict) -> None:
    write("config.json", cfg)
