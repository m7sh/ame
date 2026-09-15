"""
ui/widgets.py - Header, Navigation Sidebar, and Sticky Player Bar Widgets.
Refined with Omarchy theme color integration and interactive seek bar.
"""

from typing import Optional
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static, ListView, ListItem
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual import events

from api import Track, format_seconds
from theme import ThemeColors


class HeaderBar(Widget):
    """Top application header with Omarchy theme sync, active view and status badges."""

    active_view = reactive("🎵 Trending / Charts")
    status_text = reactive("Ready")
    autoplay_on = reactive(True)
    is_buffering = reactive(False)
    theme_name = reactive("Omarchy")

    def _get_theme(self) -> ThemeColors:
        if hasattr(self.app, "theme_manager"):
            return self.app.theme_manager.current_theme
        return ThemeColors()

    def compose(self) -> ComposeResult:
        t = self._get_theme()
        with Horizontal(id="header_container"):
            yield Static(
                f"⚡ [bold {t.primary}]YTM[/][bold {t.secondary}]usic[/] [dim {t.muted}]TUI[/]",
                id="header_logo",
            )
            yield Static(
                f"[bold {t.primary}]VIEW:[/] [bold {t.foreground}]{self.active_view}[/]",
                id="header_view_indicator",
            )
            with Horizontal(id="header_badges"):
                yield Static(
                    f"[bold {t.accent}][🎨 {self.theme_name}][/]",
                    id="header_theme_badge",
                    classes="badge",
                )
                yield Static(
                    f"[bold {t.success}][GUEST MODE][/]",
                    id="header_guest_badge",
                    classes="badge",
                )
                yield Static(
                    f"[bold {t.secondary}][AUTOPLAY: ON][/]",
                    id="header_autoplay_badge",
                    classes="badge",
                )
                yield Static(
                    f"[bold {t.primary}][{self.status_text.upper()}][/]",
                    id="header_status_badge",
                    classes="badge",
                )

    def watch_active_view(self, new_view: str) -> None:
        t = self._get_theme()
        try:
            lbl = self.query_one("#header_view_indicator", Static)
            lbl.update(f"[bold {t.primary}]VIEW:[/] [bold {t.foreground}]{new_view}[/]")
        except Exception:
            pass

    def watch_autoplay_on(self, on: bool) -> None:
        t = self._get_theme()
        try:
            badge = self.query_one("#header_autoplay_badge", Static)
            if on:
                badge.update(f"[bold {t.secondary}][AUTOPLAY: ON][/]")
            else:
                badge.update(f"[dim {t.muted}][AUTOPLAY: OFF][/]")
        except Exception:
            pass

    def watch_status_text(self, text: str) -> None:
        t = self._get_theme()
        try:
            badge = self.query_one("#header_status_badge", Static)
            if self.is_buffering:
                badge.update(f"[bold {t.accent}][⏳ BUFFERING...][/]")
            else:
                badge.update(f"[bold {t.primary}][{text.upper()}][/]")
        except Exception:
            pass

    def watch_is_buffering(self, buffering: bool) -> None:
        self.watch_status_text(self.status_text)

    def watch_theme_name(self, name: str) -> None:
        t = self._get_theme()
        try:
            badge = self.query_one("#header_theme_badge", Static)
            badge.update(f"[bold {t.accent}][🎨 {name}][/]")
        except Exception:
            pass

    def refresh_theme(self) -> None:
        """Called on Omarchy theme change to refresh all badges."""
        t = self._get_theme()
        self.theme_name = t.name
        try:
            self.query_one("#header_logo", Static).update(
                f"⚡ [bold {t.primary}]YTM[/][bold {t.secondary}]usic[/] [dim {t.muted}]TUI[/]"
            )
            self.watch_active_view(self.active_view)
            self.watch_autoplay_on(self.autoplay_on)
            self.watch_status_text(self.status_text)
            self.watch_theme_name(t.name)
            self.query_one("#header_guest_badge", Static).update(
                f"[bold {t.success}][GUEST MODE][/]"
            )
        except Exception:
            pass


