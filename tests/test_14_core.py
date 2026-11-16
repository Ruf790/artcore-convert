"""1.4, real encodes checked by ffprobe: two passes, the CPU limit, joined files, external tracks, quality metrics,
the split into parts, the current frame, the speed test — and that all of it lives with trim and filters."""
import os
import subprocess

import pytest

from conftest import ff as mk, pump
from convert import bench as B
from convert import encoders as E
from convert import ff
from convert import job as J
from convert import probe as P
from convert.runner import Runner


def run_job(qapp, j, out, av, metrics=(), threads=0, preview="", cores=None):
    """pre passes + the conversion + the passes after it, through the Runner (as the window runs them)."""
    if j.info is None:
        j.info = P.probe(j.src)
    exe = ff.ffmpeg()
    pre = J.pre_passes(j, exe, out, av, threads=threads, preview=preview)
    args = J.build_args(j, exe, out, av, threads=threads, preview=preview)
    post = J.post_passes(j, exe, out, av, metrics, threads=threads)
    r = Runner()
    res, logs, prog = [], [], []
    r.finished.connect(lambda ok, e: res.append((ok, e)))
    r.log.connect(logs.append)
    r.progress.connect(prog.append)
    r.start(args, out, J.out_duration(j), pre=pre, post=post, cores=cores)
    assert pump(qapp, 300, lambda: res), "the run did not finish"
    J.cleanup(j)
    ok, err = res[0]
    assert ok, err + "\n" + "\n".join(logs[-30:])
    return P.probe(out), logs, (pre, args, post), r


