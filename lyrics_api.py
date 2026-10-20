"""Background lyrics lookup against the LRCLIB API, off the Qt UI thread.

To keep LRCLIB traffic minimal (the user explicitly wants to avoid hammering it):
  - one network lookup per distinct track, triggered only when the song changes;
  - results (including misses) are cached in-process by (artist, title), so replays,
    seeks and pause/resume never re-fetch;
  - the exact `/api/get` endpoint is tried first, falling back to `/api/search` only
    when the exact match misses.
The time-synced scrolling is driven entirely from the local OS playback position,
so it costs no additional API calls while a song plays.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from PySide6 import QtCore

import db
from config import HTTP_TIMEOUT, LRCLIB_BASE, USER_AGENT
from logging_setup import get_logger

log = get_logger("lyrics_api")


@dataclass(frozen=True)
class LyricsResult:
    """Lyrics for one track: synced LRC text when available, plus a plain fallback."""

    artist: str
    title: str
    synced: str  # LRC-format string, or "" when the track has no synced lyrics
    plain: str  # newline-joined plain lyrics, or ""

    @property
    def is_synced(self) -> bool:
        return bool(self.synced.strip())

    @property
    def has_any(self) -> bool:
        return bool(self.synced.strip() or self.plain.strip())


# Fast in-process layer over the persistent SQLite cache (db.py). Avoids a DB round-trip on
# every poll tick; the DB is the durable layer that survives restarts. A None value records a
# confirmed miss so we don't re-query for it this run either.
_CACHE: dict[tuple[str, str], LyricsResult | None] = {}


def _request(path: str, params: dict[str, str]) -> object:
    """GET a JSON endpoint on LRCLIB with the identifying User-Agent; returns parsed JSON."""
    url = f"{LRCLIB_BASE}/{path}?{urllib.parse.urlencode(params)}"
    log.debug("HTTP GET %s", url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read().decode("utf-8")
        log.debug("HTTP %s %s (%d bytes)", resp.status, path, len(body))
        return json.loads(body)


def _result_from_record(artist: str, title: str, rec: dict) -> LyricsResult:
    """Build a LyricsResult from an LRCLIB track record."""
    return LyricsResult(
        artist=artist,
        title=title,
        synced=(rec.get("syncedLyrics") or ""),
        plain=(rec.get("plainLyrics") or ""),
    )


def lookup(artist: str, title: str, duration: int | None = None) -> LyricsResult | None:
    """Return cached-or-fetched lyrics for a track, or None if LRCLIB has nothing.

    Network is touched only on a cache miss. Tries the exact /api/get first, then
    falls back to /api/search. Caches the outcome (including a miss) either way.
    """
    key = (artist.casefold().strip(), title.casefold().strip())

    # Layer 1: in-process cache (fastest; avoids a DB hit per poll tick).
    if key in _CACHE:
        log.debug("lookup: mem cache hit for %r -> %s", key, "hit" if _CACHE[key] else "miss")
        return _CACHE[key]

    # Layer 2: persistent SQLite cache (survives restarts).
    hit, payload = db.get_cached(artist, title)
    if hit:
        result = (
            LyricsResult(artist, title, payload["synced"], payload["plain"])
            if payload else None
        )
        _CACHE[key] = result  # promote into the in-process layer
        log.debug("lookup: db cache hit for %r -> %s", key, "hit" if result else "miss")
        return result

    # Layer 3: query LRCLIB.
    log.info("lookup: cache miss, fetching %r / %r (duration=%s)", artist, title, duration)
    result: LyricsResult | None = None

    # 1) Exact match. duration (when known) improves LRCLIB's match confidence.
    get_params = {"artist_name": artist, "track_name": title}
    if duration:
        get_params["duration"] = str(duration)
    try:
        rec = _request("get", get_params)
        if isinstance(rec, dict) and (rec.get("syncedLyrics") or rec.get("plainLyrics")):
            result = _result_from_record(artist, title, rec)
            log.debug("lookup: /get matched (synced=%s)", result.is_synced)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            log.warning("lookup: /get HTTP %s", exc.code)
            raise  # a non-404 error is a real failure, not a confirmed miss
        log.debug("lookup: /get returned 404 (no exact match)")
    # URLError/timeout propagate to the caller so it can report a network problem.

    # 2) Fuzzy search fallback only when the exact get missed.
    if result is None:
        try:
            hits = _request("search", {"artist_name": artist, "track_name": title})
            if isinstance(hits, list):
                log.debug("lookup: /search returned %d hits", len(hits))
                for rec in hits:
                    if isinstance(rec, dict) and (rec.get("syncedLyrics") or rec.get("plainLyrics")):
                        result = _result_from_record(artist, title, rec)
                        log.debug("lookup: /search matched (synced=%s)", result.is_synced)
                        break
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                log.warning("lookup: /search HTTP %s", exc.code)
                raise

    # Persist to both layers. This point is only reached when LRCLIB actually answered
    # (a network error raised earlier and never caches), so a None here is a real miss.
    _CACHE[key] = result
    if result is not None:
        db.put_cached(artist, title, result.synced, result.plain, found=True)
    else:
        db.put_cached(artist, title, "", "", found=False)
    log.info("lookup: result for %r -> %s", key, "found" if result else "no lyrics")
    return result


class LyricsFetcher(QtCore.QThread):
    """Fetches lyrics for a track on a worker thread and emits the outcome back to the UI."""

    found = QtCore.Signal(object)  # LyricsResult
    failed = QtCore.Signal(str, bool)  # (human-readable message, retryable)

    def __init__(self, artist: str, title: str, duration: int | None = None) -> None:
        super().__init__()
        self._artist = artist
        self._title = title
        self._duration = duration

    def run(self) -> None:
        """Perform the lookup and emit found/failed; never raises into the Qt event loop."""
        log.debug("LyricsFetcher.run: start %r / %r", self._artist, self._title)
        try:
            result = lookup(self._artist, self._title, self._duration)
        except (urllib.error.URLError, TimeoutError):
            log.exception("LyricsFetcher.run: network error")
            # Transient: let the caller retry on the next poll rather than caching a miss.
            self.failed.emit("Network error \u2014 retrying\u2026", True)
            return
        except (ValueError, json.JSONDecodeError):
            log.exception("LyricsFetcher.run: bad JSON from LRCLIB")
            self.failed.emit("Unexpected response from LRCLIB \u2014 retrying\u2026", True)
            return
        except urllib.error.HTTPError as exc:
            log.warning("LyricsFetcher.run: HTTP %s", exc.code)
            # 5xx/429 are transient; other codes are not worth hammering.
            retryable = exc.code >= 500 or exc.code == 429
            self.failed.emit(f"LRCLIB lookup failed ({exc.code}).", retryable)
            return

        if result is None or not result.has_any:
            log.info("LyricsFetcher.run: no lyrics for %r / %r", self._artist, self._title)
            self.failed.emit("No lyrics found for that song on LRCLIB.", False)
            return
        log.debug("LyricsFetcher.run: emitting result (synced=%s)", result.is_synced)
        self.found.emit(result)
