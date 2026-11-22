"""2.5: subtitle timing — the delay of a source's own subtitle track, the stretch of another frame rate, two
points, and the shift + stretch found by the sound (speech vs. the lines)."""
import os
import subprocess

import numpy as np
import pytest

from conftest import ff as mk, pump
from convert import encoders as E
from convert import job as J
from convert import probe as P

AV = E.Available({"x264": True})
K = 25 / (24000 / 1001)                     # subtitles of a 25 fps version on 23.976 fps video


def _srt(path, cues):
    def tc(s):
        return f"{int(s // 3600):02d}:{int(s // 60 % 60):02d}:{s % 60:06.3f}".replace(".", ",")
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b) in enumerate(cues, 1):
            f.write(f"{i}\n{tc(a)} --> {tc(b)}\nline {i}\n\n")


def _cues(path, kind="s"):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", kind, "-show_entries", "packet=pts_time",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return [round(float(x), 2) for x in r.stdout.split()]


@pytest.fixture(scope="module")
def film(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("sync25"))
    srt = os.path.join(d, "s.srt")
    _srt(srt, [(10, 12), (100, 102)])
    out = os.path.join(d, "film.mkv")
    mk("-f", "lavfi", "-i", "color=s=64x64:d=130:r=24", "-i", srt, "-map", "0", "-map", "1", "-c:v", "libx264",
       "-preset", "ultrafast", "-c:s", "srt", out)
    return {"dir": d, "film": out, "srt": srt}


def _job(path, **st):
    j = J.Job(src=path, settings=J.merged({"container": "mkv", "video": {"mode": "copy"}} | st))
    j.info = P.probe(path)
    return j


def _run(j, out):
    r = subprocess.run(J.build_args(j, "ffmpeg", out, AV, False), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-600:]
    return _cues(out)


def test_maths():
    assert J.fps_scale(25, 23.976) == pytest.approx(1.0427083, abs=1e-6)
    assert J.fps_scale(23.976, 25) == pytest.approx(1 / 1.0427083, abs=1e-6)
    k, d = J.two_points(10, 13.427, 100, 107.271)
    assert k == pytest.approx(K, abs=1e-4) and d == pytest.approx(3.0, abs=0.01)
    with pytest.raises(ValueError):
        J.two_points(10, 10, 10.5, 11)
    with pytest.raises(ValueError):
        J.two_points(10, 10, 100, 200)
    assert J.timed_input("x", 1.0, 0.0, []) == ["-i", "x"]
    assert J.timed_input("x", 1.0, 2.5, []) == ["-itsoffset", "2.500", "-i", "x"]
    assert J.timed_input("x", 2.0, 0.0, [(10.0, 20.0)]) == ["-itsscale", "2.000000000", "-ss", "5.000", "-t", "5.000",
                                                           "-i", "x"]


def test_source_track_delay_and_stretch(film, tmp_path):
    j = _job(film["film"])
    s = j.info.of("subtitle")[0]
    j.tracks[str(s.index)] = {"delay": 2500}
    assert _run(j, str(tmp_path / "a.mkv")) == [12.5, 102.5]
    j.tracks[str(s.index)] = {"scale": K, "delay": -1000}
    assert _run(j, str(tmp_path / "b.mkv")) == [round(K * 10 - 1, 2), round(K * 100 - 1, 2)]
    assert J.sub_timed(j, s) and not J.plan(j)["warnings"]
    j.tracks.pop(str(s.index))
    assert not J.sub_timed(j, s) and "-itsscale" not in J.build_args(j, "ffmpeg", "/x.mkv", AV, False)


def test_with_one_trim_piece_when_encoding(film, tmp_path):
    j = _job(film["film"], video={"encoder": "x264", "speed": 1})
    s = j.info.of("subtitle")[0]
    j.tracks[str(s.index)] = {"scale": K, "delay": -1000}
    j.trim = [[5.0, 120.0]]
    assert _run(j, str(tmp_path / "c.mkv")) == [round(K * 10 - 6, 2), round(K * 100 - 6, 2)]


def test_outside_subtitles_stretch(film, tmp_path):
    j = _job(film["film"], subs="none")
    j.ext = [J.clean_ext({"path": film["srt"], "kind": "subtitle", "delay": 500, "scale": K})]
    j.infos[film["srt"]] = P.probe(film["srt"])
    assert _run(j, str(tmp_path / "d.mkv")) == [round(K * 10 + 0.5, 2), round(K * 100 + 0.5, 2)]
    assert J.Job.from_dict(j.to_dict()).ext[0]["scale"] == pytest.approx(K)


def test_warnings(film):
    j = _job(film["film"])
    s = j.info.of("subtitle")[0]
    j.tracks[str(s.index)] = {"delay": 1000}
    j.trim = [[1.0, 5.0], [8.0, 20.0]]                        # two pieces: not re-timed, said so
    assert "w_sync_ignored" in J.plan(j)["warnings"]
    assert not any(a == "-itsoffset" for a in J.build_args(j, "ffmpeg", "/x.mkv", AV, False))


