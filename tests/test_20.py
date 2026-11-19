"""2.0: subtitles burnt into the picture (text SRT / ASS / WebVTT with their fonts and styles, PGS and VobSub pictures,
forced only, outside files with a delay; the right time through one cut, several cuts, joined files and chunks; the
right place through crop and scale; HDR kept at ~200 nits, HDR → SDR after the tone mapping; Windows paths in the
filter graph; seen in the Status frame and the before / after curtain), the 5.1 / 7.1 → 2.0 matrix (normal, louder
dialogue, Dolby Pro Logic II, custom; never clipping; in the log), watch folders (only complete files, never twice,
never the results, with the folder's preset into its result folder) and Explorer's menu with presets (HKCU entries,
one running copy takes the files of every copy Explorer starts).

The PGS of the tests is written by tests/pgsmaker.py (FFmpeg reads PGS but cannot write it); the VobSub is made from
it by FFmpeg's DVD subtitle encoder. The pictures are grey; a burnt-in box is white — read at a frame by its number."""
import json
import os
import struct
import subprocess
import sys
import time

import pytest

from conftest import ff as mk, pump
from convert import config
from convert import encoders as E
from convert import filters as F
from convert import hardsub as HS
from convert import job as J
from convert import probe as P

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import pgsmaker  # noqa: E402

BOX_A = (100, 100, 400, 200)            # 1 … 3 s, a normal line
BOX_B = (1000, 700, 400, 200)           # 5 … 7 s, a forced one
ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},80,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{{\\pos(100,100)\\p1}}m 0 0 l 400 0 400 200 0 200{{\\p0}}
Dialogue: 0,0:00:05.00,0:00:07.00,Default,,0,0,0,,{{\\pos(1000,700)\\p1}}m 0 0 l 400 0 400 200 0 200{{\\p0}}
"""


def _win(qapp, **over):
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False, keep_queue=False)
    cfg.update(over)
    w = MainWindow(cfg)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    return w


def frame_lum(path: str, n: int, x: int, y: int, bits: int = 8) -> int:
    """The mean brightness of a 12×12 square at (x, y) of frame n (counted from the first frame of the video)."""
    fmt = "gray10le" if bits > 8 else "gray"
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-vf",
                        f"select=eq(n\\,{n}),crop=12:12:{x}:{y},scale=1:1:flags=area,format={fmt}", "-frames:v", "1",
                        "-f", "rawvideo", "-"], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()[-400:]
    return int.from_bytes(r.stdout[:2], "little") if bits > 8 else r.stdout[0]


def frame_max(path: str, n: int, x: int, y: int, w: int, h: int) -> int:
    """The brightest pixel of a w×h area of frame n (thin letters: the mean would be their outline's grey)."""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:v:0", "-vf",
                        f"select=eq(n\\,{n}),crop={w}:{h}:{x}:{y},format=gray", "-frames:v", "1", "-f", "rawvideo",
                        "-"], capture_output=True)
    assert r.returncode == 0, r.stderr.decode()[-400:]
    return max(r.stdout)


def run_job(j, out, av):
    args = J.build_args(j, "ffmpeg", out, av, False)
    r = subprocess.run(args, capture_output=True, text=True)
    assert r.returncode == 0, " ".join(args) + "\n" + r.stderr[-2000:]
    return args


@pytest.fixture(scope="module")
def hs(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("hardsub20"))
    sup = pgsmaker.write_sup(os.path.join(d, "pg.sup"), [(1.0, 3.0, BOX_A, False), (5.0, 7.0, BOX_B, True)])
    ass = os.path.join(d, "signs.ass")
    with open(ass, "w", encoding="utf-8") as f:
        f.write(ASS.format(font="DejaVu Sans"))
    src = os.path.join(d, "grey.mkv")
    mk("-f", "lavfi", "-i", "color=gray:s=1920x1080:r=25:d=9", "-f", "lavfi", "-i", "sine=d=9", "-i", sup, "-i", ass,
       "-map", "0", "-map", "1", "-map", "2", "-map", "3", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
       "-c:s", "copy", "-metadata:s:a:0", "language=rus", "-metadata:s:s:0", "language=rus",
       "-metadata:s:s:1", "language=eng", src)
    dsup = pgsmaker.write_sup(os.path.join(d, "dvd.sup"), [(1.0, 3.0, (40, 40, 200, 100), False),
                                                           (5.0, 7.0, (400, 400, 200, 100), True)], 720, 576)
    vob = os.path.join(d, "dvd.mks")
    mk("-fix_sub_duration", "-i", dsup, "-c:s", "dvdsub", "-f", "matroska", vob)
    dvd = os.path.join(d, "dvd.mkv")
    mk("-f", "lavfi", "-i", "color=gray:s=720x576:r=25:d=9", "-i", vob, "-map", "0", "-map", "1", "-c:v", "mpeg2video",
       "-aspect", "16:9", "-c:s", "copy", dvd)
    hdr = os.path.join(d, "hdr.mkv")
    mk("-f", "lavfi", "-i", "color=gray:s=1280x720:r=25:d=4", "-i", sup, "-i", ass, "-map", "0", "-map", "1", "-map", "2",
       "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
       "-x265-params", "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
       "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-c:s", "copy", hdr)
    srt = os.path.join(d, "Фильм it's [1].srt")
    with open(srt, "wb") as f:
        f.write("1\n00:00:01,000 --> 00:00:03,000\nПривет, мир\n\n2\n00:00:05,000 --> 00:00:07,000\nВторая строка\n"
                .encode("cp1251"))
    return {"dir": d, "sup": sup, "ass": ass, "src": src, "dvd": dvd, "vob": vob, "hdr": hdr, "srt": srt,
            "info": P.probe(src)}