@pytest.fixture(scope="module")
def extra(media, tmp_path_factory):
    """A folder of its own (the media folder is counted by other tests): the film, a second different file to
    join (640×360, 30 fps, mono 44.1 kHz AAC), external AC-3 / SRT, and a 6 s film with a keyframe every second
    (for the split)."""
    import shutil
    d = str(tmp_path_factory.mktemp("extra"))
    for n in ("Movie (2017).mkv", "dts.mkv"):
        shutil.copy(os.path.join(media, n), d)
    mk("-f", "lavfi", "-i", "testsrc=s=640x360:r=30:d=2", "-f", "lavfi", "-i", "sine=f=300:d=2:sample_rate=44100",
       "-ac", "1", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", os.path.join(d, "other.mp4"))
    mk("-f", "lavfi", "-i", "sine=f=880:d=4", "-ac", "2", "-c:a", "ac3", os.path.join(d, "Movie (2017).eng.ac3"))
    with open(os.path.join(d, "Movie (2017).bul.srt"), "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,200 --> 00:00:01,000\nЗдравей\n\n2\n00:00:02,000 --> 00:00:02,600\nЧао\n")
    mk("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=6", "-f", "lavfi", "-i", "sine=f=500:d=6",
       "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "aac", "-b:a", "128k",
       os.path.join(d, "gop.mkv"))
    return d


def movie(media):
    return os.path.join(media, "Movie (2017).mkv")


def size_mb(path):
    return os.path.getsize(path) / 1048576


# ── 1. two passes ────────────────────────────────────────────
@pytest.mark.parametrize("enc", ["x264", "x265", "vp9", "mpeg4"])
def test_two_pass_hits_the_size(qapp, media, tmp_path, av, enc):
    cont = "webm" if enc == "vp9" else "mkv"
    j = J.Job(movie(media), J.merged({"video": {"encoder": enc, "rc": "size", "size_mb": 1, "passes": 2, "speed": 2},
                                       "audio": {"codec": "opus" if enc == "vp9" else "aac", "kbps": 96},
                                       "container": cont, "subs": "none"}))
    out = str(tmp_path / f"two.{cont}")
    info, logs, (pre, args, post), r = run_job(qapp, j, out, av)
    assert len(pre) == 1 and r.phases == 2                    # the first pass, then the encode
    first = " ".join(pre[0])
    if enc == "x265":
        assert "pass=1:stats=" in first and "pass=2:stats=" in " ".join(args)
    else:
        assert "-pass:v 1" in first and "-pass:v 2" in " ".join(args) and "-f null" in first
    assert "-an" in pre[0]                                    # the first pass writes no sound
    assert info.video is not None and abs(info.duration - 3) < 0.3
    assert 0.6 < size_mb(out) < 1.35, size_mb(out)            # what was asked, give or take
    assert not [f for f in os.listdir(os.path.dirname(J.pass_prefix(j)))
                if f.startswith(os.path.basename(J.pass_prefix(j)))]       # statistics cleaned up


def test_two_pass_with_trim_and_filters(qapp, media, tmp_path, av):
    j = J.Job(movie(media), J.merged({"video": {"encoder": "x264", "rc": "bitrate", "bitrate": 1500, "passes": 2,
                                                 "speed": 1}, "subs": "none"}))
    j.trim = [[0.4, 1.4], [2.0, 2.8]]
    j.settings["filters"]["crop"] = {"on": True, "mode": "manual", "l": 0, "t": 140, "r": 0, "b": 140}
    j.settings["filters"]["deshake"] = {"on": True, "method": "vidstab", "strength": 5}
    out = str(tmp_path / "trim2.mkv")
    info, logs, (pre, args, post), r = run_job(qapp, j, out, av)
    assert len(pre) == 2 and "vidstabdetect" in " ".join(pre[0]) and "-pass:v 1" in " ".join(pre[1])
    assert "vidstabtransform" in " ".join(pre[1]) and "concat=n=2" in " ".join(pre[1])
    assert info.video.height == 800 and abs(info.duration - 1.8) < 0.25


def test_nvenc_two_pass_is_one_run():
    e = E.BY_ID["nvenc_hevc"]
    a = E.video_args(e, {"rc": "bitrate", "bitrate": 5000, "speed": 3, "passes": 2}, 8, 60)
    assert a[a.index("-multipass") + 1] == "fullres"
    a = E.video_args(e, {"rc": "quality", "quality": 26, "speed": 6, "passes": 2}, 8, 60)
    assert a[a.index("-multipass") + 1] == "qres"           # quality mode: no two passes
    av = E.Available({"nvenc_hevc": True, "x264": True, "svtav1": True})
    assert not av.two_pass(e) and av.two_pass(E.BY_ID["x264"])
    assert av.two_pass(E.BY_ID["svtav1"]) is False          # only where the build writes statistics


def test_two_pass_only_for_rate_modes(media, av):
    j = J.Job(movie(media), J.merged({"video": {"encoder": "x264", "rc": "quality", "passes": 2}}))
    j.info = P.probe(j.src)
    assert J.pre_passes(j, "ffmpeg", "o.mkv", av) == []
    assert not {"-pass", "-pass:v"} & set(J.build_args(j, "ffmpeg", "o.mkv", av))
    assert "2-pass" not in J.describe(j.settings, av)
    j.settings["video"]["rc"] = "bitrate"
    from convert.i18n import t
    assert t("two_pass_short") in J.describe(j.settings, av, t)


# ── 3. the CPU limit ─────────────────────────────────────────
def test_cpu_limit_threads_and_cores(qapp, media, tmp_path, av):
    j = J.Job(movie(media), J.merged({"video": {"encoder": "x265", "speed": 7}, "subs": "none"}))
    out = str(tmp_path / "cpu.mkv")
    j.info = P.probe(j.src)
    args = J.build_args(j, "ffmpeg", out, av, threads=1)
    s = " ".join(args)
    assert "-filter_threads 1" in s and "pools=1" in s and s.count("-threads 1") >= 1
    j2 = J.Job(movie(media), J.merged({"video": {"encoder": "x264"}}))
    j2.info = j.info
    a2 = J.build_args(j2, "ffmpeg", out, av, threads=2)
    assert a2[a2.index("libx264") + 1:].count("-threads") == 1
    seen = []
    info, logs, _c, r = run_job(qapp, j, out, av, threads=1, cores=[0])
    import psutil
    r2 = Runner()
    r2.started_cores = None

    def look(_d):
        if r2.proc is not None and r2.proc.processId() and not seen:
            try:
                seen.append(psutil.Process(int(r2.proc.processId())).cpu_affinity())
            except Exception:
                seen.append(None)
    done = []
    r2.progress.connect(look)
    r2.finished.connect(lambda ok, e: done.append(ok))
    r2.start(J.build_args(j, ff.ffmpeg(), out, av, threads=1), out, 3.0, cores=[0])
    assert pump(qapp, 120, lambda: done) and done[0]
    if seen and seen[0] is not None:
        assert seen[0] == [0]
    assert info.video is not None


# ── 8. external tracks ───────────────────────────────────────
def test_external_audio_and_subs(qapp, extra, tmp_path, av):
    j = J.Job(movie(extra), J.merged({"video": {"encoder": "x264", "speed": 7}}))
    j.ext = [J.clean_ext({"path": os.path.join(extra, "Movie (2017).eng.ac3"), "kind": "audio", "delay": 500,
                          "lang": "eng", "title": "Commentary", "default": True}),
             J.clean_ext({"path": os.path.join(extra, "Movie (2017).bul.srt"), "kind": "subtitle", "delay": -100,
                          "lang": "bul"})]
    out = str(tmp_path / "ext.mkv")
    info, logs, (pre, args, post), r = run_job(qapp, j, out, av)
    s = " ".join(args)
    assert "-itsoffset 0.500" in s and "-ss 0.100" in s
    auds, subs = info.of("audio"), info.of("subtitle")
    assert len(auds) == 3 and auds[2].codec == "ac3" and auds[2].lang == "eng" and auds[2].title == "Commentary"
    assert auds[2].default and not auds[0].default             # the outside track was made the default one
    assert len(subs) == 2 and subs[1].lang == "bul"
    # the delay really moved the sound: the external track starts 0.5 s later
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:2", "-show_entries", "packet=pts_time",
                        "-of", "csv=p=0", "-read_intervals", "%+#1", out], capture_output=True, text=True)
    assert abs(float(r.stdout.split()[0].strip(",")) - 0.5) < 0.06


