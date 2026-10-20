"""Standalone SMTC probe: print the current now-playing state as JSON, then exit.

Run as a short-lived subprocess by the widget so WinRT async never touches the GUI
process's event loop (the winsdk async bridge deadlocks under a running Qt loop).
A fresh process gets clean COM/asyncio state every call; a hang is bounded by the
parent's read timeout, which kills this child without wedging the app.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import sys

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as Mgr,
)

_PLAYING_STATUS = 4


async def _read() -> dict | None:
    mgr = await Mgr.request_async()
    session = mgr.get_current_session()
    if session is None:
        return None
    props = await session.try_get_media_properties_async()
    timeline = session.get_timeline_properties()
    playback = session.get_playback_info()
    playing = int(playback.playback_status) == _PLAYING_STATUS

    now = datetime.datetime.now(datetime.timezone.utc)
    last = timeline.last_updated_time
    return {
        "app": session.source_app_user_model_id,
        "artist": props.artist or "",
        "title": props.title or "",
        "position_ms": int(timeline.position.total_seconds() * 1000),
        "duration_ms": int(timeline.end_time.total_seconds() * 1000),
        "playing": playing,
        # Seconds since the position snapshot, so the parent can extrapolate if it wants.
        "age_ms": int((now - last).total_seconds() * 1000) if last is not None else 0,
    }


def main() -> int:
    try:
        data = asyncio.run(_read())
    except Exception as exc:  # noqa: BLE001 - report to the parent as a structured error
        json.dump({"error": repr(exc)}, sys.stdout)
        return 1
    json.dump({"state": data}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
