"""
ui/widgets.py - Minimal, terminal-native chrome for ytmusic-tui.

Contains the single-line top bar (view tabs + playback state), the clickable
Unicode seek bar, the slim bottom player bar, and the help overlay.
"""

from typing import List, Optional, Tuple

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

from api import Track, format_seconds
from theme import ThemeColors


VIEWS: List[Tuple[str, str, str]] = [
    ("1", "trending", "view_trending"),
    ("2", "radio", "view_radio"),
    ("3", "moods", "view_moods"),
    ("4", "search", "view_search"),
    ("5", "queue", "view_queue"),
]


def _palette(widget: Widget) -> ThemeColors:
    if hasattr(widget.app, "theme_manager"):
        return widget.app.theme_manager.current_theme
    return ThemeColors()


class TopBar(Static):
    """A single line of view tabs on the left and playback state on the right."""

    class TabClicked(Message):
        """Posted when a view tab is clicked."""

        def __init__(self, view_id: str) -> None:
            super().__init__()
            self.view_id = view_id

    active_view_id: reactive[str] = reactive("view_trending")
    is_playing: reactive[bool] = reactive(False)
    is_paused: reactive[bool] = reactive(False)
    is_buffering: reactive[bool] = reactive(False)
    volume: reactive[int] = reactive(80)
    queue_len: reactive[int] = reactive(0)
    theme_name: reactive[str] = reactive("Omarchy")
    status: reactive[str] = reactive("")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tab_spans: List[Tuple[int, int, str]] = []

    def _state(self) -> Tuple[str, str]:
        t = _palette(self)
        if self.is_buffering:
            return "◌", t.warning
        if self.is_paused:
            return "‖", t.warning
        if self.is_playing:
            return "▶", t.success
        return "·", t.muted

    def render(self) -> Text:
        t = _palette(self)
        width = self.size.width
        text = Text(no_wrap=True, overflow="crop")
        self._tab_spans = []

        text.append(" ")
        text.append(" ytm ", style=f"bold {t.background} on {t.accent}")
        text.append("  ")

        for index, (key, label, view_id) in enumerate(VIEWS):
            if index:
                text.append("  ")
            start = text.cell_len
            active = view_id == self.active_view_id
            style = f"bold {t.accent}" if active else t.muted
            text.append(f"{key} {label}", style=style)
            self._tab_spans.append((start, text.cell_len, view_id))

        if self.status:
            text.append("  ·  ", style=t.border)
            text.append(self.status, style=t.muted)

        glyph, glyph_color = self._state()
        status = Text(no_wrap=True)
        if self.theme_name:
            status.append(f"{self.theme_name}  ", style=t.border)
        status.append(glyph, style=f"bold {glyph_color}")
        status.append(f"  {self.volume}%", style=t.muted)
        if self.queue_len:
            status.append(f"  {self.queue_len} queued", style=t.muted)
        status.append("  ? help", style=t.border)

        if width > 0:
            padding = width - text.cell_len - status.cell_len
            if padding >= 1:
                text.append(" " * padding)
                text.append_text(status)
            elif width - text.cell_len >= 2:
                text.append(" ")
                text.append(glyph, style=f"bold {glyph_color}")

        return text

    def on_click(self, event: events.Click) -> None:
        for start, end, view_id in self._tab_spans:
            if start <= event.x < end:
                self.post_message(self.TabClicked(view_id))
                event.stop()
                return

    def refresh_theme(self) -> None:
        self.refresh()


class SeekBar(Static):
    """Clickable full-width Unicode progress bar."""

    elapsed: reactive[float] = reactive(0.0)
    total: reactive[float] = reactive(0.0)

    def render(self) -> Text:
        t = _palette(self)
        width = self.size.width
        text = Text(no_wrap=True, overflow="crop")
        if width <= 0:
            return text

        if self.total <= 0:
            text.append("─" * width, style=t.border)
            return text

        ratio = max(0.0, min(1.0, self.elapsed / self.total))
        filled = max(0, min(width - 1, int(round(ratio * (width - 1)))))
        empty = width - 1 - filled

        if filled:
            text.append("━" * filled, style=t.primary)
        text.append("●", style=t.accent)
        if empty:
            text.append("─" * empty, style=t.border)
        return text

    def on_click(self, event: events.Click) -> None:
        width = self.size.width
        if self.total <= 0 or width <= 0:
            return
        percent = max(0.0, min(100.0, (event.x / width) * 100.0))
        if hasattr(self.app, "player"):
            self.app.player.seek_percent(percent)
            event.stop()


