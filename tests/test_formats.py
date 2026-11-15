"""1.1: more containers and codecs, trim (one piece, several, cut out, copy), file / track metadata, chapters —
the command lines and real encodes read back with ffprobe."""
import os
import subprocess

import pytest

from conftest import pump
from convert import encoders as E
from convert import ff
from convert import job as J
from convert import probe as P
from convert.runner import Runner


def _job(media, name, **st):
    j = J.Job(os.path.join(media, name), J.fit_container(J.merged(st)))
    j.info = P.probe(j.src)
    return j


def _encode(qapp, j, av, tmp_path, name="out"):
    out = str(tmp_path / (name + "." + E.container(j.settings["container"]).ext))
    args = J.build_args(j, ff.ffmpeg(), out, av, work_dir=str(tmp_path / "work"))
    r = Runner()
    res = []
    r.finished.connect(lambda ok, e: res.append((ok, e)))
    r.start(args, out, J.out_duration(j))
    assert pump(qapp, 180, lambda: res)
    ok, err = res[0]
    assert ok, f"{err}\n{' '.join(args)}\n" + "\n".join(r.tail[-8:])
    return P.probe(out), args


@pytest.fixture(scope="module")
def keyed(tmp_path_factory):
    """A clip with a keyframe every half second (for cutting without encoding)."""
    out = str(tmp_path_factory.mktemp("keyed") / "keyed.mkv")
    if not os.path.isfile(out):
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24:d=4",
                        "-f", "lavfi", "-i", "sine=f=440:d=4", "-c:v", "libx264", "-preset", "ultrafast", "-g", "12",
                        "-c:a", "aac", "-metadata:s:a:0", "language=rus", out], check=True)
    return out


# ── containers ───────────────────────────────────────────────
def test_fit_container():
    st = J.fit_container(J.merged({"container": "webm"}))
    assert st["video"]["encoder"] == "vp9"
    st = J.fit_container(J.merged({"container": "avi", "audio": {"codec": "opus"}}))
    assert st["video"]["encoder"] == "auto_mpeg4" and st["audio"]["codec"] == "auto"
    st = J.fit_container(J.merged({"container": "gif", "video": {"mode": "copy"}}))
    assert st["video"]["encoder"] == "gif" and st["video"]["mode"] == "encode"
    st = J.fit_container(J.merged({"container": "mkv", "video": {"encoder": "gif"}}))
    assert st["video"]["encoder"] == "auto_hevc"                 # GIF only goes into .gif
    st = J.fit_container(J.merged({"container": "mov", "video": {"encoder": "prores"}, "audio": {"codec": "pcm24"}}))
    assert st["video"]["encoder"] == "prores" and st["audio"]["codec"] == "pcm24"
    assert E.container("mp3").audio_only and not E.container("mkv").audio_only
    assert E.container("nonsense").id == "mkv"


def test_plan_rules(media):
    j = _job(media, "Movie (2017).mkv", container="webm")
    p = J.plan(j)
    assert [c for _, c in p["audio"]] == ["opus", "opus"] and p["subs"][0][1] == "webvtt"
    j = _job(media, "Movie (2017).mkv", container="avi")
    p = J.plan(j)
    assert [c for _, c in p["audio"]] == ["copy", "mp3"] and not p["subs"] and "w_subs_dropped" in p["warnings"]
    j = _job(media, "Movie (2017).mkv", container="mp3")
    p = J.plan(j)
    assert not p["video"] and len(p["audio"]) == 1 and p["audio"][0][1] == "mp3" and "w_one_audio" in p["warnings"]
    j = _job(media, "Movie (2017).mkv", container="mka")
    assert [c for _, c in J.plan(j)["audio"]] == ["copy", "copy"]
    j = _job(media, "dts.mkv", container="m2ts", video={"mode": "copy"}, audio={"codec": "copy"})
    assert J.plan(j)["audio"][0][1] == "copy"                      # DTS fits Blu-ray
    j = _job(media, "tv.ts", container="webm", video={"mode": "copy"})
    assert "w_copy_container" in J.plan(j)["warnings"]
    with pytest.raises(RuntimeError, match="bad_copy"):
        J.build_args(j, "ffmpeg", "/o.webm", E.Available({"vp9": True}))