def _job(hs, src=None, **video):
    src = src or hs["src"]
    v = {"encoder": "x264", "speed": 1, "height": 360}
    v.update(video)
    return J.Job(src=src, settings=J.merged({"container": "mkv", "video": v, "audio": {"codec": "aac"}}),
                 info=hs["info"] if src == hs["src"] else P.probe(src))


# ── the filter graph text ───────────────────────────────────────
def test_paths_escaped_for_the_filter_graph(hs, tmp_path):
    assert HS.esc(r"C:\Фильмы\it's [1], a;b.srt", win=True) == "'C\\:/Фильмы/it\\'\\''s [1], a;b.srt'"
    assert HS.esc("D:\\Видео (2024)\\x.ass", win=True) == "'D\\:/Видео (2024)/x.ass'"
    assert F.escape_path(r"C:\a\b.trf", True) == "'C\\:/a/b.trf'"       # vid.stab / libvmaf use the same now
    # a real file with an apostrophe, brackets, a comma, a semicolon and Cyrillic, through ffmpeg itself
    j = _job(hs)
    j.ext = [J.clean_ext({"path": hs["srt"], "kind": "subtitle", "burn": "all"})]
    J.load_files(j)
    args = run_job(j, str(tmp_path / "a.mkv"), E.detect())
    vf = args[args.index("-vf") + 1]
    assert "charenc=CP1251" in vf and "filename='" in vf
    # «Привет, мир» at the bottom, in the middle
    assert frame_max(str(tmp_path / "a.mkv"), 50, 200, 300, 240, 56) > 220
    assert frame_max(str(tmp_path / "a.mkv"), 100, 200, 300, 240, 56) < 140                # gone after 3 s
    assert frame_max(str(tmp_path / "a.mkv"), 50, 200, 0, 240, 200) < 140                  # and nothing elsewhere


