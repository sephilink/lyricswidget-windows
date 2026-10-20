"""Central logging setup: console + rotating file, shared by every lyricswidget module.

The log file lives next to the script (lyricswidget.log) so a trace is easy to find
and attach. Call setup_logging() once at startup; modules then use get_logger(__name__).
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_FILENAME = "lyricswidget.log"
_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)-14s %(funcName)s: %(message)s"
_configured = False


def log_path() -> str:
    """Absolute path of the log file, beside this module."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG_FILENAME)


def setup_logging(level: int = logging.DEBUG) -> str:
    """Configure root logging to a console stream and a rotating file; return the log path.

    Idempotent: repeated calls do not stack handlers. The file rotates at ~1 MB,
    keeping 3 backups, so a long session can't grow it without bound.
    """
    global _configured
    path = log_path()
    if _configured:
        return path

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(_LOG_FORMAT)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Quiet chatty third-party loggers so the app's own trace stays readable.
    for noisy in ("asyncio", "urllib3", "winsdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    logging.getLogger(__name__).info("Logging started -> %s", path)
    return path


def get_logger(name: str) -> logging.Logger:
    """Return a module logger (configuration is handled centrally by setup_logging)."""
    return logging.getLogger(name)
