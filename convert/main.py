"""Entry point: one window; files sent from Explorer («Convert with ArtCore Convert») go to the running copy.
ArtCoreConvert.exe [--ctx] [--preset KEY] [--start S --end E] [files / folders …]
                                                                (2.0: from Explorer's menu, with a preset;
                                                                 --start / --end: a piece for the Trim tab, seconds)
ArtCoreConvert.exe --shell-menu on | off                        (2.0: the installer: Explorer's menu in HKCU)"""
from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget

from . import APP_ID, APP_TITLE, VERSION, config, winint
from .i18n import set_lang

SERVER_NAME = "ArtCoreConvert-" + (os.environ.get("USERNAME") or os.environ.get("USER") or "user")


def _send_to_running(paths: list[str], ctx: bool = False, preset: str = "", clip: tuple | None = None) -> bool:
    from .shellmenu import message
    s = QLocalSocket()
    s.connectToServer(SERVER_NAME)
    if not s.waitForConnected(400):
        return False
    s.write(message(paths, ctx, preset, clip).encode("utf-8"))
    s.waitForBytesWritten(1000)
    s.disconnectFromServer()
    return True


def start_lock():
    """2.0: Explorer starts a copy per selected file at once — one of them becomes the program, the others wait for
    it to listen and hand their files over (a lock file in %TEMP% decides who is first)."""
    import tempfile
    from PySide6.QtCore import QLockFile
    lock = QLockFile(os.path.join(tempfile.gettempdir(), SERVER_NAME + ".lock"))
    lock.setStaleLockTime(30000)
    lock.tryLock(20000)
    return lock


def parse_message(data: str) -> tuple[list[str], bool, str, tuple | None]:
    """The lines a second copy sent → (paths, from Explorer's menu, preset, the Trim piece (start, end) or None)."""
    from .shellmenu import _clip, _seconds
    paths, ctx, preset, seg = [], False, "", {}
    for ln in data.splitlines():
        if ln == "--ctx":
            ctx = True
        elif ln.startswith("--preset="):
            preset = ln.split("=", 1)[1]
        elif ln.startswith(("--start=", "--end=")):
            k, v = ln[2:].split("=", 1)
            seg[k] = _seconds(v)
        elif ln and not ln.startswith("--"):
            paths.append(ln)
    return paths, ctx, preset, _clip(seg.get("start"), seg.get("end"))


def _surface_format() -> None:
    """libmpv's OpenGL renderer (the Trim preview) wants a 3.3+ core context, as in ArtCore Player."""
    f = QSurfaceFormat()
    f.setVersion(3, 3)
    f.setProfile(QSurfaceFormat.CoreProfile)
    f.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(f)


class TitleBars(QObject):
    """Every window and dialog gets a title bar in the colours of the theme when it is first shown."""

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Show and isinstance(obj, QWidget) and obj.isWindow() and \
                not obj.property("acTitled"):
            obj.setProperty("acTitled", True)
            from . import style
            winint.dark_title_bar(int(obj.winId()), style.IS_DARK)
        return False


