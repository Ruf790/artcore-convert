"""Colours, the app icon and the dark style sheet."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap

PALETTES = {
    "dark": dict(ACCENT="#0d6efd", ACCENT2="#6f42c1", BG="#0f1115", PANEL="#171a21", PANEL2="#1f232c",
                 BORDER="#2b303b", TEXT="#e9ecef", MUTED="#9aa3b2", STRONG="#ffffff", ICON="#ffffff",
                 HOVER="rgba(255,255,255,38)", PRESSED="rgba(255,255,255,60)", SCROLL="rgba(255,255,255,50)",
                 ALT="#1a1d25", SEL="rgba(13,110,253,95)", SEL_TEXT="#ffffff", HANDLE="#ffffff",
                 PROG_TEXT="#ffffff", OK="#51cf66", ERR="#ff6b6b", WARN="#ffc53d", RUN="#4dabf7",
                 RUN_TEXT="#8ab4ff"),
    "light": dict(ACCENT="#0d6efd", ACCENT2="#6f42c1", BG="#f3f5f9", PANEL="#ffffff", PANEL2="#eef1f6",
                  BORDER="#d5dae3", TEXT="#1c2027", MUTED="#667085", STRONG="#0b0d12", ICON="#2a2f3a",
                  HOVER="rgba(0,0,0,20)", PRESSED="rgba(0,0,0,36)", SCROLL="rgba(0,0,0,55)",
                  ALT="#f7f8fb", SEL="rgba(13,110,253,45)", SEL_TEXT="#0b0d12", HANDLE="#0d6efd",
                  PROG_TEXT="#1c2027", OK="#2f9e44", ERR="#e03131", WARN="#d48806", RUN="#1c7ed6",
                  RUN_TEXT="#0b5ed7"),
}
THEME = "dark"
IS_DARK = True
ACCENT = ACCENT2 = BG = PANEL = PANEL2 = BORDER = TEXT = MUTED = STRONG = ICON = ""
OK = ERR = WARN = RUN = RUN_TEXT = ""


def set_theme(name: str) -> str:
    """dark | light | auto (as Windows apps are set: the Qt colour scheme). Call before any window is built;
    the module's colour names follow the theme. Returns the theme in use."""
    global THEME, IS_DARK
    if name == "auto":
        name = "dark"
        try:
            from PySide6.QtCore import Qt as _Qt
            from PySide6.QtGui import QGuiApplication
            app = QGuiApplication.instance()
            if app is not None and app.styleHints().colorScheme() == _Qt.ColorScheme.Light:
                name = "light"
        except Exception:
            pass
    THEME = name if name in PALETTES else "dark"
    IS_DARK = THEME == "dark"
    globals().update(PALETTES[THEME])
    _icons.clear()
    return THEME


def _check_png() -> str:
    """White check mark for checked boxes (QSS needs a file)."""
    import os
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "artcore_convert_check.png")
    if not os.path.isfile(path):
        try:
            icon("fa6s.check", "#ffffff").pixmap(12, 12).save(path)
        except Exception:
            return ""
    return path.replace("\\", "/")


def _icon_png(name: str, color: str, file: str, px: int = 12) -> str:
    """An icon as a PNG in %TEMP% (QSS images need a file)."""
    import os
    import tempfile
    path = os.path.join(tempfile.gettempdir(), file)
    if not os.path.isfile(path):
        try:
            icon(name, color).pixmap(px, px).save(path)
        except Exception:
            return ""
    return path.replace("\\", "/")


def stylesheet() -> str:
    return (QSS.format(**PALETTES[THEME])
            .replace("%CHECK%", _check_png())
            .replace("%CLOSE%", _icon_png("fa6s.xmark", MUTED, f"artcore_convert_close_{THEME}.png"))
            .replace("%CLOSE_HOT%", _icon_png("fa6s.xmark", STRONG, f"artcore_convert_close_hot_{THEME}.png")))


_icons: dict = {}


def icon(name: str, color: str = ""):
    """qtawesome icon, cached (a playlist row asks for two of them; 1000+ rows)."""
    color = color or TEXT
    k = (name, color)
    ic = _icons.get(k)
    if ic is None:
        import qtawesome as qta
        ic = _icons[k] = qta.icon(name, color=color)
    return ic


