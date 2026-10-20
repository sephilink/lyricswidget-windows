"""The floating card window: frameless/translucent/always-on-top host for the synced lyrics view.

Playback flow (designed to touch LRCLIB as little as possible):
  - a single local timer polls the OS media session (SMTC) ~4x/second -- no network;
  - lyrics are fetched from LRCLIB only when the detected track *changes*, and the
    LyricsFetcher caches by track, so pause/resume/seek/replay never re-fetch;
  - between fetches the view scrolls in time purely from the local playback position.
"""
from __future__ import annotations

import time

from PySide6 import QtCore, QtGui, QtWidgets

from config import (
    DEFAULT_SIZE,
    FETCH_RETRY_BACKOFF,
    MAX_FETCH_RETRIES,
    MIN_SIZE,
    SMTC_READ_TIMEOUT,
    SYNC_POLL_MS,
)
from logging_setup import get_logger
from lrc import SyncedLine, active_index, parse_lrc
from lyrics_api import LyricsFetcher, LyricsResult
from now_playing import NowPlayingReader, PlaybackState

try:
    import keyboard  # system-wide hotkey, works even when the window is click-through/unfocused
except Exception:  # noqa: BLE001 - optional; click-through then relies on the menu toggle only
    keyboard = None

log = get_logger("widget")

# Fully transparent overlay: no card background or border -- only the text shows on the desktop.
CARD_STYLE = "background:transparent; border:none;"
INPUT_STYLE = (
    "QLineEdit{color:#e8e8f0; background:rgba(35,36,68,220); border:1px solid rgba(124,92,255,90);"
    "border-radius:8px; padding:6px 8px; font-size:12px;}"
    "QLineEdit:focus{border:1px solid #7c5cff;}"
)
BTN_STYLE = (
    "QPushButton{color:#fff; background:#7c5cff; border:none; border-radius:8px; padding:6px 12px;"
    "font-size:12px; font-weight:600;}"
    "QPushButton:hover{background:#9479ff;} QPushButton:disabled{background:#4a4570;}"
)
CTRL_STYLE = (
    "background:rgba(35,36,68,230); color:#cfc8ff; border:1px solid rgba(124,92,255,120);"
    "border-radius:6px; font-size:12px;"
)

ACTIVE_COLOR = "#ffffff"
INACTIVE_COLOR = "#c9c9e0"  # brighter than before; the drop shadow carries contrast

# Montserrat if installed/bundled, with graceful fallbacks so text still renders everywhere.
FONT_FAMILY = "Montserrat, 'Segoe UI', system-ui, sans-serif"


def _apply_text_shadow(widget: QtWidgets.QWidget) -> None:
    """Attach a soft dark drop shadow so light text stays readable over any desktop background."""
    effect = QtWidgets.QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(6)
    effect.setColor(QtGui.QColor(0, 0, 0, 230))
    effect.setOffset(0, 1)
    widget.setGraphicsEffect(effect)


# Slim skin for the scrollbar while it's shown (on hover). When hidden we drop the bar
# entirely via the scroll-bar POLICY, which is the only reliable way to make it disappear
# -- a width:0 stylesheet still leaves Qt drawing/reserving the native bar.
SCROLLBAR_SKIN = (
    "QScrollBar:vertical{width:6px; background:transparent; margin:2px;}"
    "QScrollBar::handle:vertical{background:rgba(124,92,255,160); border-radius:3px; min-height:24px;}"
    "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical{height:0px;}"
    "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical{background:transparent;}"
)


