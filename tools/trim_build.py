"""Remove Qt parts the converter never uses from the PyInstaller output (dist/ArtCoreConvert)."""
import fnmatch
import os
import shutil
import sys

DROP_LIBS = ["*qt6quick*", "*qt6qml*", "*qt6pdf*", "*qt6virtualkeyboard*", "*qt6datavisualization*", "*qt6charts*",
             "*qt6graphs*", "*qt6shadertools*", "*qt6svg*", "*qt6webengine*", "*qt6multimedia*", "*qt6waylandclient*",
             "opengl32sw.dll"]
DROP_PLUGINS = ["platforminputcontexts", "qmltooling", "scenegraph", "renderers", "egldeviceintegrations", "wayland-*",
                "networkinformation", "tls", "position", "sensors", "multimedia", "imageformats/*pdf*",
                "imageformats/*svg*", "imageformats/*tiff*", "imageformats/*webp*", "imageformats/*icns*",
                "imageformats/*tga*", "imageformats/*wbmp*", "iconengines/*svg*"]
KEEP_TR = ("qtbase_ru.qm", "qtbase_en.qm")


def main(dist: str) -> int:
    freed = 0
    for root, dirs, files in os.walk(dist):
        for f in files:
            low = f.lower()
            p = os.path.join(root, f)
            if any(fnmatch.fnmatch(low, pat) for pat in DROP_LIBS):
                freed += os.path.getsize(p); os.remove(p)
            elif low.endswith(".qm") and os.path.basename(root) == "translations" and low not in KEEP_TR:
                freed += os.path.getsize(p); os.remove(p)
        if os.path.basename(root) == "plugins":
            for pat in DROP_PLUGINS:
                for name in list(os.listdir(root)) + [os.path.join(d, x) for d in os.listdir(root)
                                                      if os.path.isdir(os.path.join(root, d)) for x in os.listdir(os.path.join(root, d))]:
                    if fnmatch.fnmatch(name.replace("\\", "/").lower(), pat):
                        p = os.path.join(root, name)
                        if os.path.isdir(p):
                            freed += sum(os.path.getsize(os.path.join(a, b)) for a, _, fs in os.walk(p) for b in fs)
                            shutil.rmtree(p, ignore_errors=True)
                        elif os.path.isfile(p):
                            freed += os.path.getsize(p); os.remove(p)
    print(f"trimmed {freed / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist/ArtCoreConvert"))
