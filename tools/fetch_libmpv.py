"""Download libmpv-2.dll (Windows x86-64) from the latest shinchiro/mpv-winbuild-cmake release
into ./libmpv/. Used by CI and build.bat; needs 7-Zip (7z.exe) or `pip install py7zr`.

Usage: python tools/fetch_libmpv.py [--v3]     (--v3 = build for CPUs with AVX2, faster, not for old PCs)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "libmpv")
API = "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"


def _get(url: str, accept: str = "application/vnd.github+json"):
    headers = {"User-Agent": "artcore-convert-build", "Accept": accept}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {tok}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)


def main() -> int:
    v3 = "--v3" in sys.argv
    with _get(API) as r:
        rel = json.load(r)
    pat = re.compile(r"^mpv-dev-x86_64-v3-\d{8}-git-[0-9a-f]+\.7z$" if v3 else r"^mpv-dev-x86_64-\d{8}-git-[0-9a-f]+\.7z$")
    assets = [a for a in rel.get("assets", []) if pat.match(a["name"])]
    if not assets:
        print("no mpv-dev asset in", rel.get("tag_name"), [a["name"] for a in rel.get("assets", [])])
        return 1
    a = assets[0]
    print("libmpv:", rel.get("tag_name"), a["name"], f"{a['size'] / 1e6:.1f} MB")
    tmp = tempfile.mkdtemp()
    arc = os.path.join(tmp, a["name"])
    with _get(a["browser_download_url"], "application/octet-stream") as r, open(arc, "wb") as f:
        shutil.copyfileobj(r, f)
    os.makedirs(OUT, exist_ok=True)
    seven = shutil.which("7z") or shutil.which("7z.exe") or r"C:\Program Files\7-Zip\7z.exe"
    if os.path.isfile(seven) or shutil.which("7z"):
        subprocess.check_call([seven, "e", "-y", f"-o{OUT}", arc, "libmpv-2.dll"], stdout=subprocess.DEVNULL)
    else:
        import py7zr
        with py7zr.SevenZipFile(arc) as z:
            names = [n for n in z.getnames() if n.endswith("libmpv-2.dll")]
            z.extract(path=tmp, targets=names)
        shutil.copy(os.path.join(tmp, names[0]), os.path.join(OUT, "libmpv-2.dll"))
    dll = os.path.join(OUT, "libmpv-2.dll")
    if not os.path.isfile(dll):
        print("libmpv-2.dll not found in", a["name"])
        return 1
    with open(os.path.join(OUT, "VERSION.txt"), "w", encoding="utf-8") as f:
        f.write(f"{rel.get('tag_name')} {a['name']}\n")
    print("saved", dll, f"{os.path.getsize(dll) / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
