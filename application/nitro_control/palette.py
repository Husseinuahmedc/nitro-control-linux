"""The DESIGN.md OKLCH palette, converted at the native Qt boundary."""

from __future__ import annotations

import math

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


OKLCH = {
    "background": (0.170, 0.000, 0),
    "surface": (0.225, 0.000, 0),
    "raised": (0.280, 0.000, 0),
    "ink": (0.950, 0.000, 0),
    "muted": (0.740, 0.005, 0),
    "primary": (0.650, 0.160, 0),
    "primary_fill": (0.540, 0.160, 0),
    "accent": (0.850, 0.110, 150),
    "warning": (0.860, 0.110, 85),
}


def oklch_to_qcolor(lightness: float, chroma: float, hue: float) -> QColor:
    """Convert OKLCH via OKLab to sRGB; clip only at the display boundary."""
    angle = math.radians(hue)
    a, b = chroma * math.cos(angle), chroma * math.sin(angle)
    l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    linear = (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )

    def encode(value: float) -> float:
        value = max(0.0, min(1.0, value))
        return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055

    return QColor.fromRgbF(*(encode(value) for value in linear))


def colors() -> dict[str, QColor]:
    return {name: oklch_to_qcolor(*value) for name, value in OKLCH.items()}


def apply_theme(app: QApplication) -> None:
    """Keep standard Qt widgets, accessibility and the system sans family."""
    c = colors()
    app.setStyle("Fusion")
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: "background",
        QPalette.ColorRole.WindowText: "ink",
        QPalette.ColorRole.Base: "surface",
        QPalette.ColorRole.AlternateBase: "raised",
        QPalette.ColorRole.Text: "ink",
        QPalette.ColorRole.Button: "raised",
        QPalette.ColorRole.ButtonText: "ink",
        QPalette.ColorRole.Highlight: "primary_fill",
        QPalette.ColorRole.HighlightedText: "ink",
        QPalette.ColorRole.ToolTipBase: "raised",
        QPalette.ColorRole.ToolTipText: "ink",
        QPalette.ColorRole.Link: "primary",
        QPalette.ColorRole.PlaceholderText: "muted",
        QPalette.ColorRole.Mid: "muted",
        QPalette.ColorRole.Dark: "background",
        QPalette.ColorRole.Light: "raised",
    }
    for role, name in roles.items():
        palette.setColor(role, c[name])
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, c["muted"])
    app.setPalette(palette)
    font = app.font()
    font.setPixelSize(14)
    app.setFont(font)
    css = {name: value.name() for name, value in c.items()}
    app.setStyleSheet("""
        QWidget { color: %(ink)s; }
        QMainWindow, QScrollArea, QWidget#scrollContent { background: %(background)s; }
        QScrollArea { border: none; }
        QLabel { background: transparent; }
        QLabel[role="muted"] { color: %(muted)s; }
        QLabel[role="warning"] { color: %(warning)s; }
        QLabel[role="heading"] { font-size: 26px; font-weight: 600; }
        QLabel[role="section"] { font-size: 17px; font-weight: 600; }
        QLabel[role="rpm"] { font-size: 36px; font-weight: 600; }
        QLabel[role="temperature"] { font-size: 23px; font-weight: 500; }
        QLabel#stateTitle { font-weight: 600; }
        QLabel#stateTitle[autoConfirmed="true"] { color: %(accent)s; }
        QFrame#hardware { background: %(surface)s; border-radius: 6px; }
        QFrame#divider { background: %(raised)s; border: none; }
        QFrame#statusArea { background: %(surface)s; border-radius: 6px; }
        QFrame#footer { background: %(surface)s; }
        QPushButton, QToolButton {
            background: %(raised)s; border: 1px solid %(raised)s;
            border-radius: 4px; padding: 7px 12px; min-height: 18px;
        }
        QPushButton:hover, QToolButton:hover { border-color: %(muted)s; }
        QPushButton:pressed, QToolButton:pressed { background: %(surface)s; }
        QPushButton:checked { border-color: %(primary)s; color: %(ink)s; }
        QPushButton[primary="true"] {
            background: %(primary_fill)s; border-color: %(primary_fill)s;
            color: %(ink)s; font-weight: 600;
        }
        QPushButton:disabled, QToolButton:disabled {
            background: %(surface)s; color: %(muted)s; border-color: %(raised)s;
        }
        QPushButton:focus, QToolButton:focus, QSpinBox:focus, QPlainTextEdit:focus {
            border: 2px solid %(primary)s;
        }
        QToolButton#diagnosticsToggle { background: transparent; text-align: left; padding: 4px 6px; }
        QSpinBox {
            background: %(raised)s; border: 1px solid %(muted)s;
            border-radius: 4px; padding: 4px; min-width: 68px;
        }
        QSpinBox:disabled { color: %(muted)s; border-color: %(raised)s; }
        QSlider { min-height: 28px; }
        QSlider::groove:horizontal { height: 4px; background: %(raised)s; border-radius: 2px; }
        QSlider::sub-page:horizontal { background: %(primary)s; border-radius: 2px; }
        QSlider::handle:horizontal {
            background: %(ink)s; width: 14px; height: 14px;
            margin: -5px 0; border-radius: 7px; border: 1px solid %(ink)s;
        }
        QSlider::handle:horizontal:focus { border: 2px solid %(primary)s; }
        QSlider::handle:horizontal:disabled { background: %(muted)s; border-color: %(muted)s; }
        QSlider::sub-page:horizontal:disabled { background: %(muted)s; }
        QPlainTextEdit {
            background: %(surface)s; border: 1px solid %(raised)s;
            border-radius: 4px; padding: 8px; selection-background-color: %(primary_fill)s;
            selection-color: %(ink)s;
        }
        QToolTip { background: %(raised)s; color: %(ink)s; border: 1px solid %(muted)s; }
    """ % css)
