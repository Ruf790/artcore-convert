import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """Test files: H.264 1080p + AC-3 5.1 (rus, «Дубляж», default) + AAC (eng) + SRT + PGS-less MKV with a title
    and chapters; HEVC 10 bit HDR10; DTS 5.1 in MKV; an interlaced MPEG-2 TS; a broken file."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is needed")
    d = str(tmp_path_factory.mktemp("media"))
    srt = os.path.join(d, "s.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,500 --> 00:00:01,500\nПривет\n")
    meta = os.path.join(d, "chapters.txt")
    with open(meta, "w", encoding="utf-8") as f:
        f.write(";FFMETADATA1\ntitle=Test movie\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=3000\ntitle=Two\n")
    ff("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=25:d=3", "-f", "lavfi", "-i", "sine=f=440:d=3",
       "-f", "lavfi", "-i", "sine=f=330:d=3", "-i", srt, "-i", meta,
       "-filter_complex", "[1]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[a]",
       "-map", "0:v", "-map", "[a]", "-map", "2:a", "-map", "3:s", "-map_metadata", "4", "-map_chapters", "4",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a:0", "ac3", "-b:a:0", "448k", "-c:a:1", "aac", "-c:s", "srt",
       "-metadata:s:a:0", "language=rus", "-metadata:s:a:0", "title=Дубляж", "-disposition:a:0", "default",
       "-metadata:s:a:1", "language=eng", "-metadata:s:s:0", "language=rus",
       os.path.join(d, "Movie (2017).mkv"))
    ff("-f", "lavfi", "-i", "testsrc=s=1280x720:r=24:d=2", "-f", "lavfi", "-i", "sine=f=600:d=2",
       "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
       "-x265-params", "log-level=error:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc",
       "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-c:a", "flac",
       os.path.join(d, "hdr.mkv"))
    ff("-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2", "-f", "lavfi", "-i", "sine=f=500:d=2",
       "-filter_complex", "[1]pan=5.1|c0=c0|c1=c0|c2=c0|c3=c0|c4=c0|c5=c0[a]", "-map", "0:v", "-map", "[a]",
       "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "dca", "-strict", "-2", os.path.join(d, "dts.mkv"))
    ff("-f", "lavfi", "-i", "testsrc2=s=720x576:r=25:d=2", "-f", "lavfi", "-i", "sine=f=300:d=2",
       "-vf", "interlace", "-c:v", "mpeg2video", "-flags", "+ilme+ildct", "-top", "1", "-c:a", "mp2",
       os.path.join(d, "tv.ts"))
    with open(os.path.join(d, "broken.mkv"), "wb") as f:
        f.write(b"not a video at all" * 100)
    return d


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTCORE_CONVERT_DATA", str(tmp_path / "data"))
    return str(tmp_path / "data")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from convert import style
    app.setStyle("Fusion")
    app.setStyleSheet(style.stylesheet())
    yield app
    from PySide6.QtCore import QCoreApplication, QEvent
    for w in app.topLevelWidgets():
        w.hide()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture(scope="session")
def av():
    from convert import encoders as E
    return E.detect()


def pump(app, secs: float, until=None):
    import time
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        app.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True
