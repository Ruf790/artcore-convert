"""1.8: encoding in chunks (several ffmpeg, resume, reuse), EDL, name templates, Blu-ray / DVD titles, demux,
Dolby Vision / HDR10+ decisions, the size estimate."""
import os
import struct
import subprocess

import pytest

from conftest import ff as mk, pump
from convert import config
from convert import filters as F
from convert import job as J
from convert import probe as P


@pytest.fixture(scope="module")
def films(media, tmp_path_factory):
    d = str(tmp_path_factory.mktemp("films18"))
    mk("-f", "lavfi", "-i", "testsrc2=s=480x270:r=25:d=40", "-f", "lavfi", "-i", "sine=f=440:d=40",
       "-f", "lavfi", "-i", "sine=f=600:d=40", "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264",
       "-preset", "ultrafast", "-g", "50", "-c:a", "ac3", "-metadata", "title=Тест", os.path.join(d, "Film.mkv"))
    return d


def _win(qapp, **over):
    from convert.window import MainWindow
    cfg = config.load()
    cfg.update(check_updates=False, notify=False, keep_queue=False)
    cfg.update(over)
    w = MainWindow(cfg)
    w.show()
    assert pump(qapp, 30, lambda: w.av_ready)
    return w


def _frames(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_read_frames", "-of", "csv=p=0", path], capture_output=True, text=True)
    return int(r.stdout.strip() or 0)


# ── chunks ────────────────────────────────────────────────────
def test_chunk_plan_on_frames(films):
    from convert import chunks as C
    j = J.Job(src=os.path.join(films, "Film.mkv"), settings=J.merged({"video": {"encoder": "x264"}}),
              info=P.probe(os.path.join(films, "Film.mkv")))
    j.trim = [[3.0, 37.0]]
    pl = C.plan(j, 0.2)
    assert len(pl) == 3 and pl[0]["pieces"][0][0] == 3.0 and pl[-1]["pieces"][-1][1] == 37.0
    for a, b in zip(pl, pl[1:]):
        cut = a["pieces"][-1][1]
        assert cut == b["pieces"][0][0] and abs(cut * 25 - round(cut * 25)) < 1e-6      # on a frame
    assert abs(sum(c["dur"] for c in pl) - 34.0) < 1e-6
    j.trim = [[1.0, 10.0], [15.0, 39.0]]
    pl = C.plan(j, 0.2)
    assert pl[0]["pieces"] == [[1.0, 10.0], [15.0, 17.0]]                              # across the cut
    assert C.why_not(j, None) == "" and C.key(j, 3, "x264") == C.key(j, 3, "x264") != C.key(j, 2, "x264")
    j.joins = ["/x/b.mkv"]
    assert C.why_not(j, None) == "chk_joins"
    j.joins = []
    j.settings["video"]["mode"] = "copy"
    assert C.why_not(j, None) == "chk_no_video"


def test_chunks_run_stop_and_resume(qapp, films, data_dir):
    from convert import chunks as C
    w = _win(qapp, chunks={"on": True, "workers": 3, "minutes": 0.2})
    w.add_paths([os.path.join(films, "Film.mkv")])
    assert pump(qapp, 15, lambda: w.jobs[0].status == "wait")
    j = w.jobs[0]
    j.settings = J.merged({"video": {"encoder": "x264", "speed": 1, "height": 270}})
    j.trim = [[3.0, 37.0]]
    w.start_queue()
    slot = w.slots[0]
    assert pump(qapp, 90, lambda: len(slot.chunked.done) >= 1)
    assert slot.runner is slot.chunked
    w.stop_queue()
    assert pump(qapp, 20, lambda: not w._any_running())
    assert j.status == "stop"
    kept = sorted(f for f in os.listdir(slot.chunked.dir) if f.startswith("chunk_"))
    assert kept and not any(f.endswith(".part") for f in os.listdir(slot.chunked.dir))
    w.retry([j])
    w.start_queue()
    assert pump(qapp, 180, lambda: not w.running and not w._any_running())
    pump(qapp, 0.3)
    assert j.status == "done", j.error
    assert slot.chunked.reused == len(kept)
    info = P.probe(j.out)
    assert [s.codec for s in info.streams] == ["h264", "ac3", "ac3"] and info.tags.get("title") == "Тест"
    assert _frames(j.out) == 34 * 25                                                    # no frame lost or doubled
    assert abs(info.duration - 34.0) < 0.15
    assert not os.path.isdir(slot.chunked.dir)                                          # done: chunks gone
    text = open(j.log_path, encoding="utf-8").read()
    for part in ("ENCODING IN CHUNKS", "Chunks: 3, 3 at a time", f"Resumed: {len(kept)} of 3", "CHUNK ",
                 "JOINING THE CHUNKS", "SOUND AND SUBTITLES", "MUXING", "ENCODING COMPLETED!"):
        assert part in text, part
    C.purge(0)
    w.close()


def test_chunks_fall_back_for_two_pass(qapp, films, data_dir):
    w = _win(qapp, chunks={"on": True, "workers": 2, "minutes": 0.2})
    w.add_paths([os.path.join(films, "Film.mkv")])
    assert pump(qapp, 15, lambda: w.jobs[0].status == "wait")
    j = w.jobs[0]
    j.settings = J.merged({"video": {"encoder": "x264", "speed": 7, "height": 144, "rc": "bitrate", "bitrate": 300,
                                     "passes": 2}})
    j.trim = [[0, 20]]
    w.start_queue()
    assert pump(qapp, 120, lambda: not w.running and not w._any_running())
    assert j.status == "done" and w.slots[0].runner is w.slots[0].plain
    assert "два прохода" in w.log.toPlainText() or "two passes" in w.log.toPlainText()
    w.close()


# ── EDL ───────────────────────────────────────────────────────
def test_parse_edl_kinds():
    assert J.parse_edl("10.5   20.0   0\n100 130.2 3\n") == ([[10.5, 20.0], [100.0, 130.2]], True)
    cmx = ("TITLE: Timeline 1\nFCM: NON-DROP FRAME\n\n"
           "001  AX       V     C        00:00:10:00 00:00:20:12 01:00:00:00 01:00:10:12\n"
           "* FROM CLIP NAME: film.mkv\n"
           "002  AX       V     C        00:01:00:00 00:01:05:00 01:00:10:12 01:00:15:12\n"
           "003  AX       A     C        00:01:00:00 00:01:05:00 01:00:10:12 01:00:15:12\n")
    assert J.parse_edl(cmx, 25) == ([[10.0, 20.48], [60.0, 65.0]], False)
    hour = cmx.replace("00:00:10:00 00:00:20:12", "01:00:10:00 01:00:20:12").replace("00:01:00:00 00:01:05:00",
                                                                                        "01:01:00:00 01:01:05:00")
    assert J.parse_edl(hour, 25)[0] == [[10.0, 20.48], [60.0, 65.0]]                # an hour-mark timeline
    assert J.parse_edl("# mpv EDL v0\nfilm.mkv,10,5\nfilm.mkv,start=60,length=2.5\n") == \
        ([[10.0, 15.0], [60.0, 62.5]], False)
    with pytest.raises(ValueError):
        J.parse_edl("hello\nworld\n")
    assert J.write_edl([[10, 20], [30, 40]], False) == "0.000\t10.000\t0\n20.000\t30.000\t0\n40.000\t99999\t0\n"
    assert J.parse_edl(J.write_edl([[5, 8]], True))[0] == [[5.0, 8.0]]


def test_edl_into_trim_tab_and_dropped(qapp, films, data_dir, tmp_path):
    w = _win(qapp)
    src = os.path.join(films, "Film.mkv")
    w.add_paths([src])
    assert pump(qapp, 15, lambda: w.jobs[0].status == "wait")
    j = w.jobs[0]
    w.view.setCurrentItem(w.items[j.uid])
    w.show_side("trim")
    pump(qapp, 0.3)
    edl = tmp_path / "cuts.edl"
    edl.write_text("2.0\t5.0\t0\n30\t35\t3\n", encoding="utf-8")
    assert w.trim_tab.import_edl(str(edl))
    assert j.trim == [[2.0, 5.0], [30.0, 35.0]] and j.trim_cut and w.trim_tab.r_cut.isChecked()
    assert abs(J.out_duration(j) - 32.0) < 0.01
    out = tmp_path / "back.edl"
    assert w.trim_tab.export_edl(str(out)) and J.parse_edl(out.read_text())[0] == [[2.0, 5.0], [30.0, 35.0]]
    # dropped next to the film, named like it
    j.trim, j.trim_cut = [], False
    same = os.path.join(films, "Film.edl")
    with open(same, "w", encoding="utf-8") as f:
        f.write("# mpv EDL v0\nFilm.mkv,10,5\n")
    w.add_incoming([same])
    assert pump(qapp, 5, lambda: j.trim == [[10.0, 15.0]])
    assert not j.trim_cut
    os.remove(same)
    w.close()


# ── templates ─────────────────────────────────────────────────
def test_name_templates(films, tmp_path):
    import shutil
    def job(name, enc="x265", **v):
        p = str(tmp_path / name)
        shutil.copy(os.path.join(films, "Film.mkv"), p)
        info = P.probe(p)
        info.tags.pop("title", None)
        return J.Job(src=p, settings=J.merged({"video": dict({"encoder": enc}, **v)}), info=info)
    tenet = job("Tenet (2020) Blu-ray UHD HDR Remux [IMAX] -BLUEBIRD.mkv")
    assert J.render_name("{title} ({year}) {res} {vcodec}", tenet) == "Tenet (2020) 270p HEVC"
    assert J.render_name("{title}.{year}.{res}.{source}.{vcodec}.{acodec}", tenet) == "Tenet.2020.270p.Blu-ray.HEVC.AC3"
    show = job("Show.Name.S01E02.1080p.WEB-DL.mkv", "x264")
    assert J.render_name("{series}/Season {season}/{series} - {sxe} {res}", show) == "Show Name/Season 01/Show Name - S01E02 270p"
    assert J.render_name("{title} ({year}) [{hdr}]", show) == "Show Name"                         # empty parts go
    cfg = {"out_mode": "folder", "out_dir": str(tmp_path / "out"), "name_template": "{title} ({year})/{title} ({year}) {res}"}
    path = J.out_name(tenet, cfg)
    assert path == os.path.join(str(tmp_path / "out"), "Tenet (2020)", "Tenet (2020) 270p.mkv")
    big = job("Big (2019).mkv", height=2160, upscale=True)
    assert J.render_name("{res}", big) == "2160p"


# ── Blu-ray / DVD ─────────────────────────────────────────────
@pytest.fixture(scope="module")
def bd(media, tmp_path_factory):
    import bdmaker
    d = str(tmp_path_factory.mktemp("bd") / "MY DISC")
    starts = bdmaker.make(d)
    return d, starts


def test_bd_titles_and_source(bd, data_dir):
    from convert import disc as D
    d, _starts = bd
    assert D.disc_root(os.path.join(d, "BDMV", "STREAM")) == ("bd", d)
    assert D.disc_root(d) == ("bd", d) and D.disc_root(os.path.dirname(d)) is None
    ts = D.bd_titles(d, 1)
    assert [t["id"] for t in ts] == ["00800", "00001"] and ts[0].get("main")
    t = ts[0]
    assert abs(t["duration"] - 11.0) < 0.01 and len(t["files"]) == 2
    assert t["audio"] == [("AC3", "rus"), ("AC3", "eng")] and t["marks"] == [0.0, 3.0, 7.0]
    src = D.title_source(t, D.disc_name("bd", d))
    assert src.endswith("MY DISC - 00800.ffconcat")
    info = P.probe(src)
    assert not info.error and abs(info.duration - 11.0) < 0.1
    j = J.Job(src=src, settings=J.merged({"video": {"encoder": "x264", "speed": 7}}), info=info)
    D.apply_title(j, t)
    assert j.meta["streams"] == {"1": {"lang": "rus"}, "2": {"lang": "eng"}}
    assert [c[0] for c in j.chapters] == [0.0, 3.0, 7.0]
    assert J.preview_src(j).startswith("edl://") and ",start=" in J.preview_src(j)
    # a piece across both clips, the picture copied: the concat list goes down to the clips
    j.trim = [[1.0, 4.0], [8.0, 10.0]]
    j.settings["video"]["mode"] = "copy"
    from convert import encoders as E
    out = os.path.join(os.path.dirname(src), "copy.mkv")
    r = subprocess.run(J.build_args(j, "ffmpeg", out, E.detect(), False), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-500:]
    assert 4.5 < P.probe(out).duration < 6.0


def test_open_disc_in_window(qapp, bd, data_dir):
    d, _ = bd
    w = _win(qapp)
    assert w.open_disc(d, pick=lambda ts: ts[:1]) == 1
    j = w.jobs[0]
    assert pump(qapp, 20, lambda: j.status == "wait")
    assert j.src.endswith(".ffconcat") and j.meta.get("streams", {}).get("1") == {"lang": "rus"}
    assert len(j.chapters or []) == 3
    j.settings = J.merged({"video": {"encoder": "x264", "speed": 7, "height": 144}})
    w.start_queue()
    assert pump(qapp, 120, lambda: not w.running and not w._any_running())
    assert j.status == "done", j.error
    info = P.probe(j.out)
    assert abs(info.duration - 11.0) < 0.2 and [s.lang for s in info.of("audio")] == ["rus", "eng"]
    assert len(info.chapter_list) == 3 and os.path.basename(j.out).startswith("MY DISC - 00800")
    w.close()
    w = _win(qapp)
    from PySide6.QtWidgets import QDialog
    from convert import dialogs
    orig = dialogs.DiscDialog.exec
    dialogs.DiscDialog.exec = lambda self: QDialog.Accepted                             # the window, the main film
    try:
        assert w.add_incoming([d]) == 1
        dlg = w._disc_dlg
        assert dlg.view.topLevelItemCount() == 2 and dlg.chosen()[0]["id"] == "00800"
    finally:
        dialogs.DiscDialog.exec = orig
    w.close()


def _ifo(path, minutes=95, audio=(("ru", 0), ("en", 6)), subs=("ru", "en")):
    b = bytearray(0x1000)
    b[0:12] = b"DVDVIDEO-VTS"
    struct.pack_into(">H", b, 0x202, len(audio))
    for i, (lang, fmt) in enumerate(audio):
        o = 0x204 + 8 * i
        b[o] = (fmt << 5) | 0x04
        b[o + 2:o + 4] = lang.encode()
    struct.pack_into(">H", b, 0x254, len(subs))
    for i, lang in enumerate(subs):
        o = 0x256 + 6 * i
        b[o] = 0x01
        b[o + 2:o + 4] = lang.encode()
    struct.pack_into(">I", b, 0xCC, 1)                     # PGCITI at sector 1
    struct.pack_into(">H", b, 0x800, 1)
    struct.pack_into(">I", b, 0x800 + 8 + 4, 0x10)          # the PGC at +0x10
    h, m = divmod(minutes, 60)
    bcd = lambda x: ((x // 10) << 4) | (x % 10)             # noqa: E731
    b[0x810 + 4:0x810 + 8] = bytes([bcd(h), bcd(m), bcd(30), 0x40 | bcd(12)])
    with open(path, "wb") as f:
        f.write(bytes(b))


def test_dvd_titles(tmp_path):
    from convert import disc as D
    vts = tmp_path / "DVD" / "VIDEO_TS"
    vts.mkdir(parents=True)
    _ifo(str(vts / "VTS_01_0.IFO"))
    _ifo(str(vts / "VTS_02_0.IFO"), minutes=0)
    for n in ("VTS_01_0.VOB", "VTS_01_1.VOB", "VTS_01_2.VOB", "VTS_02_1.VOB"):
        (vts / n).write_bytes(b"\0" * 2048)
    assert D.disc_root(str(vts)) == ("dvd", str(tmp_path / "DVD"))
    ts = D.dvd_titles(str(tmp_path / "DVD"))
    t = ts[0]
    assert t["id"] == "VTS_01" and t.get("main") and abs(t["duration"] - (95 * 60 + 30 + 12 / 25)) < 0.01
    assert [os.path.basename(f) for f, _a, _b in t["files"]] == ["VTS_01_1.VOB", "VTS_01_2.VOB"]
    assert t["audio"] == [("AC3", "rus"), ("DTS", "eng")] and t["subs"] == ["rus", "eng"]
    assert t["langs"] == {0x80: "rus", 0x81: "eng", 0x20: "rus", 0x21: "eng"}


# ── demux ─────────────────────────────────────────────────────
def test_demux(qapp, films, data_dir, tmp_path):
    src = os.path.join(films, "Film.mkv")
    j = J.Job(src=src, settings=J.merged({}), info=P.probe(src))
    items = J.demux_items(j, str(tmp_path / "t"))
    assert [os.path.basename(x["path"]) for x in items] == ["Film - #0.h264", "Film - #1.ac3", "Film - #2.ac3"]
    from PySide6.QtCore import Qt
    from convert.dialogs import DemuxDialog
    d = DemuxDialog(j)
    d.folder.setText(str(tmp_path / "t"))
    d._fill()
    d.view.topLevelItem(2).setCheckState(0, Qt.Unchecked)
    d.start()
    assert pump(qapp, 60, lambda: hasattr(d, "ok"))
    assert d.ok and sorted(os.listdir(tmp_path / "t")) == ["Film - #0.h264", "Film - #1.ac3"]
    a = P.probe(str(tmp_path / "t" / "Film - #1.ac3"))
    assert a.of("audio")[0].codec == "ac3" and abs(a.duration - 40) < 0.5
    # the cores: DTS-HD → a DTS core, E-AC3 7.1 → an AC3 core; PCM → WAV; mov_text → SRT
    fake = P.MediaInfo(path="/x/a.m2ts", streams=[
        P.Stream(index=1, kind="audio", codec="dts", profile="DTS-HD MA", channels=8, lang="eng"),
        P.Stream(index=2, kind="audio", codec="eac3", channels=8),
        P.Stream(index=3, kind="audio", codec="pcm_bluray", channels=2),
        P.Stream(index=4, kind="subtitle", codec="mov_text"),
        P.Stream(index=5, kind="subtitle", codec="hdmv_pgs_subtitle", lang="rus")])
    k = J.Job(src="/x/a.m2ts", settings=J.merged({}), info=fake)
    names = {os.path.basename(x["path"]): x["args"] for x in J.demux_items(k, "/out")}
    assert "dca_core" in names["a - #1 eng core.dts"] and "eac3_core" in names["a - #2 core.ac3"]
    assert names["a - #3.wav"][3] == "pcm_s24le" and names["a - #4.srt"][3] == "srt" and "a - #5 rus.sup" in names
    assert not any("core" in n for n in (os.path.basename(x["path"]) for x in J.demux_items(k, "/out", cores=False)))


# ── Dolby Vision / HDR10+ ─────────────────────────────────────
def _hdr_job(hdr="Dolby Vision", plus=False, trc="smpte2084", profile=8):
    v = P.Stream(index=0, kind="video", codec="hevc", width=3840, height=2160, fps=23.976, pix_fmt="yuv420p10le",
                 bits=10, hdr=hdr, dv_profile=profile, dv_compat=1, hdr10plus=plus,
                 color={"color_primaries": "bt2020", "color_transfer": trc, "color_space": "bt2020nc"})
    info = P.MediaInfo(path="/x/film.mkv", duration=100.0, streams=[v, P.Stream(index=1, kind="audio", codec="ac3",
                                                                                channels=6, rate=48000)])
    return J.Job(src="/x/film.mkv", settings=J.merged({"video": {"encoder": "x265", "bits": "10"}}), info=info)


def test_dyn_hdr_decisions(monkeypatch):
    from convert import advanced as ADV
    from convert import encoders as E
    x265, nv = E.BY_ID["x265"], E.BY_ID["nvenc_hevc"]
    monkeypatch.setattr(ADV, "caps", lambda e: {"dolbyvision": frozenset()} if e.id in ("x265", "svtav1") else {})
    j = _hdr_job()
    assert F.hdr_keeps(j, x265, 10) and not F.tonemap_on(j, x265, 10)            # was tone-mapped to SDR before 1.8
    assert J.dyn_hdr(j, x265, 10) == {"dv": True, "hdr10p": False, "lost": []}
    assert J.dyn_hdr(j, nv, 10)["lost"] == ["w_dv_hw"] and not J.dyn_hdr(j, nv, 10)["dv"]
    j.settings["video"]["dyn_hdr"] = False
    assert not J.dyn_hdr(j, x265, 10)["dv"]
    monkeypatch.setattr(ADV, "caps", lambda e: {})                                    # an older ffmpeg
    assert J.dyn_hdr(_hdr_job(), x265, 10)["lost"] == ["w_dv"]
    # HDR10+: x265, the whole film, hdr10plus_tool there
    monkeypatch.setattr(J, "hdr10p_tool", lambda: "/tools/hdr10plus_tool.exe")
    j = _hdr_job("HDR10", plus=True)
    assert J.dyn_hdr(j, x265, 10)["hdr10p"]
    passes = J.hdr10p_passes(j, "ffmpeg")
    assert passes == [["/tools/hdr10plus_tool.exe", "extract", "-o", J.hdr10p_json(j), "/x/film.mkv"]]
    j.trim = [[10, 50]]
    assert J.dyn_hdr(j, x265, 10)["lost"] == ["w_hdr10p_cut"]
    j.trim = []
    assert J.dyn_hdr(j, nv, 10)["lost"] == ["w_hdr10p_enc"]
    m2ts = _hdr_job("HDR10", plus=True)
    m2ts.src = "/x/film.m2ts"
    p = J.hdr10p_passes(m2ts, "ffmpeg")
    assert len(p) == 2 and "hevc_mp4toannexb" in p[0] and p[1][-1].endswith(".hevc")
    from convert import chunks as C
    assert C.why_not(j, None) == "chk_hdr10p"
    monkeypatch.setattr(J, "hdr10p_tool", lambda: "")
    assert J.dyn_hdr(j, x265, 10)["lost"] == ["w_hdr10p_tool"]


def test_dv_and_hdr10p_args(monkeypatch):
    from convert import advanced as ADV
    from convert import encoders as E
    av = E.Available()
    av.ok = {"x265": True}
    av.audio = {"ac3"}
    monkeypatch.setattr(ADV, "caps", lambda e: {"dolbyvision": frozenset()} if e.id == "x265" else {})
    monkeypatch.setattr(J, "hdr10p_tool", lambda: "/t/hdr10plus_tool")
    j = _hdr_job("Dolby Vision", plus=True)
    args = J.build_args(j, "ffmpeg", "/x/out.mkv", av, False)
    assert args[args.index("-dolbyvision") + 1] == "1"
    xp = args[args.index("-x265-params") + 1]
    assert "hdr10=1" in xp and "vbv-maxrate=160000" in xp and "dhdr10-info=" in xp and "dhdr10-opt=1" in xp
    pl = J.plan(j)
    assert "w_dv_kept" in pl["warnings"] and "w_hdr10p_kept" in pl["warnings"]
    pre = J.pre_passes(j, "ffmpeg", "/x/out.mkv", av, False)
    assert j.pre_kinds == ["hdr10p"] and pre[0][0] == "/t/hdr10plus_tool"


def test_probe_reads_dv_profile_and_pid():
    j = {"format": {"duration": "10"}, "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "id": "0x1011", "width": 3840, "height": 2160,
         "avg_frame_rate": "24000/1001", "pix_fmt": "yuv420p10le", "color_transfer": "smpte2084",
         "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 7,
                             "dv_bl_signal_compatibility_id": 6}]},
        {"index": 1, "codec_type": "audio", "codec_name": "truehd", "id": "0x1100", "channels": 8}]}
    info = P.parse("/x/a.m2ts", j)
    v = info.video
    assert v.hdr == "Dolby Vision" and v.dv_profile == 7 and v.dv_compat == 6 and v.pid == 0x1011
    assert info.of("audio")[0].pid == 0x1100