def test_external_audio_with_trim_is_cut_too(qapp, extra, tmp_path, av):
    j = J.Job(movie(extra), J.merged({"video": {"encoder": "x264", "speed": 7}, "audio": {"tracks": "first"}}))
    j.trim = [[0.5, 1.5], [2.0, 3.0]]
    j.ext = [J.clean_ext({"path": os.path.join(extra, "Movie (2017).eng.ac3"), "kind": "audio", "delay": 300}),
             J.clean_ext({"path": os.path.join(extra, "Movie (2017).bul.srt"), "kind": "subtitle"})]
    out = str(tmp_path / "ext_trim.mkv")
    j.info = P.probe(j.src)
    p = J.plan(j)
    assert "w_trim_subs" in p["warnings"]
    assert [c for s_, c in p["audio"] if J.ext_no(s_) is not None] != ["copy"]      # cut: encoded
    info, logs, (pre, args, post), r = run_job(qapp, j, out, av)
    assert "adelay=delays=300" in " ".join(args) and "asplit=2" in " ".join(args)
    auds = info.of("audio")
    assert len(auds) == 2 and abs(info.duration - 2.0) < 0.2
    assert not info.of("subtitle")


def test_external_kind_and_language_guess():
    from convert.window import _lang_from_name
    assert J.ext_kind("a.SUP") == "subtitle" and J.ext_kind("x.dts") == "audio"
    assert _lang_from_name("C:/f/Film.rus.ac3", "C:/f/Film.mkv") == "rus"
    assert _lang_from_name("Film_English.srt", "Film.mkv") == "eng"
    assert _lang_from_name("Film.srt", "Film.mkv") == ""


# ── 9. joining files ─────────────────────────────────────────
def test_join_different_files(qapp, extra, tmp_path, av):
    j = J.Job(movie(extra), J.merged({"video": {"encoder": "x264", "speed": 7}, "audio": {"tracks": "first"}}))
    j.joins = [os.path.join(extra, "other.mp4"), os.path.join(extra, "dts.mkv")]
    out = str(tmp_path / "join.mkv")
    info, logs, (pre, args, post), r = run_job(qapp, j, out, av)
    s = " ".join(args)
    assert "concat=n=3:v=1:a=1" in s and "scale=1920:1080" in s and "aresample=44100" in s and "fps=25" in s
    assert abs(info.duration - 7.0) < 0.35                     # 3 + 2 + 2
    assert info.video.width == 1920 and len(info.of("audio")) == 1
    assert [c[2] for c in info.chapter_list] == ["Movie (2017)", "other", "dts"]   # a chapter per file
    assert J.src_duration(j) == pytest.approx(7.0, abs=0.2)


