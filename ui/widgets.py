"""
ui/widgets.py - Minimal, terminal-native chrome for ame.

The visual language borrows from cliamp: a letterspaced wordmark, bracketed
chips, section dividers, a block volume meter and a full-width seek bar.
"""

import random
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
    ("t", "trending", "view_trending"),
    ("r", "radio", "view_radio"),
    ("m", "moods", "view_moods"),
    ("s", "search", "view_search"),
    ("q", "queue", "view_queue"),
]


def _palette(widget: Widget) -> ThemeColors:
    if hasattr(widget.app, "theme_manager"):
        return widget.app.theme_manager.current_theme
    return ThemeColors()


def _volume_meter(volume: int, cells: int = 8) -> str:
    """Block volume meter, e.g. ██████░░."""
    filled = max(0, min(cells, int(round((volume / 100.0) * cells))))
    return "█" * filled + "░" * (cells - filled)


# Matrix digital rain: half-width katakana + digits (all single-cell glyphs).
_MATRIX_GLYPHS = (
    "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ"
    "0123456789"
)


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
            rest_style = t.foreground if active else t.muted
            text.append(label[0], style=f"bold {t.accent}")
            text.append(label[1:], style=rest_style)
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
    """cava-driven Matrix digital rain.

    Each terminal column is a falling stream of katakana/digit glyphs. The
    audio band for that column sets its fall speed and trail length, so loud
    frequencies streak faster and longer while quiet ones barely drip. The
    leading glyph glows near-white and the trail fades from the theme accent
    into the background. The widget is only shown while audio is playing.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._bands: List[float] = []
        self._columns: List[dict] = []
        self._width = 0
        self._height = 0
        self._rng = random.Random()

    def set_bands(self, bands: List[float]) -> None:
        self._bands = bands
        self._advance()
        self.refresh()

    def _matrix_colors(self) -> Tuple[str, str, str]:
        """Derive the rain palette from the active theme: (head, bright, dim)."""
        t = _palette(self)
        base = t.accent or t.primary or t.success
        head = _lerp_hex(base, "#ffffff", 0.72)
        dim = _lerp_hex(base, t.background, 0.88)
        return head, base, dim

    def _ensure_columns(self) -> None:
        width = max(0, self.size.width)
        height = max(1, self.size.height)
        if width == self._width and height == self._height and self._columns:
            return
        self._width = width
        self._height = height
        self._columns = [self._spawn_column(seeded=True) for _ in range(width)]

    def _spawn_column(self, seeded: bool = False) -> dict:
        height = self._height or 1
        head = self._rng.uniform(-height, height) if seeded else self._rng.uniform(-height, 0)
        trail = self._rng.randint(3, max(4, height))
        return {
            "head": head,
            "speed": self._rng.uniform(0.2, 0.5),
            "trail": trail,
            "chars": [self._rng.choice(_MATRIX_GLYPHS) for _ in range(height + trail + 4)],
        }

    def _advance(self) -> None:
        self._ensure_columns()
        height = self._height or 1
        count = len(self._columns)
        for index, column in enumerate(self._columns):
            amplitude = _sample(self._bands, index, count)
            column["speed"] = 0.15 + amplitude * 1.5
            column["trail"] = 3 + int(amplitude * max(3, height))
            column["head"] += column["speed"]

            chars = column["chars"]
            if chars and self._rng.random() < 0.2:
                chars[self._rng.randrange(len(chars))] = self._rng.choice(_MATRIX_GLYPHS)

            if column["head"] - column["trail"] > height:
                self._columns[index] = self._spawn_column()

    def render(self) -> Text:
        self._ensure_columns()
        width = self._width
        height = self._height
        text = Text(no_wrap=True, overflow="crop")
        if width < 1 or height < 1:
            return text

        head_color, bright, dim = self._matrix_colors()

        for row in range(height):
            if row:
                text.append("\n")
            for column in self._columns:
                distance = column["head"] - row
                trail = column["trail"]
                if distance < 0 or distance > trail:
                    text.append(" ")
                    continue

                chars = column["chars"]
                glyph = chars[row % len(chars)] if chars else " "
                if distance < 1:
                    color = head_color
                else:
                    color = _lerp_hex(bright, dim, distance / trail)
                text.append(glyph, style=color)

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


class PlayerBar(Widget):
    """Now playing, time + status and the seek bar."""

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
            yield Static("", id="player_meta")
        yield SeekBar(id="player_seek")

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

    def refresh_theme(self) -> None:
        self._render_now()
        self._render_meta()
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
        ("R", "start song radio"),
        ("d", "remove highlighted from queue"),
        ("S / c", "shuffle / clear queue"),
        ("/", "focus search"),
        ("t r m s q", "jump to view"),
        ("v", "toggle the visualizer"),
        ("T", "sync Omarchy theme"),
        ("?", "toggle this help"),
        ("Q", "quit"),
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