CASES = [
    # container, settings, source, expected (video codec | None, [audio codecs])
    ("webm", {}, "dts.mkv", "vp9", ["opus"]),
    ("avi", {"video": {"encoder": "xvid"}}, "dts.mkv", "mpeg4", ["mp3"]),
    ("avi", {"video": {"encoder": "mpeg4"}, "audio": {"codec": "ac3"}}, "dts.mkv", "mpeg4", ["ac3"]),
    ("mov", {"video": {"encoder": "prores", "quality": 2}, "audio": {"codec": "alac"}}, "dts.mkv", "prores", ["alac"]),
    ("ts", {"video": {"encoder": "mpeg2"}, "audio": {"codec": "mp2"}}, "dts.mkv", "mpeg2video", ["mp2"]),
    ("m2ts", {"video": {"encoder": "x264"}}, "dts.mkv", "h264", ["dts"]),
    ("mp4", {"video": {"encoder": "vp9", "bits": "10"}}, "hdr.mkv", "vp9", ["flac"]),
    ("gif", {}, "dts.mkv", "gif", []),
    ("mp3", {}, "Movie (2017).mkv", None, ["mp3"]),
    ("m4a", {"audio": {"codec": "alac"}}, "dts.mkv", None, ["alac"]),
    ("flac", {}, "dts.mkv", None, ["flac"]),
    ("opus", {}, "dts.mkv", None, ["opus"]),
    ("ogg", {}, "dts.mkv", None, ["vorbis"]),
    ("wav", {"audio": {"codec": "pcm24"}}, "dts.mkv", None, ["pcm_s24le"]),
    ("mka", {}, "Movie (2017).mkv", None, ["ac3", "aac"]),
    ("ac3", {}, "dts.mkv", None, ["ac3"]),
]


@pytest.mark.parametrize("cont,st,src,vcodec,acodecs", CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)])
def test_encode_into(qapp, media, tmp_path, av, cont, st, src, vcodec, acodecs):
    j = _job(media, src, container=cont, **st)
    if j.settings["video"].get("encoder") in E.BY_ID and not av.ok.get(j.settings["video"]["encoder"]):
        pytest.skip("not in this ffmpeg")
    j.settings["video"]["speed"] = 1
    info, args = _encode(qapp, j, av, tmp_path)
    assert (info.video.codec if info.video else None) == vcodec
    assert [a.codec for a in info.of("audio")] == acodecs
    if cont == "avi" and vcodec == "mpeg4":
        assert "XVID" in args
    if cont == "gif":
        assert info.video.height == 360 and not info.of("audio")
    if vcodec == "vp9" and cont == "mp4":
        assert info.video.bits == 10 and info.video.hdr == "HDR10"           # VP9 profile 2 keeps HDR10
    if vcodec == "prores":
        assert "LT" in (info.video.profile or "") or info.video.profile == "LT"


# ── trim ─────────────────────────────────────────────────────
def test_segments():
    j = J.Job("/x.mkv", J.merged({}))
    j.info = P.MediaInfo("/x.mkv", duration=100.0)
    assert J.segments(j) == [] and J.out_duration(j) == 100.0
    j.trim = [[50, 60], [10, 20], [15, 30], [95, 120]]
    assert J.segments(j) == [(10, 30), (50, 60), (95, 100)] and J.out_duration(j) == 35
    j.trim_cut = True
    assert J.segments(j) == [(0, 10), (30, 50), (60, 95)] and J.out_duration(j) == 65
    j.trim, j.trim_cut = [[0, 100]], False
    assert J.segments(j) == []                                     # the whole file is no trim
    segs = [(10, 30), (50, 60)]
    assert (J.map_time(segs, 5), J.map_time(segs, 15), J.map_time(segs, 40), J.map_time(segs, 55),
            J.map_time(segs, 70)) == (0, 5, 20, 25, None)


def test_trim_one_piece(qapp, media, tmp_path, av):
    j = _job(media, "Movie (2017).mkv", video={"encoder": "x264", "speed": 1, "height": 360})
    j.trim = [[0.5, 2.0]]
    assert "w_trim_audio" in J.plan(j)["warnings"]
    info, args = _encode(qapp, j, av, tmp_path)
    assert "-ss" in args and "-filter_complex" not in args
    assert [a.codec for a in info.of("audio")] == ["ac3", "aac"]          # encoded again, into the same codecs
    assert abs(info.duration - 1.5) < 0.15
    assert [x.lang for x in info.of("audio")] == ["rus", "eng"] and len(info.of("subtitle")) == 1
    # the source chapters (0 «One», 1 «Two») moved by the cut
    assert [(round(a, 1), t) for a, _b, t in info.chapter_list] == [(0.0, "One"), (0.5, "Two")]


