"""ffprobe info, the plan of streams and the ffmpeg command line — no encoding here."""
import os

import pytest

from convert import encoders as E
from convert import job as J
from convert import probe as P


def _job(media, name, **st):
    j = J.Job(os.path.join(media, name), J.merged(st))
    j.info = P.probe(j.src)
    return j


def test_probe(media):
    i = P.probe(os.path.join(media, "Movie (2017).mkv"))
    assert not i.error and i.title == "Test movie" and i.chapters == 2
    v = i.video
    assert (v.codec, v.width, v.height, round(v.fps), v.bits, v.hdr) == ("h264", 1920, 1080, 25, 8, "")
    a = i.of("audio")
    assert [x.codec for x in a] == ["ac3", "aac"] and a[0].channels == 6 and a[0].lang == "rus"
    assert a[0].title == "Дубляж" and a[0].default
    assert i.of("subtitle")[0].codec == "subrip" and not i.of("subtitle")[0].bitmap
    assert "H.264 1080p" in P.summary(i) and "AC-3 5.1 +1" in P.summary(i)
    h = P.probe(os.path.join(media, "hdr.mkv")).video
    assert h.bits == 10 and h.hdr == "HDR10" and h.color["color_primaries"] == "bt2020"
    tv = P.probe(os.path.join(media, "tv.ts")).video
    assert tv.interlaced
    b = P.probe(os.path.join(media, "broken.mkv"))
    assert b.error and b.video is None


def test_plan_container_rules(media):
    j = _job(media, "dts.mkv", container="mp4", audio={"codec": "copy"})
    p = J.plan(j)
    assert p["audio"][0][1] == "aac" and "w_audio_reencoded" in p["warnings"]   # DTS doesn't go into MP4
    j = _job(media, "dts.mkv", container="mkv", audio={"codec": "auto"})
    assert J.plan(j)["audio"][0][1] == "copy"
    j = _job(media, "Movie (2017).mkv", container="mp4")
    p = J.plan(j)
    assert [c for _, c in p["audio"]] == ["copy", "copy"]                        # AC-3 + AAC fit MP4
    assert p["subs"][0][1] == "mov_text"
    j = _job(media, "Movie (2017).mkv", audio={"tracks": "first"}, subs="none")
    p = J.plan(j)
    assert len(p["audio"]) == 1 and not p["subs"]
    # the Tracks tab: drop the Russian track, the English one to Opus
    j = _job(media, "Movie (2017).mkv")
    j.tracks = {"1": {"keep": False}, "2": {"codec": "opus"}}
    assert [(s.index, c) for s, c in J.plan(j)["audio"]] == [(2, "opus")]
    j = _job(media, "Movie (2017).mkv", audio={"channels": "stereo"})
    assert all(c == "aac" for _, c in J.plan(j)["audio"])                        # down-mixing needs encoding


def test_build_args(media, av):
    j = _job(media, "Movie (2017).mkv", video={"encoder": "x265", "bits": "10", "quality": 24, "height": 720},
             audio={"codec": "ac3", "channels": "stereo"})
    a = J.build_args(j, "ffmpeg", "/out.mkv", av)
    s = " ".join(a)
    assert "-c:v libx265" in s and "-crf 24" in s and "yuv420p10le" in s and "main10" in s
    assert "scale=-2:720" in s and "-c:a:0 ac3 -ac:a:0 2 -b:a:0 224k" in s and "-f matroska" in s
    assert a[-1] == "/out.mkv" and "-progress" in a and "-map_chapters" in a
    j = _job(media, "Movie (2017).mkv", video={"encoder": "x264", "height": 2160})
    assert "scale=" not in " ".join(J.build_args(j, "ffmpeg", "/o.mkv", av))      # never upscale
    j = _job(media, "hdr.mkv", container="mp4", video={"encoder": "x265", "bits": "auto"})
    s = " ".join(J.build_args(j, "ffmpeg", "/o.mp4", av))
    assert "-color_trc smpte2084" in s and "-tag:v hvc1" in s and "+faststart" in s
    j = _job(media, "tv.ts", video={"encoder": "x264"})
    assert "bwdif" in " ".join(J.build_args(j, "ffmpeg", "/o.mkv", av))
    j = _job(media, "Movie (2017).mkv", video={"mode": "copy"}, audio={"codec": "copy"})
    s = " ".join(J.build_args(j, "ffmpeg", "/o.mkv", av))
    assert "-c:v copy" in s and "-hwaccel" not in s and j.enc_used == "copy"
    j = _job(media, "Movie (2017).mkv", video={"encoder": "nvenc_hevc"})
    if not av.ok.get("nvenc_hevc"):
        with pytest.raises(RuntimeError):
            J.build_args(j, "ffmpeg", "/o.mkv", av)
    j = _job(media, "Movie (2017).mkv", video={"rc": "size", "size_mb": 10, "encoder": "x264"})
    s = " ".join(J.build_args(j, "ffmpeg", "/o.mkv", av))
    assert "-b:v" in s and "-crf" not in s