# ── size estimate ─────────────────────────────────────────────
def test_estimate_close_to_real(films, data_dir, tmp_path):
    from convert import encoders as E
    from convert import estimate as ES
    av = E.detect()
    src = os.path.join(films, "Film.mkv")
    j = J.Job(src=src, settings=J.merged({"video": {"encoder": "x264", "speed": 7, "quality": 23}}), info=P.probe(src))
    pts = ES.sample_points(j)
    assert len(pts) == ES.SAMPLES and pts[0][0] < 3 and pts[-1][1] > 37
    est = ES.Estimator()
    size, kbps = est.measure(j, av, 23)
    out = str(tmp_path / "full.mkv")
    subprocess.run(J.build_args(j, "ffmpeg", out, av, False), capture_output=True, check=True)
    real = os.path.getsize(out)
    assert abs(size - real) / real < 0.15 and kbps > 0
    smaller, _ = est.measure(j, av, 30)
    assert smaller < size
    assert ES.steps_around(E.BY_ID["x264"], 23) == [19, 21, 23, 25, 27]
    assert ES.steps_around(E.BY_ID["x264"], 50) == [46, 48, 50]


def test_estimate_dialog(qapp, films, data_dir):
    w = _win(qapp)
    w.add_paths([os.path.join(films, "Film.mkv")])
    assert pump(qapp, 15, lambda: w.jobs[0].status == "wait")
    j = w.jobs[0]
    j.settings = J.merged({"video": {"encoder": "x264", "speed": 7, "quality": 23, "height": 144}})
    d = w.estimate_dialog(j)
    assert pump(qapp, 60, lambda: d.view.topLevelItemCount() == 1 and not d.est.busy)
    d.more()
    assert pump(qapp, 120, lambda: d.view.topLevelItemCount() == 5 and not d.est.busy)
    qs = [d.view.topLevelItem(i).data(0, 0x0100) for i in range(5)]
    assert qs == [19, 21, 23, 25, 27]
    sizes = [d.rows[q][0] for q in qs]
    assert sizes == sorted(sizes, reverse=True)                                       # higher CRF: smaller
    d._pick(d.view.topLevelItem(4))
    assert j.settings["video"]["quality"] == 27
    w.close()


def test_every_string_is_there_once():
    """Every t("key") the code asks for is in i18n, and no key is written twice (a later one would win)."""
    import collections
    import glob
    import re
    from convert import i18n
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = "".join(open(f, encoding="utf-8").read() for f in glob.glob(os.path.join(root, "convert", "*.py")))
    used = set(re.findall(r'''\bt\(\s*["']([a-z0-9_]+)["']\s*[,)]''', src))
    assert sorted(k for k in used if k not in i18n.STRINGS) == []
    with open(os.path.join(root, "convert", "i18n.py"), encoding="utf-8") as f:
        keys = re.findall(r'^\s+"([a-z0-9_]+)":\s+\(', f.read(), re.M)
    assert [k for k, n in collections.Counter(keys).items() if n > 1] == []
    for key, _tpl in J.NAME_TEMPLATES:
        assert key in i18n.STRINGS