def selftest() -> int:
    """CI: the built exe finds its ffmpeg / ffprobe and libmpv, checks the encoders, builds the window and
    converts a short test clip through the real queue: x264 + AAC to MP4, x265 10 bit + copy to MKV, and (1.1)
    a trimmed VP9 WebM with edited tags and chapters, Xvid AVI, GIF and MP3; (1.4) two tasks at the same time
    with a CPU limit, a joined two-pass task with an external track split into parts, the quality metrics and the
    current frame, the speed test. Result in %TEMP%."""
    import tempfile
    import time
    import traceback
    out = os.path.join(tempfile.gettempdir(), "artcore_convert_selftest.txt")
    lines = []
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        tmp = os.path.join(tempfile.gettempdir(), "artcore_convert_selftest")
        os.environ.setdefault("ARTCORE_CONVERT_DATA", os.path.join(tmp, "data"))
        app = QApplication(sys.argv)
        from . import encoders as E, ff, probe, style
        app.setStyle("Fusion")
        app.setStyleSheet(style.stylesheet())
        lines.append(f"OK {VERSION}; ffmpeg {ff.version()} at {ff.ffmpeg()}; ffprobe {ff.ffprobe()}")
        if not ff.ffmpeg() or not ff.ffprobe():
            raise RuntimeError("ffmpeg / ffprobe not found")
        av = E.detect()
        lines.append("encoders: " + ", ".join(f"{e.ffname}={'ok' if av.ok.get(e.id) else 'no'}" for e in E.VIDEO))
        need = {"libx264", "libx265", "libsvtav1", "aac", "ac3", "eac3", "libopus", "flac", "libmp3lame",
                "h264_nvenc", "hevc_nvenc", "av1_nvenc", "h264_amf", "hevc_amf", "h264_qsv", "hevc_qsv",
                "libvpx-vp9", "mpeg4", "mpeg2video", "prores_ks", "gif", "libvorbis", "mp2", "alac", "pcm_s16le",
                "pcm_s24le"}
        missing = need - av.audio
        if missing:
            raise RuntimeError(f"this ffmpeg build lacks {sorted(missing)}")
        problems = [p for e in E.VIDEO for p in E.check_options(e)]   # GPU arguments vs this ffmpeg, no GPU needed
        lines.append(f"options of {len(E.VIDEO)} encoders checked: " + ("ok" if not problems else "; ".join(problems)))
        if problems:
            raise RuntimeError("encoder options: " + "; ".join(problems))
        # 1.3: the filters the Streams tab uses are in this ffmpeg
        from . import filters as F
        have = ff.run([ff.ffmpeg(), "-hide_banner", "-filters"], 30).stdout
        names = {ln.split()[1] for ln in have.splitlines() if len(ln.split()) > 2 and ln.startswith(" ")}
        lacking = [f for f in F.needed_filters() if f not in names]
        lines.append(f"filters: {len(F.needed_filters()) - len(lacking)} of {len(F.needed_filters())}"
                     + (f", lacking {lacking}" if lacking else ""))
        if lacking:
            raise RuntimeError(f"this ffmpeg build lacks the filters {lacking}")
        need14 = ("concat", "anullsrc", "aformat", "asplit", "split", "psnr", "ssim", "setsar", "color", "settb")
        lacking = [f for f in need14 if f not in names]
        lines.append("1.4: filters " + ("ok" if not lacking else f"lacking {lacking}") +
                     f"; libvmaf {'yes' if 'libvmaf' in names else 'no'}; SVT-AV1 two passes "
                     f"{'yes' if av.svt_2pass else 'no'}")
        if lacking:
            raise RuntimeError(f"this ffmpeg build lacks the filters {lacking}")
        mux = ff.run([ff.ffmpeg(), "-hide_banner", "-muxers"], 30).stdout
        if " segment " not in mux or " image2 " not in mux:
            raise RuntimeError("this ffmpeg build lacks the segment / image2 muxers")
        src = os.path.join(tmp, "clip.mkv")
        os.makedirs(tmp, exist_ok=True)
        r = ff.run([ff.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=640x360:r=25:d=2", "-f", "lavfi", "-i", "sine=f=440:d=2", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "ac3", "-ac", "6", src], 60)
        if r.returncode != 0:
            raise RuntimeError("making the test clip failed: " + r.stderr[-300:])
        lines.append("libxvid: " + ("yes" if "libxvid" in av.audio else "no (FFmpeg's mpeg4 is used for Xvid)"))
        # libmpv: loads and plays (no picture needed here: the offscreen platform has no OpenGL)
        from . import mpvview
        mod = mpvview.load_mpv()
        if mod is None:
            raise RuntimeError("libmpv: " + mpvview.mpv_error())
        m = mod.MPV(vo="null", ao="null", pause=True, config=False, terminal=False, ytdl=False, hr_seek="yes")
        lines.append(f"libmpv {m.mpv_version}, ffmpeg {m.ffmpeg_version}")
        m.play(src)
        t0 = time.monotonic()
        while not m.duration and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        m.command("seek", 1.0, "absolute+exact")
        t0 = time.monotonic()
        while abs((m.time_pos or 0) - 1.0) > 0.05 and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        lines.append(f"  mpv: duration {m.duration}, after seek {m.time_pos}")
        if not m.duration or abs((m.time_pos or 0) - 1.0) > 0.05:
            raise RuntimeError("libmpv did not open / seek the clip")
        m.terminate()
        cfg = config.load()
        cfg.update(check_updates=False, keep_queue=False, out_mode="folder", out_dir=os.path.join(tmp, "out"),
                   overwrite=True, on_done="nothing", notify=False)
        from . import job as J
        from .window import MainWindow
        win = MainWindow(cfg)
        win.show()
        t0 = time.monotonic()
        while not win.av_ready and time.monotonic() - t0 < 60:
            app.processEvents()
            time.sleep(0.02)
        win.add_paths([src])
        win.add_paths([src])                      # the same file again is skipped …
        j1 = win.jobs[0]
        j1.settings = J.merged({"container": "mp4", "video": {"encoder": "x264"}, "audio": {"codec": "aac"}})
        j2 = J.Job(src=src, settings=J.merged({"container": "mkv", "video": {"encoder": "x265", "bits": "10"},
                                                "audio": {"codec": "copy"}}))
        win._append(j2)                          # … so add the second task directly
        j3 = J.Job(src=src, settings=J.fit_container(J.merged({"container": "webm", "video": {"speed": 1}})))
        j3.trim = [[0.2, 0.8], [1.2, 1.8]]
        j3.meta = {"tags": {"title": "Selftest"}, "streams": {"1": {"lang": "rus"}}}
        j3.chapters = [[0.0, "A"], [0.6, "B"]]
        jobs_11 = [j3] + [J.Job(src=src, settings=J.fit_container(J.merged(st))) for st in (
            {"container": "avi", "video": {"encoder": "auto_mpeg4"}},
            {"container": "gif"},
            {"container": "mp3"},
            {"container": "mov", "video": {"encoder": "prores", "quality": 1}})]
        # 1.3: a 2.20 film in a 1080p frame — auto crop, rotation, colour, vid.stab (two passes), the sound
        # delayed and louder, two pieces joined by the concat filter
        wide = os.path.join(tmp, "wide.mkv")
        r = ff.run([ff.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x872:r=25:d=2",
                    "-f", "lavfi", "-i", "sine=f=440:d=2", "-vf", "pad=1920:1080:0:104:black", "-c:v", "libx264",
                    "-preset", "ultrafast", "-c:a", "aac", wide], 60)
        if r.returncode != 0:
            raise RuntimeError("making the letterboxed clip failed: " + r.stderr[-300:])
        j4 = J.Job(src=wide, settings=J.merged({"container": "mkv", "video": {"encoder": "x264", "height": 480},
                                                "audio": {"codec": "aac", "gain": 3},
                                                "filters": {"crop": {"on": True, "mode": "auto"},
                                                            "rotate": {"on": True, "hflip": True},
                                                            "color": {"on": True, "saturation": 1.2},
                                                            "deshake": {"on": True, "method": "vidstab"}}}))
        j4.trim = [[0.2, 0.9], [1.1, 1.8]]
        j4.tracks = {"1": {"delay": 200}}
        jobs_11.append(j4)
        for j in jobs_11:
            win._append(j)
        win._queue_changed()
        win.view.setCurrentItem(win.items[j3.uid])
        t0 = time.monotonic()
        while any(j.status == "probe" for j in win.jobs) and time.monotonic() - t0 < 30:
            app.processEvents()
            time.sleep(0.02)
        for i in range(win.side_bar.count()):                    # every side tab shows the task (Trim, Metadata …)
            win.side_bar.setCurrentIndex(i)
            app.processEvents()
        if win.trim_tab.job is not j3 or not win.meta_tab.tags.topLevelItemCount():
            raise RuntimeError("the Trim / Metadata tabs did not take the task")
        win.show_side("streams")
        t0 = time.monotonic()
        while not (win.trim_tab.bar.frames and win.trim_tab.bar.peaks) and time.monotonic() - t0 < 30:
            app.processEvents()
            time.sleep(0.02)
        lines.append(f"strip: {len(win.trim_tab.bar.frames)} frames, loudness: {len(win.trim_tab.bar.peaks)} points")
        if not win.trim_tab.bar.peaks:
            raise RuntimeError("no loudness curve under the picture")
        win.start_queue()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120 and any(j.status not in ("done", "error") for j in win.jobs):
            app.processEvents()
            time.sleep(0.02)
        for j in win.jobs:
            lines.append(f"{j.status}: {j.out} {j.error}")
            if j.status != "done":
                raise RuntimeError(f"conversion failed: {j.error}")
            info = probe.probe(j.out)
            lines.append("  " + probe.summary(info) + (" · " + probe.video_line(info.video) if info.video else ""))
        if probe.probe(j2.out).video.bits != 10:
            raise RuntimeError("x265 10 bit came out 8 bit")
        r3 = probe.probe(j3.out)
        want = {"video": "vp9", "dur": 1.2, "title": "Selftest", "chapters": ["A", "B"], "lang": "rus"}
        got = {"video": r3.video.codec, "dur": round(r3.duration, 1), "title": r3.tags.get("title"),
               "chapters": [c[2] for c in r3.chapter_list], "lang": r3.of("audio")[0].lang}
        if abs(got["dur"] - want["dur"]) <= 0.2:
            got["dur"] = want["dur"]
        if got != want:
            raise RuntimeError(f"the trimmed WebM: {got} != {want}")
        r4 = probe.probe(j4.out)
        lines.append(f"  filters: crop found {j4.crop_found}, result {r4.video.width}x{r4.video.height}, "
                     f"{r4.duration:.2f} s")
        if j4.crop_found != [0, 104, 0, 104] or (r4.video.width, r4.video.height) != (854, 388) \
                or abs(r4.duration - 1.6) > 0.25:
            raise RuntimeError(f"the filtered task: crop {j4.crop_found}, {r4.video.width}x{r4.video.height}, "
                               f"{r4.duration:.2f} s")
        jobs_11 = jobs_11[:-1]
        kinds = [(probe.probe(j.out).video.codec if probe.probe(j.out).video else "-") for j in jobs_11[1:]]
        if kinds != ["mpeg4", "gif", "-", "prores"]:
            raise RuntimeError(f"AVI / GIF / MP3 / MOV: {kinds}")
        selftest_14(win, app, src, wide, tmp, lines)
        selftest_15(win, app, src, wide, tmp, lines)
        selftest_19(win, app, src, tmp, lines)
        selftest_20(win, app, src, tmp, lines)
        selftest_21(win, app, wide, tmp, lines)
        selftest_23(tmp, lines)
        win.close()
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return 0
    except Exception:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\nFAIL\n" + traceback.format_exc())
        return 1


def selftest_14(win, app, src: str, wide: str, tmp: str, lines: list) -> None:
    """1.4 in the built exe: two places at once with a CPU limit, metrics, the current frame, a joined
    two-pass task with an external sound split into parts; the speed test with x264."""
    import time
    from . import bench as B
    from . import encoders as E, ff, probe
    from . import job as J
    ac3 = os.path.join(tmp, "clip.eng.ac3")
    r = ff.run([ff.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=700:d=3", "-ac", "2",
                "-c:a", "ac3", ac3], 60)
    if r.returncode != 0:
        raise RuntimeError("making the external track failed: " + r.stderr[-300:])
    vmaf = "libvmaf" in win.av.filters
    feats = [m for m in J.METRICS if J.metric_ok(m, win.av)]           # 1.9: every metric this ffmpeg has
    lines.append(f"1.9: metrics here: {feats}; libvmaf features {sorted(win.av.vmaf_feats)}")
    if vmaf and len(win.av.vmaf_feats) < len(E.VMAF_FEATURES):
        raise RuntimeError(f"libvmaf lacks features: {sorted(set(E.VMAF_FEATURES) - win.av.vmaf_feats)}")
    win.cfg.update(parallel=2, metrics=feats, show_frame=True)
    win.set_cpu_limit(50)
    win.show_side("status")
    a = J.Job(src=src, settings=J.merged({"container": "mkv", "video": {"encoder": "x264", "speed": 6}}))
    b = J.Job(src=wide, settings=J.merged({"container": "mp4", "video": {"encoder": "x265", "speed": 7}}))
    c = J.Job(src=src, settings=J.merged({"container": "mkv", "video": {"encoder": "x264", "rc": "bitrate",
                                                                        "bitrate": 1500, "passes": 2},
                                          "split": {"mode": "time", "minutes": 0.025}}))
    c.joins = [wide]
    c.ext = [J.clean_ext({"path": ac3, "kind": "audio", "lang": "eng", "delay": 300})]
    for j in (a, b, c):
        win._append(j)
    win._queue_changed()
    t0 = time.monotonic()
    while any(j.status == "probe" for j in win.jobs) and time.monotonic() - t0 < 30:
        app.processEvents()
        time.sleep(0.02)
    both = frame = False
    win.start_queue()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 300 and any(j.status not in ("done", "error") for j in (a, b, c)):
        app.processEvents()
        both = both or len(win._busy_slots()) == 2
        pm = win.fr_img.pixmap()
        frame = frame or (pm is not None and not pm.isNull())
        time.sleep(0.02)
    for j in (a, b, c):
        lines.append(f"1.4 {j.status}: {j.out} {j.error} {j.metrics} parts {len(j.parts)}")
        if j.status != "done":
            raise RuntimeError(f"1.4 conversion failed: {j.error}")
    if not both:
        raise RuntimeError("two tasks never ran at the same time")
    if not frame:
        raise RuntimeError("the current frame never showed on the Status tab")
    for j in (a, b):
        if not (20 < j.metrics.get("psnr", 0) < 101 and 0.8 < j.metrics.get("ssim", 0) <= 1) or \
                (vmaf and not 0 < j.metrics.get("vmaf", -1) <= 100):
            raise RuntimeError(f"quality metrics: {j.metrics}")
        if vmaf and not (15 < j.metrics.get("psnr_hvs", 0) < 101 and 0.8 < j.metrics.get("ms_ssim", 0) <= 1 and
                         20 < j.metrics.get("ciede", 0) < 100 and 0 <= j.metrics.get("cambi", -1) < 24):
            raise RuntimeError(f"1.9 quality metrics (libvmaf features): {j.metrics}")
        lines.append("  " + "; ".join(J.metric_text(k, v) for k, v in j.metrics.items()))
    if len(c.parts) != 3:                                   # 4 s in parts of 1.5 s
        raise RuntimeError(f"the joined task: {len(c.parts)} parts, not 3")
    total = sum(probe.probe(p).duration for p in c.parts)
    first = probe.probe(c.parts[0])
    lines.append(f"  joined + split: {total:.2f} s in {len(c.parts)} parts; first: {probe.summary(first)}")
    if abs(total - 4.0) > 0.4 or len(first.of("audio")) != 2 or first.of("audio")[1].lang != "eng" or \
            (first.video.width, first.video.height) != (640, 360):
        raise RuntimeError("the joined task came out wrong")
    res = B.run_one(E.BY_ID["x264"], ff.ffmpeg(), ff.ffprobe(), src, 0.0, 1.0, threads=1)
    lines.append(f"  speed test: x264 {res['fps']} fps, {res['size']} bytes")
    if not res["ok"]:
        raise RuntimeError("speed test: " + res["error"])
    win.set_cpu_limit(100)


def selftest_15(win, app, src: str, wide: str, tmp: str, lines: list) -> None:
    """1.5 in the built exe: every option the advanced encoder settings and the presets of the format can write
    is in this ffmpeg (no GPU needed to check NVENC / AMF / QSV), the container options exist in its muxers;
    device presets (Blu-ray, DVD PAL, Discord) through the real queue with a preset of the format and container
    options, profiles / levels checked by ffprobe; the comparison of two presets."""
    import json
    import subprocess
    import time
    from . import advanced as ADV
    from . import compare as C
    from . import encoders as E, ff, probe
    from . import formats as FM
    from . import job as J
    problems = []
    for e in E.VIDEO:
        caps = ADV.caps(e)
        if not caps:
            continue
        offered = ADV.offered(e, caps)
        for f in offered:
            if f.opt and f.opt not in caps:
                problems.append(f"{e.ffname}: -{f.opt}")
        st = J.merged({"fmt": {"name": "X", "profile": {e.family: (FM.PROFILES.get(e.family) or ("",))[0]},
                               "level": {e.family: (FM.LEVELS.get(e.family) or ("",))[-1]}, "gop_max": 50,
                               "gop_closed": True, "vbv_buf": 1000, "vbv_max": 2000}})
        _a, p = FM.video_args(e, st, 8, "420", 25.0, caps)
        problems += [f"{e.ffname}: {k}" for k in p if k.lstrip("-") not in caps]
        lines.append(f"  1.5 {e.ffname}: advanced {', '.join(f.key for f in offered) or '—'}")
    if problems:
        raise RuntimeError("1.5 options: " + "; ".join(problems))
    for muxer, opts in (("matroska", ("reserve_index_space", "cues_to_front", "cluster_size_limit",
                                       "cluster_time_limit", "write_crc32", "default_mode")),
                        ("mp4", ("movflags",)), ("mpegts", ("muxrate", "pcr_period", "mpegts_m2ts_mode")),
                        ("dvd", ("muxrate",))):
        h = ff.run([ff.ffmpeg(), "-hide_banner", "-h", f"muxer={muxer}"], 20).stdout
        lacking = [o for o in opts if f"-{o} " not in h]
        if lacking:
            raise RuntimeError(f"1.5: the {muxer} muxer lacks {lacking}")
    lines.append("1.5: advanced / format options and container options: ok")
    a = J.Job(src=wide, settings=J.preset_settings("bluray"))
    a.settings["video"].update(encoder="x264", speed=1)
    a.settings["mux"] = {"ts_muxrate": 0}
    b = J.Job(src=src, settings=J.preset_settings("dvd_pal"))
    c = J.Job(src=wide, settings=J.preset_settings("discord10"))
    c.settings["video"].update(encoder="x264", speed=1)
    c.trim = [[0.2, 1.8]]
    d = J.Job(src=src, settings=J.merged({"container": "mkv", "video": {"encoder": "x265", "speed": 1},
                                          "venc": {"x265": {"ref": 2, "params": "no-sao=1"}},
                                          "mux": {"mkv_cues_front": True, "mkv_crc32": False}}))
    FM.apply(d.settings, {"name": "HEVC Main@4.1 GOP 10", "container": "mkv", "vcodecs": ["hevc"],
                          "profile": {"hevc": "main"}, "level": {"hevc": "4.1"}, "gop_max": 10, "gop_closed": True})
    jobs = (a, b, c, d)
    win.cfg.update(parallel=2, metrics=[])
    for j in jobs:
        win._append(j)
    win._queue_changed()
    t0 = time.monotonic()
    while any(j.status == "probe" for j in win.jobs) and time.monotonic() - t0 < 30:
        app.processEvents()
        time.sleep(0.02)
    win.start_queue()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 300 and any(j.status not in ("done", "error") for j in jobs):
        app.processEvents()
        time.sleep(0.02)
    got = []
    for j in jobs:
        lines.append(f"1.5 {j.status}: {j.out} {j.error}")
        if j.status != "done":
            raise RuntimeError(f"1.5 conversion failed: {j.error}")
        r = subprocess.run([ff.ffprobe(), "-v", "error", "-show_streams", "-show_format", "-of", "json", j.out],
                           capture_output=True, text=True, timeout=60)
        pj = json.loads(r.stdout)
        v = next(s for s in pj["streams"] if s["codec_type"] == "video")
        got.append((v["codec_name"], v.get("profile"), int(v.get("level", 0)), v["width"], v["height"],
                    pj["format"]["format_name"]))
        lines.append(f"  {got[-1]}")
    want = [("h264", "High", 41, 1920, 1080, "mpegts"), ("mpeg2video", "Main", 8, 720, 576, "mpeg"),
            ("h264", "High", None, 1280, 720, "mov,mp4,m4a,3gp,3g2,mj2"), ("hevc", "Main", 123, 640, 360, "matroska,webm")]
    for g, w_ in zip(got, want):
        if tuple(x for x, y in zip(g, w_) if y is not None) != tuple(y for y in w_ if y is not None):
            raise RuntimeError(f"1.5 devices / format: {got}")
    if os.path.getsize(c.out) > 10 * 1000 * 1000 or abs(probe.probe(c.out).duration - 1.6) > 0.25:
        raise RuntimeError("1.5 Discord: the size / length is wrong")
    data = open(d.out, "rb").read()
    if not 0 < data.find(b"\x1c\x53\xbb\x6b") < data.find(b"\x1f\x43\xb6\x75") or b"ref=2" not in data:
        raise RuntimeError("1.5: the Cues are not in front / the advanced x265 settings are missing")
    info = probe.probe(src)
    res = [C.run_one(k, k, J.preset_settings(k) | {"video": J.preset_settings(k)["video"] | {"encoder": "x264"}},
                     src, info, 0.0, 1.5, win.av, C.work_dir(), 1, n=i) for i, k in enumerate(("telegram", "android"))]
    lines.append("  compare: " + "; ".join(f"{r['key']} {r['size']} B {r.get('psnr', 0):.1f} dB "
                                           f"VMAF {r.get('vmaf', '—')}" for r in res))
    if not all(r["ok"] and r.get("psnr") for r in res):
        raise RuntimeError("1.5 compare: " + "; ".join(r["error"] for r in res))
    win.cfg.update(parallel=1)


def _wait(app, cond, secs: float) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def selftest_20(win, app, src: str, tmp: str, lines: list) -> None:
    """2.0 in the built exe: subtitles burnt in through the real queue — an SRT in Windows-1251 with an apostrophe
    and brackets in its name (libass and its fonts in this FFmpeg build) and a PGS cut into two pieces; the 2.0
    matrix (Dolby Pro Logic II, custom); a watch folder; Explorer's menu in HKCU and «a file held open»."""
    import shutil
    import time
    from . import ff, probe, shellmenu, winint
    from . import job as J
    from .pgswriter import write_sup
    grey = os.path.join(tmp, "grey20.mkv")
    sup = write_sup(os.path.join(tmp, "box20.sup"), [(0.5, 1.5, (100, 100, 400, 200), False)])
    r = ff.run([ff.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "color=gray:s=1920x1080:r=25:d=3",
                "-f", "lavfi", "-i", "sine=d=3", "-i", sup, "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264",
                "-preset", "ultrafast", "-c:a", "ac3", "-ac", "6", "-c:s", "copy", grey], 120)
    if r.returncode != 0:
        raise RuntimeError("2.0: making the PGS clip failed: " + r.stderr[-300:])
    srt = os.path.join(tmp, "Фильм it's [2].srt")
    with open(srt, "wb") as f:
        f.write("1\n00:00:00,300 --> 00:00:02,500\nПривет, мир\n".encode("cp1251"))
    a = J.Job(src=grey, settings=J.merged({"container": "mkv", "video": {"encoder": "x264", "speed": 1, "height": 360},
                                           "audio": {"codec": "aac", "channels": "stereo", "downmix": "dplii"}}))
    a.ext = [J.clean_ext({"path": srt, "kind": "subtitle", "burn": "all"})]
    b = J.Job(src=grey, settings=J.merged({"container": "mkv", "video": {"encoder": "x264", "speed": 1, "height": 360},
                                           "audio": {"codec": "aac", "channels": "stereo", "downmix": "custom",
                                                     "dm": {"c": 0, "s": -6, "lfe": True}}}))
    b.tracks = {"2": {"burn": "all"}}
    b.trim = [[0.2, 1.0], [1.2, 2.5]]
    a.dest, b.dest = os.path.join(tmp, "out20", "srt"), os.path.join(tmp, "out20", "pgs")
    win.cfg.update(parallel=1, metrics=[], chunks={"on": False, "workers": 0, "minutes": 3})
    for j in (a, b):
        win._append(j)
    win._queue_changed()
    _wait(app, lambda: all(j.status != "probe" and j.infos.get(e["path"], 1) is not None for j in (a, b)
                           for e in j.ext), 30)
    win.start_queue()
    _wait(app, lambda: all(j.status in ("done", "error") for j in (a, b)), 300)

    def brightest(path, n, x, y, w, h):
        rr = ff.run([ff.ffmpeg(), "-v", "error", "-i", path, "-map", "0:v:0", "-vf",
                     f"select=eq(n\\,{n}),crop={w}:{h}:{x}:{y},format=gray", "-frames:v", "1", "-f", "rawvideo", "-"],
                    60, binary=True)
        return max(rr.stdout) if rr.returncode == 0 and rr.stdout else -1
    for j in (a, b):
        if j.status != "done":
            raise RuntimeError(f"2.0 burnt-in subtitles: {j.error}")
    text, after = brightest(a.out, 25, 160, 290, 320, 66), brightest(a.out, 70, 160, 290, 320, 66)
    box, bg = brightest(b.out, 10, 40, 40, 100, 50), brightest(b.out, 10, 400, 200, 100, 50)
    ach = [probe.probe(x.out).of("audio")[0].channels for x in (a, b)]
    lines.append(f"2.0 hardsub: SRT text {text} (after it {after}), PGS box {box} (grey {bg}); 2.0 matrix: {ach} ch")
    if not (text > 220 and after < 160 and box > 220 and bg < 160) or ach != [2, 2]:
        raise RuntimeError("2.0: the subtitles were not burnt in (libass / fonts?) or the matrix failed")
    # a watch folder
    wdir, wout = os.path.join(tmp, "watch20"), os.path.join(tmp, "watch20_out")
    shutil.rmtree(wdir, ignore_errors=True)
    os.makedirs(os.path.join(wdir, "sub"))
    win.cfg["watch"] = {"on": True, "stable": 2, "folders": [{"path": wdir, "preset": "remux_mkv", "out": wout,
                                                              "sub": True, "on": True}]}
    win.watcher.timer.setInterval(500)
    win.watcher.apply()
    n0 = len(win.jobs)
    shutil.copy(src, os.path.join(wdir, "sub", "w.mkv"))
    if not _wait(app, lambda: len(win.jobs) > n0 and win.jobs[-1].status == "done", 90):
        raise RuntimeError("2.0: the watch folder's file was not taken / encoded: "
                           f"{[(j.name, j.status, j.error) for j in win.jobs[n0:]]}")
    wj = win.jobs[-1]
    lines.append(f"2.0 watch folder: {wj.preset} → {wj.out}")
    if os.path.dirname(wj.out) != os.path.join(wout, "sub") or not os.path.isfile(os.path.join(wdir, "sub", "w.mkv")):
        raise RuntimeError("2.0 watch folder: the result is in the wrong place / the source is gone")
    win.cfg["watch"] = {"on": False, "stable": 10, "folders": []}
    win.watcher.apply()
    if winint.IS_WIN:                                    # Explorer's menu in HKCU and back; a file held open
        cfg = dict(win.cfg, shell_menu=True)
        err = shellmenu.apply(cfg, win.user_presets)
        on = winint.shell_menu_on()
        err2 = shellmenu.apply(dict(cfg, shell_menu=False))
        off = not winint.shell_menu_on()
        held = os.path.join(tmp, "held20.mkv")
        with open(held, "wb") as f:
            f.write(b"x")
            busy = winint.file_busy(held)
        free = not winint.file_busy(held)
        lines.append(f"2.0 Explorer menu: written {on} {err!r}, removed {off} {err2!r}; file held: {busy}, freed: {free}")
        if err or err2 or not on or not off or not busy or not free:
            raise RuntimeError("2.0: Explorer's menu / the «file held open» check failed")


def selftest_23(tmp: str, lines: list) -> None:
    """2.3 in the built exe: a font attached to an MKV survives a remux and gets its mimetype; the result is cut
    at its chapters (copied, «Name_01 Title»); mkvpropedit is looked for (it may be missing)."""
    from . import ff, probe, propedit
    from . import job as J
    from . import encoders as E
    font = os.path.join(tmp, "font23.ttf")
    with open(font, "wb") as f:
        f.write(b"\x00\x01\x00\x00" + b"\x00" * 2044)
    chap = os.path.join(tmp, "ch23.txt")
    with open(chap, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Two\n")
    src = os.path.join(tmp, "src23.mkv")
    r = ff.run([ff.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2",
                "-i", chap, "-map", "0", "-map_chapters", "1", "-c:v", "libx264", "-g", "25", "-preset", "ultrafast",
                src], 60)
    if r.returncode != 0:
        raise RuntimeError("2.3: making the clip failed: " + r.stderr[-300:])
    j = J.Job(src=src, settings=J.merged({"container": "mkv", "video": {"mode": "copy"}}))
    j.info = probe.probe(src)
    j.attach = [font]
    out = os.path.join(tmp, "out23.mkv")
    r = ff.run(J.build_args(j, ff.ffmpeg(), out, E.Available({"x264": True}), False), 60)
    res = probe.probe(out)
    att = [(s.filename, s.mime) for s in res.of("attachment")] if not res.error else []
    lines.append(f"2.3 attachments: {att}")
    if r.returncode != 0 or att != [("font23.ttf", "font/ttf")]:
        raise RuntimeError(f"2.3: the font was not attached: {att} {r.stderr[-300:]}")
    j2 = J.Job(src=out, settings=J.merged({"container": "mkv", "split": {"mode": "chapters", "chapters_n": 1}}))
    cuts, titles = J.chapter_cuts(J.read_chapters(ff.ffprobe(), out), 1)
    r = ff.run(J.split_args(j2, ff.ffmpeg(), out, cuts), 60)
    parts = J.split_parts(out)
    lines.append(f"2.3 by chapters: {cuts} {titles} → {[os.path.basename(p) for p in J.part_names(parts, titles)]}")
    if r.returncode != 0 or len(parts) != 2:
        raise RuntimeError(f"2.3: the split by chapters gave {len(parts)} parts")
    lines.append(f"2.3 mkvpropedit: {propedit.find() or 'not found'}")
    from . import release                                   # 2.4: «For a release» (Qt paints the sheet)
    res = release.make(ff.ffmpeg(), probe.probe(src), {"shots": 4, "cols": 2, "sample": 1,
                                                       "dir": os.path.join(tmp, "release24")})
    lines.append(f"2.4 for a release: {len(res['shots'])} shots, sheet {bool(res['sheet'])}, sample "
                 f"{bool(res['sample'])}, errors {res['errors']}")
    if len(res["shots"]) != 4 or not res["sheet"] or not res["sample"] or not res["info"].startswith("General"):
        raise RuntimeError("2.4: the release kit is incomplete")


def selftest_21(win, app, wide: str, tmp: str, lines: list) -> None:
    """2.1 in the built exe: next_start (today / tomorrow); the queue started by a delayed start at its moment,
    «Stop» cancelling another; the 2.20 film in a 1080p frame: auto Crop + «16:9 without bars» (Fill) → 1550×872;
    the libmpv of the build shows it cut (video-crop, else a vf crop) to the very size of the result."""
    import time
    from . import delay as DL, mpvview, probe
    from . import job as J
    now = time.time()
    same_dst = time.localtime(now).tm_isdst == time.localtime(now + 86400).tm_isdst
    ahead = time.strftime("%H:%M", time.localtime(now + 2 * 3600))
    passed = time.strftime("%H:%M", time.localtime(now - 2 * 3600))
    a, b = DL.next_start(now, ahead), DL.next_start(now, passed)
    lines.append(f"2.1 next_start: {ahead} in {DL.countdown(a - now)}, {passed} in {DL.countdown(b - now)}")
    if not (0 < a - now <= 2 * 3600 + 60 and (not same_dst or 22 * 3600 - 120 <= b - now <= 22 * 3600 + 60)):
        raise RuntimeError("2.1: next_start is wrong")
    win.cfg.update(parallel=1, metrics=[], chunks={"on": False, "workers": 0, "minutes": 3})
    win.cfg["watch"] = {"on": False, "stable": 10, "folders": []}
    if not _wait(app, lambda: not win.running and not win._any_running(), 60):   # the 2.0 queue (watch folder)
        win.stop_queue()
        _wait(app, lambda: not win._any_running(), 20)
    win.set_delayed(time.time() + 600)
    text = win.b_start.text().strip()
    lines.append(f"2.1 waiting: «{text}», running {win.running}, status bar «{win.dl_label.text()}»")
    if not text.endswith(DL.clock(win.start_at)) or not win.b_stop.isEnabled():
        raise RuntimeError("2.1: the Start button does not show the delayed start")
    win.stop_queue()
    if win.start_at or config.load()["start_at"]:
        raise RuntimeError("2.1: «Stop» did not cancel the delayed start")
    j = J.Job(src=wide, settings=J.merged({"container": "mkv", "video": {"encoder": "x264", "speed": 1, "width": 0,
                                                                          "height": 0, "fit": "fill"},
                                            "audio": {"codec": "aac"},
                                            "filters": {"crop": {"on": True, "mode": "auto"}}}))
    j.dest = os.path.join(tmp, "out21")
    win._append(j)
    win._queue_changed()
    _wait(app, lambda: j.status != "probe", 30)
    win.set_delayed(time.time() + 2)
    started = _wait(app, lambda: win.running or j.status not in ("wait",), 15)
    done = _wait(app, lambda: j.status in ("done", "error"), 120)
    res = probe.probe(j.out) if done and j.status == "done" else None
    size = (res.video.width, res.video.height) if res and res.video else None
    sar = res.video.sar if res and res.video else ""
    lines.append(f"2.1 delayed start: started {started}, {j.status}, crop {j.crop_found} → {size} SAR {sar}")
    if not started or j.status != "done" or size != (1550, 872) or sar not in ("1:1", ""):   # Crop, then «16:9
        # without bars», square pixels
        raise RuntimeError(f"2.1: the delayed start did not encode: {j.status} {j.error} {size}")
    # the preview of the build's libmpv, cut as the result
    mod = mpvview.load_mpv()
    m = mod.MPV(vo="null", ao="null", pause=True, config=False, terminal=False, ytdl=False, hr_seek="yes",
                keep_open="yes")
    v = mpvview.MpvView.__new__(mpvview.MpvView)
    v.player, v.dead = m, False
    ver = getattr(m, "mpv_version", "?")
    try:
        m.play(wide)
        t0 = time.monotonic()
        while not m.duration and time.monotonic() - t0 < 20:
            time.sleep(0.05)
        rect = mpvview.crop_rect(J.final_crop(j), 1920, 1080)
        v.set_crop(rect)

        def shown():
            try:
                o = m.video_out_params
                return (o["dw"], o["dh"]) if o else None
            except Exception:
                return None
        t0 = time.monotonic()
        while shown() != size and time.monotonic() - t0 < 10:
            time.sleep(0.05)
        cut = shown()
        shot = os.path.join(tmp, "crop21.png")
        m.command("screenshot-to-file", shot, "video")
        info = probe.probe(shot)
        pic = (info.video.width, info.video.height) if info.video else None
        v.set_crop(None)
        t0 = time.monotonic()
        while shown() != (1920, 1080) and time.monotonic() - t0 < 10:
            time.sleep(0.05)
        whole = shown()
    finally:
        m.terminate()
    lines.append(f"2.1 preview: {ver} by {v.crop_by}: "
                 f"cut {cut}, screenshot {pic}, back {whole}")
    if cut != size or whole != (1920, 1080):
        raise RuntimeError("2.1: libmpv did not show the frame cropped as the result")


def selftest_19(win, app, src: str, tmp: str, lines: list) -> None:
    """1.9: the demux stops and leaves no half-written files; the DTS core of a real DTS-HD MA track (when the
    sample is at hand: a run from the source tree); the queue menu (encode first, join all, remove all)."""
    from . import ff, probe
    from . import job as J
    from .dialogs import DemuxDialog
    j = J.Job(src=src, settings=J.merged({}))
    j.info = probe.probe(src)
    d = DemuxDialog(j, win)
    folder = os.path.join(tmp, "demux19")
    d.folder.setText(folder)
    d._fill()
    orig = J.demux_args
    J.demux_args = lambda job, exe, items: [exe, "-readrate", "0.5"] + orig(job, exe, items)[1:]
    try:
        d.start()
        if not _wait(app, lambda: os.path.isdir(folder) and any(os.path.getsize(os.path.join(folder, f)) > 0
                                                                 for f in os.listdir(folder)), 20):
            raise RuntimeError("1.9 demux: nothing was being written")
        d.stop()
        _wait(app, lambda: not d.running, 20)
    finally:
        J.demux_args = orig
    left = os.listdir(folder) if os.path.isdir(folder) else []
    lines.append(f"1.9 demux stopped: {d.state.text()!r}, left {left}")
    if left or d.ok is not False or not d.removed:
        raise RuntimeError(f"1.9 demux stop: files left {left}")
    d.start()
    _wait(app, lambda: not d.running, 60)
    made = sorted(os.listdir(folder))
    lines.append(f"  demux: {made}")
    if not d.ok or len(made) < 2:
        raise RuntimeError(f"1.9 demux: {d.state.text()}")
    d.close()
    sample = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "data",
                          "dtshd_ma_51.dts")
    if os.path.isfile(sample):                         # a real DTS-HD MA 5.1 (24 bit, XLL) in a BDAV m2ts
        with open(sample, "rb") as f:
            frames = f.read()
        raw = os.path.join(tmp, "ma.dts")
        with open(raw, "wb") as f:
            f.write(frames * 150)
        m2ts = os.path.join(tmp, "00800.m2ts")
        r = ff.run([ff.ffmpeg(), "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24",
                    "-i", raw, "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy",
                    "-shortest", "-f", "mpegts", "-mpegts_m2ts_mode", "1", m2ts], 120)
        if r.returncode != 0:
            raise RuntimeError("1.9: making the DTS-HD m2ts failed: " + r.stderr[-300:])
        jd = J.Job(src=m2ts, settings=J.merged({}))
        jd.info = probe.probe(m2ts)
        dd = DemuxDialog(jd, win)
        dd.folder.setText(os.path.join(tmp, "demux19hd"))
        dd._fill()
        rows = dd.core_rows()
        dd.start()
        _wait(app, lambda: not dd.running, 60)
        core = [it["path"] for it in dd.done_items if it.get("core")]
        prof = probe.probe(core[0]).of("audio")[0] if core and os.path.isfile(core[0]) else None
        lines.append(f"  DTS-HD MA: {[(s.codec, s.profile) for s in jd.info.of('audio')]}, rows {rows}, core "
                     f"{os.path.getsize(core[0]) if prof else 0} B {prof.profile if prof else '-'} "
                     f"{prof.channels if prof else 0} ch")
        if not rows or prof is None or prof.codec != "dts" or "HD" in prof.profile.upper() or prof.channels != 6:
            raise RuntimeError("1.9: the DTS core of DTS-HD MA did not come out")
        dd.close()
    # the queue menu, without encoding
    win.clear_queue(ask=False)
    jobs = [J.Job(src=src, settings=J.merged({})) for _ in range(3)]
    for x in jobs:
        x.info = probe.probe(src)
        x.status = "wait"
        win._append(x)
    win._queue_changed()
    win.encode_first(jobs[2])
    if win.jobs[0] is not jobs[2]:
        raise RuntimeError("1.9: «Encode first» did not move the task up")
    n = win.join_all()
    if n != 3 or len(win.jobs) != 1 or win.jobs[0] is not jobs[2]:
        raise RuntimeError(f"1.9: «Join all»: {n} files, {len(win.jobs)} tasks")
    win.clear_queue(ask=False)
    if win.jobs or not os.path.isfile(src):
        raise RuntimeError("1.9: «Remove all» left tasks or took the file")
    lines.append("  queue menu: encode first, join all, remove all — ok")


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    from . import shellmenu
    opt = shellmenu.parse_args(args)
    paths = opt["paths"]
    cfg = config.load()
    set_lang(cfg.get("language", "ru"))
    if opt["shell"] in ("on", "off"):                     # 2.0: the installer turns Explorer's menu on / off
        cfg["shell_menu"] = opt["shell"] == "on"
        config.save(cfg)
        return 1 if shellmenu.apply(cfg) else 0
    winint.init_process(APP_ID)
    _surface_format()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")
    from . import style
    style.set_look(cfg.get("look", "classic"))
    style.set_theme(cfg.get("theme", "dark"))
    app.setStyleSheet(style.stylesheet())
    app.setWindowIcon(style.app_icon())
    title_bars = TitleBars(app)
    app.installEventFilter(title_bars)
    if cfg.get("language", "ru") == "ru":
        from PySide6.QtCore import QLibraryInfo, QTranslator
        tr = QTranslator(app)
        if tr.load("qtbase_ru", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
            app.installTranslator(tr)
    lock = start_lock() if cfg.get("single_window", True) else None
    if lock is not None and _send_to_running(paths, opt["ctx"], opt["preset"], opt["clip"]):
        lock.unlock()
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)
    if lock is not None:
        lock.unlock()                                     # listening now: the next copies hand their files over
    from .window import MainWindow
    win = MainWindow(cfg)
    win.restore_geometry()
    win.show()

    def on_conn():
        s = server.nextPendingConnection()
        buf = []

        def read():
            buf.append(bytes(s.readAll()))

        def done():
            read()
            got, ctx, preset, clip = parse_message(b"".join(buf).decode("utf-8", "replace"))
            buf.clear()
            win.bring_up()
            if got:
                win.open_from_shell(got, preset, ctx, clip)
        s.readyRead.connect(read)
        s.disconnected.connect(done)
    server.newConnection.connect(on_conn)

    def start():
        win.restore_queue()
        if paths:
            win.open_from_shell(paths, opt["preset"], opt["ctx"], opt["clip"])
    QTimer.singleShot(0, start)
    QTimer.singleShot(4000, win.maybe_check_updates)
    code = app.exec()
    server.close()
    return code