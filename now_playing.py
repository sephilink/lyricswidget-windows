"""Reads the current browser 'now playing' track from the Windows media session (SMTC).

Chrome/Edge report YouTube playback to the OS media overlay, which exposes an artist and
title per session. For YouTube the reported artist is usually the channel (e.g. 'FooVEVO',
'Foo - Topic') and the title is the raw video name, so this module parses the video title
into a clean (artist, song) pair suitable for a lyrics lookup.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

from PySide6 import QtCore

from config import SMTC_READ_TIMEOUT
from logging_setup import get_logger

log = get_logger("now_playing")

# The SMTC read runs in a short-lived subprocess (now_playing_probe.py), never in this
# process. Rationale (learned the hard way): the winsdk/winrt async bridge's RequestAsync
# can block indefinitely -- under a running Qt event loop it deadlocks, and the Windows
# media-session service itself can wedge RequestAsync system-wide. Doing it in a child
# process means a slow or hung read is bounded by a timeout that kills the child, so the
# widget's GUI thread and event loop are never blocked or crashed by a bad SMTC state.
_PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "now_playing_probe.py")
_SMTC_AVAILABLE = sys.platform == "win32"
if not _SMTC_AVAILABLE:
    log.warning("SMTC now-playing is Windows-only; detection disabled on %s", sys.platform)


# Bracketed/parenthesised noise commonly appended to YouTube music titles.
_NOISE_SEGMENT = re.compile(
    r"[\(\[][^\)\]]*"
    r"(official|lyric|audio|video|visualizer|hd|hq|4k|mv|m/v|explicit|remaster|live)"
    r"[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
# Trailing free-text tags after a pipe or bullet (e.g. '| Official Music Video').
_TRAILING_TAG = re.compile(r"\s*[|\u2022].*$")
# Channel-name decorations to drop when the artist falls back to the channel.
_CHANNEL_NOISE = re.compile(r"\s*(-?\s*topic|vevo|official)\s*$", re.IGNORECASE)


def _clean_title(raw: str) -> str:
    """Strip bracketed descriptors and trailing tags from a raw YouTube video title."""
    text = _NOISE_SEGMENT.sub("", raw)
    text = _TRAILING_TAG.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -\u2013\u2014")


def _clean_channel(raw: str) -> str:
    """Turn a YouTube channel name into a plausible artist (drop 'VEVO', '- Topic', etc.)."""
    return _CHANNEL_NOISE.sub("", raw).strip()


def parse_track(artist_field: str, title_field: str) -> tuple[str, str] | None:
    """Derive (artist, song) from the SMTC artist/title fields, or None if unparseable.

    Prefers the video title's own 'Artist - Song' split (most reliable for YouTube);
    falls back to the reported channel as the artist when the title has no separator.
    """
    title = _clean_title(title_field or "")
    if not title:
        log.debug("parse_track: empty title after cleaning (raw=%r)", title_field)
        return None

    # Most music videos are titled 'Artist - Song (...)'. Split on the first dash variant.
    parts = re.split(r"\s+[-\u2013\u2014]\s+", title, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        result = (parts[0].strip(), parts[1].strip())
        log.debug("parse_track: title-split %r/%r -> %r", artist_field, title_field, result)
        return result

    channel = _clean_channel(artist_field or "")
    if channel:
        result = (channel, title)
        log.debug("parse_track: channel-fallback %r/%r -> %r", artist_field, title_field, result)
        return result
    log.debug("parse_track: unparseable (artist=%r title=%r)", artist_field, title_field)
    return None


@dataclass(frozen=True)
class PlaybackState:
    """A snapshot of the active media session's track and playback timeline."""

    artist: str  # parsed artist
    song: str  # parsed song title
    position_ms: int  # extrapolated playback position
    duration_ms: int  # total track length (0 if unknown)
    playing: bool  # True while actually playing (vs paused/stopped)

    @property
    def key(self) -> tuple[str, str]:
        return (self.artist.casefold().strip(), self.song.casefold().strip())


def _state_from_probe(data: dict) -> PlaybackState | None:
    """Turn the probe's JSON payload into a PlaybackState with a live-extrapolated position."""
    parsed = parse_track(data.get("artist", ""), data.get("title", ""))
    if parsed is None:
        log.debug("_state_from_probe: unparseable app=%r artist=%r title=%r",
                  data.get("app"), data.get("artist"), data.get("title"))
        return None

    playing = bool(data.get("playing"))
    position_ms = int(data.get("position_ms", 0))
    duration_ms = int(data.get("duration_ms", 0))

    # The probe's snapshot is already `age_ms` old; while playing, advance to "now".
    if playing:
        position_ms += int(data.get("age_ms", 0))
    if duration_ms > 0:
        position_ms = max(0, min(position_ms, duration_ms))

    log.debug("_state_from_probe: %r playing=%s pos=%dms dur=%dms",
              parsed, playing, position_ms, duration_ms)
    return PlaybackState(parsed[0], parsed[1], position_ms, duration_ms, playing)


def read_state_blocking() -> PlaybackState | None:
    """Read current playback via the probe subprocess. Safe on any thread; bounded by timeout.

    Returns None when nothing usable is playing, the read times out, or SMTC is wedged --
    the caller simply shows no sync this cycle rather than blocking.
    """
    if not _SMTC_AVAILABLE:
        return None
    try:
        proc = subprocess.run(
            [sys.executable, _PROBE],
            capture_output=True, text=True,
            timeout=SMTC_READ_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        log.warning("read_state_blocking: SMTC probe timed out (media session may be wedged)")
        return None
    except Exception:  # noqa: BLE001 - spawning failed; no state this cycle
        log.exception("read_state_blocking: could not run SMTC probe")
        return None

    if not proc.stdout.strip():
        log.debug("read_state_blocking: probe produced no output (rc=%s stderr=%r)",
                  proc.returncode, proc.stderr[:200])
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.warning("read_state_blocking: probe output not JSON: %r", proc.stdout[:200])
        return None

    if payload.get("error"):
        log.warning("read_state_blocking: probe error: %s", payload["error"])
        return None
    data = payload.get("state")
    if not data:
        return None
    return _state_from_probe(data)


class NowPlayingReader(QtCore.QThread):
    """Runs the SMTC probe on a worker thread and emits the outcome to the GUI thread."""

    state = QtCore.Signal(object)  # PlaybackState
    empty = QtCore.Signal(str)  # reason when nothing usable is playing

    def run(self) -> None:
        """Fetch the current now-playing state; emit state/empty, never raising into Qt."""
        if not _SMTC_AVAILABLE:
            log.warning("NowPlayingReader: SMTC unavailable on this platform")
            self.empty.emit("Now-playing detection is only available on Windows.")
            return
        result = read_state_blocking()
        if result is None:
            self.empty.emit("Nothing is playing (or the media session isn't responding).")
            return
        log.debug("NowPlayingReader: emitting %r", result.key)
        self.state.emit(result)