class SidebarNav(Widget):
    """Left navigation sidebar containing menu views and hotkey cheatsheet."""

    def _get_theme(self) -> ThemeColors:
        if hasattr(self.app, "theme_manager"):
            return self.app.theme_manager.current_theme
        return ThemeColors()

    def compose(self) -> ComposeResult:
        t = self._get_theme()
        with Vertical(id="sidebar_container"):
            yield Static(f"  [bold {t.secondary}]DISCOVERY[/]", id="nav_sec_discovery", classes="nav_section_title")
            with ListView(id="nav_list"):
                yield ListItem(Static("🎵  Trending / Charts"), id="nav_trending")
                yield ListItem(Static("📻  Song Radio & Recs"), id="nav_radio")
                yield ListItem(Static("📂  Moods & Playlists"), id="nav_moods")
                yield ListItem(Static("🔍  Search"), id="nav_search")
                yield ListItem(Static("📋  Current Queue"), id="nav_queue")

            yield Static(f"  [bold {t.primary}]KEYBINDINGS[/]", id="nav_sec_keys", classes="nav_section_title")
            with Vertical(id="sidebar_help"):
                yield Static(f"[bold {t.primary}]/[/]      Focus Search", classes="help_row")
                yield Static(f"[bold {t.primary}]Space[/]  Play / Pause", classes="help_row")
                yield Static(f"[bold {t.primary}]n / p[/]  Next / Prev Track", classes="help_row")
                yield Static(f"[bold {t.primary}]j / k[/]  Navigate List", classes="help_row")
                yield Static(f"[bold {t.primary}]Enter[/]  Play / Select", classes="help_row")
                yield Static(f"[bold {t.primary}]a[/]      Append to Queue", classes="help_row")
                yield Static(f"[bold {t.primary}]r[/]      Start Song Radio", classes="help_row")
                yield Static(f"[bold {t.primary}]s / c[/]  Shuffle / Clear", classes="help_row")
                yield Static(f"[bold {t.primary}]+ / -[/]  Volume Up / Down", classes="help_row")
                yield Static(f"[bold {t.primary}]Tab[/]    Switch Focus", classes="help_row")
                yield Static(f"[bold {t.primary}]q[/]      Safely Exit", classes="help_row")

    def refresh_theme(self) -> None:
        t = self._get_theme()
        try:
            self.query_one("#nav_sec_discovery", Static).update(f"  [bold {t.secondary}]DISCOVERY[/]")
            self.query_one("#nav_sec_keys", Static).update(f"  [bold {t.primary}]KEYBINDINGS[/]")
        except Exception:
            pass