@pytest.mark.parametrize("enc", E.VIDEO, ids=lambda e: e.id)
def test_every_encoder_args(enc):
    """Every encoder in every mode makes sense: its speed preset, the rate control, 10 bit where it can."""
    for rc in ("quality", "bitrate", "size"):
        for speed in (1, 4, 7):
            a = E.video_args(enc, {"rc": rc, "quality": enc.q_default, "bitrate": 5000, "size_mb": 700,
                                   "speed": speed}, max(enc.bits), 3600)
            assert "-pix_fmt" in a
            if rc == "quality":
                assert any(x in a for x in ("-cq", "-crf", "-qp_i", "-global_quality"))
            else:
                assert "-b:v" in a
    if 10 in enc.bits:
        assert E.pix_fmt(enc, 10) in ("p010le", "yuv420p10le")
    assert E.pix_fmt(enc, 8) in ("yuv420p", "nv12")


def test_auto_resolution():
    av = E.Available({"nvenc_hevc": False, "amf_hevc": True, "x265": True, "x264": True})
    assert av.resolve("auto_hevc").id == "amf_hevc" and av.gpu() == "amd"
    av = E.Available({"x265": True})
    assert av.resolve("auto_hevc").id == "x265" and av.gpu() == ""
    assert av.resolve("auto_av1") is None and not av.works("nvenc_hevc")
    av = E.Available({"nvenc_hevc": True, "amf_hevc": True, "x265": True})
    assert av.resolve("auto_hevc").id == "nvenc_hevc"


def test_target_size():
    k = E.target_kbps(700, 3600, 192)          # a 700 MB, one hour movie with 192k audio
    assert 1300 < k < 1450


def test_out_name(tmp_path):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    j = J.Job(str(src), J.merged({"container": "mkv"}))
    cfg = {"out_mode": "source", "out_suffix": ""}
    assert J.out_name(j, cfg) == str(tmp_path / "a_convert.mkv")           # never over the source
    cfg["out_suffix"] = "_c"
    (tmp_path / "a_c.mkv").write_bytes(b"x")
    assert J.out_name(j, cfg) == str(tmp_path / "a_c (2).mkv")
    cfg["overwrite"] = True
    assert J.out_name(j, cfg) == str(tmp_path / "a_c.mkv")
    cfg.update(out_mode="folder", out_dir=str(tmp_path / "out"))
    assert J.out_name(j, cfg) == str(tmp_path / "out" / "a_c.mkv")


def test_presets_and_queue_roundtrip():
    for key in J.PRESETS:
        st = J.preset_settings(key)
        assert st["video"]["mode"] in ("encode", "copy") and st["container"] in ("mkv", "mp4")
        assert J.describe(st)
    j = J.Job("/x/a.mkv", J.preset_settings("phone"), preset="phone", tracks={"1": {"keep": False}})
    j.status = "run"
    k = J.Job.from_dict(j.to_dict())
    assert k.status == "wait" and k.settings == j.settings and k.tracks == j.tracks and k.preset == "phone"
