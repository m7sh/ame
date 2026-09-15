"""
ui/widgets.py - Header, Navigation Sidebar, and Sticky Player Bar Widgets.
"""

from typing import Optional
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, ListView, ListItem, Label
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual import events

from api import Track, format_seconds


class HeaderBar(Widget):
    """Top application header with cyberpunk styling, active view and status badges."""

    active_view = reactive("🎵 Trending / Charts")
    status_text = reactive("Ready")
    autoplay_on = reactive(True)
    is_buffering = reactive(False)

    def compose(self) -> ComposeResult:
        with Horizontal(id="header_container"):
            yield Static(
                "⚡ [bold #00e5ff]YTM[/][bold #bd93f9]usic[/] [dim #6272a4]TUI[/]",
                id="header_logo",
            )
            yield Static(
                f"[bold #00e5ff]VIEW:[/] [bold #f8f8f2]{self.active_view}[/]",
                id="header_view_indicator",
            )
            with Horizontal(id="header_badges"):
                yield Static(
                    "[bold #50fa7b][GUEST MODE][/]",
                    id="header_guest_badge",
                    classes="badge",
                )
                yield Static(
                    "[bold #bd93f9][AUTOPLAY: ON][/]",
                    id="header_autoplay_badge",
                    classes="badge",
                )
                yield Static(
                    f"[#00e5ff][{self.status_text.upper()}][/]",
                    id="header_status_badge",
                    classes="badge",
                )

    def watch_active_view(self, new_view: str) -> None:
        try:
            lbl = self.query_one("#header_view_indicator", Static)
            lbl.update(f"[bold #00e5ff]VIEW:[/] [bold #f8f8f2]{new_view}[/]")
        except Exception:
            pass

    def watch_autoplay_on(self, on: bool) -> None:
        try:
            badge = self.query_one("#header_autoplay_badge", Static)
            if on:
                badge.update("[bold #bd93f9][AUTOPLAY: ON][/]")
            else:
                badge.update("[dim #6272a4][AUTOPLAY: OFF][/]")
        except Exception:
            pass

    def watch_status_text(self, text: str) -> None:
        try:
            badge = self.query_one("#header_status_badge", Static)
            if self.is_buffering:
                badge.update("[bold #ff79c6][⏳ BUFFERING...][/]")
            else:
                badge.update(f"[bold #00e5ff][{text.upper()}][/]")
        except Exception:
            pass

    def watch_is_buffering(self, buffering: bool) -> None:
        self.watch_status_text(self.status_text)


class SidebarNav(Widget):
    """Left navigation sidebar containing menu views and hotkey cheatsheet."""

    def compose(self) -> ComposeResult:
        with Vertical(id="sidebar_container"):
            yield Static("  [bold #bd93f9]DISCOVERY[/]", classes="nav_section_title")
            with ListView(id="nav_list"):
                yield ListItem(Static("🎵  Trending / Charts"), id="nav_trending")
                yield ListItem(Static("📻  Song Radio & Recs"), id="nav_radio")
                yield ListItem(Static("📂  Moods & Playlists"), id="nav_moods")
                yield ListItem(Static("🔍  Search"), id="nav_search")
                yield ListItem(Static("📋  Current Queue"), id="nav_queue")

            yield Static("  [bold #00e5ff]KEYBINDINGS[/]", classes="nav_section_title")
            with Vertical(id="sidebar_help"):
                yield Static("[bold #00e5ff]/[/]      Focus Search", classes="help_row")
                yield Static("[bold #00e5ff]Space[/]  Play / Pause", classes="help_row")
                yield Static("[bold #00e5ff]n / p[/]  Next / Prev Track", classes="help_row")
                yield Static("[bold #00e5ff]j / k[/]  Navigate List", classes="help_row")
                yield Static("[bold #00e5ff]Enter[/]  Play / Select", classes="help_row")
                yield Static("[bold #00e5ff]a[/]      Append to Queue", classes="help_row")
                yield Static("[bold #00e5ff]r[/]      Start Song Radio", classes="help_row")
                yield Static("[bold #00e5ff]s / c[/]  Shuffle / Clear", classes="help_row")
                yield Static("[bold #00e5ff]+ / -[/]  Volume Up / Down", classes="help_row")
                yield Static("[bold #00e5ff]Tab[/]    Switch Focus", classes="help_row")
                yield Static("[bold #00e5ff]q[/]      Safely Exit", classes="help_row")


class InteractiveSeekBar(Static):
    """
    Unicode seek bar that supports mouse clicks and updates visually.
    Format: 01:42 ━━━━━━━━━━━━━━━━━━━━●────────────────────────────── 03:55
    """

    elapsed: reactive[float] = reactive(0.0)
    total: reactive[float] = reactive(0.0)

    def on_click(self, event: events.Click) -> None:
        """Handle clicking anywhere along the seek bar."""
        if self.total <= 0:
            return
        # Calculate percentage based on click x position relative to widget width
        width = self.size.width
        if width > 16:
            # Leave room for timestamps on left and right (~7 chars each)
            bar_start = 8
            bar_end = width - 8
            bar_len = max(1, bar_end - bar_start)
            click_rel = max(0, min(bar_len, event.x - bar_start))
            percent = (click_rel / bar_len) * 100.0
            # Post message or call player seek_percent
            if hasattr(self.app, "player"):
                self.app.player.seek_percent(percent)

    def render(self) -> str:
        elapsed_str = format_seconds(self.elapsed)
        total_str = format_seconds(self.total)

        width = self.size.width
        if width < 30:
            # Compact view for small width
            return f"[#00e5ff]{elapsed_str}[/] / [dim]{total_str}[/]"

        # Reserve space for timestamps and margins: "00:00 " (6) and " 00:00" (6)
        bar_width = max(10, min(60, width - 18))

        if self.total > 0:
            ratio = max(0.0, min(1.0, self.elapsed / self.total))
            filled_len = int(round(ratio * (bar_width - 1)))
            empty_len = max(0, bar_width - 1 - filled_len)
        else:
            filled_len = 0
            empty_len = max(0, bar_width - 1)

        filled_bar = "━" * filled_len
        knob = "●"
        empty_bar = "─" * empty_len

        return (
            f"[bold #00e5ff]{elapsed_str}[/] "
            f"[bold #00e5ff]{filled_bar}[/]"
            f"[bold #ffffff]{knob}[/]"
            f"[#27272a]{empty_bar}[/] "
            f"[dim #6272a4]{total_str}[/]"
        )