class SyncedLyricsView(QtWidgets.QScrollArea):
    """Scrollable lyric column that highlights the active line and smoothly keeps it centred."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)  # hidden until hover
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setStyleSheet("background:transparent;")
        # Skin the (occasionally shown) scrollbar directly on the bar object.
        self.verticalScrollBar().setStyleSheet(SCROLLBAR_SKIN)
        self._inner = QtWidgets.QWidget()
        self._inner.setStyleSheet("background:transparent;")
        self._layout = QtWidgets.QVBoxLayout(self._inner)
        self._layout.setContentsMargins(4, 8, 4, 8)
        self._layout.setSpacing(6)
        self._layout.setAlignment(QtCore.Qt.AlignTop)
        self.setWidget(self._inner)
        self._labels: list[QtWidgets.QLabel] = []
        self._lines: list[SyncedLine] = []
        self._active = -1
        self._one_line = False  # when True, show only the active lyric line

        # Scrollbar hidden (zero-width) until the view is hovered/focused.
        self.set_scrollbar_visible(False)

        # Smooth animated scrolling to the active line instead of a hard jump.
        self._scroll_anim = QtCore.QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._scroll_anim.setDuration(350)
        self._scroll_anim.setEasingCurve(QtCore.QEasingCurve.InOutCubic)

    def set_scrollbar_visible(self, on: bool) -> None:
        """Show the slim scrollbar on hover/focus; fully remove it (via policy) otherwise."""
        self.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarAsNeeded if on else QtCore.Qt.ScrollBarAlwaysOff
        )

    def set_one_line(self, on: bool) -> None:
        """One-line mode: show only the active lyric line, hiding all the others."""
        self._one_line = on
        self._apply_line_visibility()

    def _apply_line_visibility(self) -> None:
        """Show every label, or only the active one when one-line mode is on."""
        if not self._one_line:
            for lbl in self._labels:
                lbl.setVisible(True)
            return
        # One-line mode: show only the active line (fall back to the first if none is active yet).
        show = self._active if 0 <= self._active < len(self._labels) else 0
        for i, lbl in enumerate(self._labels):
            lbl.setVisible(i == show)

    def set_message(self, text: str) -> None:
        """Clear timed content and show a single centred status line."""
        self._lines = []
        self._set_labels([text], muted=True)
        self._active = -1

    def set_plain(self, text: str) -> None:
        """Show un-timed lyrics as static lines (no highlight/scroll)."""
        self._lines = []
        self._set_labels(text.splitlines() or [""], muted=False)
        self._active = -1

    def set_synced(self, lines: list[SyncedLine]) -> None:
        """Load timed lines; highlighting then follows update_position()."""
        self._lines = lines
        self._set_labels([ln.text or "\u2026" for ln in lines], muted=False)
        self._active = -1

    @property
    def has_sync(self) -> bool:
        return bool(self._lines)

    def _set_labels(self, texts: list[str], muted: bool) -> None:
        """Rebuild the label column from a list of strings."""
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._labels = []
        color = "#9a9ab5" if muted else INACTIVE_COLOR
        for text in texts:
            lbl = QtWidgets.QLabel(text)
            lbl.setWordWrap(True)
            lbl.setAlignment(QtCore.Qt.AlignHCenter)
            lbl.setStyleSheet(f"color:{color}; font-size:13px; font-family:{FONT_FAMILY};")
            _apply_text_shadow(lbl)
            self._layout.addWidget(lbl)
            self._labels.append(lbl)
        self.verticalScrollBar().setValue(0)
        self._apply_line_visibility()

    def update_position(self, position_ms: int) -> None:
        """Highlight the line active at position_ms and scroll it to the view centre."""
        if not self._lines:
            return
        idx = active_index(self._lines, position_ms)
        if idx == self._active:
            return
        self._active = idx
        for i, lbl in enumerate(self._labels):
            if i == idx:
                lbl.setStyleSheet(
                    f"color:{ACTIVE_COLOR}; font-size:15px; font-weight:700; font-family:{FONT_FAMILY};"
                )
            else:
                lbl.setStyleSheet(
                    f"color:{INACTIVE_COLOR}; font-size:13px; font-family:{FONT_FAMILY};"
                )
        if self._one_line:
            self._apply_line_visibility()  # keep only the (new) active line showing
        if 0 <= idx < len(self._labels):
            self._centre_on(self._labels[idx])

    def _centre_on(self, label: QtWidgets.QLabel) -> None:
        """Smoothly animate the scroll so the given label eases to the vertical centre."""
        bar = self.verticalScrollBar()
        target = label.pos().y() + label.height() // 2 - self.viewport().height() // 2
        target = max(bar.minimum(), min(target, bar.maximum()))
        self._scroll_anim.stop()
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.start()


class Widget(QtWidgets.QWidget):
    """Frameless, translucent, always-on-top, draggable card wrapping the synced lyrics view."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
            | QtCore.Qt.Tool
        )
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.resize(*DEFAULT_SIZE)
        self.setMinimumSize(*MIN_SIZE)
        self._drag_pos: QtCore.QPoint | None = None
        self._resize_edge: str | None = None
        self._resize_start_geo: QtCore.QRect | None = None
        self._resize_start_mouse: QtCore.QPoint | None = None
        self._margin = 8  # px hot-zone at edges/corners for resize

        self._fetcher: LyricsFetcher | None = None
        self._loaded_key: tuple[str, str] | None = None  # track we currently hold lyrics for
        self._fetching_key: tuple[str, str] | None = None  # track a fetch is in flight for
        self._last_tick_key: tuple[str, str] | None = None  # last track logged by the poll
        self._retry_after: float = 0.0  # perf_counter gate before retrying a failed fetch
        self._retry_key: tuple[str, str] | None = None  # track the retry counter applies to
        self._retry_count: int = 0  # retryable failures so far for _retry_key

        # SMTC is read on a worker thread (NowPlayingReader); the GUI thread only extrapolates
        # the position from the most recent reading, so WinRT never runs on the GUI thread.
        self._reader: NowPlayingReader | None = None
        self._last_state: PlaybackState | None = None
        self._last_state_at: float = 0.0  # perf_counter timestamp of the last reading

        self._click_through = False  # win32 WS_EX_TRANSPARENT state (Ctrl+Alt+C)

        # Opaque window; the transparency is purely the absent card background, so text stays crisp.
        self.setWindowOpacity(1.0)

        # --- card surface (fully transparent: only the text shows) ---
        self.card = QtWidgets.QFrame(self)
        self.card.setStyleSheet(CARD_STYLE)

        # --- manual search row, hidden by default, toggled from the menu ---
        self.artist = QtWidgets.QLineEdit()
        self.artist.setPlaceholderText("Artist")
        self.artist.setStyleSheet(INPUT_STYLE)
        self.title = QtWidgets.QLineEdit()
        self.title.setPlaceholderText("Song title")
        self.title.setStyleSheet(INPUT_STYLE)
        self.search_btn = QtWidgets.QPushButton("Search")
        self.search_btn.setStyleSheet(BTN_STYLE)
        self.search_btn.clicked.connect(self._manual_search)
        self.artist.returnPressed.connect(self._manual_search)
        self.title.returnPressed.connect(self._manual_search)

        self.search_bar = QtWidgets.QWidget()
        search_row = QtWidgets.QHBoxLayout(self.search_bar)
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self.artist, 1)
        search_row.addWidget(self.title, 1)
        search_row.addWidget(self.search_btn, 0)
        self.search_bar.setVisible(False)  # hidden until toggled from the menu

        # --- heading + synced lyric view ---
        self.heading = QtWidgets.QLabel("")
        self.heading.setAlignment(QtCore.Qt.AlignHCenter)
        self.heading.setWordWrap(True)  # long artist/song names wrap instead of truncating
        self.heading.setStyleSheet(
            f"color:#ffffff; font-size:13px; font-weight:700; font-family:{FONT_FAMILY};"
        )
        _apply_text_shadow(self.heading)
        self.heading.setVisible(False)

        self.view = SyncedLyricsView()
        self.view.set_message(
            "Play a song in your browser \u2014 lyrics follow along automatically."
        )

        card_layout = QtWidgets.QVBoxLayout(self.card)
        card_layout.setContentsMargins(14, 14, 14, 14)
        card_layout.setSpacing(10)
        card_layout.addWidget(self.search_bar)
        card_layout.addWidget(self.heading)
        card_layout.addWidget(self.view, 1)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.card)

        # --- overlay controls (revealed on hover) ---
        self.close_btn = QtWidgets.QPushButton("\u2715", self)
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.setStyleSheet(CTRL_STYLE)
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setVisible(False)

        self.menu_btn = QtWidgets.QToolButton(self)
        self.menu_btn.setText("\u2630")
        self.menu_btn.setFixedSize(24, 22)
        self.menu_btn.setStyleSheet(CTRL_STYLE)
        self.menu_btn.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.menu_btn.setMenu(self._build_menu())
        self.menu_btn.setVisible(False)

        # --- poll: triggers an off-thread SMTC read + extrapolates position locally ---
        self._follow = True
        self._poll = QtCore.QTimer(self)
        self._poll.setInterval(SYNC_POLL_MS)
        self._poll.timeout.connect(self._tick)
        self._poll.start()
        # Re-read SMTC less often than we scroll: the worker read is the only WinRT cost.
        self._reads_per_scroll = max(1, 1000 // SYNC_POLL_MS)  # aim for ~one SMTC read/second
        self._tick_count = 0

        # Ctrl+Alt+C toggles click-through system-wide (works even when unfocused/click-through).
        self._register_global_hotkey()

    def _build_menu(self) -> QtWidgets.QMenu:
        """Assemble the settings menu: opacity + the follow-playback toggle."""
        menu = QtWidgets.QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:#10101f; border:1px solid #7c5cff;} QLabel{color:#cfc8ff;}"
        )
        self.opacity = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.opacity.setRange(40, 100)
        self.opacity.setValue(100)
        self.opacity.setToolTip("Text opacity")
        self.opacity.valueChanged.connect(lambda v: self.setWindowOpacity(v / 100.0))

        container = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(container)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(3)
        lbl = QtWidgets.QLabel("Opacity")
        lbl.setStyleSheet("color:#cfc8ff; font-size:10px;")
        lay.addWidget(lbl)
        lay.addWidget(self.opacity)
        action = QtWidgets.QWidgetAction(self)
        action.setDefaultWidget(container)
        menu.addAction(action)

        menu.addSeparator()
        self.follow_action = menu.addAction("Follow what's playing")
        self.follow_action.setCheckable(True)
        self.follow_action.setChecked(True)
        self.follow_action.toggled.connect(self._set_follow)

        self.oneline_action = menu.addAction("One-line mode")
        self.oneline_action.setCheckable(True)
        self.oneline_action.setChecked(False)
        self.oneline_action.toggled.connect(self._set_one_line)

        self.search_action = menu.addAction("Show search box")
        self.search_action.setCheckable(True)
        self.search_action.setChecked(False)
        self.search_action.toggled.connect(self._set_search_visible)

        menu.addSeparator()
        self.ct_action = menu.addAction("Toggle click-through (Ctrl+Alt+C)")
        self.ct_action.setCheckable(True)
        self.ct_action.toggled.connect(self._set_click_through)
        return menu

    def _set_search_visible(self, on: bool) -> None:
        """Show or hide the manual artist/title search bar (hidden by default)."""
        log.info("_set_search_visible: %s", on)
        self.search_bar.setVisible(on)

    def _set_one_line(self, on: bool) -> None:
        """Toggle one-line lyric mode (show only the current line)."""
        log.info("_set_one_line: %s", on)
        self.view.set_one_line(on)

    # --- playback poll: the single driver of fetch + sync ---

    def _set_follow(self, on: bool) -> None:
        """Enable/disable auto-following the browser's playback."""
        log.info("_set_follow: %s", on)
        self._follow = on
        if on:
            self._request_read()

    def _tick(self) -> None:
        """GUI-thread poll: periodically trigger an off-thread SMTC read; scroll from last state.

        This NEVER touches WinRT on the GUI thread. The actual media-session read happens on a
        NowPlayingReader worker; here we only kick that off occasionally and extrapolate the
        current playback position from the most recent reading, so the scroll stays smooth.
        """
        if not self._follow:
            return

        self._tick_count += 1
        if self._tick_count % self._reads_per_scroll == 0:
            self._request_read()

        self._scroll_to_now()

    def _request_read(self) -> None:
        """Start a worker SMTC read if one isn't already running (off the GUI thread)."""
        if self._reader is not None and self._reader.isRunning():
            return
        self._reader = NowPlayingReader()
        self._reader.state.connect(self._on_state)
        self._reader.empty.connect(self._on_state_empty)
        self._reader.start()

    def _on_state(self, state: PlaybackState) -> None:
        """Receive a fresh SMTC reading from the worker; record it and fetch on track change."""
        self._last_state = state
        self._last_state_at = time.perf_counter()

        if state.key != self._last_tick_key:
            log.info("_on_state: track=%r playing=%s pos=%dms", state.key, state.playing, state.position_ms)
            self._last_tick_key = state.key

        if (
            state.key != self._loaded_key
            and state.key != self._fetching_key
            and time.perf_counter() >= self._retry_after
        ):
            log.debug("_on_state: new track vs loaded=%r fetching=%r", self._loaded_key, self._fetching_key)
            self._start_fetch(state)

        self._scroll_to_now()

    def _on_state_empty(self, reason: str) -> None:
        """Worker reports nothing usable playing; drop the cached state so the scroll pauses."""
        if self._last_tick_key is not None:
            log.debug("_on_state_empty: %s", reason)
            self._last_tick_key = None
        self._last_state = None

    def _scroll_to_now(self) -> None:
        """Highlight the lyric line for the current (extrapolated) playback position."""
        state = self._last_state
        if state is None or state.key != self._loaded_key or not self.view.has_sync:
            return
        position_ms = state.position_ms
        if state.playing:
            # Extrapolate from the last reading so the line advances between worker reads.
            position_ms += int((time.perf_counter() - self._last_state_at) * 1000)
            if state.duration_ms > 0:
                position_ms = min(position_ms, state.duration_ms)
        self.view.update_position(position_ms)

    def _start_fetch(self, state: PlaybackState) -> None:
        """Fetch lyrics for a newly detected track (one network call, cached thereafter)."""
        if self._fetcher is not None and self._fetcher.isRunning():
            log.debug("_start_fetch: skipped, a fetch is already running")
            return
        log.info("_start_fetch: %r (duration=%sms)", state.key, state.duration_ms)
        self._fetching_key = state.key
        self.artist.setText(state.artist)
        self.title.setText(state.song)
        self.heading.setText(f"{state.artist}\n{state.song}")
        self.heading.setVisible(True)
        self.view.set_message("Loading lyrics\u2026")

        duration = (state.duration_ms // 1000) or None
        self._fetcher = LyricsFetcher(state.artist, state.song, duration)
        self._fetcher.found.connect(lambda res: self._on_found(res, state.key))
        self._fetcher.failed.connect(lambda msg, retry: self._on_failed(msg, state.key, retry))
        self._fetcher.start()

    def _manual_search(self) -> None:
        """Look up whatever is typed in the inputs; turns off follow so it won't be overridden."""
        artist = self.artist.text().strip()
        title = self.title.text().strip()
        log.info("_manual_search: %r / %r", artist, title)
        if not artist or not title:
            self.view.set_message("Enter both an artist and a song title.")
            return
        if self._fetcher is not None and self._fetcher.isRunning():
            log.debug("_manual_search: skipped, a fetch is already running")
            return
        self._set_follow(False)
        if hasattr(self, "follow_action"):
            self.follow_action.setChecked(False)

        key = (artist.casefold().strip(), title.casefold().strip())
        self._fetching_key = key
        self.heading.setText(f"{artist}\n{title}")
        self.heading.setVisible(True)
        self.view.set_message("Searching\u2026")

        self._fetcher = LyricsFetcher(artist, title)
        self._fetcher.found.connect(lambda res: self._on_found(res, key))
        self._fetcher.failed.connect(lambda msg, retry: self._on_failed(msg, key, retry))
        self._fetcher.start()

    def _on_found(self, result: LyricsResult, key: tuple[str, str]) -> None:
        """Render fetched lyrics; synced when available, otherwise a static plain column."""
        self._fetching_key = None
        self._loaded_key = key
        self._reset_retries()
        self.heading.setText(f"{result.artist}\n{result.title}")
        self.heading.setVisible(True)
        if result.is_synced:
            lines = parse_lrc(result.synced)
            log.info("_on_found: %r synced lyrics, %d lines", key, len(lines))
            self.view.set_synced(lines)
        else:
            log.info("_on_found: %r plain lyrics only (%d chars)", key, len(result.plain))
            self.view.set_plain(result.plain)

    def _on_failed(self, message: str, key: tuple[str, str], retryable: bool) -> None:
        """Render an error. A genuine miss is cached as loaded; a transient error stays retryable.

        For a retryable failure (network blip, 5xx/429) we clear _fetching_key but DON'T set
        _loaded_key, so the next poll re-attempts the fetch once the problem clears -- instead of
        the widget getting stuck on 'Network error' until the track changes.
        """
        self._fetching_key = None
        if not retryable:
            # A genuine miss (no lyrics / client error): mark loaded so we stop re-fetching.
            log.warning("_on_failed: %r permanent -> %s", key, message)
            self._loaded_key = key
            self._reset_retries()
            self.view.set_message(message)
            return

        # Retryable (network / 5xx / 429): count attempts per track and cap them.
        if key != self._retry_key:
            self._retry_key = key
            self._retry_count = 0
        self._retry_count += 1
        log.warning("_on_failed: %r retryable attempt %d/%d -> %s",
                    key, self._retry_count, MAX_FETCH_RETRIES, message)

        if self._retry_count >= MAX_FETCH_RETRIES:
            # Give up on this track: stop re-fetching until it changes (don't poison the cache,
            # so a manual search or returning to the track later can still try again fresh).
            self._loaded_key = key
            self.view.set_message(
                f"Couldn't reach LRCLIB after {MAX_FETCH_RETRIES} tries. "
                "Check your connection; it'll retry on the next song."
            )
            return

        # Back off before the poll re-attempts, so an outage isn't hammered.
        self._retry_after = time.perf_counter() + FETCH_RETRY_BACKOFF
        self.view.set_message(f"{message} ({self._retry_count}/{MAX_FETCH_RETRIES})")

    def _reset_retries(self) -> None:
        """Clear the per-track retry counter (on success or a permanent result)."""
        self._retry_key = None
        self._retry_count = 0

    # --- click-through (Ctrl+Alt+C), mirrors soundwidget ---

    def _register_global_hotkey(self) -> None:
        """Bind Ctrl+Alt+C system-wide so click-through can be toggled back off without focus."""
        if keyboard is None:
            log.warning("keyboard module unavailable; click-through via menu only")
            return
        try:
            # keyboard fires on its own thread; marshal onto the Qt thread via a queued invoke.
            keyboard.add_hotkey(
                "ctrl+alt+c",
                lambda: QtCore.QMetaObject.invokeMethod(
                    self, "_toggle_click_through", QtCore.Qt.QueuedConnection
                ),
            )
        except Exception:  # noqa: BLE001 - hotkey unavailable (permissions); menu toggle still works
            log.exception("could not register Ctrl+Alt+C hotkey")

    @QtCore.Slot()
    def _toggle_click_through(self) -> None:
        """Flip click-through (invoked from the global hotkey) and keep the menu item in sync."""
        self.ct_action.toggle()

    def _set_click_through(self, on: bool) -> None:
        """Toggle win32 WS_EX_TRANSPARENT so clicks pass through to whatever is behind the widget.

        We set the window's extended style directly rather than Qt's WA_TransparentForMouseEvents,
        so the controls stay clickable whenever click-through is OFF, and the global hotkey
        (Ctrl+Alt+C) brings it back when it's ON.
        """
        self._click_through = on
        log.info("_set_click_through: %s", on)
        try:
            import ctypes

            hwnd = int(self.winId())
            gwl_exstyle = -20
            ws_ex_transparent = 0x00000020
            ws_ex_layered = 0x00080000
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, gwl_exstyle)
            if on:
                style |= ws_ex_transparent | ws_ex_layered
            else:
                style &= ~ws_ex_transparent
            user32.SetWindowLongW(hwnd, gwl_exstyle, style)
        except Exception:  # noqa: BLE001 - non-Windows or restricted
            log.exception("_set_click_through: could not set window style")
        if hasattr(self, "ct_action") and self.ct_action.isChecked() != on:
            self.ct_action.setChecked(on)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        # Fully transparent background: a near-invisible fill keeps the window a valid mouse
        # target for hover/drag/resize while leaving only the text visible on the desktop.
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 1))

    # --- overlay placement + hover reveal ---

    def _position_overlay(self) -> None:
        """Place the floating menu/close buttons in the top corners over the card."""
        m = 8
        self.menu_btn.move(m, m)
        self.close_btn.move(self.width() - self.close_btn.width() - m, m)

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt override
        self._position_overlay()
        super().resizeEvent(e)

    def enterEvent(self, _e) -> None:  # noqa: N802 - Qt override
        self._position_overlay()
        self.menu_btn.setVisible(True)
        self.close_btn.setVisible(True)
        self.menu_btn.raise_()
        self.close_btn.raise_()
        self.view.set_scrollbar_visible(True)

    def leaveEvent(self, _e) -> None:  # noqa: N802 - Qt override
        if self.menu_btn.menu().isVisible():
            return
        self.menu_btn.setVisible(False)
        self.close_btn.setVisible(False)
        self.view.set_scrollbar_visible(False)

    # --- drag + aspect-free resize from edges/corners ---

    def _edge_at(self, pos: QtCore.QPoint) -> str | None:
        """Return which edge/corner the point is in ('tl','tr','bl','br','l','r','t','b') or None."""
        m = self._margin
        w, h = self.width(), self.height()
        left, right = pos.x() <= m, pos.x() >= w - m
        top, bottom = pos.y() <= m, pos.y() >= h - m
        if top and left:
            return "tl"
        if top and right:
            return "tr"
        if bottom and left:
            return "bl"
        if bottom and right:
            return "br"
        if left:
            return "l"
        if right:
            return "r"
        if top:
            return "t"
        if bottom:
            return "b"
        return None

    def _cursor_for(self, edge: str | None) -> QtCore.Qt.CursorShape:
        """Map an edge id to the appropriate resize cursor (diagonal on corners)."""
        return {
            "tl": QtCore.Qt.SizeFDiagCursor, "br": QtCore.Qt.SizeFDiagCursor,
            "tr": QtCore.Qt.SizeBDiagCursor, "bl": QtCore.Qt.SizeBDiagCursor,
            "l": QtCore.Qt.SizeHorCursor, "r": QtCore.Qt.SizeHorCursor,
            "t": QtCore.Qt.SizeVerCursor, "b": QtCore.Qt.SizeVerCursor,
        }.get(edge, QtCore.Qt.ArrowCursor)

    def mousePressEvent(self, e) -> None:  # noqa: N802 - Qt override
        # Shift+Click toggles click-through; an edge/corner press resizes; else drag.
        if e.button() == QtCore.Qt.LeftButton and (e.modifiers() & QtCore.Qt.ShiftModifier):
            self.ct_action.toggle()
            return
        if e.button() != QtCore.Qt.LeftButton:
            return
        edge = self._edge_at(e.position().toPoint())
        if edge:
            self._resize_edge = edge
            self._resize_start_geo = self.geometry()
            self._resize_start_mouse = e.globalPosition().toPoint()
        else:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802 - Qt override
        if self._resize_edge and (e.buttons() & QtCore.Qt.LeftButton):
            self._perform_resize(e.globalPosition().toPoint())
            return
        if self._drag_pos is not None and (e.buttons() & QtCore.Qt.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            return
        self.setCursor(self._cursor_for(self._edge_at(e.position().toPoint())))

    def _perform_resize(self, global_pos: QtCore.QPoint) -> None:
        """Resize from the grabbed edge/corner, clamped to the minimum size."""
        geo = QtCore.QRect(self._resize_start_geo)
        dx = global_pos.x() - self._resize_start_mouse.x()
        dy = global_pos.y() - self._resize_start_mouse.y()
        edge = self._resize_edge
        x, y, w, h = geo.x(), geo.y(), geo.width(), geo.height()

        if edge in ("l", "tl", "bl"):
            new_w = max(self.minimumWidth(), w - dx)
            x = geo.right() - new_w + 1
            w = new_w
        if edge in ("r", "tr", "br"):
            w = max(self.minimumWidth(), w + dx)
        if edge in ("t", "tl", "tr"):
            new_h = max(self.minimumHeight(), h - dy)
            y = geo.bottom() - new_h + 1
            h = new_h
        if edge in ("b", "bl", "br"):
            h = max(self.minimumHeight(), h + dy)
        self.setGeometry(x, y, w, h)

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802 - Qt override
        self._drag_pos = None
        self._resize_edge = None
        self._resize_start_geo = None
        self._resize_start_mouse = None

    def closeEvent(self, e) -> None:  # noqa: N802 - Qt override
        log.info("closeEvent: shutting down")
        if keyboard is not None:
            try:
                keyboard.clear_all_hotkeys()
            except Exception:  # noqa: BLE001 - nothing registered / already cleared
                pass
        self._poll.stop()
        if self._fetcher is not None and self._fetcher.isRunning():
            self._fetcher.wait(2000)
        if self._reader is not None and self._reader.isRunning():
            # Wait longer than the probe timeout so the worker finishes before teardown,
            # otherwise destroying a running QThread can crash on exit.
            self._reader.wait(int((SMTC_READ_TIMEOUT + 1) * 1000))
        e.accept()