def test_trim_pieces_encoded(qapp, media, tmp_path, av):
    j = _job(media, "Movie (2017).mkv", video={"encoder": "x264", "speed": 1, "height": 360},
             audio={"normalize": True})
    j.trim = [[0, 0.8], [2.0, 3.0]]
    p = J.plan(j)
    assert "w_trim_subs" in p["warnings"] and not p["subs"]
    info, args = _encode(qapp, j, av, tmp_path)
    assert "-filter_complex" in args and "concat=n=2:v=1:a=2" in " ".join(args)
    assert abs(info.duration - 1.8) < 0.2
    a = info.of("audio")
    assert [x.lang for x in a] == ["rus", "eng"] and a[0].title == "Дубляж" and a[0].default
    assert [t for _a, _b, t in info.chapter_list] == ["One", "Two"]


def test_trim_cut_out_and_copy(qapp, media, tmp_path, av, keyed):
    j = J.Job(keyed, J.merged({"video": {"mode": "copy"}, "audio": {"codec": "copy"}}))
    j.info = P.probe(keyed)
    j.trim, j.trim_cut = [[1.0, 2.0]], True                         # throw out the 2nd second
    assert J.segments(j)[0] == (0, 1.0) and J.segments(j)[1][0] == 2.0 and "w_trim_copy" in J.plan(j)["warnings"]
    info, args = _encode(qapp, j, av, tmp_path)
    assert "concat" in args and "-filter_complex" not in args
    assert abs(info.duration - 3.0) < 0.3 and info.of("audio")[0].lang == "rus"
    # only the sound, cut in two pieces
    j2 = J.Job(keyed, J.merged({"container": "flac"}))
    j2.info = j.info
    j2.trim = [[0, 1], [3, 4]]
    info, _ = _encode(qapp, j2, av, tmp_path, "sound")
    assert info.video is None and abs(info.duration - 2.0) < 0.15


# ── metadata and chapters ────────────────────────────────────
def test_metadata_edits(qapp, media, tmp_path, av):
    j = _job(media, "Movie (2017).mkv", video={"mode": "copy"})
    assert J.source_tags(j.info).get("title") == "Test movie" and "encoder" not in J.source_tags(j.info)
    j.meta = {"tags": {"title": "Новое название", "comment": "ArtCore"},
              "streams": {"1": {"lang": "ukr", "title": "Украинский", "default": False},
                          "2": {"default": True, "title": ""}, "3": {"forced": True}}}
    info, args = _encode(qapp, j, av, tmp_path)
    assert info.tags.get("title") == "Новое название" and info.tags.get("comment") == "ArtCore"
    a = info.of("audio")
    assert (a[0].lang, a[0].title, a[0].default) == ("ukr", "Украинский", False)
    assert a[1].default and a[1].title == ""
    assert info.of("subtitle")[0].forced
    # a deleted tag goes away
    j.meta = {"tags": {}}
    info, _ = _encode(qapp, j, av, tmp_path, "notags")
    assert not info.tags.get("title")


def test_chapters_edited(qapp, media, tmp_path, av):
    j = _job(media, "Movie (2017).mkv", container="mp4", video={"mode": "copy"})
    j.chapters = [[1.5, "Конец; = #"], [0, "Начало"]]
    assert J.out_chapters(j) == [(0, 1.5, "Начало"), (1.5, 3.0, "Конец; = #")] or \
        J.out_chapters(j)[1][2] == "Конец; = #"
    info, _ = _encode(qapp, j, av, tmp_path)
    assert [(round(a, 1), t) for a, _b, t in info.chapter_list] == [(0.0, "Начало"), (1.5, "Конец; = #")]
    j.chapters = []                                               # none at all
    info, _ = _encode(qapp, j, av, tmp_path, "nochap")
    assert not info.chapter_list
    assert [c[0] for c in J.auto_chapters(1500, 5)] == [0, 300, 600, 900, 1200]
    assert J.auto_chapters(1500, 5, "Глава {0}")[1][1] == "Глава 2"