def test_join_with_trim_across_the_files(qapp, extra, tmp_path, av):
    j = J.Job(movie(extra), J.merged({"video": {"encoder": "x264", "speed": 7}, "subs": "none"}))
    j.joins = [os.path.join(extra, "other.mp4")]
    j.info = P.probe(j.src)
    J.load_files(j)
    j.trim = [[2.0, 4.0]]                                      # the last second of one, the first of the other
    assert J.pieces(j) == [(0, 2.0, pytest.approx(3.0, abs=0.05)), (1, 0.0, pytest.approx(1.0, abs=0.05))]
    j.settings["filters"]["color"] = {"on": True, "brightness": 0.05, "contrast": 1.1, "saturation": 1.2,
                                      "gamma": 1.0, "hue": 0, "temp": 6500}
    out = str(tmp_path / "jt.mkv")
    info, *_ = run_job(qapp, j, out, av)
    assert abs(info.duration - 2.0) < 0.2 and len(info.of("audio")) == 2   # the missing track: silence


def test_join_copy_uses_the_concat_list(qapp, extra, tmp_path, av):
    j = J.Job(os.path.join(extra, "gop.mkv"), J.merged({"video": {"mode": "copy"}, "audio": {"codec": "copy"}}))
    j.joins = [os.path.join(extra, "gop.mkv")]
    out = str(tmp_path / "jc.mkv")
    info, logs, (pre, args, post), r = run_job(qapp, j, out, av)
    assert "concat" in args and "-c:v" in args and args[args.index("-c:v") + 1] == "copy"
    assert abs(info.duration - 12.0) < 0.3


# ── 5. quality metrics ───────────────────────────────────────
def test_metrics_psnr_ssim(qapp, media, tmp_path, av):
    j = J.Job(movie(media), J.merged({"video": {"encoder": "x264", "quality": 20, "speed": 5}}))
    j.trim = [[0.5, 2.5]]
    j.settings["filters"]["crop"] = {"on": True, "mode": "manual", "l": 0, "t": 140, "r": 0, "b": 140}
    out = str(tmp_path / "m.mkv")
    info, logs, (pre, args, post), r = run_job(qapp, j, out, av, metrics=("psnr", "ssim", "vmaf"))
    want = 3 if "libvmaf" in av.filters else 2
    assert len(post) == 1 and r.phases == 2 and post[0].count("-i") == 2
    m = J.parse_metrics(logs)
    assert len(m) == want
    assert 38 < m["psnr"] < 100 and 0.95 < m["ssim"] <= 1.0
    # a worse encode measures worse
    j.settings["video"]["quality"] = 45
    info, logs2, *_ = run_job(qapp, j, out, av, metrics=("psnr",))
    assert J.parse_metrics(logs2)["psnr"] < m["psnr"] - 3


def test_metrics_lines():
    m = J.parse_metrics(["[Parsed_psnr_3 @ 0x1] PSNR y:41.2 u:43.0 v:43.6 average:41.92 min:39.10 max:44.26",
                         "[Parsed_ssim_4 @ 0x2] SSIM Y:0.98 (17.2) U:0.99 (20.1) V:0.99 (20.3) All:0.985123 (18.27)",
                         "[Parsed_libvmaf_5 @ 0x3] VMAF score: 95.120000"])
    assert m == {"psnr": 41.92, "ssim": 0.985123, "vmaf": 95.12}


# ── 6. the current frame ─────────────────────────────────────
def test_current_frame_is_written(qapp, media, tmp_path, av):
    j = J.Job(movie(media), J.merged({"video": {"encoder": "x264", "speed": 7}}))
    png = str(tmp_path / "frame.png")
    out = str(tmp_path / "f.mkv")
    info, logs, (pre, args, post), r = run_job(qapp, j, out, av, preview=png)
    assert args[-1] == png and out + ".part" not in args          # the Runner swaps only the result's path
    from PySide6.QtGui import QPixmap
    pm = QPixmap(png)
    assert not pm.isNull() and pm.width() == 320
    assert info.video is not None and len(info.streams) == len(P.probe(out).streams)
    plain = J.build_args(j, "ffmpeg", out, av)                    # the copied command has no preview
    assert "[vprev]" not in " ".join(plain)