def test_time_map_and_setpts_go_back_exactly():
    tm = [(0, 500000, 3000000), (3000000, 4500000, 1500000), (4500000, 39500000, 1500000)]
    there, back = HS.shift_filters(tm)
    assert there[0] == "settb=1/1000000"
    for out_t in (0, 2999999, 3000000, 4400000, 4500000, 5999000):
        k = 0 if out_t < 3000000 else 1 if out_t < 4500000 else 2
        src_t = out_t + tm[k][1] - tm[k][0]
        # the way back picks the same piece by the source time (halfway between pieces)
        mids = [(tm[i][1] + tm[i][2] + tm[i + 1][1]) // 2 for i in range(2)]
        k2 = 0 if src_t < mids[0] else 1 if src_t < mids[1] else 2
        assert k2 == k
    assert HS.shift_filters([(0, 0, 10 ** 13)]) == ([], [])          # the whole file, no offset: nothing to do


# ── pictures: PGS / VobSub ──────────────────────────────────────
def test_pgs_burnt_scaled_alpha_and_no_track_left(hs, av, tmp_path):
    j = _job(hs)
    j.tracks = {"2": {"burn": "all"}}
    p = J.plan(j)
    assert p["burn"] is not None and p["burn"].bitmap
    assert [s.index for s, _h in p["subs"]] == [3]                    # the PGS is in the picture, the ASS stays
    out = str(tmp_path / "pgs.mkv")
    run_job(j, out, av)
    # 1920×1080 → 640×360: box A at (33, 33) … (167, 100)
    assert frame_lum(out, 12, 60, 60) < 140                            # 0.5 s: nothing yet
    assert frame_lum(out, 50, 60, 60) > 220                            # 2 s: the box, white
    assert 110 < frame_lum(out, 50, 400, 60) < 140                     # beside it the grey stays (the canvas is clear)
    assert frame_lum(out, 100, 60, 60) < 140                           # 4 s: gone
    assert frame_lum(out, 150, 380, 260) > 220                         # box B
    subs = P.probe(out).of("subtitle")
    assert [s.codec for s in subs] == ["ass"]


def test_pgs_forced_only(hs, av, tmp_path):
    j = _job(hs)
    j.tracks = {"2": {"burn": "forced"}}
    out = str(tmp_path / "forced.mkv")
    args = run_job(j, out, av)
    assert args[args.index("-forced_subs_only:s") + 1] == "1"
    assert frame_lum(out, 50, 60, 60) < 140                            # the normal line is not drawn
    assert frame_lum(out, 150, 380, 260) > 220                         # the forced one is


def test_pgs_through_several_pieces_and_crop(hs, av, tmp_path):
    """Cut [0.5, 3.5] + [4.5, 6.5]: box A on the result at 0.5 … 2.5 s, box B at 3.5 …; the crop (140 px top and
    bottom) moves the box the way it moves the picture."""
    j = _job(hs, filters={})
    j.settings["filters"]["crop"].update(on=True, mode="manual", t=140, b=140)
    j.tracks = {"2": {"burn": "all"}}
    j.trim = [[0.5, 3.5], [4.5, 6.5]]
    out = str(tmp_path / "cut.mkv")
    args = run_job(j, out, av)
    assert "concat=n=2:v=2" in args[args.index("-filter_complex") + 1]
    assert J.out_size(j, E.BY_ID["x264"]) == (640, 266)
    # box A (100…300 high) after the crop: 0…160 → 0…53 on the result
    assert frame_lum(out, 5, 60, 20) < 140
    assert frame_lum(out, 25, 60, 20) > 220
    assert frame_lum(out, 25, 60, 70) < 140                            # where it would be without the crop
    assert frame_lum(out, 70, 60, 20) < 140
    assert frame_lum(out, 95, 380, (700 - 140) // 3 + 10) > 220        # box B, 3.8 s on the result


def test_vobsub_outside_file_with_delay(hs, av, tmp_path):
    """A DVD (720×576, 16:9) with an outside VobSub 1 s late: the box comes at 2 … 4 s."""
    info = P.probe(hs["dvd"])
    j = J.Job(src=hs["dvd"], settings=J.merged({"container": "mkv", "video": {"encoder": "x264", "speed": 1, "height": 576},
                                               "subs": "none"}), info=info)
    j.ext = [J.clean_ext({"path": hs["vob"], "kind": "subtitle", "delay": 1000, "burn": "all"})]
    J.load_files(j)
    out = str(tmp_path / "dvd.mkv")
    run_job(j, out, av)
    v = P.probe(out).video
    assert (v.width, v.height) == (1024, 576)                          # square pixels
    x, y = int(60 * 1024 / 720), 60
    assert frame_lum(out, 37, x, y) < 140                              # 1.5 s: not yet (1 s late)
    assert frame_lum(out, 75, x, y) > 200                              # 3 s
    assert frame_lum(out, 110, x, y) < 140                             # 4.4 s: gone


# ── text ────────────────────────────────────────────────────────
def test_ass_through_pieces_joined_files_and_chunks(hs, av, tmp_path):
    j = _job(hs)
    j.tracks = {"3": {"burn": "all"}}
    j.trim = [[0.5, 3.5], [4.5, 6.5]]
    out = str(tmp_path / "ass.mkv")
    args = run_job(j, out, av)
    fc = args[args.index("-filter_complex") + 1]
    assert "setpts='PTS+if(lt(PTS," in fc and ":si=1" in fc
    assert frame_lum(out, 5, 60, 60) < 140 and frame_lum(out, 25, 60, 60) > 220 and frame_lum(out, 70, 60, 60) < 140
    assert frame_lum(out, 95, 380, 260) > 220
    # two files joined: the second's own subtitles at their own time
    two = str(tmp_path / "two.mkv")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", hs["src"], "-map", "0", "-c", "copy", "-t", "4", two], check=True)
    k = _job(hs, src=two)
    k.joins = [hs["src"]]
    J.load_files(k)
    k.tracks = {"3": {"burn": "all"}}
    out2 = str(tmp_path / "joined.mkv")
    run_job(k, out2, av)
    base = round(J._dur(k.info) * 25)
    assert frame_lum(out2, 50, 60, 60) > 220                           # the first file's line
    assert frame_lum(out2, base + 50, 60, 60) > 220                    # the second file's, 4 s later
    assert frame_lum(out2, base + 12, 60, 60) < 140
    # a chunk of the task in chunks: its piece of the film, its piece of the subtitles
    from convert import chunks as CH
    c = CH.chunk_job(j, [[4.0, 8.0]])
    assert c.tracks["3"]["burn"] == "all"
    out3 = str(tmp_path / "chunk.mkv")
    run_job(c, out3, av)
    assert frame_lum(out3, 12, 380, 260) < 140 and frame_lum(out3, 40, 380, 260) > 220   # 5 … 7 s = 1 … 3 s


def test_srt_style_and_place(hs):
    st = J.merged({"hardsub": {"font": "Verdana", "size": 54, "outline": 3, "pos": "top", "color": "yellow",
                               "margin": 54, "bold": True}})
    s = HS.force_style(st)
    assert "FontName=Verdana" in s and "FontSize=14.4" in s and "Alignment=8" in s and "MarginV=14.4" in s
    assert "PrimaryColour=&H0000F0FF" in s and "Bold=-1" in s and "Outline=0.8" in s
    assert HS.charenc(hs["srt"]) == "CP1251"


def test_hdr_kept_subtitles_not_blinding(hs, av, tmp_path):
    j = J.Job(src=hs["hdr"], settings=J.merged({"container": "mkv", "video": {"encoder": "x265", "speed": 1, "bits": "10"}}),
              info=P.probe(hs["hdr"]))
    for idx in ("1", "2"):                                             # PGS, then ASS (on a canvas, brought down)
        j.tracks = {idx: {"burn": "all"}}
        out = str(tmp_path / f"hdr{idx}.mkv")
        args = run_job(j, out, av)
        assert "lutrgb=r='st(0" in args[args.index("-filter_complex") + 1]
        assert P.probe(out).video.hdr == "HDR10"
        grey = frame_lum(out, 50, 600, 400, 10)
        white = frame_lum(out, 50, 150, 120, 10)               # 1920 canvas → 1280: box A at (67 … 333, 67 … 200)
        assert grey + 30 < white < 700, (grey, white)          # ~200 nits (PQ ≈ 0.58), not 10 000 (940)
    # HDR → SDR: the letters after the tone mapping, plain white
    j.settings["video"].update(encoder="x264", bits="8")
    j.tracks = {"2": {"burn": "all"}}
    out = str(tmp_path / "sdr.mkv")
    args = run_job(j, out, av)
    vf = args[args.index("-vf") + 1]
    assert vf.index("tonemap=") < vf.index("subtitles=")
    assert frame_lum(out, 50, 150, 120) > 220


def test_auto_burns_the_forced(hs):
    st = J.merged({"hardsub": {"auto": "forced"}})
    j = J.Job(src=hs["src"], settings=st, info=hs["info"])
    b = HS.choice(j)                                                   # sound rus, PGS rus → its forced lines
    assert b is not None and b.auto and b.stream.index == 2 and b.mode == "forced"
    info = P.probe(hs["src"])
    info.of("subtitle")[1].forced = True
    info.of("subtitle")[1].lang = "rus"
    k = J.Job(src=hs["src"], settings=st, info=info)
    b = HS.choice(k)                                                   # a track flagged forced wins
    assert b.stream.index == 3 and b.mode == "all"
    k.tracks = {"3": {"burn": ""}}                                     # «don't burn» on the Streams tab: nothing
    assert HS.choice(k) is None
    j.settings["video"]["mode"] = "copy"
    assert HS.active(j) is None and "w_burn_copy" in J.plan(j)["warnings"]


def test_status_frame_and_before_after_show_the_subtitles(hs, av, tmp_path):
    j = _job(hs)
    j.tracks = {"2": {"burn": "all"}}
    j.trim = [[1.5, 2.6]]
    out = str(tmp_path / "pv.mkv")
    png = str(tmp_path / "frame.png")
    args = J.build_args(j, "ffmpeg", out, av, False, preview=png)
    assert subprocess.run(args, capture_output=True).returncode == 0
    from PySide6.QtGui import QImage
    im = QImage(png)
    assert not im.isNull() and (im.pixel(30, 30) & 0xFF) > 200         # 320 wide: box A at (17 … 83, 17 … 50)
    from convert import beforeafter as BA
    k = BA.piece_job(j, 1.0)
    assert J.plan(k)["burn"] is not None
    g = BA.Grabber()
    a, b, err = g.frames(j, av, 2.0, use_result=False, hw=False)
    assert not err and (QImage(b).pixel(60, 60) & 0xFF) > 200 and (QImage(a).pixel(60, 60) & 0xFF) < 160


def test_streams_tab_burn_choice(qapp, hs, data_dir):
    from PySide6.QtWidgets import QComboBox
    w = _win(qapp)
    w.add_paths([hs["src"]])
    assert pump(qapp, 20, lambda: w.jobs[0].info is not None)
    j = w.jobs[0]
    w.view.setCurrentItem(w.items[j.uid])
    w.show_side("streams")
    pump(qapp, 0.5)
    boxes = [b for b in w.tracks_box.findChildren(QComboBox) if b.objectName() == "trackBurn" and b.isVisibleTo(w)]
    assert [b.count() for b in boxes] == [3, 2]                        # PGS: + «forced only»; ASS: no
    boxes[1].setCurrentIndex(1)
    pump(qapp, 0.5)
    assert j.tracks["3"]["burn"] == "all" and J.plan(j)["burn"].stream.index == 3
    boxes = [b for b in w.tracks_box.findChildren(QComboBox) if b.objectName() == "trackBurn" and b.isVisibleTo(w)]
    boxes[0].setCurrentIndex(2)                                        # another track: the first loses the mark
    pump(qapp, 0.5)
    assert j.tracks["2"]["burn"] == "forced" and j.tracks["3"]["burn"] == ""
    from PySide6.QtCore import QTimer                                  # the look dialog opens and writes the preset
    from PySide6.QtWidgets import QSpinBox

    def poke():
        d = w._hs_dialog
        d.findChildren(QSpinBox)[0].setValue(60)
        d.reject()
    QTimer.singleShot(200, poke)
    w.hardsub_dialog()
    assert j.settings["hardsub"]["size"] == 60
    w.close()


# ── the 2.0 matrix ─────────────────────────────────────────────
def _mix(tmp_path, av, ch: str, mode: str, dm=None, amp: float = 0.5) -> tuple[float, float, int]:
    """A 5.1(side) FLAC with a 100 Hz tone in the given channels → stereo FLAC with the matrix → (peak L, peak R,
    the sign of L·R)."""
    src = str(tmp_path / f"in_{ch.replace('|', '_')}.mka")
    if not os.path.isfile(src):
        pan = "|".join(f"{c}=c0" for c in ch.split("|"))
        mk("-f", "lavfi", "-i", f"sine=f=100:d=1:sample_rate=48000", "-af", f"volume={amp * 8:g},pan=5.1(side)|{pan}",
           "-c:a", "flac", src)
    info = P.probe(src)
    j = J.Job(src=src, settings=J.merged({"container": "mka", "audio": {"codec": "flac", "channels": "stereo",
                                                                        "downmix": mode, "dm": dm or {}}}), info=info)
    out = str(tmp_path / f"out_{mode}_{ch.replace('|', '_')}.mka")
    run_job(j, out, av)
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", out, "-f", "f32le", "-"], capture_output=True)
    n = len(r.stdout) // 8
    lr = [struct.unpack_from("<ff", r.stdout, i * 8) for i in range(n)]
    pl, pr = max(abs(a) for a, _b in lr), max(abs(b) for _a, b in lr)
    return pl, pr, (1 if sum(a * b for a, b in lr) > 0 else -1)


def test_downmix_matrix_math():
    left, right, k = F.downmix_matrix({"downmix": "dialog"}, 6, "5.1(side)")
    assert dict(left) == pytest.approx({"FL": 0.4, "FC": 0.4, "SL": 0.2}, abs=0.002)
    assert sum(x for _n, x in left) <= 1.0 + 1e-9 and k == pytest.approx(0.4, abs=0.001)
    left, _r, _k = F.downmix_matrix({"downmix": "custom", "dm": {"c": 0, "f": 0, "s": -6, "b": -12, "lfe": True}}, 8, "7.1")
    d = dict(left)                                                     # 7.1: SL the side, BL the back
    assert d["SL"] / d["FL"] == pytest.approx(0.501, abs=0.01) and d["BL"] / d["FL"] == pytest.approx(0.251, abs=0.01)
    assert "LFE" in d and sum(d.values()) == pytest.approx(1.0, abs=1e-6)
    assert F.downmix_filters({"downmix": "dplii"}, 6, "5.1") == ["aresample=matrix_encoding=dplii:ochl=stereo"]
    assert not F.downmix_on({"downmix": "dialog"}, 2, 2) and not F.downmix_on({"downmix": "normal"}, 6, 2)
    assert "custom: C 0 dB" in F.downmix_text({"downmix": "custom", "dm": {"c": 0}}, 6, "5.1")


def test_downmix_real_levels_and_no_clipping(av, tmp_path):
    def ratio(mode, dm=None):
        c = _mix(tmp_path, av, "FC", mode, dm)[0]
        f = _mix(tmp_path, av, "FL", mode, dm)[0]
        s = _mix(tmp_path, av, "SL", mode, dm)[0]
        return c / f, s / f
    n_c, n_s = ratio("normal")
    d_c, d_s = ratio("dialog")
    assert n_c == pytest.approx(0.707, abs=0.03) and n_s == pytest.approx(0.707, abs=0.03)
    assert d_c == pytest.approx(1.0, abs=0.03) and d_s == pytest.approx(0.5, abs=0.03)     # centre +3 dB, back −3 dB
    c_c, _ = ratio("custom", {"c": 6, "f": 0, "s": -3, "b": -3})
    assert c_c == pytest.approx(2.0, abs=0.06)
    # every channel at the same time, near full scale: never past 1.0
    for mode in ("dialog", "custom"):
        pl, pr, _sg = _mix(tmp_path, av, "FL|FR|FC|LFE|SL|SR", mode, {"c": 6, "lfe": True}, amp=0.95)
        assert pl <= 1.0 and pr <= 1.0 and pl > 0.6
    # Dolby Pro Logic II: a surround channel reaches both sides, in opposite phase
    pl, pr, sg = _mix(tmp_path, av, "SL", "dplii")
    assert pl > 0.05 and pr > 0.03 and sg < 0


def test_downmix_in_the_log_and_ui(qapp, media, data_dir, tmp_path):
    from PySide6.QtWidgets import QComboBox
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path), save_logs=True)
    w.add_paths([os.path.join(media, "Movie (2017).mkv")])
    assert pump(qapp, 20, lambda: w.jobs[0].info is not None)
    j = w.jobs[0]
    j.settings = J.merged({"container": "mkv", "video": {"mode": "copy"}, "audio": {"codec": "aac"}})
    w.view.setCurrentItem(w.items[j.uid])
    w.show_side("streams")
    pump(qapp, 0.3)
    assert not w.a_dm.isVisibleTo(w)
    w._set("audio", "channels", "stereo")
    pump(qapp, 0.3)
    assert w.a_dm.isVisibleTo(w)
    w.a_dm.setCurrentIndex(w.a_dm.findData("dialog"))
    pump(qapp, 0.3)
    assert j.settings["audio"]["downmix"] == "dialog"
    dm = [b for b in w.tracks_box.findChildren(QComboBox) if b.objectName() == "trackDownmix" and b.isVisibleTo(w)]
    assert len(dm) == 1                                                # only the 5.1 track mixes down
    dm[0].setCurrentIndex(dm[0].findData("dplii"))
    pump(qapp, 0.3)
    assert j.tracks["1"]["downmix"] == "dplii"
    w.start_queue()
    assert pump(qapp, 120, lambda: j.status in ("done", "error"))
    assert j.status == "done", j.error
    text = open(j.log_path, encoding="utf-8").read()
    assert "Downmix:    5.1(side) > 2.0, Dolby Pro Logic II" in text
    w.close()


