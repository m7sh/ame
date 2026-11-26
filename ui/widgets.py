"""
ui/widgets.py - Minimal, terminal-native chrome for ame.

The visual language borrows from cliamp: a letterspaced wordmark, bracketed
chips, section dividers, a block volume meter and a full-width seek bar.
"""

import math
import random
from typing import Any, List, Optional, Tuple

from PIL import Image as PILImage
from rich.markup import escape
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

from api import Track, format_seconds
from artwork import (
    create_album_image_widget,
    create_mini_image_widget,
    image_backend_name,
    square_cell_dimensions,
)
from equalizer import get_eq_preset
from lyrics import LyricLine, LyricsResult, active_lyric_index, active_word_index
from song_info import SongInfo
from theme import ThemeColors


VIEWS: List[Tuple[str, str, str]] = [
    ("h", "home", "view_trending"),
    ("e", "explore", "view_explore"),
    ("s", "search", "view_search"),
    ("r", "radio", "view_radio"),
    ("F", "library", "view_favourites"),
    ("q", "queue", "view_queue"),
    ("w", "window", "view_now_playing"),
    ("Y", "stats", "view_stats"),
    (",", "prefs", "view_settings"),
]


def _chip_text(label: str, color: str, bracket_color: str, bold: bool = True) -> Text:
    """Build a bracketed chip as rich Text so brackets never collide with Rich markup."""
    out = Text(no_wrap=True)
    out.append("[", style=bracket_color)
    out.append(label, style=f"bold {color}" if bold else color)
    out.append("]", style=bracket_color)
    return out


def _palette(widget: Widget) -> ThemeColors:
    try:
        app = widget.app
        if hasattr(app, "theme_manager"):
            return app.theme_manager.current_theme
    except Exception:
        pass
    return ThemeColors()


def _volume_meter(volume: int, cells: int = 8) -> str:
    """Block volume meter, e.g. ██████░░."""
    filled = max(0, min(cells, int(round((volume / 100.0) * cells))))
    return "█" * filled + "░" * (cells - filled)


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
    speed: reactive[float] = reactive(1.0)
    sleep_remaining_s: reactive[int] = reactive(0)

    SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tab_spans: List[Tuple[int, int, str]] = []
        self._spinner_idx: int = 0

    def on_mount(self) -> None:
        self.set_interval(0.08, self._tick_spinner)

    def _tick_spinner(self) -> None:
        if self.status:
            self._spinner_idx += 1
            self.refresh()

    def render(self) -> Text:
        t = _palette(self)
        width = self.size.width
        text = Text(no_wrap=True, overflow="crop")
        self._tab_spans = []
        active_span: Optional[Tuple[int, int]] = None

        compact = 0 < width < 100
        tab_gap = " " if compact else "  "

        text.append(" " if compact else "  ")
        text.append("A M E", style=f"bold {t.accent}")
        text.append("  " if compact else "   ")

        for index, (key, label, view_id) in enumerate(VIEWS):
            if index:
                text.append(tab_gap)
            start = text.cell_len
            active = view_id == self.active_view_id
            if active:
                text.append("▸", style=f"bold {t.accent}")
            rest_style = f"bold {t.foreground}" if active else t.muted
            if label.lower().startswith(key.lower()):
                text.append(key[0], style=f"bold {t.accent}")
                text.append(label[1:], style=rest_style)
            else:
                text.append(key, style=f"bold {t.accent}")
                text.append(f" {label}", style=rest_style)
            end = text.cell_len
            self._tab_spans.append((start, end, view_id))
            if active:
                active_span = (start, end)

        if self.status:
            frame = self.SPINNER_FRAMES[self._spinner_idx % len(self.SPINNER_FRAMES)]
            text.append("  ·  ", style=t.border)
            text.append(f"{frame} ", style=f"bold {t.accent}")
            max_status = max(12, width - text.cell_len - 28) if width > 0 else len(self.status)
            status_str = (
                self.status if len(self.status) <= max_status else self.status[: max_status - 1] + "…"
            )
            text.append(status_str, style=t.foreground)

        def _build_right(include_theme: bool, include_help: bool, include_meter: bool) -> Text:
            r = Text(no_wrap=True)
            if include_theme and self.theme_name:
                r.append(f"{self.theme_name}  ", style=t.border)
            if self.sleep_remaining_s > 0:
                mins, secs = divmod(self.sleep_remaining_s, 60)
                r.append(f"[☾ {mins:02d}:{secs:02d}]  ", style=f"bold {t.warning}")
            if abs(self.speed - 1.0) >= 0.01:
                r.append(f"[{self.speed:.2g}×]  ", style=f"bold {t.primary}")
            if include_meter:
                r.append("VOL ", style=t.border)
                r.append(_volume_meter(self.volume), style=f"bold {t.success}")
                r.append(f" {self.volume}%", style=t.muted)
            else:
                r.append(f"VOL {self.volume}%", style=t.muted)
            if self.queue_len:
                r.append(f"  [Q{self.queue_len}]", style=f"bold {t.accent}")
            if include_help:
                r.append("  ? help", style=t.border)
            return r

        if width > 0:
            right = _build_right(
                include_theme=width >= 104 and bool(self.theme_name),
                include_help=True,
                include_meter=True,
            )
            if width - text.cell_len - right.cell_len < 1:
                right = _build_right(include_theme=False, include_help=True, include_meter=True)
            if width - text.cell_len - right.cell_len < 1:
                right = _build_right(include_theme=False, include_help=False, include_meter=False)
            padding = width - text.cell_len - right.cell_len
            if padding >= 1:
                text.append(" " * padding)
                text.append_text(right)
        else:
            right = _build_right(include_theme=False, include_help=True, include_meter=True)
            text.append("  ")
            text.append_text(right)

        # Row 2: full-width hairline divider with active tab highlight
        rule_w = max(width, text.cell_len, 24)
        text.append("\n")
        if active_span and active_span[1] <= rule_w:
            a_start, a_end = active_span
            if a_start > 0:
                text.append("─" * a_start, style=t.border)
            text.append("━" * (a_end - a_start), style=f"bold {t.accent}")
            if a_end < rule_w:
                text.append("─" * (rule_w - a_end), style=t.border)
        else:
            text.append("─" * rule_w, style=t.border)

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


