"""Parse LRCLIB synced lyrics (LRC format) into timed lines and resolve the active line."""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

# An LRC timestamp tag: [mm:ss.xx] or [mm:ss.xxx]; a line may carry several.
_TAG = re.compile(r"\[(\d+):(\d{2})(?:[.:](\d{1,3}))?\]")


@dataclass(frozen=True)
class SyncedLine:
    """A single timed lyric line."""

    start_ms: int
    text: str


def parse_lrc(synced: str) -> list[SyncedLine]:
    """Parse LRC text into time-sorted SyncedLines; lines with no tag are dropped.

    A line may have multiple timestamp tags (repeated lyric); each becomes its own entry.
    """
    lines: list[SyncedLine] = []
    for raw in synced.splitlines():
        tags = list(_TAG.finditer(raw))
        if not tags:
            continue
        text = _TAG.sub("", raw).strip()
        for m in tags:
            minutes, seconds, frac = m.group(1), m.group(2), m.group(3) or "0"
            ms = int(minutes) * 60000 + int(seconds) * 1000 + int(frac.ljust(3, "0"))
            lines.append(SyncedLine(ms, text))
    lines.sort(key=lambda ln: ln.start_ms)
    return lines


def active_index(lines: list[SyncedLine], position_ms: int) -> int:
    """Return the index of the line active at position_ms, or -1 before the first line."""
    if not lines:
        return -1
    starts = [ln.start_ms for ln in lines]
    # Rightmost line whose start_ms <= position_ms.
    idx = bisect.bisect_right(starts, position_ms) - 1
    return idx