# ── watch folders ──────────────────────────────────────────────
def test_watch_takes_only_complete_files(qapp, data_dir, tmp_path, monkeypatch):
    from convert import watch as WT
    folder = tmp_path / "in"
    (folder / "sub").mkdir(parents=True)
    out = folder / "done"                                              # a result folder inside: never looked into
    out.mkdir()
    cfg = config.load()
    cfg["watch"] = {"on": True, "stable": 5, "folders": [{"path": str(folder), "preset": "remux_mkv", "out": str(out),
                                                           "sub": True, "on": True}]}
    queue: list[str] = []
    wt = WT.Watcher(cfg, lambda: queue)
    clock = [1000.0]
    wt.clock = lambda: clock[0]
    got = []
    wt.found.connect(lambda p, f: got.append(p))
    a = folder / "sub" / "a.mkv"
    a.write_bytes(b"x" * 1000)
    (out / "result.mkv").write_bytes(b"y" * 10)
    (folder / "b.mkv.part").write_bytes(b"z")

    def scan(dt=0.0):
        clock[0] += dt
        wt.tick()
        assert pump(qapp, 10, lambda: not wt.scanning)
    scan()
    assert got == []                                                   # first seen: wait
    a.write_bytes(b"x" * 2000)                                         # still growing
    scan(6)
    assert got == []
    scan(3)
    assert got == []                                                   # unchanged only 3 s
    busy = [True]
    monkeypatch.setattr(WT.winint, "file_busy", lambda p: busy[0])
    scan(3)
    assert got == []                                                   # held open by a copy
    busy[0] = False
    scan(6)
    assert got == [str(a)]                                             # complete: taken once
    scan(6)
    assert got == [str(a)]
    wt.done(str(a))
    wt2 = WT.Watcher(cfg, lambda: [])                                  # the next start: remembered
    wt2.clock = lambda: clock[0]
    got2 = []
    wt2.found.connect(lambda p, f: got2.append(p))
    for dt in (0, 6, 6):
        clock[0] += dt
        wt2.tick()
        assert pump(qapp, 10, lambda: not wt2.scanning)
    assert got2 == []
    a.write_bytes(b"new" * 900)                                        # a new file under the same name: again
    for dt in (0, 6, 6):
        clock[0] += dt
        wt2.tick()
        assert pump(qapp, 10, lambda: not wt2.scanning)
    assert got2 == [str(a)]
    assert os.path.isfile(a)                                           # sources are never touched