class AnimatedLoader(Widget):
    """Clean, music-themed animated loading widget with equalizer waves and marquee."""

    DEFAULT_CSS = """
    AnimatedLoader {
        width: 100%;
        height: 1fr;
        content-align: center middle;
        background: ansi_default;
    }
    """

    SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    WAVE_CHARS = (" ", "▂", "▃", "▄", "▅", "▆", "▇", "█")

    title: reactive[str] = reactive("LOADING")
    subtitle: reactive[str] = reactive("")

    def __init__(self, title: str = "LOADING", subtitle: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.title = title
        self.subtitle = subtitle
        self._frame: int = 0

    def on_mount(self) -> None:
        self.set_interval(0.08, self._tick)

    def _tick(self) -> None:
        if self.display:
            self._frame += 1
            self.refresh()

    def set_message(self, title: str, subtitle: str = "") -> None:
        self.title = title
        self.subtitle = subtitle
        self.refresh()

    def render(self) -> Text:
        t = _palette(self)
        spin = self.SPINNER[self._frame % len(self.SPINNER)]
        h = self.size.height
        compact = h > 0 and h < 11

        text = Text(justify="center")
        if not compact:
            text.append("\n\n")
        else:
            text.append("\n")

        text.append(f"{spin}  ", style=f"bold {t.accent}")
        text.append(f"{self.title}\n", style=f"bold {t.foreground}")

        if self.subtitle:
            text.append(f"{self.subtitle}\n", style=t.muted)
            if not compact:
                text.append("\n")
        else:
            text.append("\n")

        num_bars = 22
        wave_text = Text()
        for i in range(num_bars):
            val = (math.sin(self._frame * 0.3 + i * 0.45) + 1) / 2
            char_idx = int(val * (len(self.WAVE_CHARS) - 1))
            char = self.WAVE_CHARS[char_idx]
            style = f"bold {t.accent}" if char_idx >= 4 else t.primary
            wave_text.append(char, style=style)
            wave_text.append(" ")

        text.append_text(wave_text)
        text.append("\n")

        if not compact:
            text.append("\n")
            bar_width = 24
            pos = int((math.sin(self._frame * 0.22) + 1) / 2 * (bar_width - 6))
            bar_str = "░" * pos + "■" * 6 + "░" * (bar_width - pos - 6)
            text.append("▸ ", style=t.border)
            text.append(f"[ {bar_str} ]", style=f"bold {t.accent}")
            text.append(" ◂\n", style=t.border)

        return text


class Spectrum(Static):
    """Sleek 1-row symmetrical Hi-Fi Graphic Equalizer above the player bar."""

    _BAR_LEVELS = " ▁▂▃▄▅▆▇█"
    _EQ_FRAMES = ("▁▅▃", "▃▂▆", "▅▇▂", "▂▆▄")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._bands: List[float] = []
        self._smoothed: List[float] = []

    def set_bands(self, bands: List[float]) -> None:
        self._bands = list(bands or [])
        self.refresh()

    def render(self) -> Text:
        t = _palette(self)
        width = max(0, self.size.width)
        text = Text(no_wrap=True, overflow="crop")
        if width < 4:
            return text

        # Airy 2-cell stride ("▄ ▆ ") keeps the bar minimal and crisp
        step = 2 if width >= 36 else 1
        bars = max(4, width // step)
        if len(self._smoothed) != bars:
            self._smoothed = [0.0] * bars

        half = bars // 2
        base_color = t.border
        mid_color = t.primary or t.secondary
        peak_color = t.accent or t.primary

        for i in range(bars):
            # Symmetrical mirror from center (bass/mids in center, highs out)
            mirror_dist = abs(i - half) / max(1, half)
            raw = _sample(self._bands, int(mirror_dist * max(1, bars - 1)), bars) if self._bands else 0.0
            amp = max(raw, self._smoothed[i] * 0.72)
            self._smoothed[i] = amp

            level = min(len(self._BAR_LEVELS) - 1, max(1, int(round(amp * (len(self._BAR_LEVELS) - 1)))))
            glyph = self._BAR_LEVELS[level]
            if amp < 0.35:
                color = _lerp_hex(base_color, mid_color, amp / 0.35)
            else:
                color = _lerp_hex(mid_color, peak_color, (amp - 0.35) / 0.65)
            text.append(glyph, style=f"bold {color}" if amp >= 0.65 else color)
            if step == 2 and i < bars - 1:
                text.append(" ")

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
            text.append("┄" * width, style=t.warning)
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
            fill_color = _lerp_hex(t.primary, t.accent, ratio)
            text.append("━" * filled, style=fill_color)
        text.append("◉", style=f"bold {t.accent}")
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


class MaximizeNowPlayingMsg(Message):
    """Posted when the user asks to open/maximize the Now Playing album window."""


class MinimizeNowPlayingMsg(Message):
    """Posted when the user asks to minimize the Now Playing album window."""


class PlayerControlMsg(Message):
    """Posted when a clickable playback control button is pressed."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action


class SeekToMsg(Message):
    """Posted when clicking a synced lyric line to jump playback."""

    def __init__(self, seconds: float) -> None:
        super().__init__()
        self.seconds = max(0.0, seconds)


class JumpQueueIndexMsg(Message):
    """Posted when clicking an item in the Now Playing Up Next side panel."""

    def __init__(self, index: int) -> None:
        super().__init__()
        self.index = index


class SidebarNavMsg(Message):
    """Posted when a sidebar navigation item is clicked."""

    def __init__(self, target: str) -> None:
        super().__init__()
        self.target = target


class SidebarItem(Static):
    """Clickable row inside the left navigation sidebar."""

    def __init__(self, key_hint: str, label: str, target: str, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.key_hint = key_hint
        self.item_label = label
        self.target = target

    def render_item(self, active: bool, t: ThemeColors) -> None:
        txt = Text(no_wrap=True, overflow="crop")
        txt.append("▌ " if active else "  ", style=f"bold {t.accent}" if active else t.border)
        txt.append(f"{self.key_hint} ", style=f"bold {t.accent}")
        txt.append(self.item_label, style=f"bold {t.foreground}" if active else t.muted)
        self.update(txt)

    def on_click(self, event: events.Click) -> None:
        self.post_message(SidebarNavMsg(self.target))
        event.stop()


class Sidebar(Widget):
    """Collapsible left navigation rail."""

    active_view_id: reactive[str] = reactive("view_trending")

    SECTIONS: List[Tuple[str, List[Tuple[str, str, str]]]] = [
        (
            "BROWSE",
            [
                ("h", "Home", "view_trending"),
                ("e", "New & Moods", "view_explore"),
                ("s", "Search", "view_search"),
                ("r", "Radio", "view_radio"),
            ],
        ),
        (
            "LIBRARY",
            [
                ("F", "Library", "view_favourites"),
                ("q", "Queue", "view_queue"),
                ("Y", "Replay", "view_stats"),
            ],
        ),
        (
            "PLAYER",
            [
                ("w", "Album Window", "view_now_playing"),
                (",", "Settings", "view_settings"),
                ("?", "Keybindings", "action_help"),
            ],
        ),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._now_title: str = ""
        self._eq_label: str = "Flat"
        self._xfade_s: int = 0
        self._skip_silence: bool = False

    def compose(self) -> ComposeResult:
        yield Static("", id="sidebar_brand")
        for sec_title, items in self.SECTIONS:
            yield Static(f"▸ {sec_title}", classes="sidebar_section")
            for key_hint, label, target in items:
                yield SidebarItem(key_hint, label, target, classes="sidebar_item")
        yield Static("", id="sidebar_spacer")
        yield Static("", id="sidebar_footer")

    def on_mount(self) -> None:
        self.refresh_theme()

    def watch_active_view_id(self, _value: str) -> None:
        self.refresh_theme()

    def set_playback_meta(
        self,
        track_title: str = "",
        eq_preset: str = "flat",
        crossfade_s: int = 0,
        skip_silence: bool = False,
    ) -> None:
        self._now_title = track_title
        self._eq_label = get_eq_preset(eq_preset).label
        self._xfade_s = int(crossfade_s or 0)
        self._skip_silence = bool(skip_silence)
        self._render_footer()

    def _render_footer(self) -> None:
        t = _palette(self)
        try:
            ftr = self.query_one("#sidebar_footer", Static)
        except Exception:
            return
        txt = Text(no_wrap=True, overflow="ellipsis")
        if self._now_title:
            txt.append("♫ ", style=f"bold {t.accent}")
            txt.append(self._now_title, style=t.foreground)
        else:
            txt.append("guest mode", style=t.muted)
        dsp_bits: List[str] = []
        if self._eq_label != "Flat":
            dsp_bits.append(f"EQ:{self._eq_label}")
        if self._xfade_s > 0:
            dsp_bits.append(f"XF:{self._xfade_s}s")
        if self._skip_silence:
            dsp_bits.append("SKIP")
        if dsp_bits:
            txt.append("\n" + " · ".join(dsp_bits), style=t.secondary)
        ftr.update(txt)

    def refresh_theme(self) -> None:
        t = _palette(self)
        try:
            brand = Text(no_wrap=True)
            brand.append("  A M E", style=f"bold {t.accent}")
            brand.append("  ·  雨", style=t.muted)
            self.query_one("#sidebar_brand", Static).update(brand)
            for hdr in self.query(".sidebar_section"):
                if isinstance(hdr, Static):
                    hdr.styles.color = t.border
            for item in self.query(SidebarItem):
                item.render_item(item.target == self.active_view_id, t)
            self._render_footer()
        except Exception:
            pass


def format_nerd_stats(
    stream_info: Any = None,
    quality: str = "",
    loudness_norm: bool = True,
) -> str:
    """Format 'Stats for nerds' readout for the active stream."""
    if stream_info is not None:
        parts: List[str] = []
        codec = getattr(stream_info, "codec", "") or ""
        if codec:
            parts.append(codec.upper())
        kbps = getattr(stream_info, "kbps", 0) or 0
        if kbps > 0:
            parts.append(f"{kbps} kbps")
        cached_path = getattr(stream_info, "cached_path", None)
        client_id = getattr(stream_info, "client_id", "") or ""
        if cached_path or client_id == "CACHE":
            parts.append("CACHED")
        elif client_id:
            parts.append(client_id)
        loudness_db = getattr(stream_info, "loudness_db", None)
        if loudness_norm and loudness_db is not None:
            gain = max(-12.0, min(6.0, -float(loudness_db)))
            parts.append(f"gain {gain:+.1f} dB")
        elif not loudness_norm:
            parts.append("norm off")
        if parts:
            return " · ".join(parts)
    return quality


class ControlChip(Static):
    """Small clickable terminal-native action chip."""

    def __init__(self, action: str, label: str = "", **kwargs) -> None:
        super().__init__(label, **kwargs)
        self.control_action = action

    def on_click(self, event: events.Click) -> None:
        self.post_message(PlayerControlMsg(self.control_action))
        event.stop()


class MiniArtBox(Widget):
    """Clickable 6x3 mini album avatar in the minimized PlayerBar."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._image_widget = create_mini_image_widget(id="mini_art_image")
        self._has_art = False

    def compose(self) -> ComposeResult:
        if self._image_widget is not None:
            yield self._image_widget
        yield Static("♫", id="mini_art_placeholder")

    def on_mount(self) -> None:
        self._sync_visibility()

    def set_artwork(self, img: Optional[PILImage.Image]) -> None:
        self._has_art = img is not None and self._image_widget is not None
        if self._image_widget is not None:
            try:
                self._image_widget.image = img
            except Exception:
                self._has_art = False
        self._sync_visibility()

    def _sync_visibility(self) -> None:
        try:
            if self._image_widget is not None:
                self._image_widget.display = self._has_art
            self.query_one("#mini_art_placeholder", Static).display = not self._has_art
        except Exception:
            pass

    def on_click(self, event: events.Click) -> None:
        self.post_message(MaximizeNowPlayingMsg())
        event.stop()


class PlayerBar(Widget):
    """Minimized bottom mini player with mini avatar, controls, and seek bar."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._track: Optional[Track] = None
        self._quality: str = ""
        self._pos: float = 0.0
        self._dur: float = 0.0
        self._state: Tuple[bool, bool, bool] = (False, False, False)
        self._loop_mode: str = "off"
        self._crossfade_seconds: int = 0
        self._loudness_norm: bool = True
        self._speed: float = 1.0
        self._eq_preset: str = "flat"
        self._skip_silence: bool = False
        self._sleep_remaining_s: int = 0
        self._is_fav: bool = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="player_bar_body"):
            yield MiniArtBox(id="mini_art_box")
            with Vertical(id="player_bar_main"):
                with Horizontal(id="player_row"):
                    yield Static("", id="player_now")
                    yield Static("", id="player_meta")
                with Horizontal(id="player_controls_row"):
                    yield ControlChip("prev", "", id="mini_btn_prev", classes="mini_btn")
                    yield ControlChip("toggle_play", "", id="mini_btn_play", classes="mini_btn")
                    yield ControlChip("next", "", id="mini_btn_next", classes="mini_btn")
                    yield ControlChip("fav", "", id="mini_btn_fav", classes="mini_btn")
                    yield Static("", id="player_controls_spacer")
                    yield ControlChip("maximize", "", id="mini_btn_expand", classes="mini_btn")
                yield SeekBar(id="player_seek")

    def on_mount(self) -> None:
        self._render_now()
        self._render_meta()
        self._render_controls()

    def on_click(self, event: events.Click) -> None:
        # Clicking the track title/meta area maximizes the album window.
        if event.y == 0 and self._track is not None:
            self.post_message(MaximizeNowPlayingMsg())
            event.stop()

    def _render_now(self) -> None:
        t = _palette(self)
        try:
            now = self.query_one("#player_now", Static)
        except Exception:
            return

        if self._track is None:
            now.update(f"[{t.muted}]♫ nothing playing[/]")
            return

        title = escape(self._track.title)
        artist = escape(self._track.artist)
        now.update(
            f"[bold {t.accent}]♫ {title}[/]  "
            f"[{t.border}]·[/]  [{t.secondary}]{artist}[/]"
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
            eq = Spectrum._EQ_FRAMES[int(self._pos * 4) % len(Spectrum._EQ_FRAMES)]
            glyph, label, color = eq, "Playing", t.success
        else:
            glyph, label, color = "■", "Stopped", t.muted

        elapsed = format_seconds(self._pos)
        total = format_seconds(self._dur) if self._dur > 0 else "--:--"

        out = Text(no_wrap=True)
        out.append(f"{elapsed} / {total}", style=f"bold {t.foreground}")
        if self._quality:
            out.append(f"  {self._quality}", style=t.border)
        out.append(f"   {glyph} {label}", style=f"bold {color}")

        if self._loop_mode == "loop_one":
            out.append("  ")
            out.append_text(_chip_text("↻ LOOP", t.accent, t.accent))
        elif self._loop_mode == "stop_after_one":
            out.append("  ")
            out.append_text(_chip_text("■ STOP", t.warning, t.warning))
        elif self._loop_mode == "off":
            out.append("  ")
            out.append_text(_chip_text("→ OFF", t.muted, t.muted, bold=False))

        if self._crossfade_seconds > 0:
            out.append("  ")
            out.append_text(_chip_text(f"XF {self._crossfade_seconds}s", t.primary, t.primary))

        if abs(self._speed - 1.0) >= 0.01:
            out.append("  ")
            out.append_text(_chip_text(f"{self._speed:.2g}×", t.primary, t.primary))

        if self._eq_preset != "flat":
            out.append("  ")
            out.append_text(_chip_text(f"EQ {get_eq_preset(self._eq_preset).label}", t.secondary, t.secondary))

        if self._skip_silence:
            out.append("  ")
            out.append_text(_chip_text("SKIP", t.success, t.success))

        if self._sleep_remaining_s > 0:
            mins, secs = divmod(self._sleep_remaining_s, 60)
            out.append("  ")
            out.append_text(_chip_text(f"☾ {mins:02d}:{secs:02d}", t.warning, t.warning))

        meta.update(out)

    def _render_controls(self) -> None:
        t = _palette(self)
        playing, paused, buffering = self._state
        play_glyph = "▶" if (paused or not playing) else "⏸"
        if buffering and not playing:
            play_glyph = "◌"
        fav_txt = Text("♥", style=f"bold {t.accent}") if self._is_fav else Text("♡", style=t.muted)
        try:
            self.query_one("#mini_btn_prev", ControlChip).update(Text("⏮", style=t.foreground))
            self.query_one("#mini_btn_play", ControlChip).update(Text(play_glyph, style=f"bold {t.accent}"))
            self.query_one("#mini_btn_next", ControlChip).update(Text("⏭", style=t.foreground))
            self.query_one("#mini_btn_fav", ControlChip).update(fav_txt)
            self.query_one("#mini_btn_expand", ControlChip).update(
                _chip_text("+ album", t.accent, t.muted)
            )
        except Exception:
            pass

    def set_track(self, track: Optional[Track], quality: str = "") -> None:
        self._track = track
        if quality:
            self._quality = quality
        if track is None:
            self._quality = ""
            self.set_artwork(None)
        self._render_now()
        self._render_meta()
        self._render_controls()

    def set_artwork(self, img: Optional[PILImage.Image]) -> None:
        try:
            self.query_one("#mini_art_box", MiniArtBox).set_artwork(img)
        except Exception:
            pass

    def set_favourite(self, is_fav: bool) -> None:
        self._is_fav = bool(is_fav)
        self._render_controls()

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

    def set_loop_mode(self, mode: str) -> None:
        self._loop_mode = mode
        self._render_meta()

    def set_crossfade(self, seconds: int) -> None:
        self._crossfade_seconds = max(0, int(seconds))
        self._render_meta()

    def set_loudness(self, enabled: bool) -> None:
        self._loudness_norm = bool(enabled)
        self._render_meta()

    def set_speed(self, speed: float) -> None:
        self._speed = float(speed)
        self._render_meta()

    def set_eq_preset(self, preset_id: str) -> None:
        self._eq_preset = preset_id or "flat"
        self._render_meta()

    def set_skip_silence(self, enabled: bool) -> None:
        self._skip_silence = bool(enabled)
        self._render_meta()

    def set_sleep_remaining(self, seconds: int) -> None:
        self._sleep_remaining_s = max(0, int(seconds))
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
        self._render_controls()

    def refresh_theme(self) -> None:
        self._render_now()
        self._render_meta()
        self._render_controls()
        try:
            self.query_one("#player_seek", SeekBar).refresh()
        except Exception:
            pass


class LyricLineItem(Static):
    """Clickable lyric line inside the Now Playing side panel."""

    def __init__(self, line: LyricLine, index: int, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.lyric_line = line
        self.line_index = index

    def on_click(self, event: events.Click) -> None:
        if self.lyric_line.time_ms is not None:
            self.post_message(SeekToMsg(self.lyric_line.time_ms / 1000.0))
            event.stop()


class UpNextItem(Static):
    """Clickable queued track row inside the Now Playing side panel."""

    def __init__(self, index: int, track: Track, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.queue_index = index
        self.track = track

    def on_click(self, event: events.Click) -> None:
        self.post_message(JumpQueueIndexMsg(self.queue_index))
        event.stop()


class NowPlayingView(Widget):
    """Maximized album window with split lyrics/up-next & playback controls."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._track: Optional[Track] = None
        self._stream_info: Any = None
        self._quality: str = ""
        self._pos: float = 0.0
        self._dur: float = 0.0
        self._state: Tuple[bool, bool, bool] = (False, False, False)
        self._loop_mode: str = "off"
        self._crossfade_seconds: int = 0
        self._loudness_norm: bool = True
        self._speed: float = 1.0
        self._eq_preset: str = "flat"
        self._skip_silence: bool = False
        self._sleep_remaining_s: int = 0
        self._dynamic_tint: bool = True
        self._show_nerd_stats: bool = True
        self._is_fav: bool = False
        self._queue: List[Track] = []
        self._next_track: Optional[Track] = None
        self._queue_count: int = 0
        self._artwork: Optional[PILImage.Image] = None
        self._art_widget = create_album_image_widget(id="np_art_image")
        # Default to split view with Lyrics open on the right.
        self._side_panel: Optional[str] = "lyrics"
        self._user_side_preference: Optional[str] = "lyrics"
        self._lyrics: LyricsResult = LyricsResult()
        self._lyrics_loading: bool = False
        self._lyrics_offset_ms: int = 0
        self._active_lyric_idx: int = -1
        self._active_word_idx: int = -1

    def compose(self) -> ComposeResult:
        with Vertical(id="np_root"):
            with Horizontal(id="np_header_row"):
                yield SectionHeader(id="np_header", classes="section_header")
                yield ControlChip("minimize", "", id="np_minimize_btn")
            with Horizontal(id="np_body"):
                with Vertical(id="np_left_col"):
                    with Center(id="np_art_stage"):
                        with Container(id="np_art_box"):
                            if self._art_widget is not None:
                                yield self._art_widget
                            yield Static("", id="np_art_placeholder")
                    with Vertical(id="np_footer"):
                        yield Static("", id="np_title")
                        yield Static("", id="np_subtitle")
                        with Horizontal(id="np_links_row"):
                            yield ControlChip("open_artist", "", id="np_artist_btn", classes="np_link_btn")
                            yield ControlChip("open_album", "", id="np_album_btn", classes="np_link_btn")
                            yield ControlChip("song_info", "", id="np_info_btn", classes="np_link_btn")
                        yield Static("", id="np_nerd_stats")
                        with Horizontal(id="np_time_row"):
                            yield Static("00:00", id="np_elapsed")
                            yield Static("", id="np_status_center")
                            yield Static("--:--", id="np_total")
                        yield SeekBar(id="np_seek")
                        with Horizontal(id="np_controls"):
                            yield ControlChip("fav", "", id="np_btn_fav", classes="np_btn")
                            yield ControlChip("prev", "", id="np_btn_prev", classes="np_btn")
                            yield ControlChip("toggle_play", "", id="np_btn_play", classes="np_btn np_btn_primary")
                            yield ControlChip("next", "", id="np_btn_next", classes="np_btn")
                            yield ControlChip("loop", "", id="np_btn_loop", classes="np_btn")
                        with Horizontal(id="np_controls_secondary"):
                            yield ControlChip("crossfade", "", id="np_btn_xfade", classes="np_btn")
                            yield ControlChip("loudness", "", id="np_btn_norm", classes="np_btn")
                            yield ControlChip("speed", "", id="np_btn_speed", classes="np_btn")
                            yield ControlChip("eq", "", id="np_btn_eq", classes="np_btn")
                            yield ControlChip("skip_silence", "", id="np_btn_skip", classes="np_btn")
                            yield ControlChip("sleep", "", id="np_btn_sleep", classes="np_btn")
                            yield ControlChip("toggle_lyrics", "", id="np_btn_lyrics", classes="np_btn")
                            yield ControlChip("toggle_queue", "", id="np_btn_queue", classes="np_btn")
                            yield ControlChip("toggle_tint", "", id="np_btn_tint", classes="np_btn")
                        yield ControlChip("open_queue", "", id="np_up_next")
                with Vertical(id="np_side_panel"):
                    with Horizontal(id="np_side_header_row"):
                        yield SectionHeader(id="np_side_header", classes="section_header")
                        yield ControlChip("close_side", "", id="np_side_close_btn")
                    yield VerticalScroll(id="np_lyrics_scroll")
                    yield VerticalScroll(id="np_queue_scroll")

    def on_mount(self) -> None:
        self._sync_side_panel_visibility()
        self._sync_art_visibility()
        self._resize_art_box()
        self._render_all()

    def on_resize(self, event: events.Resize) -> None:
        self._sync_side_panel_visibility()
        self._resize_art_box()

    @property
    def side_panel_mode(self) -> Optional[str]:
        return self._side_panel

    def toggle_side_panel(self, mode: str) -> Optional[str]:
        """Toggle right-hand panel between None, 'lyrics', and 'queue'."""
        self._side_panel = None if self._side_panel == mode else mode
        self._user_side_preference = self._side_panel
        self._sync_side_panel_visibility()
        self._resize_art_box()
        self._render_controls()
        self._render_side_panel()
        return self._side_panel

    def close_side_panel(self) -> None:
        if self._side_panel is not None:
            self._side_panel = None
            self._user_side_preference = None
            self._sync_side_panel_visibility()
            self._resize_art_box()
            self._render_controls()

    def _sync_side_panel_visibility(self) -> None:
        narrow = self.size.width > 0 and self.size.width < 76
        show_side = self._side_panel in ("lyrics", "queue")
        try:
            side = self.query_one("#np_side_panel", Vertical)
            left = self.query_one("#np_left_col", Vertical)
            stage = self.query_one("#np_art_stage", Center)
            side.display = show_side
            # On narrow terminals, hide the big cover when reading lyrics/queue.
            stage.display = not (show_side and narrow)
            if show_side and narrow:
                left.styles.height = "auto"
                left.styles.width = "100%"
                side.styles.width = "100%"
                side.styles.height = "1fr"
                side.styles.border_left = ("none", "transparent")
            elif show_side:
                left.styles.height = "100%"
                left.styles.width = "52%"
                side.styles.width = "48%"
                side.styles.height = "100%"
                side.styles.border_left = ("tall", _palette(self).border)
            else:
                left.styles.height = "100%"
                left.styles.width = "100%"
                side.styles.border_left = ("none", "transparent")
            self.query_one("#np_lyrics_scroll", VerticalScroll).display = self._side_panel == "lyrics"
            self.query_one("#np_queue_scroll", VerticalScroll).display = self._side_panel == "queue"
        except Exception:
            pass

    def _resize_art_box(self) -> None:
        """Keep the album avatar visually square and always leave room for controls below."""
        try:
            box = self.query_one("#np_art_box", Container)
            left = self.query_one("#np_left_col", Vertical)
        except Exception:
            return

        total_w = self.size.width
        total_h = self.size.height
        if total_w <= 0 or total_h <= 0:
            return

        avail_w = left.size.width if left.size.width > 0 else (
            int(total_w * 0.5) if self._side_panel and total_w >= 76 else total_w
        )
        reserved_rows = 14
        max_rows = max(4, min(36, total_h - reserved_rows))
        max_cols = max(8, min(88, avail_w - 4))
        cols, rows = square_cell_dimensions(max_cols, max_rows)
        if cols > 0 and rows > 0:
            box.styles.width = cols
            box.styles.height = rows
            if self._art_widget is not None:
                self._art_widget.styles.width = cols
                self._art_widget.styles.height = rows

    def _sync_art_visibility(self) -> None:
        has_img = self._artwork is not None and self._art_widget is not None
        try:
            if self._art_widget is not None:
                self._art_widget.display = has_img
            self.query_one("#np_art_placeholder", Static).display = not has_img
        except Exception:
            pass
        self._render_placeholder()

    def _render_placeholder(self) -> None:
        t = _palette(self)
        try:
            ph = self.query_one("#np_art_placeholder", Static)
        except Exception:
            return
        if self._track is None:
            ph.update(f"[{t.muted}]\n♫\nno track[/]")
        elif self._track.thumbnail:
            ph.update(f"[bold {t.accent}]\n♫\n[{t.muted}]loading cover…[/][/]")
        else:
            ph.update(f"[bold {t.accent}]\n♫\n[{t.muted}]no cover[/][/]")

    def _render_header(self) -> None:
        t = _palette(self)
        backend = image_backend_name()
        chips = [("album window", True)]
        if backend != "placeholder":
            chips.append((backend, False))
        try:
            self.query_one("#np_header", SectionHeader).set_content("now playing", chips)
            self.query_one("#np_minimize_btn", ControlChip).update(
                _chip_text("− minimize", t.accent, t.muted)
            )
        except Exception:
            pass

    def _render_track_info(self) -> None:
        t = _palette(self)
        try:
            title_w = self.query_one("#np_title", Static)
            sub_w = self.query_one("#np_subtitle", Static)
            links_row = self.query_one("#np_links_row", Horizontal)
            artist_btn = self.query_one("#np_artist_btn", ControlChip)
            album_btn = self.query_one("#np_album_btn", ControlChip)
            info_btn = self.query_one("#np_info_btn", ControlChip)
            nerd_w = self.query_one("#np_nerd_stats", Static)
        except Exception:
            return

        if self._track is None:
            title_w.update(Text("Nothing Playing", style=t.muted))
            sub_w.update(Text("Pick a track from Home, Explore, Search, or Queue", style=t.border))
            links_row.display = False
            nerd_w.update("")
            return

        title_txt = Text(no_wrap=True, overflow="ellipsis")
        title_txt.append(self._track.title, style=f"bold {t.foreground}")
        if self._is_fav:
            title_txt.append("  ♥", style=f"bold {t.accent}")
        title_w.update(title_txt)

        sub_txt = Text(no_wrap=True, overflow="ellipsis")
        sub_txt.append(self._track.artist, style=t.secondary)
        if self._track.album:
            sub_txt.append("  ·  ", style=t.border)
            sub_txt.append(self._track.album, style=t.muted)
        sub_w.update(sub_txt)

        has_artist = bool(self._track.artist_id)
        has_album = bool(self._track.album_id)
        links_row.display = True
        artist_btn.display = has_artist
        album_btn.display = has_album
        info_btn.display = True
        if has_artist:
            artist_btn.update(_chip_text("g artist", t.primary, t.border, bold=False))
        if has_album:
            album_btn.update(_chip_text("G album", t.primary, t.border, bold=False))
        info_btn.update(_chip_text("I info", t.secondary, t.border, bold=False))

        if self._show_nerd_stats:
            stats = format_nerd_stats(self._stream_info, self._quality, self._loudness_norm)
            nerd_w.display = bool(stats)
            nerd_w.update(Text(stats, style=t.muted) if stats else "")
        else:
            nerd_w.display = False

    def _render_progress(self) -> None:
        t = _palette(self)
        playing, paused, buffering = self._state
        if buffering:
            glyph, label, color = "◌", "Buffering", t.warning
        elif paused:
            glyph, label, color = "⏸", "Paused", t.warning
        elif playing:
            eq = Spectrum._EQ_FRAMES[int(self._pos * 4) % len(Spectrum._EQ_FRAMES)]
            glyph, label, color = eq, "Playing", t.success
        else:
            glyph, label, color = "■", "Stopped", t.muted

        elapsed = format_seconds(self._pos)
        total = format_seconds(self._dur) if self._dur > 0 else "--:--"
        try:
            self.query_one("#np_elapsed", Static).update(Text(elapsed, style=f"bold {t.foreground}"))
            self.query_one("#np_status_center", Static).update(Text(f"{glyph} {label}", style=f"bold {color}"))
            self.query_one("#np_total", Static).update(Text(total, style=t.muted))
            bar = self.query_one("#np_seek", SeekBar)
            bar.elapsed = self._pos
            bar.total = self._dur
            bar.buffering = buffering
        except Exception:
            pass

    def _render_controls(self) -> None:
        t = _palette(self)
        playing, paused, buffering = self._state
        play_label = "▶ play" if (paused or not playing) else "⏸ pause"
        if buffering and not playing:
            play_label = "◌ loading"

        fav_chip = _chip_text("♥ fav", t.accent, t.accent) if self._is_fav else _chip_text("♡ fav", t.muted, t.border, bold=False)
        if self._loop_mode == "loop_one":
            loop_chip = _chip_text("↻ loop 1", t.accent, t.accent)
        elif self._loop_mode == "stop_after_one":
            loop_chip = _chip_text("■ stop 1", t.warning, t.warning)
        else:
            loop_chip = _chip_text("→ flow", t.muted, t.border, bold=False)

        xfade_chip = (
            _chip_text(f"XF {self._crossfade_seconds}s", t.primary, t.primary)
            if self._crossfade_seconds > 0
            else _chip_text("XF off", t.muted, t.border, bold=False)
        )
        norm_chip = (
            _chip_text("NORM", t.success, t.success)
            if self._loudness_norm
            else _chip_text("NORM off", t.muted, t.border, bold=False)
        )
        speed_chip = (
            _chip_text(f"{self._speed:.2g}×", t.primary, t.primary)
            if abs(self._speed - 1.0) >= 0.01
            else _chip_text("1×", t.muted, t.border, bold=False)
        )
        eq_label = get_eq_preset(self._eq_preset).label
        eq_chip = (
            _chip_text(f"EQ {eq_label}", t.secondary, t.secondary)
            if self._eq_preset != "flat"
            else _chip_text("EQ flat", t.muted, t.border, bold=False)
        )
        skip_chip = (
            _chip_text("SKIP", t.success, t.success)
            if self._skip_silence
            else _chip_text("skip", t.muted, t.border, bold=False)
        )
        if self._sleep_remaining_s > 0:
            mins, secs = divmod(self._sleep_remaining_s, 60)
            sleep_chip = _chip_text(f"☾ {mins:02d}:{secs:02d}", t.warning, t.warning)
        else:
            sleep_chip = _chip_text("☾ sleep", t.muted, t.border, bold=False)

        lyrics_chip = (
            _chip_text("L lyrics", t.accent, t.accent)
            if self._side_panel == "lyrics"
            else _chip_text("L lyrics", t.muted, t.border, bold=False)
        )
        queue_chip = (
            _chip_text("O queue", t.accent, t.accent)
            if self._side_panel == "queue"
            else _chip_text("O queue", t.muted, t.border, bold=False)
        )
        tint_chip = (
            _chip_text("D tint", t.secondary, t.secondary)
            if self._dynamic_tint
            else _chip_text("D tint", t.muted, t.border, bold=False)
        )

        try:
            self.query_one("#np_btn_fav", ControlChip).update(fav_chip)
            self.query_one("#np_btn_prev", ControlChip).update(_chip_text("⏮ prev", t.foreground, t.border, bold=False))
            self.query_one("#np_btn_play", ControlChip).update(_chip_text(play_label, t.accent, t.accent))
            self.query_one("#np_btn_next", ControlChip).update(_chip_text("⏭ next", t.foreground, t.border, bold=False))
            self.query_one("#np_btn_loop", ControlChip).update(loop_chip)
            self.query_one("#np_btn_xfade", ControlChip).update(xfade_chip)
            self.query_one("#np_btn_norm", ControlChip).update(norm_chip)
            self.query_one("#np_btn_speed", ControlChip).update(speed_chip)
            self.query_one("#np_btn_eq", ControlChip).update(eq_chip)
            self.query_one("#np_btn_skip", ControlChip).update(skip_chip)
            self.query_one("#np_btn_sleep", ControlChip).update(sleep_chip)
            self.query_one("#np_btn_lyrics", ControlChip).update(lyrics_chip)
            self.query_one("#np_btn_queue", ControlChip).update(queue_chip)
            self.query_one("#np_btn_tint", ControlChip).update(tint_chip)
        except Exception:
            pass

    def _render_up_next(self) -> None:
        t = _palette(self)
        try:
            up_next = self.query_one("#np_up_next", ControlChip)
        except Exception:
            return
        if self._next_track is None:
            up_next.update(Text("up next · autoplay when queue ends", style=t.border))
            return
        out = Text(no_wrap=True, overflow="ellipsis")
        out.append("up next · ", style=t.muted)
        out.append(self._next_track.title, style=t.foreground)
        out.append(" — ", style=t.border)
        out.append(self._next_track.artist, style=t.secondary)
        if self._queue_count > 1:
            out.append(f"  (+{self._queue_count - 1} more)", style=t.muted)
        up_next.update(out)

    def _render_side_panel(self) -> None:
        t = _palette(self)
        if self._side_panel is None:
            return
        try:
            header = self.query_one("#np_side_header", SectionHeader)
            self.query_one("#np_side_close_btn", ControlChip).update(
                _chip_text("×", t.muted, t.border, bold=False)
            )
        except Exception:
            return

        if self._side_panel == "lyrics":
            chips: List[Tuple[str, bool]] = []
            if self._lyrics_loading:
                chips.append(("loading…", True))
            elif self._lyrics.source:
                chips.append((self._lyrics.source, True))
                sync_label = (
                    "word sync"
                    if self._lyrics.word_synced
                    else ("synced" if self._lyrics.synced else "plain")
                )
                chips.append((sync_label, False))
                if self._lyrics_offset_ms != 0:
                    chips.append((f"{self._lyrics_offset_ms / 1000:+.2f}s", True))
            header.set_content("lyrics", chips)
            self._rebuild_lyrics_items()
        elif self._side_panel == "queue":
            header.set_content(
                "up next",
                [(f"{self._queue_count} track" + ("s" if self._queue_count != 1 else ""), True)],
            )
            self._rebuild_queue_items()

    def _rebuild_lyrics_items(self) -> None:
        t = _palette(self)
        try:
            scroll = self.query_one("#np_lyrics_scroll", VerticalScroll)
        except Exception:
            return
        scroll.remove_children()

        if self._lyrics_loading:
            scroll.mount(Static(f"\n[{t.muted}]fetching lyrics…[/]", classes="lyric_empty"))
            return
        if not self._lyrics.lines:
            scroll.mount(Static(f"\n[{t.muted}]no lyrics found for this track[/]", classes="lyric_empty"))
            return

        widgets = [LyricLineItem(line, idx, classes="lyric_line") for idx, line in enumerate(self._lyrics.lines)]
        scroll.mount_all(widgets)
        self._style_lyric_lines(scroll_to_active=True)

    def _style_lyric_lines(self, scroll_to_active: bool = False) -> None:
        if self._side_panel != "lyrics":
            return
        t = _palette(self)
        try:
            scroll = self.query_one("#np_lyrics_scroll", VerticalScroll)
            items = list(scroll.query(LyricLineItem))
        except Exception:
            return

        near_style = _lerp_hex(t.muted, t.foreground, 0.72)
        mid_style = _lerp_hex(t.muted, t.foreground, 0.32)
        far_style = t.muted

        active_widget: Optional[LyricLineItem] = None
        for item in items:
            idx = item.line_index
            line = item.lyric_line
            txt = escape(line.text)
            if not self._lyrics.synced:
                item.update(f"[{t.foreground}]{txt}[/]")
            elif idx == self._active_lyric_idx:
                if line.words:
                    rich_line = Text()
                    rich_line.append("▸ ", style=f"bold {t.accent}")
                    for wi, word in enumerate(line.words):
                        if wi:
                            rich_line.append(" ")
                        if wi < self._active_word_idx:
                            style = f"bold {t.accent}"
                        elif wi == self._active_word_idx:
                            style = f"bold reverse {t.accent}"
                        else:
                            style = f"bold {t.foreground}"
                        rich_line.append(word.text, style=style)
                    item.update(rich_line)
                elif line.is_gap:
                    nxt_t = (
                        self._lyrics.lines[idx + 1].time_ms
                        if idx + 1 < len(self._lyrics.lines)
                        else None
                    )
                    cur_ms = int(max(0.0, self._pos) * 1000) + self._lyrics_offset_ms
                    start_ms = line.time_ms or 0
                    if nxt_t and nxt_t > start_ms:
                        frac = max(0.0, min(1.0, (cur_ms - start_ms) / (nxt_t - start_ms)))
                    else:
                        frac = 0.5
                    filled = 1 if frac < 0.34 else (2 if frac < 0.68 else 3)
                    dots = " ".join("◉" if d < filled else "○" for d in range(3))
                    item.update(f"[bold {t.accent}]  {dots}[/]")
                else:
                    item.update(f"[bold {t.accent}]▸ {txt}[/]")
                active_widget = item
            elif self._active_lyric_idx >= 0:
                dist = abs(idx - self._active_lyric_idx)
                if line.is_gap:
                    item.update(f"[{far_style}]  ···[/]")
                elif dist == 1:
                    item.update(f"[{near_style}]  {txt}[/]")
                elif dist == 2:
                    item.update(f"[{mid_style}]  {txt}[/]")
                else:
                    item.update(f"[{far_style}]  {txt}[/]")
            else:
                item.update(f"[{far_style}]  {txt}[/]")

        if scroll_to_active and active_widget is not None:
            try:
                scroll.scroll_to_widget(active_widget, animate=False, top=False)
            except Exception:
                pass

    def _rebuild_queue_items(self) -> None:
        t = _palette(self)
        try:
            scroll = self.query_one("#np_queue_scroll", VerticalScroll)
        except Exception:
            return
        scroll.remove_children()
        if not self._queue:
            scroll.mount(Static(f"\n[{t.muted}]queue is empty · autoplay active[/]", classes="lyric_empty"))
            return

        widgets: List[Widget] = []
        for idx, tr in enumerate(self._queue):
            row = UpNextItem(idx, tr, classes="up_next_row")
            prefix = f"[bold {t.accent}]▸ NEXT[/]" if idx == 0 else f"[{t.accent}]{idx + 1:>2}.[/]"
            row.update(
                f"{prefix} "
                f"[{'bold ' if idx == 0 else ''}{t.foreground}]{escape(tr.title)}[/] "
                f"[{t.border}]—[/] [{t.secondary}]{escape(tr.artist)}[/] "
                f"[{t.muted}]({tr.display_duration})[/]"
            )
            widgets.append(row)
        scroll.mount_all(widgets)

    def _render_all(self) -> None:
        self._render_header()
        self._render_placeholder()
        self._render_track_info()
        self._render_progress()
        self._render_controls()
        self._render_up_next()
        self._render_side_panel()

    def set_track(
        self,
        track: Optional[Track],
        quality: str = "",
        stream_info: Any = None,
    ) -> None:
        self._track = track
        if quality:
            self._quality = quality
        if track is None:
            self._quality = ""
            self._stream_info = None
            self._lyrics = LyricsResult()
            self._lyrics_offset_ms = 0
            self._active_lyric_idx = -1
            self._active_word_idx = -1
            self.set_artwork(None)
        elif stream_info is not None:
            self._stream_info = stream_info
        self._render_track_info()
        self._render_placeholder()
        if self._side_panel == "lyrics":
            self._render_side_panel()

    def set_lyrics(self, lyrics: LyricsResult, loading: bool = False, offset_ms: int = 0) -> None:
        self._lyrics = lyrics
        self._lyrics_loading = loading
        self._lyrics_offset_ms = int(offset_ms)
        self._active_lyric_idx = (
            active_lyric_index(lyrics.lines, self._pos, offset_ms=self._lyrics_offset_ms)
            if lyrics.synced
            else -1
        )
        self._active_word_idx = (
            active_word_index(
                lyrics.lines[self._active_lyric_idx],
                self._pos,
                offset_ms=self._lyrics_offset_ms,
            )
            if self._active_lyric_idx >= 0
            else -1
        )
        if self._side_panel == "lyrics":
            self._render_side_panel()

    def set_lyrics_loading(self, loading: bool) -> None:
        self._lyrics_loading = loading
        if loading:
            self._lyrics = LyricsResult()
            self._active_lyric_idx = -1
            self._active_word_idx = -1
        if self._side_panel == "lyrics":
            self._render_side_panel()

    def set_lyrics_offset(self, offset_ms: int) -> None:
        self._lyrics_offset_ms = int(offset_ms)
        if self._lyrics.synced and self._lyrics.lines:
            self._active_lyric_idx = active_lyric_index(
                self._lyrics.lines, self._pos, offset_ms=self._lyrics_offset_ms
            )
            self._active_word_idx = (
                active_word_index(
                    self._lyrics.lines[self._active_lyric_idx],
                    self._pos,
                    offset_ms=self._lyrics_offset_ms,
                )
                if self._active_lyric_idx >= 0
                else -1
            )
        if self._side_panel == "lyrics":
            self._render_side_panel()

    def set_stream_info(self, stream_info: Any, quality: str = "") -> None:
        self._stream_info = stream_info
        if quality:
            self._quality = quality
        self._render_track_info()

    def set_artwork(self, img: Optional[PILImage.Image]) -> None:
        self._artwork = img
        if self._art_widget is not None:
            try:
                self._art_widget.image = img
            except Exception:
                self._artwork = None
        self._sync_art_visibility()
        self._resize_art_box()

    def set_progress(self, pos: float, duration: float) -> None:
        self._pos = pos
        self._dur = duration
        self._render_progress()
        if self._lyrics.synced and self._lyrics.lines:
            new_idx = active_lyric_index(
                self._lyrics.lines, pos, offset_ms=self._lyrics_offset_ms
            )
            new_word = (
                active_word_index(
                    self._lyrics.lines[new_idx], pos, offset_ms=self._lyrics_offset_ms
                )
                if new_idx >= 0
                else -1
            )
            if new_idx != self._active_lyric_idx:
                self._active_lyric_idx = new_idx
                self._active_word_idx = new_word
                self._style_lyric_lines(scroll_to_active=True)
            elif new_word != self._active_word_idx:
                self._active_word_idx = new_word
                self._style_lyric_lines(scroll_to_active=False)

    def set_state(self, is_playing: bool, is_paused: bool, is_buffering: bool) -> None:
        self._state = (is_playing, is_paused, is_buffering)
        self._render_progress()
        self._render_controls()

    def set_favourite(self, is_fav: bool) -> None:
        self._is_fav = bool(is_fav)
        self._render_track_info()
        self._render_controls()

    def set_loop_mode(self, mode: str) -> None:
        self._loop_mode = mode
        self._render_controls()

    def set_crossfade(self, seconds: int) -> None:
        self._crossfade_seconds = max(0, int(seconds))
        self._render_controls()

    def set_loudness(self, enabled: bool) -> None:
        self._loudness_norm = bool(enabled)
        self._render_track_info()
        self._render_controls()

    def set_speed(self, speed: float) -> None:
        self._speed = float(speed)
        self._render_controls()

    def set_eq_preset(self, preset_id: str) -> None:
        self._eq_preset = preset_id or "flat"
        self._render_controls()

    def set_skip_silence(self, enabled: bool) -> None:
        self._skip_silence = bool(enabled)
        self._render_controls()

    def set_sleep_remaining(self, seconds: int) -> None:
        self._sleep_remaining_s = max(0, int(seconds))
        self._render_controls()

    def set_dynamic_tint(self, enabled: bool) -> None:
        self._dynamic_tint = bool(enabled)
        self._render_controls()

    def set_show_nerd_stats(self, enabled: bool) -> None:
        self._show_nerd_stats = bool(enabled)
        self._render_track_info()

    def set_queue_preview(self, queue: List[Track]) -> None:
        self._queue = list(queue)
        self._queue_count = len(queue)
        self._next_track = queue[0] if queue else None
        self._render_up_next()
        if self._side_panel == "queue":
            self._render_side_panel()

    def refresh_theme(self) -> None:
        self._render_all()
        try:
            self.query_one("#np_seek", SeekBar).refresh()
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
        ("l", "cycle loop (off / loop one / stop)"),
        ("x", "cycle crossfade (off / 3s / 6s)"),
        ("N", "toggle loudness normalization"),
        ("C", "cycle playback speed (1x..2x)"),
        ("E", "cycle 7-band equalizer preset"),
        ("M", "toggle skip silence"),
        ("Z", "cycle sleep timer (15..60m)"),
        ("< / >", "adjust lyrics timing -/+0.25s"),
        ("L / O", "toggle lyrics / up-next split"),
        ("D", "toggle dynamic album-art tint"),
        ("j / k", "move down / up"),
        ("J / K", "reorder queue down / up"),
        ("[ / ]", "switch sub-tab / stats period"),
        ("enter", "play highlighted / open item"),
        ("a / i", "append to queue / play next"),
        ("A / P", "queue all / play all in view"),
        ("R", "start song radio"),
        ("g / G", "open current artist / album"),
        ("I", "song info & credits"),
        ("f / F", "toggle favourite / open library"),
        ("d", "remove from queue / favourites"),
        ("S / c", "shuffle / clear queue"),
        ("/", "focus search (or Spotify URL)"),
        ("t h e r s F q w Y ,", "jump to view / settings"),
        ("b / v", "toggle sidebar / cava spectrum"),
        ("u / U", "refresh recs / check updates"),
        ("w / m", "maximize / minimize album window"),
        ("T", "sync Omarchy theme"),
        ("?", "toggle this help"),
        ("Q", "quit"),
    ]

    def __init__(self, theme_name: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._theme_name = theme_name

    def compose(self) -> ComposeResult:
        t = _palette(self)
        mid = (len(self.ROWS) + 1) // 2
        left_rows = self.ROWS[:mid]
        right_rows = self.ROWS[mid:]
        lines: List[str] = []
        for idx in range(mid):
            lk, ld = left_rows[idx]
            left_cell = f"[bold {t.accent}]{escape(lk):<7}[/] [{t.foreground}]{escape(ld):<29}[/]"
            if idx < len(right_rows):
                rk, rd = right_rows[idx]
                right_cell = f"[bold {t.accent}]{escape(rk):<10}[/] [{t.foreground}]{escape(rd)}[/]"
            else:
                right_cell = ""
            lines.append(f"{left_cell} [{t.border}]│[/] {right_cell}")

        with Container(id="help_panel"):
            title = "KEYBINDINGS"
            if self._theme_name:
                title = f"KEYBINDINGS  ·  {escape(self._theme_name)}"
            yield Static(f"[bold {t.accent}]{title}[/]", classes="help_title")
            yield Static(f"[{t.border}]{'─' * 72}[/]")
            yield Static("\n".join(lines), classes="help_line")
            yield Static(f"[{t.border}]{'─' * 72}[/]")
            yield Static(f"[{t.muted}]press ? or esc to close[/]", classes="help_footer")


class SongInfoScreen(ModalScreen):
    """Modal showing release info, YouTube stats, stream codec, and track credits."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("I", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def __init__(self, track: Track, info: Optional[SongInfo] = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._track = track
        self._info = info

    def compose(self) -> ComposeResult:
        t = _palette(self)
        with Container(id="song_info_panel"):
            yield Static(f"[bold {t.accent}]SONG INFO & CREDITS[/]", classes="help_title")
            yield Static(
                f"[bold {t.foreground}]{escape(self._track.title)}[/]\n"
                f"[{t.secondary}]{escape(self._track.artist)}[/]",
                id="song_info_subtitle",
            )
            yield VerticalScroll(Static(self._render_body_markup(), id="song_info_body"), id="song_info_scroll")
            yield Static(f"[{t.muted}]press I, q, or esc to close[/]", classes="help_footer")

    def set_info(self, info: SongInfo) -> None:
        self._info = info
        try:
            self.query_one("#song_info_body", Static).update(self._render_body_markup())
        except Exception:
            pass

    def _render_body_markup(self) -> str:
        t = _palette(self)
        info = self._info
        if info is None:
            album_line = f"[{t.muted}]Album:[/] [{t.foreground}]{escape(self._track.album)}[/]\n" if self._track.album else ""
            return f"\n{album_line}[{t.muted}]fetching release date, counts & credits…[/]"

        rows: List[Tuple[str, str]] = [
            ("Album", info.album or self._track.album or "—"),
            ("Release", info.release_date or info.release_year or "—"),
            ("Views", info.views or "—"),
            ("Likes", info.likes or "—"),
            ("Comments", info.comments or "—"),
        ]
        if info.codec_info:
            rows.append(("Audio", info.codec_info))

        lines = [
            f"[{t.muted}]{escape(label + ':'):<11}[/] [{t.foreground}]{escape(val)}[/]"
            for label, val in rows
        ]
        if info.credits:
            lines.append("")
            lines.append(f"[bold {t.accent}]CREDITS[/]")
            for c in info.credits:
                lines.append(f"[{t.secondary}]{escape(c.role)}[/]")
                lines.append(f"  [{t.foreground}]{escape(c.text)}[/]")
        elif info.failed:
            lines.append("")
            lines.append(f"[{t.warning}]could not fetch full remote credits[/]")
        else:
            lines.append("")
            lines.append(f"[{t.muted}]no extra credits listed[/]")

        return "\n".join(lines)
