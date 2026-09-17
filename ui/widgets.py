"""
ui/widgets.py - Minimal, terminal-native chrome for ytmusic-tui.

The visual language borrows from cliamp: a letterspaced wordmark, bracketed
chips, section dividers, a block volume meter, a full-width seek bar and a
key-pill help bar.
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
    ("6", "library", "view_library"),
]

HELP_HINTS: List[Tuple[str, str]] = [
    ("Esc", "back"),
    ("Space", "play/pause"),
    ("n/p", "skip"),
    ("←/→", "seek"),
    ("a", "add"),
    ("/", "search"),
    ("?", "help"),
    ("q", "quit"),
]


def _palette(widget: Widget) -> ThemeColors:
    if hasattr(widget.app, "theme_manager"):
        return widget.app.theme_manager.current_theme
    return ThemeColors()


def _contrast(hex_color: str) -> str:
    """Pick black or white for readable text on a given background (WCAG-ish)."""
    try:
        value = int(hex_color.lstrip("#")[:6], 16)
    except ValueError:
        return "#ffffff"

    def linear(channel: int) -> float:
        component = channel / 255
        if component <= 0.04045:
            return component / 12.92
        return ((component + 0.055) / 1.055) ** 2.4

    luminance = (
        0.2126 * linear(value >> 16)
        + 0.7152 * linear((value >> 8) & 0xFF)
        + 0.0722 * linear(value & 0xFF)
    )
    return "#000000" if luminance > 0.179 else "#ffffff"


def _volume_meter(volume: int, cells: int = 8) -> str:
    """Block volume meter, e.g. ██████░░."""
    filled = max(0, min(cells, int(round((volume / 100.0) * cells))))
    return "█" * filled + "░" * (cells - filled)


_BLOCKS = " ▁▂▃▄▅▆▇█"


def _lerp_hex(start: str, end: str, factor: float) -> str:
    """Blend two hex colors, factor 0 -> start, 1 -> end."""
    try:
        a = int(start.lstrip("#")[:6], 16)
        b = int(end.lstrip("#")[:6], 16)
    except ValueError:
        return start

    factor = max(0.0, min(1.0, factor))
    channels = []
    for shift in (16, 8, 0):
        va = (a >> shift) & 0xFF
        vb = (b >> shift) & 0xFF
        channels.append(int(round(va + (vb - va) * factor)))
    return "#{:02x}{:02x}{:02x}".format(*channels)


def _spectrum_color(t: ThemeColors, fraction: float) -> str:
    """Vertical gradient: green low, yellow mid, red high."""
    if fraction < 0.5:
        return _lerp_hex(t.success, t.warning, fraction / 0.5)
    return _lerp_hex(t.warning, t.danger, (fraction - 0.5) / 0.5)


def _sample(bands: List[float], index: int, count: int) -> float:
    """Linearly resample the source bands into ``count`` display bars."""
    if not bands:
        return 0.0
    if count <= 1:
        return bands[len(bands) // 2]
    position = index * (len(bands) - 1) / (count - 1)
    low = int(position)
    high = min(low + 1, len(bands) - 1)
    frac = position - low
    return bands[low] * (1 - frac) + bands[high] * frac


class TopBar(Static):
    """Letterspaced wordmark and view tabs, with state chips on the right."""

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

    def render(self) -> Text:
        t = _palette(self)
        width = self.size.width
        text = Text(no_wrap=True, overflow="crop")
        self._tab_spans = []

        text.append("  ")
        wordmark = "Y T M U S I C" if width >= 96 else "Y T M"
        text.append(wordmark, style=f"bold {t.accent}")
        text.append("   ")

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

        right = Text(no_wrap=True)
        if width >= 104 and self.theme_name:
            right.append(f"{self.theme_name}  ", style=t.border)
        right.append("VOL ", style=t.border)
        right.append(_volume_meter(self.volume), style=f"bold {t.success}")
        right.append(f" {self.volume}%", style=t.muted)
        if self.queue_len:
            right.append(f"  [Q{self.queue_len}]", style=f"bold {t.accent}")
        right.append("  ? help", style=t.border)

        if width > 0:
            padding = width - text.cell_len - right.cell_len
            if padding >= 1:
                text.append(" " * padding)
                text.append_text(right)

        return text

    def on_click(self, event: events.Click) -> None:
        for start, end, view_id in self._tab_spans:
            if start <= event.x < end:
                self.post_message(self.TabClicked(view_id))
                event.stop()
                return

    def refresh_theme(self) -> None:
        self.refresh()


class SectionHeader(Static):
    """A cliamp-style divider: ▸─ label ── [chip] [chip] ──."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._label: str = ""
        self._chips: List[Tuple[str, bool]] = []

    def set_content(self, label: str, chips: Optional[List[Tuple[str, bool]]] = None) -> None:
        self._label = label
        self._chips = chips or []
        self.refresh()

    def render(self) -> Text:
        t = _palette(self)
        text = Text(no_wrap=True, overflow="crop")
        if not self._label:
            return text

        text.append("▸─ ", style=t.border)
        text.append(self._label, style=f"bold {t.accent}")
        text.append(" ──", style=t.border)

        for chip_text, active in self._chips:
            text.append(" ")
            if active:
                text.append(f"[{chip_text}]", style=f"bold {t.accent}")
            else:
                text.append(f"[{chip_text}]", style=t.muted)

        if self._chips:
            text.append(" ──", style=t.border)
        return text


