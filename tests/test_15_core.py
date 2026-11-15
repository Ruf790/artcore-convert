"""1.5, real encodes checked by ffprobe: the preset of the format (profiles, levels, keyframes, VBV, extension,
allowed codecs, sound limits, the frame, the length), the container options, the device presets, the advanced
encoder settings, the comparison of two presets — with trim, filters, two passes and joined files, and old
presets / queues that open without errors."""
import json
import os
import subprocess

import pytest

from conftest import ff as mk
from convert import advanced as ADV
from convert import compare as C
from convert import encoders as E
from convert import ff
from convert import formats as FM
from convert import job as J
from convert import probe as P


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """640×360 25 fps 4 s: AAC stereo (eng) + AC-3 5.1 (rus, default) + SRT; and a second clip to join."""
    d = str(tmp_path_factory.mktemp("c15"))
    srt = os.path.join(d, "s.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,500 --> 00:00:01,500\nHi\n")
    mk("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4",
       "-f", "lavfi", "-i", "sine=f=330:d=4", "-i", srt,
       "-filter_complex", "[2]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[a]",
       "-map", "0:v", "-map", "1:a", "-map", "[a]", "-map", "3:s", "-c:v", "libx264", "-preset", "ultrafast",
       "-c:a:0", "aac", "-c:a:1", "ac3", "-c:s", "srt", "-metadata:s:a:0", "language=eng",
       "-metadata:s:a:1", "language=rus", "-disposition:a:0", "0", "-disposition:a:1", "default",
       os.path.join(d, "clip.mkv"))
    mk("-f", "lavfi", "-i", "testsrc=s=640x360:r=25:d=2", "-f", "lavfi", "-i", "sine=f=600:d=2",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", os.path.join(d, "more.mkv"))
    return d


def src(clip, name="clip.mkv"):
    return os.path.join(clip, name)


def job(path, **st):
    j = J.Job(path, J.fit_container(J.merged(st)))
    j.info = P.probe(path)
    return j


def encode(j, out, av):
    """pre passes + the conversion, as the window runs them; returns ffprobe's streams + format."""
    exe = ff.ffmpeg()
    J.load_files(j)
    cmds = J.pre_passes(j, exe, out, av) + [J.build_args(j, exe, out, av)]
    for a in cmds:
        r = subprocess.run(a, capture_output=True, text=True)
        assert r.returncode == 0, " ".join(a) + "\n" + r.stderr[-2500:]
    J.cleanup(j)
    return probe_json(out), cmds[-1]


def probe_json(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def vstream(pj):
    return next(s for s in pj["streams"] if s["codec_type"] == "video")


def astreams(pj):
    return [s for s in pj["streams"] if s["codec_type"] == "audio"]


def key_frames(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "packet=flags",
                        "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return [i for i, ln in enumerate(r.stdout.split()) if "K" in ln]


def sei_options(path) -> str:
    """The «options: …» line x264 / x265 write into the stream."""
    data = open(path, "rb").read()
    i = data.find(b"options:")
    assert i >= 0, "no encoder options in the stream"
    return data[i:i + 2000].split(b"\x00")[0].decode("latin-1")


def fmt(**kw):
    return FM.clean(kw)


# ── the preset of the format: profiles and levels ───────────
@pytest.mark.parametrize("enc,fam,profile,level,want_prof,want_level,cont", [
    ("x264", "h264", "main", "3.1", "Main", 31, "mp4"),
    ("x264", "h264", "baseline", "3.0", "Constrained Baseline", 30, "mkv"),
    ("x265", "hevc", "main", "4.1", "Main", 123, "mkv"),
    ("svtav1", "av1", "main", "4.1", "Main", 9, "mkv"),
    ("mpeg4", "mpeg4", "simple", "3", "Simple Profile", 3, "avi"),
    ("mpeg2", "mpeg2video", "main", "main", "Main", 8, "ts"),
])
def test_profile_and_level(clip, tmp_path, av, enc, fam, profile, level, want_prof, want_level, cont):
    j = job(src(clip), container=cont, video={"encoder": enc, "bits": "10" if fam == "hevc" else "auto", "speed": 1},
            audio={"codec": "auto", "tracks": "first"}, subs="none",
            fmt=fmt(name="T", container=cont, profile={fam: profile}, level={fam: level}))
    pj, args = encode(j, str(tmp_path / f"p.{cont}"), av)
    v = vstream(pj)
    assert v["profile"] == want_prof and int(v["level"]) == want_level, (v["profile"], v["level"], args)
    if fam == "hevc":                        # Main: 8 bit, even with 10 bit asked for
        assert v["pix_fmt"] == "yuv420p"
    if profile == "simple":                  # no B-frames in Simple Profile
        assert int(v.get("has_b_frames", 0)) == 0


def test_profile_main10_keeps_10_bit_and_format_wins_over_advanced(clip, tmp_path, av):
    j = job(src(clip), video={"encoder": "x265", "bits": "10", "speed": 1}, subs="none",
            venc={"x265": {"profile": "main"}}, fmt=fmt(name="T", profile={"hevc": "main10"}))
    pj, args = encode(j, str(tmp_path / "m10.mkv"), av)
    assert vstream(pj)["profile"] == "Main 10" and vstream(pj)["pix_fmt"] == "yuv420p10le"
    assert args[args.index("-profile:v") + 1] == "main10" and args.count("-profile:v") == 1


# ── keyframes and VBV ───────────────────────────────────────
def test_gop_in_frames_and_seconds_closed(clip, tmp_path, av):
    j = job(src(clip), video={"encoder": "x264", "speed": 1}, subs="none",
            fmt=fmt(name="G", gop_max=10, gop_min=10, gop_closed=True))
    out = str(tmp_path / "g.mkv")
    _pj, args = encode(j, out, av)
    keys = key_frames(out)
    assert keys[:4] == [0, 10, 20, 30] and "-flags" in args and args[args.index("-flags") + 1] == "+cgop"
    j = job(src(clip), video={"encoder": "x265", "speed": 1}, subs="none",
            fmt=fmt(name="G", gop_max=0.5, gop_unit="sec", gop_closed=True))
    out = str(tmp_path / "g2.mkv")
    _pj, args = encode(j, out, av)
    assert max(b - a for a, b in zip(key_frames(out), key_frames(out)[1:])) <= 13           # 0.5 s at 25 fps
    assert "open-gop=0" in args[args.index("-x265-params") + 1]


def test_vbv_caps_the_bit_rate(clip, tmp_path, av):
    j = job(src(clip), video={"encoder": "x264", "rc": "bitrate", "bitrate": 6000, "speed": 1}, subs="none",
            fmt=fmt(name="V", vbv_buf=1500, vbv_max=1000))
    j.tracks = {"1": {"keep": False}, "2": {"keep": False}}            # the picture alone
    pj, args = encode(j, str(tmp_path / "v.mkv"), av)
    assert args[args.index("-maxrate") + 1] == "1000k" and args[args.index("-bufsize") + 1] == "1500k"
    assert args[args.index("-b:v") + 1] == "1000k"
    kbps = int(pj["format"]["size"]) * 8 / float(pj["format"]["duration"]) / 1000
    dur = float(pj["format"]["duration"])
    assert not astreams(pj) and kbps < (1000 + 1500 / dur) * 1.05     # max rate × time + one full buffer


# ── the extension, codecs, sound limits, the frame, the length ─
def test_extension_codecs_and_sound_limits(clip, tmp_path, av):
    f = fmt(name="TV", container="mp4", ext="m4v", vcodecs=["h264"], acodecs=["ac3", "aac"], a_maxch=2,
            max_audio=1)
    st = J.merged({"container": "mkv", "video": {"encoder": "x265"}, "audio": {"codec": "auto"}})
    FM.apply(st, f)
    assert st["container"] == "mp4" and st["video"]["encoder"] == "auto_h264"     # the format picks
    st["video"].update(encoder="x264", speed=1)
    j = J.Job(src(clip), st)
    j.info = P.probe(j.src)
    j.tracks = {"1": {"keep": False}}                              # only the 5.1 AC-3 goes out …
    out = J.out_name(j, {"out_mode": "folder", "out_dir": str(tmp_path), "out_suffix": ""})
    assert out.endswith(".m4v")
    pj, _a = encode(j, out, av)
    a = astreams(pj)
    assert pj["format"]["format_name"].startswith("mov,mp4") and len(a) == 1
    assert a[0]["codec_name"] == "ac3" and a[0]["channels"] == 2               # … mixed down to stereo
    assert "w_fmt_codec" not in J.plan(j)["warnings"]
    # sound the format doesn't take becomes its first codec; more tracks than it allows are left out
    st2 = J.merged({"container": "mkv", "video": {"encoder": "x264", "speed": 1}, "audio": {"codec": "auto"},
                    "subs": "none"})
    FM.apply(st2, fmt(name="AC3 only", container="mkv", acodecs=["ac3"], max_audio=1))
    j2 = J.Job(src(clip), st2)
    j2.info = P.probe(j2.src)
    p = J.plan(j2)
    assert [c for _s, c in p["audio"]] == ["ac3"] and "w_fmt_audio_n" in p["warnings"]
    pj2, _a = encode(j2, str(tmp_path / "ac3.mkv"), av)
    assert [s["codec_name"] for s in astreams(pj2)] == ["ac3"]


def test_frame_by_default_and_as_a_limit(clip, tmp_path, av):
    j = job(src(clip), video={"encoder": "x264", "speed": 1}, subs="none",
            fmt=fmt(name="S", width=426, height=240))
    pj, _a = encode(j, str(tmp_path / "s1.mkv"), av)
    assert (vstream(pj)["width"], vstream(pj)["height"]) == (426, 240)
    j = job(src(clip), video={"encoder": "x264", "speed": 1, "width": 640, "height": 360}, subs="none",
            fmt=fmt(name="S", width=426, height=240))
    assert J.target_box(j) == (640, 360)                              # not a limit: the chosen frame stays
    j.settings["fmt"]["size_limit"] = True
    pj, _a = encode(j, str(tmp_path / "s2.mkv"), av)
    assert (vstream(pj)["width"], vstream(pj)["height"]) == (426, 240)


def test_length_with_trim_and_joined(clip, tmp_path, av):
    j = job(src(clip), video={"encoder": "x264", "speed": 1}, subs="none", fmt=fmt(name="D", dur=1.5))
    pj, _a = encode(j, str(tmp_path / "d1.mkv"), av)
    assert abs(float(pj["format"]["duration"]) - 1.5) < 0.2
    j = job(src(clip), video={"encoder": "x264", "speed": 1}, subs="none", fmt=fmt(name="D", dur=2))
    j.trim = [[0.5, 1.5], [2.0, 3.5]]
    assert J.segments(j) == [(0.5, 1.5), (2.0, 3.0)]
    pj, _a = encode(j, str(tmp_path / "d2.mkv"), av)
    assert abs(float(pj["format"]["duration"]) - 2.0) < 0.2
    j = job(src(clip), video={"encoder": "x264", "speed": 1}, subs="none", fmt=fmt(name="D", dur=5))
    j.joins = [src(clip, "more.mkv")]
    J.load_files(j)
    pj, _a = encode(j, str(tmp_path / "d3.mkv"), av)
    assert abs(float(pj["format"]["duration"]) - 5.0) < 0.25


def test_format_with_filters_two_passes_trim_and_join(clip, tmp_path, av):
    j = job(src(clip), video={"encoder": "x264", "rc": "bitrate", "bitrate": 800, "passes": 2, "speed": 1},
            subs="none", filters={"crop": {"on": True, "mode": "manual", "t": 20, "b": 20},
                                  "color": {"on": True, "saturation": 1.3}},
            fmt=fmt(name="All", profile={"h264": "main"}, level={"h264": "3.0"}, gop_max=12, width=320,
                    height=180))
    j.trim = [[0.2, 1.2], [2.0, 3.0]]
    j.joins = [src(clip, "more.mkv")]
    J.load_files(j)
    out = str(tmp_path / "all.mkv")
    pj, args = encode(j, out, av)
    v = vstream(pj)
    assert v["profile"] == "Main" and int(v["level"]) == 30 and v["width"] == 320
    assert "-pass" in args and "concat=" in " ".join(args)
    assert max(b - a for a, b in zip(key_frames(out), key_frames(out)[1:])) <= 12


# ── the container options ───────────────────────────────────
def _ebml_pos(path, ident: bytes) -> int:
    return open(path, "rb").read().find(ident)


def test_mkv_options(clip, tmp_path, av):
    j = job(src(clip), video={"mode": "copy"}, audio={"codec": "copy"}, subs="none",
            mux={"mkv_cues_front": True, "mkv_crc32": False, "mkv_cluster_ms": 500, "mkv_default": "passthrough"})
    out = str(tmp_path / "o.mkv")
    pj, args = encode(j, out, av)
    cues, cluster = _ebml_pos(out, b"\x1c\x53\xbb\x6b"), _ebml_pos(out, b"\x1f\x43\xb6\x75")
    assert 0 < cues < cluster                                         # the index before the first cluster
    assert "-write_crc32" in args and "-cluster_time_limit" in args
    dflt = [s["disposition"]["default"] for s in astreams(pj)]
    assert dflt == [0, 1]                                             # passthrough: as the tracks have it
    j.settings["mux"] = {"mkv_index_kb": 64}
    out2 = str(tmp_path / "o2.mkv")
    _pj, args = encode(j, out2, av)
    assert args[args.index("-reserve_index_space") + 1] == str(64 * 1024)
    assert _ebml_pos(out2, b"\x1c\x53\xbb\x6b") < _ebml_pos(out2, b"\x1f\x43\xb6\x75")


def _atoms(path):
    data = open(path, "rb").read()
    out, i = [], 0
    while i + 8 <= len(data):
        size = int.from_bytes(data[i:i + 4], "big")
        out.append(data[i + 4:i + 8].decode("latin-1"))
        if size == 1:
            size = int.from_bytes(data[i + 8:i + 16], "big")
        if size < 8:
            break
        i += size
    return out


def test_mp4_faststart_fragmented_and_ts_rate(clip, tmp_path, av):
    j = job(src(clip), container="mp4", video={"mode": "copy"}, audio={"codec": "aac", "tracks": "first"},
            subs="none")
    out = str(tmp_path / "fs.mp4")
    encode(j, out, av)
    a = _atoms(out)
    assert a.index("moov") < a.index("mdat")
    j.settings["mux"] = {"mp4_faststart": False}
    out = str(tmp_path / "nofs.mp4")
    encode(j, out, av)
    a = _atoms(out)
    assert a.index("moov") > a.index("mdat")
    j.settings["mux"] = {"mp4_frag": True}
    out = str(tmp_path / "frag.mp4")
    pj, _a = encode(j, out, av)
    assert "moof" in _atoms(out) and float(pj["format"]["duration"]) > 3.5
    j = job(src(clip), container="ts", video={"mode": "copy"}, audio={"codec": "copy", "tracks": "first"},
            subs="none", mux={"ts_muxrate": 4000, "ts_pcr_ms": 20})
    out = str(tmp_path / "cbr.ts")
    _pj, args = encode(j, out, av)
    assert args[args.index("-muxrate") + 1] == "4000k" and args[args.index("-pcr_period") + 1] == "20"
    kbps = os.path.getsize(out) * 8 / 4.0 / 1000
    assert 3500 < kbps < 4600                                         # a constant 4 Mbit/s stream


# ── device presets ──────────────────────────────────────────
def test_every_device_preset_builds(clip, av):
    for _g, key, _st in FM.DEVICES:
        st = J.preset_settings(key)
        assert st["fmt"]["name"] and st["container"] in E.CONT
        j = J.Job(src(clip), st)
        j.info = P.probe(j.src)
        if J.resolve(j, av) is None:                                  # a GPU-only preset on a PC without it
            continue
        args = J.build_args(j, "ffmpeg", "/tmp/x." + J.out_ext(st), av)
        assert "-c:v" in args, key


@pytest.mark.parametrize("key", ["dvd_pal", "dvd_ntsc", "bluray", "discord10", "youtube", "iphone_hevc", "android"])
def test_device_presets_encode(clip, tmp_path, av, key):
    st = J.preset_settings(key)
    if key in ("youtube", "iphone_hevc", "android", "bluray", "discord10"):
        st["video"]["speed"] = 1
    j = J.Job(src(clip), st)
    j.info = P.probe(j.src)
    pj, args = encode(j, str(tmp_path / ("out." + J.out_ext(st))), av)
    v, a = vstream(pj), astreams(pj)
    if key.startswith("dvd"):
        assert v["codec_name"] == "mpeg2video" and v["display_aspect_ratio"] == "16:9"
        assert (v["width"], v["height"]) == ((720, 576) if key == "dvd_pal" else (720, 480))
        assert v["r_frame_rate"] == ("25/1" if key == "dvd_pal" else "30000/1001")
        assert pj["format"]["format_name"] == "mpeg" and all(x["codec_name"] == "ac3" for x in a)
        assert all(int(x["sample_rate"]) == 48000 for x in a)
    elif key == "bluray":
        assert v["profile"] == "High" and int(v["level"]) == 41 and pj["format"]["format_name"] == "mpegts"
        assert all(x["codec_name"] == "ac3" for x in a) and "bluray-compat=1" in " ".join(args)
    elif key == "discord10":
        assert os.path.getsize(args[-1]) < 10 * 1000 * 1000 and len(a) == 1 and a[0]["channels"] <= 2
        assert "-pass" in args
    elif key == "youtube":
        assert v["profile"] == "High" and len(a) == 1 and a[0]["codec_name"] == "aac"
        assert args[args.index("-g") + 1] == "12"                      # half a second at 25 fps
    elif key == "iphone_hevc":
        assert v["codec_tag_string"] == "hvc1" and v["profile"] == "Main 10" and int(v["level"]) == 153
    elif key == "android":
        assert v["profile"] == "High" and int(v["level"]) == 41 and all(x["channels"] <= 2 for x in a)


# ── the advanced encoder settings ───────────────────────────
def test_advanced_x264(clip, tmp_path, av):
    j = job(src(clip), video={"encoder": "x264", "speed": 4}, subs="none",
            venc={"x264": {"tune": "film", "ref": 2, "bframes": 0, "aq_strength": 1.3, "deblock": "-2:-1",
                           "psy_rd": "0.8:0.1", "params": "me=umh:subme=6", "level": "4.0"}})
    out = str(tmp_path / "a264.mkv")
    pj, args = encode(j, out, av)
    o = sei_options(out)
    for want in ("ref=2", "bframes=0", "aq=1:1.30", "deblock=1:-2:-1", "psy_rd=0.80:0.10", "me=umh", "subme=6"):
        assert want in o, (want, o)
    assert int(vstream(pj)["level"]) == 40 and int(vstream(pj).get("has_b_frames", 0)) == 0


def test_advanced_x265(clip, tmp_path, av):
    j = job(src(clip), video={"encoder": "x265", "speed": 1}, subs="none",
            venc={"x265": {"ref": 2, "bframes": 2, "aq_mode": "1", "psy_rd": 1.5, "deblock": "-1:-1",
                           "params": "no-sao=1", "level": "3.1"}})
    out = str(tmp_path / "a265.mkv")
    pj, args = encode(j, out, av)
    o = sei_options(out)
    for want in ("ref=2", "bframes=2", "aq-mode=1", "psy-rd=1.50", "deblock=-1:-1", "no-sao"):
        assert want in o, (want, o)
    assert int(vstream(pj)["level"]) == 93
    assert args[args.index("-x265-params") + 1].startswith("log-level=error")


def test_advanced_svt_and_vp9(clip, tmp_path, av):
    j = job(src(clip), video={"encoder": "svtav1", "speed": 1}, subs="none",
            venc={"svtav1": {"preset": 12, "film_grain": 8, "tune": "0", "overlays": True}})
    _pj, args = encode(j, str(tmp_path / "svt.mkv"), av)
    assert args[args.index("-preset") + 1] == "12"
    assert "film-grain=8" in args[args.index("-svtav1-params") + 1]
    j = job(src(clip), container="webm", video={"encoder": "vp9", "speed": 1}, subs="none",
            venc={"vp9": {"deadline": "realtime", "cpu_used": 8, "tile_columns": 2, "row_mt": True}})
    pj, args = encode(j, str(tmp_path / "vp9.webm"), av)
    assert args[args.index("-deadline") + 1] == "realtime" and args[args.index("-cpu-used") + 1] == "8"
    assert vstream(pj)["codec_name"] == "vp9"


def test_advanced_only_what_the_build_has():
    e = E.BY_ID["nvenc_hevc"]
    caps = {"preset": frozenset({"p1", "p7"}), "tune": frozenset({"hq", "ll"}), "spatial-aq": frozenset()}
    keys = [f.key for f in ADV.offered(e, caps)]
    assert keys == ["preset", "tune", "spatial_aq", "extra"]
    tune = next(f for f in ADV.offered(e, caps) if f.key == "tune")
    assert tune.values == ("", "hq", "ll")                             # uhq: not in this build
    a, p = ADV.args(e, {"tune": "uhq", "preset": "p7", "lookahead": 20, "spatial_aq": False}, caps)
    assert a == ["-preset", "p7", "-spatial-aq", "0"] and not p
    caps["split_encode_mode"] = frozenset({"auto", "forced", "disabled"})
    f = next(f for f in ADV.offered(e, caps) if f.key == "split_encode")
    assert f.values == ("", "auto", "disabled", "forced")
    assert ADV.args(e, {"split_encode": "forced"}, caps)[0] == ["-split_encode_mode", "forced"]
    # the encoder's own speed preset is replaced, not doubled
    base = E.video_args(e, {"rc": "quality", "quality": 26, "speed": 3}, 8)
    out = FM.put(base, *ADV.args(e, {"preset": "p7", "tune": "ll"}, caps))
    assert out.count("-preset") == 1 and out[out.index("-preset") + 1] == "p7" and out[out.index("-tune") + 1] == "ll"
    assert ADV.clean(e, {"lookahead": 9999, "junk": 1, "tune": ""}) == {"lookahead": 250}


def test_check_options_of_advanced_and_format_args():
    """Every option the advanced settings and the presets of the format can write is in this ffmpeg (as the
    self-test of the build does for every encoder)."""
    problems = []
    for e in E.VIDEO:
        caps = ADV.caps(e)
        if not caps:
            continue
        for f in ADV.offered(e, caps):
            if f.how in ("opt", "param", "raw") and f.opt and f.opt not in caps:
                problems.append((e.id, f.key))
        st = J.merged({"fmt": fmt(name="X", profile={e.family: (FM.PROFILES.get(e.family) or ("",))[0]},
                                  level={e.family: (FM.LEVELS.get(e.family) or ("",))[-1]}, gop_max=50,
                                  gop_min=5, gop_closed=True, vbv_buf=1000, vbv_max=2000)})
        a, p = FM.video_args(e, st, 8, "420", 25.0, caps)
        for k in a[0::2]:
            name = k.lstrip("-").split(":")[0]
            if name not in caps and name not in ("profile", "level", "g", "keyint_min", "flags", "maxrate",
                                                 "bufsize", "bf", "sc_threshold"):
                problems.append((e.id, k))
        for k in p:
            if k.lstrip("-") not in caps:
                problems.append((e.id, k))
    assert not problems


# ── old presets and queues, the library of presets of the format ─
def test_old_presets_and_queue_open(clip, tmp_path, av, data_dir):
    from convert import config
    old = {"Мой 1.0": {"container": "mkv", "video": {"encoder": "auto_hevc", "height": 720}},
           "Мой 1.3": {"container": "mp4", "video": {"encoder": "x264"}, "filters": {"crop": {"on": True}},
                       "audio": {"codec": "aac", "gain": 2}}}
    config.write("presets.json", old)
    user = config.read("presets.json")
    for name in old:
        st = J.preset_settings(name, user)
        assert st["fmt"] == {} and st["venc"] == {} and st["mux"] == FM.MUX_DEFAULT
        j = J.Job(src(clip), st)
        j.info = P.probe(j.src)
        J.build_args(j, "ffmpeg", "/tmp/o." + J.out_ext(st), av)
    q = {"src": src(clip), "settings": {"container": "mp4", "video": {"encoder": "x264"}, "fmt": "junk",
                                        "venc": None, "mux": {"mp4_frag": "yes"}}, "preset": "phone",
         "status": "wait", "trim": [[0, 1]]}
    j = J.Job.from_dict(q)
    assert j.settings["fmt"] == {} and j.settings["venc"] == {} and FM.mux(j.settings)["mp4_frag"] is True
    j.info = P.probe(j.src)
    J.build_args(j, "ffmpeg", "/tmp/o.mp4", av)
    j2 = J.Job.from_dict(json.loads(json.dumps(J.Job(src(clip), J.preset_settings("bluray")).to_dict())))
    assert j2.settings["fmt"]["name"] == "Blu-ray (H.264)" and j2.settings["venc"]["x264"]["params"]
    bad = J.merged({"fmt": {"profile": {"h264": "ultra"}, "gop_max": "x", "vcodecs": ["h264", "nope"],
                            "container": "zzz", "ext": ".M4V!"}})
    assert bad["fmt"]["profile"] == {} and bad["fmt"]["vcodecs"] == ["h264"] and bad["fmt"]["container"] == ""
    assert bad["fmt"]["ext"] == "m4v"


def test_format_library_export_import(tmp_path, data_dir):
    lib = {"Мой ТВ": fmt(name="Мой ТВ", container="mkv", vcodecs=["hevc"], profile={"hevc": "main10"})}
    FM.save_user_formats(lib)
    got = FM.user_formats()
    assert got["Мой ТВ"]["profile"] == {"hevc": "main10"}
    path = str(tmp_path / "f.acformat.json")
    FM.export_file(path, [got["Мой ТВ"], FM.BUILTIN[0]])
    back = FM.import_file(path)
    assert [f["name"] for f in back] == ["Мой ТВ", FM.BUILTIN[0]["name"]] and back[0] == got["Мой ТВ"]
    bad = str(tmp_path / "bad.json")
    with open(bad, "w") as f:
        f.write('{"hello": 1}')
    with pytest.raises(ValueError):
        FM.import_file(bad)
    assert FM.unique_name("Мой ТВ", got) == "Мой ТВ (2)"


# ── the comparison ──────────────────────────────────────────
def test_compare_two_presets(clip, tmp_path, av):
    info = P.probe(src(clip))
    res = []
    for n, key in enumerate(("telegram", "discord10")):
        st = J.preset_settings(key)
        st["video"]["speed"] = 1
        r = C.run_one(key, key, st, src(clip), info, 1.0, 2.0, av, str(tmp_path), n=n)
        assert r["ok"], r["error"]
        assert r["size"] > 0 and r["speed"] > 0 and 20 < r["psnr"] < 101 and 0.5 < r["ssim"] <= 1
        assert abs(P.probe(r["out"]).duration - 2.0) < 0.2
        res.append(r)
    b = C.best(res)
    assert b["size"] in ("telegram", "discord10") and "psnr" in b
    assert C.full_size(res[0], 4.0) == res[0]["size"] * 2
    clip_ = C.test_clip(ff.ffmpeg(), str(tmp_path), 2)
    assert abs(P.probe(clip_).duration - 2.0) < 0.1


def test_nvenc_h264_10_bit_where_the_card_has_it():
    """RTX 50: NVENC writes High 10 H.264 — once the start-up test said so, 10 bit is offered and used."""
    try:
        E.enable_h264_10(True)
        e = E.BY_ID["nvenc_h264"]
        a = E.video_args(e, {"rc": "quality", "quality": 23, "speed": 5}, 10)
        assert a[a.index("-pix_fmt") + 1] == "p010le" and a[a.index("-profile:v") + 1] == "high10"
        j = J.Job("x.mkv", J.merged({"video": {"encoder": "nvenc_h264", "bits": "10"}}))
        assert J.out_bits(j, e) == 10
        j.settings["fmt"] = fmt(name="H", profile={"h264": "high"})          # High: 8 bit
        assert J.out_bits(j, e) == 8
    finally:
        E.enable_h264_10(False)
    e = E.BY_ID["nvenc_h264"]
    a = E.video_args(e, {"rc": "quality", "quality": 23, "speed": 5}, 10)
    assert a[a.index("-pix_fmt") + 1] == "yuv420p" and a[a.index("-profile:v") + 1] == "high"