def test_burnt_in_gets_the_delay(film):
    from convert import hardsub as HS
    j = _job(film["film"], video={"encoder": "x264", "speed": 1})
    s = j.info.of("subtitle")[0]
    j.tracks[str(s.index)] = {"delay": 2000, "scale": K}
    b = HS.Burn(s, None, "all")
    tm = HS.time_map(j, b)
    assert tm[0][1] == -2_000_000                              # the picture at 0 shows the subtitles' −2 s
    j.tracks[str(s.index)]["burn"] = "all"
    j.settings.setdefault("hardsub", {})


# ── by the sound ──
def _speechy(seed=3, dur=240, k=K, d=3.2, sr=16000):
    rng = np.random.default_rng(seed)
    t = np.arange(sr * dur) / sr
    x = 0.08 * (np.sin(2 * np.pi * 110 * t) + 0.6 * np.sin(2 * np.pi * 164.8 * t)) + rng.normal(0, 0.01, len(t))
    lines, pos = [], 4.0
    while pos < dur - 8:
        ln = rng.uniform(1.0, 4.0)
        a, b = int(pos * sr), int((pos + ln) * sr)
        n = rng.normal(0, 1, b - a)
        f = np.fft.rfft(n)
        fr = np.fft.rfftfreq(len(n), 1 / sr)
        f[(fr < 300) | (fr > 3000)] = 0
        n = np.fft.irfft(f, len(n))
        x[a:b] += 0.25 * n / np.abs(n).max() * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * np.arange(b - a) / sr))
        lines.append((pos, pos + ln))
        pos += ln + rng.uniform(0.5, 7)
    x = (x / np.abs(x).max() * 0.9 * 32767).astype("<i2")
    return x, [((a - d) / k, (b - d) / k) for a, b in lines if (a - d) / k > 0]


@pytest.fixture(scope="module")
def talky(tmp_path_factory):
    import wave
    d = str(tmp_path_factory.mktemp("talky"))
    x, cues = _speechy()
    wav = os.path.join(d, "a.wav")
    with wave.open(wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(x.tobytes())
    srt = os.path.join(d, "late.srt")
    _srt(srt, cues)
    _x2, other = _speechy(seed=11)                                 # lines of another film
    wrong = os.path.join(d, "wrong.srt")
    _srt(wrong, other)
    film = os.path.join(d, "Talk.mkv")
    mk("-f", "lavfi", "-i", "color=s=64x64:d=240:r=24", "-i", wav, "-i", srt, "-map", "0", "-map", "1", "-map", "2",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt", film)
    return {"film": film, "srt": srt, "wrong": wrong}


def test_found_by_the_sound(talky):
    from convert import subsync as SS
    r = SS.sync("ffmpeg", "ffprobe", talky["film"], 0, talky["film"], 2, 240)
    assert r["scale"] == pytest.approx(K, abs=1e-6) and abs(r["delay"] - 3.2) < 0.2 and r["sure"] >= SS.SURE
    r = SS.sync("ffmpeg", "ffprobe", talky["film"], 0, talky["wrong"], 0, 240)
    assert r["sure"] < SS.SURE                                           # the wrong film's lines: not a clear match


def test_streams_tab_two_points_and_auto(qapp, talky, data_dir):
    from PySide6.QtWidgets import QComboBox, QSpinBox
    from test_14_ui import _select, _win
    from convert.dialogs import SyncPointsDialog
    w = _win(qapp)
    assert w.add_paths([talky["film"]]) == 1
    assert pump(qapp, 15, lambda: w.jobs[-1].status != "probe")
    j = w.jobs[-1]
    _select(qapp, w, [j])
    w.show_side("streams")
    pump(qapp, 0.3)
    fps = [b for b in w.tracks_box.findChildren(QComboBox) if b.objectName() == "syncFps" and b.isVisible()]
    dl = [b for b in w.tracks_box.findChildren(QSpinBox) if b.objectName() == "syncDelay" and b.isVisible()]
    assert len(fps) == 1 and len(dl) == 1
    fps[0].setCurrentIndex(fps[0].findData(J.fps_scale(25, 23.976)))
    assert j.tracks["2"]["scale"] == pytest.approx(K)
    pump(qapp, 0.3)
    dl = [b for b in w.tracks_box.findChildren(QSpinBox) if b.objectName() == "syncDelay" and b.isVisible()]
    dl[0].setValue(1500)
    assert j.tracks["2"]["delay"] == 1500
    d = SyncPointsDialog(lambda: 65.25)
    (a1, b1), (a2, b2) = d.edits
    a1.setText("0:10"), a2.setText("1:40")
    b1.setText("0:13.427")
    d._from_view(b2)
    assert b2.text() == "1:05.250" and d.ok.isEnabled() is False      # 65.25 for 100 s: a stretch over 20 %
    b2.setText("1:47.271")
    assert d.ok.isEnabled() and d.result_ks[0] == pytest.approx(K, abs=1e-4)
    w.set_sub_sync(("src", 2), scale=1.0, delay=0)
    assert "2" not in j.tracks
    w.sync_auto(("src", 2), confirm=False)
    assert pump(qapp, 60, lambda: "2" in j.tracks)
    assert j.tracks["2"]["scale"] == pytest.approx(K, abs=1e-6) and abs(j.tracks["2"]["delay"] - 3200) < 200
    w.close()