def test_watch_folder_encodes_with_its_preset(qapp, media, data_dir, tmp_path):
    import shutil
    folder, out = tmp_path / "watch", tmp_path / "results"
    (folder / "Season 1").mkdir(parents=True)
    w = _win(qapp, watch={"on": True, "stable": 2, "folders": [{"path": str(folder), "preset": "remux_mkv",
                                                                 "out": str(out), "sub": True, "on": True}]})
    w.watcher.timer.setInterval(300)
    assert w.watcher.active
    src = folder / "Season 1" / "ep.mkv"
    shutil.copy(os.path.join(media, "Movie (2017).mkv"), src)
    assert pump(qapp, 30, lambda: w.jobs and w.jobs[0].status == "done"), [(j.status, j.error) for j in w.jobs]
    j = w.jobs[0]
    assert j.preset == "remux_mkv" and j.origin == "watch"
    assert os.path.dirname(j.out) == str(out / "Season 1") and os.path.isfile(j.out)
    assert os.path.isfile(src)
    pump(qapp, 3)
    assert len(w.jobs) == 1                                            # neither the source nor the result again
    from convert import watch as WT
    st = WT.Store()
    assert st.has(str(src), os.path.getsize(src), os.path.getmtime(src)) and st.has(j.out, os.path.getsize(j.out),
                                                                                     os.path.getmtime(j.out))
    w.close()