def test_job_roundtrip_with_edits():
    j = J.Job("/x/a.mkv", J.merged({}))
    j.trim, j.trim_cut = [[1.0, 2.5]], True
    j.meta = {"tags": {"title": "x"}, "streams": {"1": {"lang": "eng"}}}
    j.chapters = [[0.0, "A"], [60.0, "B"]]
    k = J.Job.from_dict(j.to_dict())
    assert (k.trim, k.trim_cut, k.meta, k.chapters) == (j.trim, True, j.meta, j.chapters) and k.edited
    assert not J.Job.from_dict(J.Job("/y.mkv", J.merged({})).to_dict()).edited


# ── resolution ───────────────────────────────────────────────
def test_resolution_math():
    j = J.Job("/dvd.vob", J.merged({}))
    j.info = P.MediaInfo("/dvd.vob", duration=10)
    j.info.streams = [P.Stream(0, "video", codec="mpeg2video", width=720, height=576, sar="64:45")]
    assert J.scale_filters(j) == [] and J.out_size(j) == (720, 576)          # as the source
    j.settings["video"].update(width=1280, height=720)                       # fit, no upscale: square pixels only
    assert J.scale_filters(j) == ["scale=1024:576:flags=lanczos", "setsar=1"] and J.out_size(j) == (1024, 576)
    j.settings["video"]["upscale"] = True
    assert J.out_size(j) == (1280, 720)
    j.settings["video"].update(width=720, height=576, fit="stretch")
    assert J.scale_filters(j) == ["scale=720:576:flags=lanczos", "setsar=1"]  # anamorphic → square, same size
    j.info.streams[0] = P.Stream(0, "video", codec="h264", width=1920, height=1080, sar="1:1")
    j.settings["video"].update(fit="pad", upscale=False)
    assert J.scale_filters(j) == ["scale=720:406:flags=lanczos", "setsar=1",
                                  "pad=720:576:(720-iw)/2:(576-ih)/2:color=black"]
    j.settings["video"].update(width=0, height=720, fit="fit")              # 1.0 presets: a height alone
    assert J.out_size(j) == (1280, 720) and J.res_label(j.settings["video"]) == "720p"
    j.info.streams[0] = P.Stream(0, "video", codec="h264", width=3840, height=1600, sar="1:1")
    j.settings["video"].update(width=1920, height=1080)
    assert J.out_size(j) == (1920, 800)                                     # wide films stay wide
    assert J.res_label({"width": 720, "height": 576}) == "720×576"


def test_resolution_encode(qapp, media, tmp_path, av):
    j = _job(media, "dts.mkv", video={"encoder": "x264", "speed": 1, "width": 720, "height": 576, "fit": "pad"})
    info, _ = _encode(qapp, j, av, tmp_path)
    assert (info.video.width, info.video.height) == (720, 576)
    j = _job(media, "dts.mkv", video={"encoder": "x264", "speed": 1, "width": 1280, "height": 720, "upscale": True})
    info, _ = _encode(qapp, j, av, tmp_path, "up")
    assert (info.video.width, info.video.height) == (1280, 720)


def test_per_track_audio_and_language(media, av):
    j = _job(media, "Movie (2017).mkv", video={"mode": "copy"}, audio={"codec": "ac3", "kbps": 448})
    j.tracks = {"2": {"codec": "opus", "kbps": 96, "channels": "stereo", "normalize": True}}
    a = " ".join(J.build_args(j, "ffmpeg", "/o.mkv", av))
    assert "-c:a:0 ac3" in a and "-b:a:0 448k" in a and "-filter:a:0" not in a
    assert "-c:a:1 libopus" in a and "-b:a:1 96k" in a and "-filter:a:1 loudnorm" in a
    j = _job(media, "Movie (2017).mkv", lang="eng")
    p = J.plan(j)
    assert [s.lang for s, _ in p["audio"]] == ["eng"] and not p["subs"]          # the Russian subtitles too
    j = _job(media, "Movie (2017).mkv", lang="rus")
    p = J.plan(j)
    assert [s.lang for s, _ in p["audio"]] == ["rus"] and len(p["subs"]) == 1
    j = _job(media, "Movie (2017).mkv", lang="jpn")                               # none: the first audio stays
    assert len(J.plan(j)["audio"]) == 1
