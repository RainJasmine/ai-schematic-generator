#!/usr/bin/env python3
"""KiCad schematic action entrypoint for the AI schematic generator."""

from __future__ import annotations

import sys
import os
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent
GENERATED_PROTO = PLUGIN_ROOT / "generated_proto"

if str(GENERATED_PROTO) not in sys.path:
    sys.path.insert(0, str(GENERATED_PROTO))

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from ai_schematic.ui import GeneratorWindow  # noqa: E402


def configure_font(app) -> None:
    """Use an installed CJK font; also works when Qt has no system fallback."""
    from PySide6.QtGui import QFont, QFontDatabase
    choices = ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC")
    available = set(QFontDatabase.families())
    if os.name == "nt" and not any(family in available for family in choices):
        font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc"
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
            available = set(QFontDatabase.families())
    for family in choices:
        if family in available:
            app.setFont(QFont(family, 10))
            break


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("RF EDA AI Schematic Assistant")
    configure_font(app)
    window = GeneratorWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
