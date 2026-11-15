"""Entry point: one window; files sent from Explorer («Convert with ArtCore Convert») go to the running copy.
ArtCoreConvert.exe [files / folders …]"""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QWidget

from . import APP_ID, APP_TITLE, VERSION, config, winint
from .i18n import set_lang

SERVER_NAME = "ArtCoreConvert-" + (os.environ.get("USERNAME") or os.environ.get("USER") or "user")


def _send_to_running(paths: list[str]) -> bool:
    s = QLocalSocket()
    s.connectToServer(SERVER_NAME)
    if not s.waitForConnected(400):
        return False
    s.write(("\n".join(os.path.abspath(p) for p in paths) if paths else "--show").encode("utf-8"))
    s.waitForBytesWritten(1000)
    s.disconnectFromServer()
    return True


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
    win.cfg.update(parallel=2, metrics=["psnr", "ssim"] + (["vmaf"] if vmaf else []), show_frame=True)
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


def main() -> int:
    args = sys.argv[1:]
    if "--selftest" in args:
        return selftest()
    paths = [a for a in args if not a.startswith("--")]
    cfg = config.load()
    set_lang(cfg.get("language", "ru"))
    winint.init_process(APP_ID)
    _surface_format()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")
    from . import style
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
    if cfg.get("single_window", True) and _send_to_running(paths):
        return 0
    server = QLocalServer()
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)
    from .window import MainWindow
    win = MainWindow(cfg)
    win.restore_geometry()
    win.show()

    def on_conn():
        s = server.nextPendingConnection()

        def read():
            data = bytes(s.readAll()).decode("utf-8", "replace")
            got = [ln for ln in data.splitlines() if ln and not ln.startswith("--")]
            if win.isMinimized():
                win.showNormal()
            win.raise_()
            win.activateWindow()
            if got:
                win.add_paths(got)
        s.readyRead.connect(read)
    server.newConnection.connect(on_conn)

    def start():
        win.restore_queue()
        if paths:
            win.add_paths(paths)
    QTimer.singleShot(0, start)
    QTimer.singleShot(4000, win.maybe_check_updates)
    code = app.exec()
    server.close()
    return code