class InteractiveSeekBar(Static):
    """
    Unicode seek bar styled with Omarchy palette and mouse click support.
    Format: 01:42 ━━━━━━━━━━━━━━━━━━━━●────────────────────────────── 03:55
    """

    elapsed: reactive[float] = reactive(0.0)
    total: reactive[float] = reactive(0.0)

    def _get_theme(self) -> ThemeColors:
        if hasattr(self.app, "theme_manager"):
            return self.app.theme_manager.current_theme
        return ThemeColors()

    def on_click(self, event: events.Click) -> None:
        """Handle clicking anywhere along the seek bar."""
        if self.total <= 0:
            return
        width = self.size.width
        if width > 16:
            bar_start = 8
            bar_end = width - 8
            bar_len = max(1, bar_end - bar_start)
            click_rel = max(0, min(bar_len, event.x - bar_start))
            percent = (click_rel / bar_len) * 100.0
            if hasattr(self.app, "player"):
                self.app.player.seek_percent(percent)

    def render(self) -> str:
        t = self._get_theme()
        elapsed_str = format_seconds(self.elapsed)
        total_str = format_seconds(self.total)

        width = self.size.width
        if width < 30:
            return f"[{t.primary}]{elapsed_str}[/] / [dim {t.muted}]{total_str}[/]"

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
            f"[bold {t.primary}]{elapsed_str}[/] "
            f"[bold {t.primary}]{filled_bar}[/]"
            f"[bold {t.accent}]{knob}[/]"
            f"[{t.border}]{empty_bar}[/] "
            f"[dim {t.muted}]{total_str}[/]"
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

    def _get_theme(self) -> ThemeColors:
        if hasattr(self.app, "theme_manager"):
            return self.app.theme_manager.current_theme
        return ThemeColors()

    def compose(self) -> ComposeResult:
        t = self._get_theme()
        with Vertical(id="player_bar_container"):
            # Row 1: Track Title, Artist, Album, Quality, Autoplay
            with Horizontal(id="player_track_row"):
                yield Static(f"🎵  [dim {t.muted}]No track playing — Select a song and press Enter[/]", id="player_title_info")
                with Horizontal(id="player_meta_badges"):
                    yield Static(f"[bold {t.primary} border][{self.quality}][/]", id="player_quality_badge")
                    yield Static(f"[bold {t.secondary}][AUTOPLAY: ON][/]", id="player_autoplay_badge")

            # Row 2: Dynamic Unicode Seek Bar
            with Container(id="player_seek_container"):
                yield InteractiveSeekBar(id="player_seek_bar")

            # Row 3: Controls State, Volume meter, Queue count, Quick Hotkeys
            with Horizontal(id="player_controls_row"):
                yield Static(f"[dim {t.muted}][⏹ STOPPED][/]", id="player_state_badge")
                yield Static(self._format_volume_meter(self.volume), id="player_volume_badge")
                yield Static(f"[bold {t.primary}][QUEUE: {self.queue_len} tracks][/]", id="player_queue_badge")
                yield Static(
                    f"[dim {t.muted}][Space] Pause  [n] Next  [p] Prev  [+/-] Vol  [a] Add  [r] Radio  [s] Shuffle[/]",
                    id="player_hotkeys_hint",
                )

    def _format_volume_meter(self, vol: int) -> str:
        """Create volume meter with Unicode block glyphs."""
        t = self._get_theme()
        blocks = [" ", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
        total_blocks = 8
        active = int(round((vol / 100.0) * total_blocks))
        meter = ""
        for i in range(total_blocks):
            if i < active:
                meter += blocks[min(i, len(blocks) - 1)]
            else:
                meter += " "
        return f"[bold {t.secondary}][VOL: {vol}% {meter}][/]"

    def update_track(self, track: Optional[Track], quality: str, autoplay: bool) -> None:
        self.track = track
        self.quality = quality
        self.autoplay = autoplay
        t = self._get_theme()

        try:
            title_lbl = self.query_one("#player_title_info", Static)
            if track:
                title_lbl.update(
                    f"🎵  [bold {t.accent}]{track.title}[/]  [dim {t.muted}]•[/]  "
                    f"[bold {t.secondary}]{track.artist}[/]  "
                    f"[dim {t.muted}]•  {track.album or 'Single'}[/]"
                )
            else:
                title_lbl.update(f"🎵  [dim {t.muted}]No track playing — Select a song and press Enter[/]")

            q_badge = self.query_one("#player_quality_badge", Static)
            q_badge.update(f"[bold {t.primary}][{quality}][/]")

            ap_badge = self.query_one("#player_autoplay_badge", Static)
            if autoplay:
                ap_badge.update(f"[bold {t.secondary}][AUTOPLAY: ON][/]")
            else:
                ap_badge.update(f"[dim {t.muted}][AUTOPLAY: OFF][/]")
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
        t = self._get_theme()

        try:
            state_badge = self.query_one("#player_state_badge", Static)
            if is_buffering:
                state_badge.update(f"[bold {t.accent}][⏳ BUFFERING][/]")
            elif is_paused:
                state_badge.update(f"[bold {t.warning}][⏸ PAUSED][/]")
            elif is_playing:
                state_badge.update(f"[bold {t.success}][▶ PLAYING][/]")
            else:
                state_badge.update(f"[dim {t.muted}][⏹ STOPPED][/]")

            vol_badge = self.query_one("#player_volume_badge", Static)
            vol_badge.update(self._format_volume_meter(volume))

            q_badge = self.query_one("#player_queue_badge", Static)
            q_badge.update(f"[bold {t.primary}][QUEUE: {queue_len} tracks][/]")
        except Exception:
            pass

    def refresh_theme(self) -> None:
        """Refresh bar colors on theme change."""
        self.update_track(self.track, self.quality, self.autoplay)
        self.update_state(
            self.is_playing,
            self.is_paused,
            self.is_buffering,
            self.volume,
            self.queue_len,
        )
        try:
            bar = self.query_one("#player_seek_bar", InteractiveSeekBar)
            bar.refresh()
        except Exception:
            pass