class Spectrum(Static):
    """cava-driven spectrum bars with fractional blocks and a vertical gradient."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._bands: List[float] = []

    def set_bands(self, bands: List[float]) -> None:
        self._bands = bands
        self.refresh()

    def render(self) -> Text:
        t = _palette(self)
        width = self.size.width
        height = self.size.height
        text = Text(no_wrap=True, overflow="crop")
        if width < 3 or height < 1:
            return text

        bar_count = max(1, min(48, width // 4))
        bar_width = max(1, (width - (bar_count - 1)) // bar_count)
        bands = self._bands

        for row in range(height):
            if row:
                text.append("\n")
            row_bottom = (height - 1 - row) / height
            span = 1.0 / height
            color = _spectrum_color(t, row_bottom)

            used = 0
            for index in range(bar_count):
                level = _sample(bands, index, bar_count)
                fill = (level - row_bottom) / span
                block = _BLOCKS[int(round(max(0.0, min(1.0, fill)) * 8))]
                text.append(block * bar_width, style=color)
                used += bar_width
                if index < bar_count - 1:
                    text.append(" ")
                    used += 1
            if used < width:
                text.append(" " * (width - used))

        return text

    def refresh_theme(self) -> None:
        self.refresh()


class SeekBar(Static):
    """Clickable full-width Unicode progress bar."""

    elapsed: reactive[float] = reactive(0.0)
    total: reactive[float] = reactive(0.0)
    buffering: reactive[bool] = reactive(False)

    def render(self) -> Text:
        t = _palette(self)
        width = self.size.width
        text = Text(no_wrap=True, overflow="crop")
        if width <= 0:
            return text

        if self.buffering:
            text.append("─" * width, style=t.border)
            return text

        if self.total <= 0:
            if self.elapsed > 0:
                label = " STREAMING "
                pad = width - len(label)
                if pad < 0:
                    text.append("━" * width, style=t.primary)
                else:
                    left = pad // 2
                    text.append("━" * left, style=t.primary)
                    text.append(label, style=f"bold {t.background} on {t.primary}")
                    text.append("━" * (pad - left), style=t.primary)
            else:
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


class HelpBar(Static):
    """Key pills followed by dim labels, cliamp style."""

    def render(self) -> Text:
        t = _palette(self)
        text = Text(no_wrap=True, overflow="crop")
        key_fg = _contrast(t.accent)
        for index, (key, label) in enumerate(HELP_HINTS):
            if index:
                text.append("  ")
            text.append(f" {key} ", style=f"bold {key_fg} on {t.accent}")
            text.append(f" {label}", style=t.muted)
        return text

    def refresh_theme(self) -> None:
        self.refresh()


class PlayerBar(Widget):
    """Now playing, time + status, seek bar and the help bar."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._track: Optional[Track] = None
        self._quality: str = ""
        self._pos: float = 0.0
        self._dur: float = 0.0
        self._state: Tuple[bool, bool, bool] = (False, False, False)
        self.help_visible: bool = True

    def compose(self) -> ComposeResult:
        with Horizontal(id="player_row"):
            yield Static("", id="player_now")
            yield Static("", id="player_meta")
        yield SeekBar(id="player_seek")
        yield HelpBar(id="player_help")

    def on_mount(self) -> None:
        self._render_now()
        self._render_meta()

    def _render_now(self) -> None:
        t = _palette(self)
        try:
            now = self.query_one("#player_now", Static)
        except Exception:
            return

        if self._track is None:
            now.update(f"[{t.muted}]♫ nothing playing[/]")
            return

        now.update(
            f"[bold {t.accent}]♫ {self._track.title}[/]  "
            f"[{t.border}]·[/]  [{t.secondary}]{self._track.artist}[/]"
        )

    def _render_meta(self) -> None:
        t = _palette(self)
        try:
            meta = self.query_one("#player_meta", Static)
        except Exception:
            return

        playing, paused, buffering = self._state
        if buffering:
            glyph, label, color = "◌", "Buffering", t.warning
        elif paused:
            glyph, label, color = "⏸", "Paused", t.warning
        elif playing:
            glyph, label, color = "▶", "Playing", t.success
        else:
            glyph, label, color = "■", "Stopped", t.muted

        elapsed = format_seconds(self._pos)
        total = format_seconds(self._dur) if self._dur > 0 else "--:--"
        quality = f"  [{t.border}]{self._quality}[/]" if self._quality else ""

        meta.update(
            f"[bold {t.foreground}]{elapsed} / {total}[/]"
            f"{quality}   "
            f"[bold {color}]{glyph} {label}[/]"
        )

    def set_track(self, track: Optional[Track], quality: str = "") -> None:
        self._track = track
        if quality:
            self._quality = quality
        if track is None:
            self._quality = ""
        self._render_now()
        self._render_meta()

    def set_progress(self, pos: float, duration: float) -> None:
        self._pos = pos
        self._dur = duration
        try:
            bar = self.query_one("#player_seek", SeekBar)
            bar.elapsed = pos
            bar.total = duration
            bar.buffering = self._state[2]
        except Exception:
            pass
        self._render_meta()

    def set_state(
        self,
        is_playing: bool,
        is_paused: bool,
        is_buffering: bool,
    ) -> None:
        self._state = (is_playing, is_paused, is_buffering)
        try:
            self.query_one("#player_seek", SeekBar).buffering = is_buffering
        except Exception:
            pass
        self._render_now()
        self._render_meta()

    def toggle_help(self) -> bool:
        self.help_visible = not self.help_visible
        try:
            self.query_one("#player_help", HelpBar).display = self.help_visible
        except Exception:
            pass
        return self.help_visible

    def refresh_theme(self) -> None:
        self._render_now()
        self._render_meta()
        for selector in ("#player_seek", "#player_help"):
            try:
                self.query_one(selector).refresh()
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
        ("6", "library (signed in)"),
        ("g", "sign in / account"),
        ("h", "toggle the help bar"),
        ("v", "toggle the visualizer"),
        ("t", "sync Omarchy theme"),
        ("?", "toggle this help"),
        ("q", "quit"),
    ]

    def __init__(self, theme_name: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._theme_name = theme_name

    def compose(self) -> ComposeResult:
        t = _palette(self)
        rows = "\n".join(
            f"[bold {t.accent}]{key:<7}[/] [{t.foreground}]{desc}[/]"
            for key, desc in self.ROWS
        )
        with Container(id="help_panel"):
            title = "KEYBINDINGS"
            if self._theme_name:
                title = f"KEYBINDINGS  ·  {self._theme_name}"
            yield Static(f"[bold {t.accent}]{title}[/]", classes="help_title")
            yield Static(rows, classes="help_line")
            yield Static(f"[{t.muted}]press ? or esc to close[/]", classes="help_footer")
