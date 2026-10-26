# lyricswidget

A floating, always-on-top desktop lyrics widget for Windows, built with PySide6 in
the same style as `soundwidget`: a frameless, translucent, draggable card you can
park anywhere on screen.

Play a song in **Spotify or your browser** and the lyrics load automatically and
**scroll in time with the music** (karaoke-style line highlighting). You can also type
an artist and title and hit **Search**. Lyrics come from
[LRCLIB](https://lrclib.net), a free, key-less lyrics API that provides time-synced
lyrics.

## How it follows playback

The widget reads the currently playing track and playback position from the
**Windows System Media Transport Controls (SMTC)** — the same "now playing" data
behind the Windows media overlay. Any app that reports to it is picked up, with no
browser extension or tab pairing needed:

- **Spotify** — the desktop client. This also covers **Spotify Connect**: when you
  play on your phone (or another device) and the desktop client is open, Windows still
  reports the track and a live playback position, so the lyrics follow your phone.
- **Chrome, Edge and other Chromium browsers** playing YouTube.

Controls:

- **Follow what's playing** (on by default, toggle in the settings menu) — the
  widget detects the current track and keeps the active lyric line centred as it plays.
- Pause, resume and seeking are tracked automatically; the active line follows the
  real playback position.

The two sources report metadata differently, so the widget parses per source:

- **Spotify** already reports clean, structured artist and title fields, so they're
  used as-is — a legitimate song title containing a dash (e.g. `Marvins Room - Live`)
  is preserved intact.
- **YouTube** titles are freeform, so the widget parses the video title
  (`Artist - Song (Official Video)` and similar) into a clean artist/song pair and
  strips tags like `(Official Video)`, `[Lyrics]`, `| Official Music Video`. The
  reported channel name (e.g. `FooVEVO`, `Foo - Topic`) is used only as a fallback
  artist when the title has no separator.

## Keeping LRCLIB traffic minimal

LRCLIB is a free community service, so the widget is deliberately frugal with it:

- **One lookup per song.** Lyrics are fetched only when the detected track
  *changes* — never on a timer and never per line.
- **In-process cache.** Every result (including confirmed misses) is cached by
  `(artist, title)`, so replaying, seeking, or pausing/resuming the same song makes
  no further calls.
- **Local time-sync.** The line-by-line scrolling is driven entirely from the OS
  playback position (polled locally a few times a second), which costs no network
  calls while a song plays.
- **Exact match first.** The precise `/api/get` endpoint (with track duration when
  known) is tried first; the fuzzier `/api/search` is only used as a fallback.
- An identifying `User-Agent` is sent on every request, as LRCLIB requests.

## Overlay UI

The widget is a **borderless, fully transparent** always-on-top overlay: only the song
title and the lyric text show on your desktop (each drawn with a soft shadow so it stays
readable over any background). There's no card, panel, or frame.

- **Search box is hidden by default.** Enable it from the menu (**Show search box**) when
  you want to type an artist/title manually; it hides again when you turn it off.
- **Click-through** mirrors `soundwidget`: toggle it from the menu, with **Shift+Click**
  on the widget, or with the global hotkey **Ctrl+Alt+C**. When on, mouse clicks pass
  straight through to whatever is behind the widget; the hotkey brings interactivity back
  even while the window is click-through and unfocused.
- Hover to reveal the menu (`☰`) and close (`✕`) controls in the corners.
- Drag to move; grab any edge or corner to resize. The **Opacity** slider fades the text.

## Features

- Borderless, fully transparent, always-on-top overlay (text only)
- Click-through (Ctrl+Alt+C / Shift+Click / menu), like soundwidget
- Search box hidden by default, toggled from the menu
- Time-synced lyrics that scroll with the playing song (when synced lyrics exist)
- Auto-detects the current track from Spotify (incl. phone playback via Spotify
  Connect) or the browser's now-playing media (Windows SMTC)
- Falls back to a static plain-lyrics view when a track has no synced lyrics
- Persistent SQLite lyrics cache (fetches each track from LRCLIB at most once, ever)
- Background lookups keep the UI responsive

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python lyricswidget.py
```

## Notes

- Synced lyrics availability depends on the LRCLIB catalogue; some tracks have only
  plain lyrics (shown static) or none at all.
- The widget needs an internet connection for the one-time-per-song lyrics lookup.
- SMTC is Windows-only; on other platforms the now-playing detection reports as
  unavailable and you can still use manual search.