class PlayerBar(Widget):
    """Slim two-line player: now playing + time, then the seek bar."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._track: Optional[Track] = None
        self._quality: str = ""
        self._pos: float = 0.0
        self._dur: float = 0.0
        self._state: Tuple[bool, bool, bool] = (False, False, False)

    def compose(self) -> ComposeResult:
        with Horizontal(id="player_row"):
            yield Static("", id="player_now")
            yield Static("", id="player_time")
        yield SeekBar(id="player_seek")

    def on_mount(self) -> None:
        self._render_now()
        self._render_time()

    def _render_now(self) -> None:
        t = _palette(self)
        try:
            now = self.query_one("#player_now", Static)
        except Exception:
            return

        if self._track is None:
            now.update(f"[{t.muted}]·  nothing playing[/]")
            return

        playing, paused, buffering = self._state
        if buffering:
            glyph, color = "◌", t.warning
        elif paused:
            glyph, color = "‖", t.warning
        elif playing:
            glyph, color = "▶", t.success
        else:
            glyph, color = "·", t.muted

        now.update(
            f"[bold {color}]{glyph}[/]  "
            f"[bold {t.foreground}]{self._track.title}[/]  "
            f"[{t.muted}]·[/]  [{t.secondary}]{self._track.artist}[/]"
        )

    def _render_time(self) -> None:
        t = _palette(self)
        try:
            label = self.query_one("#player_time", Static)
        except Exception:
            return
        elapsed = format_seconds(self._pos)
        total = format_seconds(self._dur) if self._dur > 0 else "--:--"
        parts = f"[{t.muted}]{elapsed} / {total}[/]"
        if self._quality:
            parts += f"[{t.border}]  ·  {self._quality}[/]"
        label.update(parts)

    def set_track(self, track: Optional[Track], quality: str = "") -> None:
        self._track = track
        if quality:
            self._quality = quality
        if track is None:
            self._quality = ""
        self._render_now()
        self._render_time()

    def set_progress(self, pos: float, duration: float) -> None:
        self._pos = pos
        self._dur = duration
        try:
            bar = self.query_one("#player_seek", SeekBar)
            bar.elapsed = pos
            bar.total = duration
        except Exception:
            pass
        self._render_time()

    def set_state(
        self,
        is_playing: bool,
        is_paused: bool,
        is_buffering: bool,
    ) -> None:
        self._state = (is_playing, is_paused, is_buffering)
        self._render_now()

    def refresh_theme(self) -> None:
        self._render_now()
        self._render_time()
        try:
            self.query_one("#player_seek", SeekBar).refresh()
        except Exception:
            pass


class HelpScreen(ModalScreen):
    """Keyboard cheatsheet overlay toggled with ``?``."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("question_mark", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    ROWS: List[Tuple[str, str]] = [
        ("space", "play / pause"),
        ("n / p", "next / previous track"),
        ("← / →", "seek -5s / +5s"),
        ("+ / -", "volume up / down"),
        ("j / k", "move down / up"),
        ("enter", "play highlighted item"),
        ("a", "append highlighted to queue"),
        ("A", "queue every track in view"),
        ("P", "play every track in view"),
        ("r", "start song radio"),
        ("d", "remove highlighted from queue"),
        ("s / c", "shuffle / clear queue"),
        ("/", "focus search"),
        ("1 - 5", "jump to view"),
        ("esc", "back / close overlay"),
        ("t", "sync Omarchy theme"),
        ("?", "toggle this help"),
        ("q", "quit"),
    ]

    def compose(self) -> ComposeResult:
        t = _palette(self)
        rows = "\n".join(
            f"[bold {t.accent}]{key:<7}[/] [{t.foreground}]{desc}[/]"
            for key, desc in self.ROWS
        )
        with Container(id="help_panel"):
            yield Static("KEYBINDINGS", classes="help_title")
            yield Static(rows, classes="help_line")
            yield Static(f"[{t.muted}]press ? or esc to close[/]", classes="help_footer")