def test_watch_dialog_writes_the_folders(qapp, data_dir, tmp_path):
    from convert.dialogs import WatchDialog
    cfg = config.load()
    d = WatchDialog(cfg, [("hevc_hw", "HEVC")])
    d.add({"path": str(tmp_path), "preset": "hevc_hw", "out": "", "sub": False, "on": True})
    assert d.tree.topLevelItemCount() == 1 and d.tree.topLevelItem(0).text(2) == "HEVC"
    d.stable.setValue(15)
    d.accept()
    assert cfg["watch"]["stable"] == 15 and cfg["watch"]["folders"][0]["path"] == os.path.normpath(str(tmp_path))
    config.save(cfg)
    assert config.load()["watch"]["folders"][0]["preset"] == "hevc_hw"


# ── Explorer's menu, one running copy ───────────────────────────
def test_shell_menu_entries_and_arguments(data_dir):
    from convert import shellmenu as SM
    from convert import winint as W
    cfg = config.load()
    e = SM.entries(cfg, {"Мой": {}})
    verbs = [k for k in e if k.endswith(W.SHELL_KEY)]
    assert len(verbs) == len(__import__("convert").VIDEO_EXT) + 1                         # every video type + folders
    assert all(e[k]["ExtendedSubCommandsKey"] == W.SHELL_MENU for k in verbs)
    cmds = [v[""] for k, v in e.items() if k.endswith("\\command")]
    assert cmds[0].endswith('--ctx "%1"') and any('--preset "hevc_hw" "%1"' in c for c in cmds)
    assert any('--preset "Мой"' in c for c in cmds)
    assert all(k.startswith("Software\\Classes\\") for k in e)                            # HKCU only
    o = SM.parse_args(["--ctx", "--preset", "remux_mkv", "C:\\a b.mkv", "D:\\dir"])
    assert o == {"ctx": True, "preset": "remux_mkv", "shell": "", "paths": ["C:\\a b.mkv", "D:\\dir"], "selftest": False}
    assert SM.parse_args(["--shell-menu", "on"])["shell"] == "on"
    from convert.main import parse_message
    got = parse_message(SM.message(["/x/a.mkv"], True, "remux_mkv"))
    assert got == (["/x/a.mkv"], True, "remux_mkv")
    assert parse_message("--show") == ([], False, "")