# ── 10. the split ────────────────────────────────────────────
def test_split_by_time(qapp, extra, tmp_path, av):
    src = os.path.join(extra, "gop.mkv")
    j = J.Job(src, J.merged({"video": {"mode": "copy"}, "audio": {"codec": "copy"},
                             "split": {"mode": "time", "minutes": 0.04}}))      # 2.4 s
    out = str(tmp_path / "Film.mkv")
    info, *_ = run_job(qapp, j, out, av)
    assert J.split_on(j)
    r = Runner()
    res = []
    r.finished.connect(lambda ok, e: res.append(ok))
    r.start(J.split_args(j, ff.ffmpeg(), out), J.split_pattern(out), 6.0, direct=True)
    assert pump(qapp, 60, lambda: res) and res[0]
    parts = J.split_parts(out)
    assert [os.path.basename(p) for p in parts] == ["Film_01.mkv", "Film_02.mkv", "Film_03.mkv"]
    durs = [P.probe(p).duration for p in parts]
    assert abs(sum(durs) - 6.0) < 0.2 and all(len(P.probe(p).of("audio")) == 1 for p in parts)


def test_split_by_size(qapp, extra, tmp_path, av):
    src = os.path.join(extra, "gop.mkv")
    j = J.Job(src, J.merged({"video": {"mode": "copy"}, "audio": {"codec": "copy"}}))
    out = str(tmp_path / "Big.mkv")
    run_job(qapp, j, out, av)
    whole = os.path.getsize(out)
    limit_mb = whole / 3.2 / 1048576
    times = J.size_times(ff.ffprobe(), out, limit_mb)
    assert len(times) >= 3
    r = Runner()
    res = []
    r.finished.connect(lambda ok, e: res.append(ok))
    j.settings["split"] = {"mode": "size", "size_mb": limit_mb, "minutes": 20}
    r.start(J.split_args(j, ff.ffmpeg(), out, times), J.split_pattern(out), 6.0, direct=True)
    assert pump(qapp, 60, lambda: res) and res[0]
    parts = J.split_parts(out)
    assert len(parts) == len(times) + 1
    assert all(os.path.getsize(p) <= limit_mb * 1048576 for p in parts)


def test_cut_times_logic():
    pk = [(i * 0.04, 1000, i % 25 == 0) for i in range(250)]          # 10 s, a key every second, 25 kB / s
    assert J.cut_times(pk, 60000 + 65536) == pytest.approx([2.0, 4.0, 6.0, 8.0])
    assert J.cut_times(pk, 10 ** 9) == []


# ── 2. the speed test ────────────────────────────────────────
def test_bench_runs_encoders(media, av):
    src, start, length = B.piece(movie(media), 3.0)
    assert src and length == 3.0 and start == 0.0
    assert B.piece(None, 0)[0] is None
    for e in (E.BY_ID["x264"], E.BY_ID["mpeg4"]):
        r = B.run_one(e, ff.ffmpeg(), ff.ffprobe(), src, 0.0, 1.0)
        assert r["ok"], r
        assert r["fps"] > 0 and r["size"] > 0 and r["speed"] > 0
    r = B.run_one(E.BY_ID["x264"], ff.ffmpeg(), ff.ffprobe(), None, 0, 1.0, threads=1)
    assert r["ok"]
    assert all(e.id != "gif" for e in B.encoders(av)) and E.BY_ID["x264"] in B.encoders(av)


# ── everything together, in the copied command ───────────────
def test_everything_in_the_copied_command(media, extra, av):
    j = J.Job(movie(media), J.merged({"video": {"encoder": "x264", "rc": "bitrate", "bitrate": 2000,
                                                 "passes": 2}, "split": {"mode": "size", "size_mb": 1}}))
    j.info = P.probe(j.src)
    j.joins = [os.path.join(extra, "other.mp4")]
    j.ext = [J.clean_ext({"path": os.path.join(extra, "Movie (2017).eng.ac3"), "kind": "audio"})]
    J.load_files(j)
    times = J.estimate_size_times(j, av)
    assert times and all(a < b for a, b in zip(times, times[1:]))
    cmd = J.split_args(j, "ffmpeg", "o.mkv", times)
    assert "-segment_times" in cmd and cmd[-1] == "o_%02d.mkv"
    d = J.Job.from_dict(j.to_dict())
    assert d.joins == j.joins and d.ext == j.ext and d.settings["split"]["mode"] == "size"
    assert d.settings["video"]["passes"] == 2