def app_icon_pixmap(size: int = 256) -> QPixmap:
    """The ArtCore tile (blue → purple → pink, a gloss on top) with a white film frame and, around it, two
    arrows chasing each other: «convert». From 32 px up the frame has its sprocket holes."""
    from PySide6.QtGui import QPen
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 256.0
    r = QRectF(8 * s, 8 * s, 240 * s, 240 * s)

    def tile(rect: QRectF) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(rect, rect.width() * 0.235, rect.height() * 0.235)
        return path

    p.setPen(Qt.NoPen)
    if size >= 32:
        for i, a in enumerate((26, 18, 10)):
            p.setBrush(QColor(0, 0, 0, a))
            p.drawPath(tile(r.translated(0, (3 + i * 2) * s)))
    body = tile(r)
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    g.setColorAt(0, QColor("#1f6bff"))
    g.setColorAt(0.55, QColor("#7a3cf0"))
    g.setColorAt(1, QColor("#e83e8c"))
    p.setBrush(g)
    p.drawPath(body)
    p.save()
    p.setClipPath(body)
    gl = QLinearGradient(0, r.top(), 0, r.top() + r.height() * 0.55)
    gl.setColorAt(0, QColor(255, 255, 255, 70))
    gl.setColorAt(1, QColor(255, 255, 255, 0))
    p.setBrush(gl)
    p.drawEllipse(QRectF(r.left() - 40 * s, r.top() - 150 * s, r.width() + 80 * s, 260 * s))
    p.restore()
    if size >= 32:
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 50), max(1.0, 2 * s)))
        p.drawPath(tile(r.adjusted(1.5 * s, 1.5 * s, -1.5 * s, -1.5 * s)))
        p.setPen(Qt.NoPen)

    def art(color: QColor, dy: float = 0.0, holes: bool = True):
        p.save()
        p.translate(128 * s, (128 + dy) * s)
        p.scale(s, s)
        # two arcs with arrow heads, around the frame
        pen = QPen(color, 15, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        box = QRectF(-84, -84, 168, 168)
        p.drawArc(box, 30 * 16, 110 * 16)            # top: from right-up to left
        p.drawArc(box, 210 * 16, 110 * 16)           # bottom: from left-down to right
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        import math
        for ang in (140, 320):                       # the heads, where each arc ends (counter-clockwise)
            a = math.radians(ang)
            x, y = 84 * math.cos(a), -84 * math.sin(a)
            tx, ty = -math.sin(a), -math.cos(a)      # the arc's direction there
            nx, ny = math.cos(a), -math.sin(a)
            head = QPainterPath()
            head.moveTo(x + tx * 26, y + ty * 26)
            head.lineTo(x + nx * 20 - tx * 4, y + ny * 20 - ty * 4)
            head.lineTo(x - nx * 20 - tx * 4, y - ny * 20 - ty * 4)
            head.closeSubpath()
            p.drawPath(head)
        # the film frame with a play triangle
        frame = QPainterPath()
        frame.addRoundedRect(QRectF(-50, -38, 100, 76), 12, 12)
        if holes:
            for i in range(4):
                for yy in (-31, 23):
                    frame.addRoundedRect(QRectF(-38 + i * 21, yy, 11, 8), 2, 2)
        play = QPainterPath()
        play.moveTo(-12, -14)
        play.lineTo(18, 0)
        play.lineTo(-12, 14)
        play.closeSubpath()
        frame.addPath(play)
        frame.setFillRule(Qt.OddEvenFill)
        p.drawPath(frame)
        p.restore()

    if size >= 24:
        art(QColor(0, 0, 0, 55), 5, size >= 32)
    art(QColor("#ffffff"), 0, size >= 32)
    p.end()
    return pm


def app_icon() -> QIcon:
    ic = QIcon()
    for n in (16, 20, 24, 32, 48, 64, 128, 256):
        ic.addPixmap(app_icon_pixmap(n))
    return ic


QSS = """
QWidget {{ color: {TEXT}; font-size: 10pt; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {PANEL2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 4px 7px; border-radius: 6px; }}
QMenu {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: #ffffff; }}
QMenu::item:disabled {{ color: {MUTED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QMenu::icon {{ padding-left: 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
QPushButton {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 9px; padding: 8px 16px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton#primary {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2});
                       border: none; color: #ffffff; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT}; }}
QToolButton#ctl {{ background: transparent; border: none; border-radius: 8px; padding: 6px; }}
QToolButton#ctl:hover {{ background: {HOVER}; }}
QToolButton#ctl:pressed {{ background: {PRESSED}; }}
QToolButton#ctl::menu-indicator {{ image: none; width: 0; }}
QToolButton#ctlText {{ background: transparent; border: none; border-radius: 8px; padding: 6px 8px;
                       font-weight: 600; }}
QToolButton#ctlText:hover {{ background: {HOVER}; }}
QToolButton#ctlText::menu-indicator {{ image: none; width: 0; }}
QLabel#time {{ font-size: 10pt; color: {STRONG}; padding: 0 6px; }}
QLabel#title {{ font-size: 13pt; font-weight: 600; color: {STRONG}; }}
QLabel#badge {{ background: rgba(255,255,255,30); border: 1px solid rgba(255,255,255,60); border-radius: 5px;
                padding: 1px 6px; font-size: 8.5pt; font-weight: 700; color: #ffffff; }}
QLabel#badgeHdr {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #f59f00, stop:1 #e8590c);
                   border-radius: 5px; padding: 1px 6px; font-size: 8.5pt; font-weight: 700; color: #ffffff; }}
QSlider#volume::groove:horizontal {{ height: 4px; background: rgba(255,255,255,70); border-radius: 2px; }}
QSlider#volume::sub-page:horizontal {{ background: #ffffff; border-radius: 2px; }}
QSlider#volume::handle:horizontal {{ background: #ffffff; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}
QListWidget#playlist {{ background: transparent; border: none; outline: none; }}
QListWidget#playlist::item {{ padding: 8px 10px; border-radius: 8px; color: {TEXT}; }}
QListWidget#playlist::item:hover {{ background: {HOVER}; }}
QListWidget#playlist::item:selected {{ background: rgba(13,110,253,90); }}
QListWidget#recent {{ background: transparent; border: none; outline: none; }}
QListWidget#recent::item {{ padding: 9px 12px; border-radius: 9px; }}
QListWidget#recent::item:hover {{ background: {PANEL2}; }}
QListWidget#recent::item:selected {{ background: {PANEL2}; color: {TEXT}; }}
QCheckBox {{ spacing: 9px; }}
QCheckBox::indicator {{ width: 17px; height: 17px; border-radius: 5px; border: 1px solid {BORDER}; background: {PANEL2}; }}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url(%CHECK%); }}
QLineEdit, QComboBox {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px 9px; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {SCROLL}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QTreeWidget#playlist {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; outline: none;
                        alternate-background-color: {ALT}; padding: 4px; }}
QTreeWidget#playlist::item {{ padding: 5px 4px; border: none; }}
QTreeWidget#playlist::item:hover {{ background: {HOVER}; }}
QTreeWidget#playlist::item:selected {{ background: {SEL}; color: {SEL_TEXT}; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER};
                        padding: 6px 6px; font-size: 9pt; font-weight: 600; }}
QSplitter::handle {{ background: transparent; }}
QWidget#transport {{ background: {PANEL}; border-top: 1px solid {BORDER}; }}
QWidget#nowPanel {{ background: transparent; }}
QWidget#mini {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 14px; }}
QLabel#artist {{ font-size: 11.5pt; color: {TEXT}; }}
QLabel#album {{ font-size: 10pt; color: {MUTED}; }}
QLabel#format {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 rgba(13,110,253,70), stop:1 rgba(111,66,193,70));
                 border: 1px solid {BORDER}; border-radius: 7px; padding: 3px 9px; font-size: 9pt;
                 font-weight: 600; color: {STRONG}; }}
QLabel#hint {{ color: {MUTED}; font-size: 9pt; }}
QToolButton#on {{ background: rgba(13,110,253,70); border: none; border-radius: 8px; padding: 6px; }}
QToolButton#on:hover {{ background: rgba(13,110,253,110); }}
QToolButton#play {{ border: none; border-radius: 22px; padding: 10px;
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 {ACCENT2}); }}
QToolButton#play:hover {{ background: {ACCENT}; }}
QSlider::groove:horizontal {{ height: 4px; background: {SCROLL}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {HANDLE}; width: 12px; height: 12px; margin: -4px 0; border-radius: 6px; }}
QDoubleSpinBox, QSpinBox {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px 6px; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 12px; padding: 12px 10px 8px 10px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{ background: transparent; padding: 7px 14px; color: {MUTED}; border: none; }}
QTabBar::tab:selected {{ color: {STRONG}; border-bottom: 2px solid {ACCENT}; }}
QTabBar#plTabs::tab {{ padding: 6px 8px 6px 12px; min-width: 60px; max-width: 220px; }}
QTabBar#plTabs::tab:hover:!selected {{ color: {TEXT}; }}
QTabBar#plTabs::close-button {{ image: url(%CLOSE%); subcontrol-position: right; border-radius: 4px;
                               margin-left: 4px; padding: 2px; }}
QTabBar#plTabs::close-button:hover {{ image: url(%CLOSE_HOT%); background: {HOVER}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {SCROLL}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QToolButton#primaryBtn {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2});
                          border: none; border-radius: 9px; padding: 7px 16px; color: #ffffff; font-weight: 700; }}
QToolButton#primaryBtn:hover {{ background: {ACCENT}; }}
QToolButton#primaryBtn:disabled {{ background: {PANEL2}; color: {MUTED}; }}
QToolButton:disabled {{ color: {MUTED}; }}
QProgressBar {{ background: {PANEL2}; border: 1px solid {BORDER}; border-radius: 7px; text-align: center;
                color: {PROG_TEXT}; min-height: 18px; font-size: 9pt; }}
QProgressBar::chunk {{ border-radius: 6px;
                       background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT2}); }}
QRadioButton::indicator {{ width: 15px; height: 15px; border-radius: 8px; border: 1px solid {BORDER}; background: {PANEL2}; }}
QRadioButton::indicator:checked {{ background: qradialgradient(cx:0.5, cy:0.5, radius:0.5, fx:0.5, fy:0.5,
                                   stop:0 #ffffff, stop:0.35 #ffffff, stop:0.45 {ACCENT}, stop:1 {ACCENT}); border-color: {ACCENT}; }}
QStatusBar {{ background: {BG}; }}
QStatusBar::item {{ border: none; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {SCROLL}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
"""


set_theme("dark")
