"""Settings in %APPDATA%\\ArtCoreConvert\\config.json, the queue in queue.json, own presets in presets.json.
A portable.ini next to the exe keeps everything in .\\Data instead."""
from __future__ import annotations

import copy
import json
import os
import sys

DEFAULTS = {
    "language":      "ru",
    "single_window": True,          # files opened from Explorer go to the running converter
    "check_updates": True,
    "last_update_check": 0,
    "update_notified": "",
    "geometry":      "",
    "splitter":      [],
    "last_dir":      "",
    # output
    "out_mode":      "source",      # source = next to the source file | folder = out_dir
    "out_dir":       "",
    "out_suffix":    "_convert",    # name.mkv → name_convert.mkv
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
    "encoders":      {},            # cache of the hardware test: {ffmpeg version: {encoder: ok}}
}

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
    if cfg.get("out_mode") not in ("source", "folder"):
        cfg["out_mode"] = "source"
    if cfg.get("priority") not in PRIORITIES:
        cfg["priority"] = "below"
    if cfg.get("on_done") not in ON_DONE:
        cfg["on_done"] = "nothing"
    for k in ("defaults", "encoders"):
        if not isinstance(cfg.get(k), dict):
            cfg[k] = {}
    if not isinstance(cfg.get("out_suffix"), str):
        cfg["out_suffix"] = "_convert"
    return cfg


def save(cfg: dict) -> None:
    write("config.json", cfg)
