"""1.3: every video / sound filter of the Streams tab — the command lines, real encodes read back with ffprobe
(sizes, rates, colour tags, levels measured with signalstats / astats), with one piece and with several pieces
joined by the concat filter, the two-pass stabilisation through the Runner, presets and the queue file."""
import json
import os
import subprocess

import pytest

from conftest import pump
from convert import encoders as E
from convert import ff
from convert import filters as F
from convert import job as J
from convert import probe as P
from convert.runner import Runner


def mk(path, *args):
    if not os.path.isfile(path):
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args, path], check=True)
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("filters")
    c = {}
    # a 2.20 film (1920×872) inside a 1080p frame: 104 px bars top and bottom
    c["letterbox"] = mk(str(d / "letterbox.mkv"), "-f", "lavfi", "-i", "testsrc2=s=1920x872:r=24:d=2",
                        "-vf", "pad=1920:1080:0:104:black", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18")
    c["small"] = mk(str(d / "small.mkv"), "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-f", "lavfi",
                    "-i", "sine=f=440:d=2:sample_rate=48000", "-ac", "2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "10", "-c:a", "flac")
    c["surround"] = mk(str(d / "surround.mkv"), "-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=2", "-f", "lavfi",
                       "-i", "sine=f=440:d=2", "-filter_complex", "[1]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[a]",
                       "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "ac3")
    c["gray"] = mk(str(d / "gray.mkv"), "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=25:d=2",
                   "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0")
    c["still"] = mk(str(d / "still.mkv"), "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=25:d=2",
                    "-c:v", "libx264", "-preset", "ultrafast")
    # NTSC film: 23.976 progressive made 29.97 interlaced by 3:2 pulldown
    c["telecine"] = mk(str(d / "telecine.mkv"), "-f", "lavfi", "-i", "testsrc2=s=320x240:r=24000/1001:d=3",
                       "-vf", "telecine=pattern=23", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10",
                       "-flags", "+ilme+ildct")
    c["tv25i"] = mk(str(d / "tv25i.mkv"), "-f", "lavfi", "-i", "testsrc2=s=720x576:r=50:d=1",
                    "-vf", "tinterlace=mode=interleave_top,setfield=tff", "-c:v", "libx264", "-preset", "ultrafast",
                    "-flags", "+ilme+ildct", "-x264-params", "tff=1")
    c["dim"] = mk(str(d / "dim.mkv"), "-f", "lavfi", "-i", "color=c=0x606060:s=320x240:r=25:d=1",
                  "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0")
    c["shaky"] = mk(str(d / "shaky.mkv"), "-f", "lavfi", "-i", "testsrc2=s=400x300:r=25:d=2",
                    "-vf", "crop=320:240:40+20*sin(n/2):30+15*cos(n/3)", "-c:v", "libx264", "-preset", "ultrafast")
    logo = str(d / "logo.png")
    mk(logo, "-f", "lavfi", "-i", "color=c=red:s=64x48", "-frames:v", "1")
    c["logo"] = logo
    return c


def _job(src, crop_found=None, **st):
    j = J.Job(src, J.fit_container(J.merged(st)))
    j.info = P.probe(src)
    j.crop_found = crop_found
    return j


def _filters(**f) -> dict:
    return {"filters": {k: dict(v) for k, v in f.items()}}


def _encode(qapp, j, av, tmp_path, name="out", work=None):
    out = str(tmp_path / (name + "." + E.container(j.settings["container"]).ext))
    wd = work or str(tmp_path / "work")
    args = J.build_args(j, ff.ffmpeg(), out, av, work_dir=wd)
    pre = J.pre_passes(j, ff.ffmpeg(), out, av, work_dir=wd)
    r = Runner()
    res, fr = [], []
    r.finished.connect(lambda ok, e: res.append((ok, e)))
    r.progress.connect(lambda d: fr.append(d["frac"]))
    r.start(args, out, J.out_duration(j), pre=pre)
    assert pump(qapp, 240, lambda: res)
    ok, err = res[0]
    assert ok, f"{err}\n{' '.join(args)}\n" + "\n".join(r.tail[-10:])
    return out, args, pre, fr


def ffprobe(path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def vstream(path) -> dict:
    return next(s for s in ffprobe(path)["streams"] if s["codec_type"] == "video")


def astream(path, n=0) -> dict:
    return [s for s in ffprobe(path)["streams"] if s["codec_type"] == "audio"][n]


def frames(path) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return int(r.stdout.strip().split(",")[0])


def stats(path, vf="null", at=0.5) -> dict:
    """signalstats of one frame: YAVG, UAVG, VAVG …"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-ss", str(at), "-i", path, "-frames:v", "1", "-vf",
                        f"{vf},signalstats,metadata=print:file=-", "-f", "null", "-"], capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        if "lavfi.signalstats." in line:
            k, v = line.split("lavfi.signalstats.", 1)[1].split("=")
            out[k] = float(v)
    return out


def psnr(a, b, vf_b="null") -> float:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", a, "-i", b, "-filter_complex",
                        f"[0:v]format=yuv420p,scale=320:240[x];[1:v]{vf_b},format=yuv420p,scale=320:240[y];[x][y]psnr",
                        "-f", "null", "-"], capture_output=True, text=True)
    line = [ln for ln in r.stderr.splitlines() if "PSNR" in ln][-1]
    v = line.split("average:")[1].split()[0]
    return 100.0 if v == "inf" else float(v)


def apeak(path, n=0, start=None, dur=None) -> float:
    """The peak level of an audio track, dBFS (optionally of a part)."""
    trim = ""
    if start is not None:
        trim = f"atrim=start={start}" + (f":duration={dur}" if dur is not None else "") + ","
    af = (trim + "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=Peak_level,"
          "ametadata=print:key=lavfi.astats.Overall.Peak_level:file=-")
    r = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-i", path, "-map", f"0:a:{n}", "-af", af,
                        "-f", "null", "-"], capture_output=True, text=True)
    vals = [float(ln.split("=")[1]) for ln in r.stdout.splitlines() if "Peak_level=" in ln and "inf" not in ln]
    return max(vals) if vals else -200.0


X264 = {"container": "mkv", "video": {"encoder": "x264", "speed": 1}, "audio": {"codec": "copy"}}


# ── settings ─────────────────────────────────────────────────
def test_settings_merge_and_queue_file():
    st = J.merged({"video": {"encoder": "x264"}})                  # a 1.2 preset: no filters at all
    assert st["filters"] == F.FILTER_DEFAULTS
    st = J.merged(_filters(crop={"on": True, "mode": "manual", "t": 10, "nonsense": 1}, bogus={"on": True}))
    assert st["filters"]["crop"]["t"] == 10 and st["filters"]["crop"]["l"] == 0
    assert "nonsense" not in st["filters"]["crop"] and "bogus" not in st["filters"]
    assert F.FILTER_DEFAULTS["crop"]["t"] == 0                     # the defaults stay untouched
    j = J.Job("/x.mkv", st)
    j.crop_found = [0, 104, 0, 104]
    j2 = J.Job.from_dict(json.loads(json.dumps(j.to_dict())))
    assert j2.settings["filters"]["crop"]["t"] == 10 and j2.crop_found == [0, 104, 0, 104]
    assert J.Job.from_dict({"src": "/x.mkv"}).crop_found is None
    # a user preset keeps its filters
    user = {"mine": J.merged(_filters(rotate={"on": True, "angle": 90}))}
    assert J.preset_settings("mine", user)["filters"]["rotate"]["angle"] == 90


def test_presets_save_filters(qapp, clips, data_dir):
    from convert import config
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False)
    w = MainWindow(cfg)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    w.add_paths([clips["small"]])
    assert pump(qapp, 20, lambda: w.jobs[0].info is not None)
    w.view.setCurrentItem(w.items[w.jobs[0].uid])
    w._setf("sharpen", "on", True)
    w._setf("sharpen", "amount", 1.5)
    from PySide6.QtWidgets import QInputDialog
    orig = QInputDialog.getText
    QInputDialog.getText = staticmethod(lambda *a, **k: ("sharp", True))
    try:
        w.save_preset()
    finally:
        QInputDialog.getText = orig
    saved = config.read("presets.json")["sharp"]
    assert saved["filters"]["sharpen"] == {"on": True, "amount": 1.5}
    w.close()


# ── crop ─────────────────────────────────────────────────────
def test_crop_detect_letterbox(clips):
    """The 2.20 film in a 1080p frame: the bars are found at every point of the film."""
    info = P.probe(clips["letterbox"])
    found = F.detect_crop(ff.ffmpeg(), clips["letterbox"], info.duration, 1920, 1080)
    assert found == (0, 104, 0, 104)
    info = P.probe(clips["small"])
    assert F.detect_crop(ff.ffmpeg(), clips["small"], info.duration, 320, 240) == (0, 0, 0, 0)


def test_crop_encode_auto_and_manual(qapp, clips, av, tmp_path):
    j = _job(clips["letterbox"], [0, 104, 0, 104], **X264, **_filters(crop={"on": True, "mode": "auto"}))
    assert J.out_size(j, E.BY_ID["x264"]) == (1920, 872)
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "auto")
    assert "crop=1920:872:0:104" in " ".join(args)
    v = vstream(out)
    assert (v["width"], v["height"]) == (1920, 872)
    assert stats(out)["YMIN"] > 10                       # no black bar left inside
    # by hand, then scaled to 720p: the crop comes first, the frame keeps its 2.20 shape
    j = _job(clips["letterbox"], None, **X264, **_filters(crop={"on": True, "mode": "manual", "t": 104, "b": 104,
                                                                "l": 40, "r": 40}))
    j.settings["video"]["height"] = 720
    assert J.out_size(j, E.BY_ID["x264"]) == (1280, 606)
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "manual")
    v = vstream(out)
    assert (v["width"], v["height"]) == (1280, 606)
    # auto with nothing found yet: no crop at all
    j = _job(clips["letterbox"], None, **X264, **_filters(crop={"on": True, "mode": "auto"}))
    assert "crop=" not in " ".join(J.build_args(j, "ffmpeg", "/o.mkv", av))


# ── interlace ────────────────────────────────────────────────
def test_deinterlace_methods(qapp, media, clips, av, tmp_path):
    src = clips["tv25i"]
    assert P.probe(src).video.interlaced
    j = _job(src, container="mkv", video={"encoder": "x264", "speed": 1})
    assert "bwdif=mode=send_frame:parity=auto:deint=all" in J.video_filters(j)    # as in 1.0 (auto)
    j.settings["filters"]["interlace"].update(method="yadif", double=True, parity="tff")
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "yadif2")
    assert "yadif=mode=send_field:parity=tff:deint=all" in " ".join(args)
    v = vstream(out)
    assert v["avg_frame_rate"] in ("50/1",) and v.get("field_order", "progressive") == "progressive"
    j.settings["video"]["deint"] = "off"
    assert not any("yadif" in f or "bwdif" in f for f in J.video_filters(j))


def test_inverse_telecine(qapp, clips, av, tmp_path):
    j = _job(clips["telecine"], **X264, **_filters(interlace={"ivtc": True}))
    assert J.video_filters(j)[:3] == ["fieldmatch=order=auto:combmatch=full", "yadif=deint=interlaced", "decimate"]
    out, _, _, _ = _encode(qapp, j, av, tmp_path, "ivtc")
    a, b = (int(x) for x in vstream(out)["avg_frame_rate"].split("/"))
    assert abs(a / b - 23.976) < 0.05


# ── HDR → SDR ────────────────────────────────────────────────
def test_hdr_to_sdr(qapp, media, av, tmp_path):
    src = os.path.join(media, "hdr.mkv")
    j = _job(src, container="mkv", video={"encoder": "x264", "speed": 1}, audio={"codec": "copy"})
    enc = E.BY_ID["x264"]
    assert F.tonemap_on(j, enc, 8)                                  # 8 bit H.264: switched on by itself
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "sdr")
    assert "tonemap=tonemap=hable" in " ".join(args)
    v = vstream(out)
    assert (v.get("color_transfer"), v.get("color_primaries"), v.get("color_space")) == ("bt709", "bt709", "bt709")
    assert P.probe(out).video.hdr == ""
    # 10 bit HEVC keeps the HDR …
    j = _job(src, container="mkv", video={"encoder": "x265", "bits": "10", "speed": 1})
    assert not F.tonemap_on(j, E.BY_ID["x265"], 10)
    assert "tonemap" not in " ".join(J.build_args(j, "ffmpeg", "/o.mkv", av))
    # … unless asked: tone-mapped into 10 bit SDR, another curve
    j.settings["filters"]["hdr"].update(mode="on", algo="mobius")
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "sdr10")
    assert "tonemap=tonemap=mobius" in " ".join(args) and "format=yuv420p10le" in " ".join(args)
    v = vstream(out)
    assert v["pix_fmt"] == "yuv420p10le" and v.get("color_transfer") == "bt709"
    j.settings["filters"]["hdr"]["mode"] = "off"
    j.settings["video"]["bits"] = "8"
    assert not F.tonemap_on(j, E.BY_ID["x265"], 8)
    # libplacebo (Vulkan): the command line only — CI has no graphics card
    j.settings["filters"]["hdr"].update(mode="on", engine="libplacebo", algo="bt.2390")
    assert any(f.startswith("libplacebo=tonemapping=bt.2390") for f in J.video_filters(j, E.BY_ID["x265"]))


# ── geometry: rotation, pad, aspect ──────────────────────────
def test_rotate_flip(qapp, clips, av, tmp_path):
    j = _job(clips["small"], **X264, **_filters(rotate={"on": True, "angle": 90}))
    assert J.out_size(j, E.BY_ID["x264"]) == (240, 320)
    out, _, _, _ = _encode(qapp, j, av, tmp_path, "r90")
    v = vstream(out)
    assert (v["width"], v["height"]) == (240, 320)
    assert psnr(out, clips["small"], "transpose=clock") > 30
    j = _job(clips["small"], **X264, **_filters(rotate={"on": True, "angle": 0, "hflip": True}))
    out, _, _, _ = _encode(qapp, j, av, tmp_path, "hflip")
    assert psnr(out, clips["small"], "hflip") > 30 > psnr(out, clips["small"])
    j = _job(clips["small"], **X264, **_filters(rotate={"on": True, "angle": 180}))
    assert J.video_filters(j) == ["hflip", "vflip"]
    j = _job(clips["small"], **X264, **_filters(rotate={"on": True, "angle": 270}))
    assert J.video_filters(j) == ["transpose=cclock"]


def test_pad_and_aspect(qapp, clips, av, tmp_path):
    j = _job(clips["small"], **X264, **_filters(pad={"on": True, "l": 20, "t": 10, "r": 20, "b": 10, "color": "white"},
                                                aspect={"on": True, "dar": "16:9"}))
    assert J.out_size(j, E.BY_ID["x264"]) == (360, 260)
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "pad")
    v = vstream(out)
    assert (v["width"], v["height"]) == (360, 260) and v["display_aspect_ratio"] == "16:9"
    assert stats(out, "crop=16:200:0:30")["YAVG"] > 230            # the white border on the left
    # the resolution's own «fit with bars» and the Pad row together
    j = _job(clips["letterbox"], [0, 104, 0, 104], **X264, **_filters(crop={"on": True}, pad={"on": True, "t": 40, "b": 40}))
    j.settings["video"].update(width=1280, height=720, fit="pad")
    assert J.out_size(j, E.BY_ID["x264"]) == (1280, 800)
    vf = J.video_filters(j)
    assert vf.index("pad=1280:720:(1280-iw)/2:(720-ih)/2:color=black") < vf.index("pad=iw+0:ih+80:0:40:color=black")


# ── clean-up filters ─────────────────────────────────────────
def test_deshake_one_pass(qapp, clips, av, tmp_path):
    j = _job(clips["shaky"], **X264, **_filters(deshake={"on": True, "method": "deshake", "strength": 6}))
    assert J.pre_passes(j, "ffmpeg", "/o.mkv", av) == []
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "deshake")
    assert "deshake=rx=48:ry=48:edge=mirror" in " ".join(args)
    assert (vstream(out)["width"], vstream(out)["height"]) == (320, 240)


def test_vidstab_two_passes(qapp, clips, av, tmp_path):
    """vid.stab: the analysis pass writes the motion file, the encode reads it — run by the Runner one after the
    other, the progress bar shared between them; works with several pieces too."""
    wd = str(tmp_path / "wk")
    j = _job(clips["shaky"], **X264, **_filters(deshake={"on": True, "method": "vidstab", "strength": 5}))
    j.trim = [[0.0, 0.8], [1.0, 1.8]]
    pre = J.pre_passes(j, "ffmpeg", "/o.mkv", av, work_dir=wd)
    assert len(pre) == 1 and pre[0][-3:] == ["-f", "null", "-"] and "vidstabdetect" in " ".join(pre[0])
    assert "concat=n=2" in " ".join(pre[0])
    out, args, pre, fr = _encode(qapp, j, av, tmp_path, "vidstab", work=wd)
    trf = J.work_file(j, "vidstab", wd, "trf")
    assert os.path.isfile(trf) and os.path.getsize(trf) > 0
    assert "vidstabtransform=input=" in " ".join(args)
    assert fr and max(fr) == 1.0 and any(0.0 < x <= 0.5 for x in fr)        # first half: the analysis
    assert abs(float(ffprobe(out)["format"]["duration"]) - 1.6) < 0.2


def test_denoise_grain_sharpen(qapp, clips, av, tmp_path):
    base = clips["small"]
    for name, f, want in (
            ("hq", {"denoise": {"on": True, "method": "hqdn3d", "strength": 6}}, "hqdn3d=6:4.5:9:6.75"),
            ("nl", {"denoise": {"on": True, "method": "nlmeans", "strength": 2}}, "nlmeans=s=2.4:p=7:r=9"),
            ("grain", {"grain": {"on": True, "strength": 30}}, "noise=c0s=30:c0f=t+u"),
            ("sharp", {"sharpen": {"on": True, "amount": 1.5}}, "unsharp=5:5:1.5:5:5:0")):
        j = _job(base, **X264, **_filters(**f))
        out, args, _, _ = _encode(qapp, j, av, tmp_path, name)
        assert want in " ".join(args)
        assert 18 < psnr(out, base) < 60                           # changed, still the same picture
    # grain on a flat grey frame: the grey is no longer flat
    j = _job(clips["gray"], **X264, **_filters(grain={"on": True, "strength": 40}))
    out, _, _, _ = _encode(qapp, j, av, tmp_path, "grain_gray")
    s = stats(out)
    assert s["YMAX"] - s["YMIN"] > 20


def test_delogo(qapp, clips, av, tmp_path):
    j = _job(clips["small"], **X264, **_filters(delogo={"on": True, "x": 250, "y": 10, "w": 60, "h": 40}))
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "delogo")
    assert "delogo=x=250:y=10:w=60:h=40" in " ".join(args)
    # the logo area changed, the rest of the picture did not
    region = "crop=60:40:250:10"
    assert psnr(out, clips["small"], "null") > 25
    a, b = stats(out, region), stats(clips["small"], region)
    assert abs(a["YAVG"] - b["YAVG"]) > 0.5 or abs(a["UAVG"] - b["UAVG"]) > 0.5
    # a rectangle reaching outside the frame is pulled in
    j.settings["filters"]["delogo"].update(x=300, y=0, w=200, h=500)
    assert J.video_filters(j) == ["delogo=x=300:y=1:w=19:h=238"]
    j.settings["filters"]["delogo"]["w"] = 0
    assert J.video_filters(j) == []                               # nothing drawn yet: no filter


def test_color_correction(qapp, clips, av, tmp_path):
    g = clips["gray"]
    base = stats(g)
    j = _job(g, **X264, **_filters(color={"on": True, "brightness": 0.2}))
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "bright")
    assert "eq=brightness=0.2:contrast=1:saturation=1:gamma=1" in " ".join(args)
    assert stats(out)["YAVG"] > base["YAVG"] + 30
    j = _job(clips["small"], **X264, **_filters(color={"on": True, "saturation": 0.0}))
    out, _, _, _ = _encode(qapp, j, av, tmp_path, "gray")
    s = stats(out)
    assert abs(s["UAVG"] - 128) < 2 and abs(s["VAVG"] - 128) < 2 and s["UMAX"] - s["UMIN"] < 6
    # white balance: warmer = more red (V up), less blue (U down); the midtone balance
    j = _job(g, **X264, **_filters(color={"on": True, "temp": 3000}))
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "warm")
    assert "colortemperature=temperature=3000" in " ".join(args)
    s = stats(out)
    assert s["VAVG"] > base["VAVG"] + 3 and s["UAVG"] < base["UAVG"] - 3
    j = _job(g, **X264, **_filters(color={"on": True, "b": 0.5}))
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "blue")
    assert "colorbalance=rs=0:gs=0:bs=0.25:rm=0:gm=0:bm=0.5:rh=0:gh=0:bh=0.25" in " ".join(args)
    assert stats(out)["UAVG"] > base["UAVG"] + 10
    j = _job(g, **X264, **_filters(color={"on": True}))           # all neutral: nothing
    assert J.video_filters(j) == []


def test_overlay_picture(qapp, clips, av, tmp_path):
    j = _job(clips["gray"], **X264, **_filters(overlay={"on": True, "path": clips["logo"], "pos": "br",
                                                        "margin": 10, "opacity": 100}))
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "logo")
    assert "-filter_complex" in args and args.count("-i") == 2 and "-vf" not in args
    corner = stats(out, "crop=40:30:262:192")                       # inside the 64×48 logo, 10 px from the corner
    other = stats(out, "crop=40:30:10:10")
    assert corner["VAVG"] > 200 and abs(other["VAVG"] - 128) < 3    # red there, grey elsewhere
    # half transparent, top left, scaled to 25 % of the width
    j.settings["filters"]["overlay"].update(pos="tl", opacity=50, size=25)
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "logo50")
    g = " ".join(args)
    assert "scale=80:-2,format=rgba,colorchannelmixer=aa=0.5" in g and "overlay=x=10:y=10" in g
    half = stats(out, "crop=40:20:20:15")
    assert 150 < half["VAVG"] < 220
    # the picture is gone: no overlay, no error
    j.settings["filters"]["overlay"]["path"] = str(tmp_path / "missing.png")
    assert "-filter_complex" not in J.build_args(j, "ffmpeg", "/o.mkv", av)


# ── pixel format, colour space, frame rate ───────────────────
def test_pixel_format_and_color_space(qapp, clips, av, tmp_path):
    j = _job(clips["small"], **X264, **_filters(pixfmt={"on": True, "chroma": "444"}))
    out, _, _, _ = _encode(qapp, j, av, tmp_path, "x264_444")
    assert vstream(out)["pix_fmt"] == "yuv444p"
    j = _job(clips["small"], container="mkv", video={"encoder": "x265", "bits": "10", "speed": 1},
             **_filters(pixfmt={"on": True, "chroma": "422", "matrix": "bt601", "range": "pc"}))
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "x265_422")
    v = vstream(out)
    assert v["pix_fmt"] == "yuv422p10le" and v.get("color_range") == "pc" and v.get("color_space") == "bt470bg"
    assert "colorspace=all=bt601-6-625:range=pc:iall=bt601-6-625:irange=tv" in " ".join(args)
    # BT.2020 tags; SVT-AV1 can't write 4:4:4 → 4:2:0
    j = _job(clips["small"], container="mkv", video={"encoder": "x264", "speed": 1},
             **_filters(pixfmt={"on": True, "matrix": "bt2020"}))
    out, _, _, _ = _encode(qapp, j, av, tmp_path, "bt2020")
    assert vstream(out).get("color_primaries") == "bt2020"
    j = _job(clips["small"], container="mkv", video={"encoder": "svtav1"}, **_filters(pixfmt={"on": True, "chroma": "444"}))
    assert F.chroma(j, E.BY_ID["svtav1"]) == "420"
    assert "yuv444p" not in " ".join(J.build_args(j, "ffmpeg", "/o.mkv", E.Available({"svtav1": True})))
    j = _job(clips["small"], container="mov", video={"encoder": "prores"}, **_filters(pixfmt={"on": True, "chroma": "444"}))
    a = J.build_args(j, "ffmpeg", "/o.mov", av)
    assert a[a.index("-pix_fmt") + 1] == "yuv444p10le" and a[a.index("-profile:v") + 1] == "4"


def test_framerate(qapp, clips, av, tmp_path):
    st = dict(X264, video={"encoder": "x264", "fps": 50, "speed": 1})
    j = _job(clips["small"], **st, **_filters(fps={"mode": "cfr"}))
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "cfr50")
    assert "fps=50" in " ".join(args) and args[args.index("-fps_mode") + 1] == "cfr"
    assert vstream(out)["avg_frame_rate"] == "50/1" and abs(frames(out) - 100) <= 2
    # 1.0: a higher rate without CFR is left as is
    j = _job(clips["small"], **dict(X264, video={"encoder": "x264", "fps": 50}))
    assert not any(f.startswith("fps=") for f in J.video_filters(j))
    j = _job(clips["small"], **st, **_filters(fps={"interp": True}))
    j.trim = [[0.0, 0.6]]
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "interp")
    assert "minterpolate=fps=50" in " ".join(args) and vstream(out)["avg_frame_rate"] == "50/1"
    # a still picture: repeated frames dropped (VFR)
    j = _job(clips["still"], **X264, **_filters(fps={"dedup": True}))
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "dedup")
    assert "mpdecimate" in " ".join(args) and args[args.index("-fps_mode") + 1] == "vfr"
    assert frames(out) < 20


# ── sound ────────────────────────────────────────────────────
def test_audio_delay(qapp, clips, av, tmp_path):
    src = clips["small"]
    j = _job(src, **X264)
    j.tracks = {"1": {"delay": 500}}
    p = J.plan(j)
    assert p["audio"][0][1] == "flac" and "w_afilter_copy" in p["warnings"]   # copy → the same codec
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "later")
    assert "adelay=delays=500:all=1" in " ".join(args)
    assert apeak(out, start=0, dur=0.3) < -80 and apeak(out, start=0.7, dur=0.5) > -30   # silence, then the tone
    assert abs(float(astream(out).get("duration", ffprobe(out)["format"]["duration"])) - 2.5) < 0.15 or \
        abs(float(ffprobe(out)["format"]["duration"]) - 2.5) < 0.15
    j.tracks = {"1": {"delay": -500}}
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "earlier")
    assert "atrim=start=0.5,asetpts=PTS-STARTPTS" in " ".join(args)
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=pts_time",
                        "-of", "csv=p=0", out], capture_output=True, text=True)
    last = max(float(x) for x in r.stdout.split() if x.strip())
    assert 1.3 < last < 1.6                                         # the sound is half a second shorter


def test_audio_rate_bits_layout(qapp, clips, av, tmp_path):
    j = _job(clips["small"], container="mkv", video={"mode": "copy"}, audio={"codec": "flac", "rate": 44100, "abits": 24})
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "flac24")
    a = astream(out)
    assert a["sample_rate"] == "44100" and a.get("bits_per_raw_sample") == "24"
    j = _job(clips["small"], container="wav", audio={"codec": "pcm16", "abits": 32, "channels": "mono"})
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "pcm32")
    a = astream(out)
    assert a["codec_name"] == "pcm_s32le" and a["channels"] == 1
    # Opus takes 48 kHz (and a few lower) only: 44.1 → 48
    j = _job(clips["small"], container="mkv", video={"mode": "copy"}, audio={"codec": "opus", "rate": 44100})
    assert "aresample" not in " ".join(J.build_args(j, "ffmpeg", "/o.mkv", av))
    j.settings["audio"]["rate"] = 22050
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "opus24")
    assert "aresample=24000" in " ".join(args)
    # 5.1 → 7.1 is no upmix; 5.1 → stereo
    j = _job(clips["surround"], container="mkv", video={"mode": "copy"}, audio={"codec": "aac", "channels": "7.1"})
    assert "-ac:a:0" not in J.build_args(j, "ffmpeg", "/o.mkv", av)
    j.tracks = {"1": {"channels": "stereo", "rate": 32000}}
    out, _, _, _ = _encode(qapp, j, av, tmp_path, "stereo")
    a = astream(out)
    assert a["channels"] == 2 and a["sample_rate"] == "32000"


def test_audio_gain(qapp, clips, av, tmp_path):
    src = clips["small"]
    before = apeak(src)
    j = _job(src, container="mkv", video={"mode": "copy"}, audio={"codec": "flac", "gain": 6})
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "plus6")
    assert "volume=6dB" in " ".join(args)
    assert abs(apeak(out) - (before + 6)) < 0.5
    # per track, with the normalisation after it
    j = _job(src, container="mkv", video={"mode": "copy"}, audio={"codec": "aac"})
    j.tracks = {"1": {"gain": -10.0, "normalize": True}}
    a = J.build_args(j, "ffmpeg", "/o.mkv", av)
    assert a[a.index("-filter:a:0") + 1] == "volume=-10dB,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000"


# ── several pieces: everything after the concat filter ───────
def test_all_filters_with_pieces(qapp, clips, av, tmp_path):
    j = _job(clips["letterbox"], [0, 104, 0, 104], container="mkv", video={"encoder": "x264", "speed": 1, "height": 360},
             **_filters(crop={"on": True}, rotate={"on": True, "angle": 0, "hflip": True},
                        denoise={"on": True}, sharpen={"on": True}, color={"on": True, "contrast": 1.2},
                        pad={"on": True, "t": 8, "b": 8}, overlay={"on": True, "path": clips["logo"]},
                        pixfmt={"on": True, "matrix": "bt709", "range": "tv"}, fps={"mode": "cfr"}))
    j.settings["video"]["fps"] = 25
    j.trim = [[0.2, 0.9], [1.1, 1.8]]
    assert J.multi_encode(j)
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "pieces")
    g = args[args.index("-filter_complex") + 1]
    assert g.index("concat=n=2") < g.index("crop=1920:872:0:104") < g.index("overlay=") and "[vout]" in g
    v = vstream(out)
    w, h = J.out_size(j, E.BY_ID["x264"])
    assert (v["width"], v["height"]) == (w, h) == (640, 306)
    assert v["avg_frame_rate"] == "25/1"
    assert abs(float(ffprobe(out)["format"]["duration"]) - 1.4) < 0.15
    # the sound filters go after the concat too
    j = _job(clips["small"], container="mkv", video={"encoder": "x264", "speed": 1}, audio={"codec": "aac"},
             **_filters(rotate={"on": True, "angle": 90}))
    j.tracks = {"1": {"delay": 200, "gain": 3}}
    j.trim = [[0.0, 0.5], [1.0, 1.5]]
    out, args, _, _ = _encode(qapp, j, av, tmp_path, "pieces_audio")
    g = args[args.index("-filter_complex") + 1]
    assert "[aj0]adelay=delays=200:all=1,volume=3dB[ao0]" in g and "transpose=clock" in g
    assert (vstream(out)["width"], vstream(out)["height"]) == (240, 320)


def test_copy_command_has_filters(qapp, clips, data_dir):
    """«Copy the ffmpeg command»: the filters are in it, vid.stab as two commands."""
    from PySide6.QtWidgets import QApplication

    from convert import config
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False)
    w = MainWindow(cfg)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    w.add_paths([clips["shaky"]])
    assert pump(qapp, 20, lambda: w.jobs[0].info is not None)
    j = w.jobs[0]
    j.settings = J.merged(X264, _filters(rotate={"on": True, "angle": 90},
                                         deshake={"on": True, "method": "vidstab"}))
    w._copy_cmd(j)
    text = QApplication.clipboard().text()
    first, second = text.split(" && ")
    assert "vidstabdetect" in first and "vidstabtransform" in second and "transpose=clock" in second
    w.close()
