"""Shared constants used across the lyricswidget modules."""
from __future__ import annotations

# LRCLIB public API. It asks clients to send an identifying User-Agent.
LRCLIB_BASE = "https://lrclib.net/api"
USER_AGENT = "lyricswidget/1.0 (https://github.com/; personal side project)"
HTTP_TIMEOUT = 12  # seconds for a lyrics lookup

# Local playback-position poll. This is OS-only (SMTC), never a network call,
# so it can be frequent without touching LRCLIB.
SYNC_POLL_MS = 250

# Hard cap on the SMTC probe subprocess. The Windows media-session RequestAsync can hang
# system-wide; this bounds the wait so a wedged session never freezes the widget.
SMTC_READ_TIMEOUT = 4.0  # seconds

# Max retryable lyrics-fetch attempts per track before giving up (network error / 5xx / 429).
# Keeps a sustained outage from generating unbounded calls to LRCLIB.
MAX_FETCH_RETRIES = 4

# Backoff between those retries.
FETCH_RETRY_BACKOFF = 5.0  # seconds

# Local SQLite cache (persists lyrics across restarts, next to the app).
DB_FILENAME = "lyricswidget.db"
# How long a confirmed "no lyrics" result is trusted before LRCLIB is re-checked;
# the catalogue grows, so a past miss shouldn't be permanent.
MISS_TTL_SECONDS = 7 * 24 * 3600  # one week

DEFAULT_SIZE = (440, 320)
MIN_SIZE = (320, 200)