def test_open_from_shell_preset_and_start(qapp, media, data_dir, tmp_path):
    w = _win(qapp, ctx_start=True, out_mode="folder", out_dir=str(tmp_path))
    w.open_from_shell([os.path.join(media, "Movie (2017).mkv")], "remux_mkv", ctx=True)
    j = w.jobs[0]
    assert j.preset == "remux_mkv" and j.settings["video"]["mode"] == "copy"
    assert w.running
    assert pump(qapp, 60, lambda: j.status == "done"), j.error
    w.open_from_shell([os.path.join(media, "dts.mkv")], "", ctx=False)         # not from the menu: just queued
    assert w.jobs[1].status in ("wait", "probe") and w.jobs[1].preset == w.cfg.get("preset")
    w.close()


def _wait_server(name: str, secs: float) -> bool:
    from PySide6.QtNetwork import QLocalSocket
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        s = QLocalSocket()
        s.connectToServer(name)
        if s.waitForConnected(200):
            s.write(b"--show")
            s.waitForBytesWritten(500)
            s.disconnectFromServer()
            return True
        time.sleep(0.2)
    return False


def test_one_copy_takes_the_files_of_every_copy(qapp, media, tmp_path):
    """Explorer starts a copy per selected file, all at once: one becomes the program, the rest hand their files over
    and quit — and so do copies started later."""
    data = tmp_path / "data"
    data.mkdir()
    with open(data / "config.json", "w", encoding="utf-8") as f:
        json.dump({"check_updates": False, "keep_queue": True, "notify": False}, f)
    user = f"actest{os.getpid()}"
    env = dict(os.environ, ARTCORE_CONVERT_DATA=str(data), USER=user, USERNAME=user, QT_QPA_PLATFORM="offscreen",
               ARTCORE_NO_MPV="1")
    files = []
    for i in range(3):
        p = tmp_path / f"clip{i}.mkv"
        p.write_bytes(open(os.path.join(media, "Movie (2017).mkv"), "rb").read())
        files.append(str(p))
    run = os.path.join(ROOT, "run.py")
    procs = [subprocess.Popen([sys.executable, run, "--ctx", "--preset", "remux_mkv", f], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for f in files]
    try:
        assert _wait_server("ArtCoreConvert-" + user, 60)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 30 and sum(p.poll() is None for p in procs) > 1:
            time.sleep(0.2)
        alive = [p for p in procs if p.poll() is None]
        assert len(alive) == 1 and all(p.returncode == 0 for p in procs if p is not alive[0])
        late = subprocess.run([sys.executable, run, os.path.join(media, "dts.mkv")], env=env, timeout=60)
        assert late.returncode == 0
        t0 = time.monotonic()
        jobs = []
        while time.monotonic() - t0 < 20:
            q = config_read(data / "queue.json")
            jobs = q.get("jobs", []) if q else []
            if len(jobs) == 4:
                break
            time.sleep(0.3)
        assert sorted(os.path.basename(j["src"]) for j in jobs) == ["clip0.mkv", "clip1.mkv", "clip2.mkv", "dts.mkv"]
        assert {j["preset"] for j in jobs if j["src"].endswith(tuple(f"clip{i}.mkv" for i in range(3)))} == {"remux_mkv"}
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()


def config_read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@pytest.mark.skipif(sys.platform != "win32", reason="the registry is Windows'")
def test_shell_menu_in_the_registry(data_dir):
    import winreg
    from convert import shellmenu as SM
    from convert import winint as W
    cfg = config.load()
    cfg["shell_menu"] = True
    assert SM.apply(cfg) == ""
    assert W.shell_menu_on()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\SystemFileAssociations\.mkv\shell\{W.SHELL_KEY}") as k:
        assert winreg.QueryValueEx(k, "ExtendedSubCommandsKey")[0] == W.SHELL_MENU
    cfg["shell_menu"] = False
    assert SM.apply(cfg) == ""
    assert not W.shell_menu_on()


def test_tray_minimise(qapp, data_dir, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    w = _win(qapp, tray=True)
    assert w._tray is not None and w._tray_menu is not None
    w.showMinimized()
    pump(qapp, 0.5)
    assert not w.isVisible()                                           # in the tray; the queue and the watching go on
    w.bring_up()
    pump(qapp, 0.3)
    assert w.isVisible()
    w.close()


def test_settings_dialog_has_the_new_options(qapp, data_dir):
    from convert.dialogs import SettingsDialog
    cfg = config.load()
    d = SettingsDialog(cfg)
    d.shell.setChecked(True)
    d.ctx_start.setChecked(True)
    d.tray.setChecked(True)
    d.accept()
    assert cfg["shell_menu"] is True and cfg["ctx_start"] is True and cfg["tray"] is True

