#!/usr/bin/env python3
"""KiCad schematic action entrypoint for the AI schematic generator."""

from __future__ import annotations

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent
GENERATED_PROTO = PLUGIN_ROOT / "generated_proto"

if str(GENERATED_PROTO) not in sys.path:
    sys.path.insert(0, str(GENERATED_PROTO))

if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from ai_schematic.ui import GeneratorWindow  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AI Schematic Generator")
    window = GeneratorWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
