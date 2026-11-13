"""Real encodes through the Runner: x264 → MP4, x265 10 bit, SVT-AV1, remux, audio to AC-3, pause, cancel."""
import os
import time

import pytest

from conftest import pump
from convert import ff
from convert import job as J
from convert import probe as P
from convert.runner import Runner


def _run(qapp, media, tmp_path, av, name, st, tracks=None, cancel_at=None, pause=False):
    j = J.Job(os.path.join(media, name), J.merged(st), tracks=tracks or {})
    j.info = P.probe(j.src)
    out = str(tmp_path / ("out." + j.settings["container"]))
    args = J.build_args(j, ff.ffmpeg(), out, av)
    r = Runner()
    res, prog, logs = [], [], []
    r.finished.connect(lambda ok, e: res.append((ok, e)))
    r.progress.connect(prog.append)
    r.log.connect(logs.append)
    r.start(args, out, j.info.duration)
    if pause:
        assert pump(qapp, 10, lambda: prog)
        assert r.pause(True) and r.paused
        n = len(prog)
        pump(qapp, 0.8)
        assert len(prog) <= n + 1                       # nothing moves while paused
        assert r.pause(False) and not r.paused
    if cancel_at is not None:
        assert pump(qapp, 10, lambda: prog and prog[-1]["frac"] > cancel_at)
        r.cancel()
    assert pump(qapp, 120, lambda: res)
    return j, out, res[0], prog, logs


def test_x264_mp4_aac(qapp, media, tmp_path, av):
    j, out, (ok, err), prog, logs = _run(qapp, media, tmp_path, av, "Movie (2017).mkv",
                                         {"container": "mp4", "video": {"encoder": "x264", "speed": 1},
                                          "audio": {"codec": "aac", "channels": "stereo"}})
    assert ok and not err and os.path.isfile(out) and not os.path.exists(out + ".part")
    assert prog[-1]["frac"] == 1.0 and prog[-1]["speed"] > 0
    i = P.probe(out)
    assert i.format.startswith("mov") and i.video.codec == "h264"
    assert [a.codec for a in i.of("audio")] == ["aac", "aac"] and [a.channels for a in i.of("audio")] == [2, 1]   # 5.1 → stereo, mono stays
    assert i.of("subtitle")[0].codec == "mov_text" and i.title == "Test movie" and i.chapters == 2
    assert logs[0].startswith("$ ")


def test_x265_10bit_hdr_keeps_colours(qapp, media, tmp_path, av):
    j, out, (ok, err), *_ = _run(qapp, media, tmp_path, av, "hdr.mkv",
                                 {"video": {"encoder": "x265", "bits": "auto", "speed": 1}})
    assert ok, err
    v = P.probe(out).video
    assert v.codec == "hevc" and v.bits == 10 and v.hdr == "HDR10"


def test_svtav1(qapp, media, tmp_path, av):
    if not av.ok.get("svtav1"):
        pytest.skip("no libsvtav1 in this ffmpeg")
    j, out, (ok, err), *_ = _run(qapp, media, tmp_path, av, "tv.ts",
                                 {"video": {"encoder": "svtav1", "speed": 1, "bits": "10"},
                                  "audio": {"codec": "opus"}})
    assert ok, err
    i = P.probe(out)
    assert i.video.codec == "av1" and i.video.bits == 10 and not i.video.interlaced
    assert i.of("audio")[0].codec == "opus"


def test_remux_and_audio_ac3(qapp, media, tmp_path, av):
    j, out, (ok, err), *_ = _run(qapp, media, tmp_path, av, "dts.mkv",
                                 J.preset_settings("audio_ac3"))
    assert ok, err
    i = P.probe(out)
    src = P.probe(os.path.join(media, "dts.mkv"))
    assert i.video.codec == "h264" and src.video.codec == "h264"
    a = i.of("audio")[0]
    assert a.codec == "ac3" and a.channels == 6 and a.bitrate == 640000


def test_tracks_choice(qapp, media, tmp_path, av):
    j, out, (ok, err), *_ = _run(qapp, media, tmp_path, av, "Movie (2017).mkv",
                                 {"video": {"mode": "copy"}, "subs": "none"},
                                 tracks={"1": {"keep": False}, "2": {"codec": "flac"}})
    assert ok, err
    i = P.probe(out)
    assert [(a.codec, a.lang) for a in i.of("audio")] == [("flac", "eng")] and not i.of("subtitle")


def test_pause_resume(qapp, media, tmp_path, av):
    j, out, (ok, err), prog, _ = _run(qapp, media, tmp_path, av, "Movie (2017).mkv",
                                      {"video": {"encoder": "x265", "speed": 7}}, pause=True)
    assert ok, err


def test_cancel_deletes_the_part(qapp, media, tmp_path, av):
    j, out, (ok, err), *_ = _run(qapp, media, tmp_path, av, "Movie (2017).mkv",
                                 {"video": {"encoder": "x265", "speed": 7}}, cancel_at=0.05)
    assert not ok and not err                               # cancelled: no error text
    time.sleep(0.3)
    assert not os.path.exists(out) and not os.path.exists(out + ".part")


def test_failure_reports_ffmpeg(qapp, media, tmp_path, av):
    j = J.Job(os.path.join(media, "Movie (2017).mkv"), J.merged({"video": {"encoder": "x264"}}))
    j.info = P.probe(j.src)
    out = str(tmp_path / "x.mkv")
    args = J.build_args(j, ff.ffmpeg(), out, av)
    args.insert(args.index("-c:v") + 2, "-bogus_option_xyz")
    args.insert(args.index("-bogus_option_xyz") + 1, "1")
    r = Runner()
    res = []
    r.finished.connect(lambda ok, e: res.append((ok, e)))
    r.start(args, out, j.info.duration)
    assert pump(qapp, 30, lambda: res)
    ok, err = res[0]
    assert not ok and "bogus_option_xyz" in err and not os.path.exists(out + ".part")
