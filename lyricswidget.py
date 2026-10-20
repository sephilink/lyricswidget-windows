"""Entry point: launch the floating always-on-top lyrics widget.

The implementation is split across modules:
  - config.py         shared constants
  - logging_setup.py  console + rotating-file logging
  - lrc.py            synced-lyrics (LRC) parsing + active-line lookup
  - lyrics_api.py     background LRCLIB lookup thread + per-track cache
  - now_playing.py    Windows media-session (SMTC) track + playback reader
  - widget.py         the floating card window
"""
from __future__ import annotations

import sys

from PySide6 import QtWidgets

from logging_setup import get_logger, setup_logging

log = get_logger("main")


def _load_fonts() -> None:
    """Register the bundled Montserrat font so the UI uses it regardless of system fonts."""
    import os

    from PySide6 import QtGui

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Montserrat-variable.ttf")
    if not os.path.exists(path):
        log.warning("Montserrat font not found at %s; falling back to system font", path)
        return
    fid = QtGui.QFontDatabase.addApplicationFont(path)
    if fid < 0:
        log.warning("Failed to load bundled Montserrat font")
        return
    families = QtGui.QFontDatabase.applicationFontFamilies(fid)
    log.info("Loaded Montserrat font: %s", families)


def _install_excepthook() -> None:
    """Route any uncaught exception to the log before the process dies."""
    def handler(exc_type, exc_value, exc_tb):
        log.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = handler


def main() -> int:
    """Launch the Qt app and show the widget in the top-right corner of the primary screen."""
    path = setup_logging()
    _install_excepthook()
    log.info("Starting lyricswidget; log file at %s", path)

    from widget import Widget  # imported after logging is configured

    app = QtWidgets.QApplication(sys.argv)
    log.debug("QApplication created")
    _load_fonts()
    w = Widget()
    screen = app.primaryScreen().availableGeometry()
    w.move(screen.right() - w.width() - 24, screen.top() + 24)
    log.debug("Widget positioned at top-right of %sx%s screen", screen.width(), screen.height())
    w.show()
    log.info("Widget shown; entering event loop")
    rc = app.exec()
    log.info("Event loop exited with code %s", rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
