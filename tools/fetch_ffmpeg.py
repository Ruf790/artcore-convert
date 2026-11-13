"""Download FFmpeg for Windows x86-64 (BtbN/FFmpeg-Builds, the GPL «shared» build of the newest release
branch: x264, x265, SVT-AV1, NVENC, AMF, QSV, Opus, LAME …) into ./ffmpeg/ — bin/ffmpeg.exe, bin/ffprobe.exe,
their DLLs and LICENSE.txt. Used by CI and build.bat.

Usage: python tools/fetch_ffmpeg.py [--master]     (--master = the nightly master build instead)"""
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "ffmpeg")
API = "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"


def _get(url: str, accept: str = "application/vnd.github+json"):
    headers = {"User-Agent": "artcore-convert-build", "Accept": accept}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {tok}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300)


def pick(assets: list[dict], master: bool) -> dict | None:
    if master:
        return next((a for a in assets if a["name"] == "ffmpeg-master-latest-win64-gpl-shared.zip"), None)
    best, best_v = None, (0,)
    for a in assets:
        m = re.match(r"^ffmpeg-n(\d+)\.(\d+)(?:\.(\d+))?-latest-win64-gpl-shared-[\d.]+\.zip$", a["name"])
        if m:
            v = tuple(int(x or 0) for x in m.groups())
            if v > best_v:
                best, best_v = a, v
    return best or next((a for a in assets if a["name"] == "ffmpeg-master-latest-win64-gpl-shared.zip"), None)


def main() -> int:
    with _get(API) as r:
        rel = json.load(r)
    a = pick(rel.get("assets", []), "--master" in sys.argv)
    if a is None:
        print("no win64-gpl-shared asset in", rel.get("tag_name"), [x["name"] for x in rel.get("assets", [])])
        return 1
    print("FFmpeg:", rel.get("tag_name"), a["name"], f"{a['size'] / 1e6:.1f} MB")
    tmp = tempfile.mkdtemp()
    arc = os.path.join(tmp, a["name"])
    with _get(a["browser_download_url"], "application/octet-stream") as r, open(arc, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(os.path.join(OUT, "bin"))
    with zipfile.ZipFile(arc) as z:
        for name in z.namelist():
            parts = name.split("/")
            if len(parts) < 2 or name.endswith("/"):
                continue
            rest = "/".join(parts[1:])
            if rest.startswith("bin/") and (rest.endswith(".dll") or rest in ("bin/ffmpeg.exe", "bin/ffprobe.exe")):
                dst = os.path.join(OUT, *rest.split("/"))
            elif rest == "LICENSE.txt":
                dst = os.path.join(OUT, "LICENSE.txt")
            else:
                continue
            with z.open(name) as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
    with open(os.path.join(OUT, "VERSION.txt"), "w", encoding="utf-8") as f:
        f.write(f"{a['name']} ({rel.get('tag_name')})\nhttps://github.com/BtbN/FFmpeg-Builds\n")
    shutil.rmtree(tmp, ignore_errors=True)
    for exe in ("ffmpeg.exe", "ffprobe.exe"):
        if not os.path.isfile(os.path.join(OUT, "bin", exe)):
            print("missing", exe)
            return 1
    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(OUT) for f in fs)
    print(f"ffmpeg/: {size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
