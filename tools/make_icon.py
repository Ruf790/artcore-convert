"""Render the app icon (convert/style.py) into assets/icon.ico (all sizes; needs Pillow)."""
import os
import sys
from io import BytesIO

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtWidgets import QApplication

app = QApplication([])
from convert.style import app_icon_pixmap

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
out = os.path.join(ROOT, "assets", "icon.ico")
os.makedirs(os.path.dirname(out), exist_ok=True)
try:
    from PIL import Image
    imgs = []
    for n in SIZES:
        ba = QByteArray(); buf = QBuffer(ba); buf.open(QIODevice.WriteOnly)
        app_icon_pixmap(n).save(buf, "PNG")
        imgs.append(Image.open(BytesIO(bytes(ba))).convert("RGBA"))
    imgs[-1].save(out, format="ICO", sizes=[(n, n) for n in SIZES], append_images=imgs[:-1])
except ImportError:
    app_icon_pixmap(256).save(out, "ICO")
print("saved", out)