class BottomPlayerBar(Widget):
    """
    Sticky bottom player bar displaying currently playing track, dynamic Unicode
    seek bar, state indicators, volume level, and quick controls.
    """

    track: reactive[Optional[Track]] = reactive(None)
    is_playing: reactive[bool] = reactive(False)
    is_paused: reactive[bool] = reactive(False)
    is_buffering: reactive[bool] = reactive(False)
    volume: reactive[int] = reactive(80)
    quality: reactive[str] = reactive("Opus Best Audio")
    autoplay: reactive[bool] = reactive(True)
    queue_len: reactive[int] = reactive(0)

    def compose(self) -> ComposeResult:
        with Vertical(id="player_bar_container"):
            # Row 1: Track Title, Artist, Album, Quality, Autoplay
            with Horizontal(id="player_track_row"):
                yield Static("🎵  [dim #6272a4]No track playing — Select a song and press Enter[/]", id="player_title_info")
                with Horizontal(id="player_meta_badges"):
                    yield Static(f"[bold #00e5ff border][{self.quality}][/]", id="player_quality_badge")
                    yield Static("[bold #bd93f9][AUTOPLAY: ON][/]", id="player_autoplay_badge")

            # Row 2: Dynamic Unicode Seek Bar
            with Container(id="player_seek_container"):
                yield InteractiveSeekBar(id="player_seek_bar")

            # Row 3: Controls State, Volume meter, Queue count, Quick Hotkeys
            with Horizontal(id="player_controls_row"):
                yield Static("[dim #6272a4][⏹ STOPPED][/]", id="player_state_badge")
                yield Static(self._format_volume_meter(self.volume), id="player_volume_badge")
                yield Static(f"[bold #00e5ff][QUEUE: {self.queue_len} tracks][/]", id="player_queue_badge")
                yield Static(
                    "[dim #6272a4][Space] Pause  [n] Next  [p] Prev  [+/-] Vol  [a] Add  [r] Radio  [s] Shuffle[/]",
                    id="player_hotkeys_hint",
                )

    def _format_volume_meter(self, vol: int) -> str:
        """Create volume meter with Unicode block glyphs."""
        blocks = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        total_blocks = 8
        active = int(round((vol / 100.0) * total_blocks))
        meter = ""
        for i in range(total_blocks):
            if i < active:
                meter += blocks[min(i, len(blocks) - 1)]
            else:
                meter += " "
        return f"[bold #bd93f9][VOL: {vol}% {meter}][/]"

    def update_track(self, track: Optional[Track], quality: str, autoplay: bool) -> None:
        self.track = track
        self.quality = quality
        self.autoplay = autoplay

        try:
            title_lbl = self.query_one("#player_title_info", Static)
            if track:
                title_lbl.update(
                    f"🎵  [bold #00e5ff]{track.title}[/]  [dim #6272a4]•[/]  "
                    f"[bold #bd93f9]{track.artist}[/]  "
                    f"[dim #6272a4]•  {track.album or 'Single'}[/]"
                )
            else:
                title_lbl.update("🎵  [dim #6272a4]No track playing — Select a song and press Enter[/]")

            q_badge = self.query_one("#player_quality_badge", Static)
            q_badge.update(f"[bold #00e5ff][{quality}][/]")

            ap_badge = self.query_one("#player_autoplay_badge", Static)
            if autoplay:
                ap_badge.update("[bold #bd93f9][AUTOPLAY: ON][/]")
            else:
                ap_badge.update("[dim #6272a4][AUTOPLAY: OFF][/]")
        except Exception:
            pass

    def update_progress(self, elapsed: float, total: float) -> None:
        try:
            bar = self.query_one("#player_seek_bar", InteractiveSeekBar)
            bar.elapsed = elapsed
            bar.total = total
        except Exception:
            pass

    def update_state(
        self,
        is_playing: bool,
        is_paused: bool,
        is_buffering: bool,
        volume: int,
        queue_len: int,
    ) -> None:
        self.is_playing = is_playing
        self.is_paused = is_paused
        self.is_buffering = is_buffering
        self.volume = volume
        self.queue_len = queue_len

        try:
            state_badge = self.query_one("#player_state_badge", Static)
            if is_buffering:
                state_badge.update("[bold #ff79c6][⏳ BUFFERING][/]")
            elif is_paused:
                state_badge.update("[bold #f1fa8c][⏸ PAUSED][/]")
            elif is_playing:
                state_badge.update("[bold #50fa7b][▶ PLAYING][/]")
            else:
                state_badge.update("[dim #6272a4][⏹ STOPPED][/]")

            vol_badge = self.query_one("#player_volume_badge", Static)
            vol_badge.update(self._format_volume_meter(volume))

            q_badge = self.query_one("#player_queue_badge", Static)
            q_badge.update(f"[bold #00e5ff][QUEUE: {queue_len} tracks][/]")
        except Exception:
            pass
