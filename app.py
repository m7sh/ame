"""
app.py - Main Textual application for ame.

An unauthenticated, keyboard-driven YouTube Music player for the terminal.
Built with Textual, ytmusicapi (guest mode), yt-dlp and a headless mpv daemon.
"""

import sys

if len(sys.argv) > 1:
    arg = sys.argv[1]
    if arg in ("--help", "-h"):
        print("""ame — minimal, keyboard-driven YouTube Music player for the terminal

Usage:
  ame                  Launch player
  ame --update         Update ame to the latest version
  ame --uninstall      Uninstall ame and remove symlinks / data
  ame --help, -h       Show this help message
""")
        sys.exit(0)
    elif arg in ("--update", "update"):
        import subprocess
        from pathlib import Path
        update_sh = Path(__file__).resolve().parent / "update.sh"
        if update_sh.exists():
            sys.exit(subprocess.call(["bash", str(update_sh)] + sys.argv[2:]))
        else:
            print("Error: update.sh not found.", file=sys.stderr)
            sys.exit(1)
    elif arg in ("--uninstall", "uninstall"):
        import subprocess
        from pathlib import Path
        uninstall_sh = Path(__file__).resolve().parent / "uninstall.sh"
        if uninstall_sh.exists():
            sys.exit(subprocess.call(["bash", str(uninstall_sh)] + sys.argv[2:]))
        else:
            print("Error: uninstall.sh not found.", file=sys.stderr)
            sys.exit(1)

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Input, TabbedContent, TabPane
from textual import work
from textual import events

from api import YTMusicAPI, Track, Playlist, Category
from artwork import ArtworkCache, extract_artwork_colors
from integrations import DiscordPresence, MPRISController
from lyrics import LyricsManager, LyricsResult
from player import MPVPlayer
from favourites import FavouritesStore
from recommendations import is_likely_video
from settings import AppSettingsStore, RecentHistoryStore
from spectrum import CavaSpectrum
from stats import ListeningStatsStore
from theme import OmarchyThemeManager, ThemeColors
from ui.widgets import (
    TopBar,
    Sidebar,
    SidebarNavMsg,
    PlayerBar,
    NowPlayingView,
    HelpScreen,
    Spectrum,
    MaximizeNowPlayingMsg,
    MinimizeNowPlayingMsg,
    PlayerControlMsg,
    SeekToMsg,
    JumpQueueIndexMsg,
)
from ui.views import (
    TrendingView,
    ExploreView,
    MoodDetailView,
    RadioView,
    PlaylistDetailView,
    ArtistDetailView,
    SearchView,
    FavouritesView,
    QueueView,
    StatsView,
    SettingsView,
    PlayTrackMsg,
    PlayQueueIndexMsg,
    MoveQueueItemMsg,
    QueueTrackMsg,
    StartRadioMsg,
    OpenPlaylistMsg,
    OpenMoodMsg,
    OpenAlbumMsg,
    OpenArtistMsg,
    ExecuteSearchMsg,
    ToggleSettingMsg,
)

SLEEP_TIMER_PRESETS_MIN = (0, 15, 30, 45, 60)


class AmeApp(App):
    """Minimal, terminal-native YouTube Music player with Omarchy theme sync."""

    CSS_PATH = "styles.tcss"
    TITLE = "ame"
    SUB_TITLE = "guest mode"

    # Keep the terminal-default background so the terminal's own transparency
    # (Alacritty/kitty opacity) shows through. Hex accents stay truecolor.
    ansi_color = True

    BINDINGS = [
        Binding("slash", "focus_search", "Search", show=False),
        Binding("space", "toggle_play", "Play/Pause", show=False),
        Binding("n", "next_track", "Next", show=False),
        Binding("p", "prev_track", "Previous", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("a", "queue_selected", "Append", show=False),
        Binding("i", "play_next_selected", "Play Next", show=False),
        Binding("A", "queue_all", "Queue All", show=False),
        Binding("P", "play_all", "Play All", show=False),
        Binding("J", "move_queue_down", "Move Queue Down", show=False),
        Binding("K", "move_queue_up", "Move Queue Up", show=False),
        Binding("d", "remove_selected", "Remove", show=False),
        Binding("f", "toggle_favourite", "Favourite", show=False),
        Binding("F", "switch_view_favourites", "Favourites", show=False),
        Binding("R", "radio_selected", "Radio", show=False),
        Binding("g", "open_current_artist", "Open Artist", show=False),
        Binding("G", "open_current_album", "Open Album", show=False),
        Binding("plus", "volume_up", "Vol +", show=False),
        Binding("equals", "volume_up", "Vol +", show=False),
        Binding("minus", "volume_down", "Vol -", show=False),
        Binding("underscore", "volume_down", "Vol -", show=False),
        Binding("S", "shuffle_queue", "Shuffle", show=False),
        Binding("c", "clear_queue", "Clear", show=False),
        Binding("left", "seek_backward", "Seek -5s", show=False),
        Binding("right", "seek_forward", "Seek +5s", show=False),
        Binding("escape", "handle_escape", "Back", show=False),
        Binding("T", "reload_theme", "Sync Theme", show=False),
        Binding("question_mark", "show_help", "Help", show=False),
        Binding("v", "toggle_visualizer", "Visualizer", show=False),
        Binding("w", "toggle_now_playing", "Now Playing", show=False),
        Binding("m", "toggle_now_playing", "Minimize/Maximize", show=False),
        Binding("L", "toggle_lyrics_panel", "Lyrics", show=False),
        Binding("O", "toggle_up_next_panel", "Up Next", show=False),
        Binding("D", "toggle_dynamic_tint", "Dynamic Tint", show=False),
        Binding("C", "cycle_speed", "Speed", show=False),
        Binding("Z", "cycle_sleep_timer", "Sleep Timer", show=False),
        Binding("Y", "switch_view_stats", "Stats", show=False),
        Binding("l", "cycle_loop", "Loop Mode", show=False),
        Binding("x", "cycle_crossfade", "Crossfade", show=False),
        Binding("N", "toggle_loudness", "Loudness Norm", show=False),
        Binding("Q", "quit_app", "Quit", show=False),
        Binding("ctrl+c", "quit_app", "Quit", show=False),
        Binding("h", "switch_view_home", "Home", show=False),
        Binding("t", "switch_view_trending", "Home", show=False),
        Binding("e", "switch_view_explore", "Explore", show=False),
        Binding("r", "switch_view_radio", "Radio", show=False),
        Binding("s", "switch_view_search", "Search", show=False),
        Binding("q", "switch_view_queue", "Queue", show=False),
        Binding("b", "toggle_sidebar", "Sidebar", show=False),
        Binding("comma", "switch_view_settings", "Settings", show=False),
        Binding("left_square_bracket", "prev_tab", "Previous Tab", show=False),
        Binding("right_square_bracket", "next_tab", "Next Tab", show=False),
        Binding("u", "refresh_recommendations", "Refresh Recs", show=False),
        Binding("U", "check_app_update", "Check Update", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.theme_manager = OmarchyThemeManager()
        self._theme_version = 0
        self._install_theme()

        self.settings = AppSettingsStore()
        self.recent_history = RecentHistoryStore()
        s = self.settings.data

        self.api = YTMusicAPI()
        self.artwork_cache = ArtworkCache()
        self.lyrics_manager = LyricsManager(ytm_fetcher=self.api.get_ytm_lyrics)
        self.stats = ListeningStatsStore()
        self.player = MPVPlayer(resolver=self.api.resolver)
        self.player.autoplay = s.autoplay
        self.player.crossfade_seconds = s.crossfade_seconds
        self.player.loudness_normalization = s.loudness_normalization
        self.player.speed = s.speed

        self.favourites = FavouritesStore()
        self.mpris: Optional[MPRISController] = None
        self.discord: Optional[DiscordPresence] = None
        self.spectrum: Optional[CavaSpectrum] = None
        self._visualizer_on = s.visualizer_enabled
        self._dynamic_tint = s.dynamic_art_tint

        self.active_view_id = "view_trending"
        self.previous_view_id = "view_trending"
        self._browse_view_id = "view_trending"

        # Incremented on every search / artwork / lyrics request so stale results
        # from an in-flight worker can be discarded when moved on.
        self._search_seq = 0
        self._artwork_seq = 0
        self._lyrics_seq = 0
        self._current_artwork_track_id: Optional[str] = None
        self._current_lyrics_track_id: Optional[str] = None
        self._current_artwork_img = None

        # Sleep timer & listening stats accumulator
        self._sleep_minutes: int = 0
        self._sleep_deadline: Optional[float] = None
        self._sleep_fade_base_vol: Optional[int] = None
        self._last_tick_ts: float = time.monotonic()
        self._pending_listen_track: Optional[Track] = None
        self._pending_listen_sec: float = 0.0
        self._last_mpris_pos_sync: float = 0.0

    # ------------------------------------------------------------------ #
    # Theme plumbing
    # ------------------------------------------------------------------ #
    def _install_theme(self) -> None:
        """Register the current palette as a fresh Textual theme and apply it."""
        self._theme_version += 1
        slot = "a" if self._theme_version % 2 else "b"
        theme = OmarchyThemeManager.build_textual_theme(
            self.theme_manager.current_theme, self._theme_version
        )
        theme.name = f"omarchy-synced-{slot}"
        self.register_theme(theme)
        self.theme = theme.name

    def _refresh_widgets_theme(self) -> None:
        try:
            self.query_one("#top_bar", TopBar).refresh_theme()
            self.query_one("#sidebar", Sidebar).refresh_theme()
            self.query_one("#player_bar", PlayerBar).refresh_theme()
            self.query_one("#view_now_playing", NowPlayingView).refresh_theme()
            self.query_one("#spectrum", Spectrum).refresh_theme()
            view = self.query_one(f"#{self.active_view_id}")
            if hasattr(view, "refresh_theme"):
                view.refresh_theme()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield TopBar(id="top_bar")
        with Horizontal(id="main_body"):
            yield Sidebar(id="sidebar")
            with ContentSwitcher(initial="view_trending", id="content_switcher"):
                yield TrendingView(id="view_trending")
                yield ExploreView(id="view_explore")
                yield MoodDetailView(id="view_mood_detail")
                yield RadioView(id="view_radio")
                yield PlaylistDetailView(id="view_playlist_detail")
                yield ArtistDetailView(id="view_artist_detail")
                yield SearchView(id="view_search")
                yield FavouritesView(id="view_favourites")
                yield QueueView(id="view_queue")
                yield StatsView(id="view_stats")
                yield SettingsView(id="view_settings")
                yield NowPlayingView(id="view_now_playing")
        yield Spectrum(id="spectrum")
        yield PlayerBar(id="player_bar")

    def on_mount(self) -> None:
        self.player.on_track_change = self._on_player_track_change
        self.player.on_state_change = self._on_player_state_change
        self.player.on_progress = self._on_player_progress
        self.player.on_queue_change = self._on_player_queue_change
        self.player.on_autoplay_trigger = self._on_player_autoplay_trigger
        self.player.on_message = self._on_player_message
        self.player.on_volume_change = self._on_player_volume_change
        self.player.on_crossfade_change = self._on_player_crossfade_change
        self.player.on_speed_change = self._on_player_speed_change

        s = self.settings.data
        if s.mpris_enabled:
            self.mpris = MPRISController(on_command=self._on_mpris_command)
        if s.discord_rpc:
            self.discord = DiscordPresence()

        top = self.query_one("#top_bar", TopBar)
        top.theme_name = self.theme_manager.current_theme.name
        top.volume = self.player.volume
        top.speed = self.player.speed

        bar = self.query_one("#player_bar", PlayerBar)
        bar.set_crossfade(s.crossfade_seconds)
        bar.set_loudness(s.loudness_normalization)
        bar.set_speed(s.speed)

        np = self.query_one("#view_now_playing", NowPlayingView)
        np.set_crossfade(s.crossfade_seconds)
        np.set_loudness(s.loudness_normalization)
        np.set_speed(s.speed)
        np.set_dynamic_tint(s.dynamic_art_tint)
        np.set_show_nerd_stats(s.show_nerd_stats)

        self._refresh_favourites()
        self._refresh_settings_view()
        self._sync_sidebar_visibility()

        self.set_interval(0.25, self._tick_progress)
        self.set_interval(1.5, self._check_theme_sync)

        if CavaSpectrum.available():
            self.spectrum = CavaSpectrum(on_update=self._on_spectrum_bands)
        else:
            self._visualizer_on = False
        self._sync_visualizer_visibility(False, False)

        self._focus_content()
        self.load_initial_data()

    # ------------------------------------------------------------------ #
    # Periodic timers (progress, listening stats, sleep timer)
    # ------------------------------------------------------------------ #
    def _flush_listening_stats(self) -> None:
        if self._pending_listen_track is not None and self._pending_listen_sec >= 1.0:
            try:
                self.stats.record_listening(self._pending_listen_track, self._pending_listen_sec)
            except Exception:
                pass
            self._pending_listen_sec = 0.0

    def _tick_progress(self) -> None:
        now = time.monotonic()
        dt = max(0.0, min(1.0, now - self._last_tick_ts))
        self._last_tick_ts = now

        if self.player.is_playing and not self.player.is_paused:
            pos = self.player.playback_pos
            dur = self.player.duration
            self.query_one("#player_bar", PlayerBar).set_progress(pos, dur)
            self.query_one("#view_now_playing", NowPlayingView).set_progress(pos, dur)

            cur = self.player.current_track
            if cur is not None:
                if self._pending_listen_track is None or self._pending_listen_track.id != cur.id:
                    self._flush_listening_stats()
                    self._pending_listen_track = cur
                self._pending_listen_sec += dt
                if self._pending_listen_sec >= 5.0:
                    self._flush_listening_stats()

            if self.mpris and now - self._last_mpris_pos_sync >= 2.0:
                self._last_mpris_pos_sync = now
                self.mpris.update_position(pos)

        self._tick_sleep_timer(now)

    def _tick_sleep_timer(self, now: float) -> None:
        if self._sleep_deadline is None:
            return
        remaining = self._sleep_deadline - now
        if remaining <= 0:
            self._sleep_deadline = None
            self._sleep_minutes = 0
            saved_vol = self._sleep_fade_base_vol
            self._sleep_fade_base_vol = None
            self._update_sleep_ui(0)
            if self.player.is_playing and not self.player.is_paused:
                self.player.toggle_pause()
            if saved_vol is not None:
                self.player.set_volume(saved_vol)
            self.notify("sleep timer ended · playback paused", timeout=4)
            return

        if remaining <= 5.0 and self.player.is_playing and not self.player.is_paused:
            if self._sleep_fade_base_vol is None:
                self._sleep_fade_base_vol = self.player.volume
            faded = max(0, int(round(self._sleep_fade_base_vol * (remaining / 5.0))))
            self.player.set_volume(faded)

        self._update_sleep_ui(int(remaining))

    def _update_sleep_ui(self, remaining_s: int) -> None:
        try:
            self.query_one("#top_bar", TopBar).sleep_remaining_s = remaining_s
            self.query_one("#player_bar", PlayerBar).set_sleep_remaining(remaining_s)
            self.query_one("#view_now_playing", NowPlayingView).set_sleep_remaining(remaining_s)
        except Exception:
            pass

    def _check_theme_sync(self) -> None:
        new_theme = self.theme_manager.reload()
        if new_theme:
            if self._dynamic_tint and self._current_artwork_img is not None:
                self._apply_dynamic_art_palette(self._current_artwork_img, notify_user=False)
            else:
                self._apply_theme(new_theme)

    def _apply_theme(self, colors: ThemeColors, notify_user: bool = True) -> None:
        self._install_theme()
        self.query_one("#top_bar", TopBar).theme_name = colors.name
        self._refresh_widgets_theme()
        self.refresh(layout=True)
        if notify_user:
            self.notify(f"theme · {colors.name}", timeout=2)

    def _apply_dynamic_art_palette(self, img: Any, notify_user: bool = False) -> None:
        if not self._dynamic_tint or img is None:
            colors = self.theme_manager.apply_artwork_tint(None)
            self._apply_theme(colors, notify_user=notify_user)
            return
        dark = self.theme_manager.base_theme.mode != "light"
        extracted = extract_artwork_colors(img, dark_mode=dark)
        if extracted:
            accent, secondary = extracted
            colors = self.theme_manager.apply_artwork_tint(accent, secondary)
            self._apply_theme(colors, notify_user=False)

    def action_reload_theme(self) -> None:
        self.theme_manager.base_theme = self.theme_manager.load_theme()
        if self._dynamic_tint and self._current_artwork_img is not None:
            self._apply_dynamic_art_palette(self._current_artwork_img, notify_user=False)
            self.notify(f"theme · {self.theme_manager.base_theme.name}", timeout=2)
        else:
            self.theme_manager.current_theme = self.theme_manager.base_theme
            self._apply_theme(self.theme_manager.current_theme)

    # ------------------------------------------------------------------ #
    # Thread-safe callback helper
    # ------------------------------------------------------------------ #
    def _safe_call(self, callback, *args, **kwargs) -> None:
        try:
            if getattr(self, "_thread_id", None) == threading.get_ident():
                callback(*args, **kwargs)
            else:
                self.call_from_thread(callback, *args, **kwargs)
        except Exception:
            pass

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#top_bar", TopBar).status = text
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Player & Desktop Integration callbacks
    # ------------------------------------------------------------------ #
    def _on_mpris_command(self, msg: Dict[str, Any]) -> None:
        cmd = msg.get("cmd")
        if cmd == "play_pause":
            self._safe_call(self.action_toggle_play)
        elif cmd == "play":
            if self.player.is_paused:
                self._safe_call(self.action_toggle_play)
        elif cmd == "pause":
            if self.player.is_playing and not self.player.is_paused:
                self._safe_call(self.action_toggle_play)
        elif cmd == "next":
            self._safe_call(self.action_next_track)
        elif cmd == "previous":
            self._safe_call(self.action_prev_track)
        elif cmd == "stop":
            self._safe_call(self.player.stop)
        elif cmd == "seek_rel":
            self._safe_call(self.player.seek, float(msg.get("seconds", 0.0)))
        elif cmd == "seek_abs":
            self._safe_call(self.player.seek_to, float(msg.get("seconds", 0.0)))
        elif cmd == "volume":
            val = int(msg.get("value", 80))
            self._safe_call(self.player.set_volume, val)
        elif cmd == "speed":
            self._safe_call(self.player.set_speed, float(msg.get("value", 1.0)))
        elif cmd == "quit":
            self._safe_call(self.action_quit_app)

    def _on_player_track_change(self, track: Optional[Track]) -> None:
        def _update():
            bar = self.query_one("#player_bar", PlayerBar)
            np = self.query_one("#view_now_playing", NowPlayingView)
            quality = self.player.current_stream_quality
            stream_info = self.player.current_stream_info
            is_fav = bool(track and self.favourites.contains(track.id))

            bar.set_track(track, quality)
            bar.set_favourite(is_fav)
            np.set_track(track, quality, stream_info)
            np.set_favourite(is_fav)

            if self.mpris:
                self.mpris.update_track(track)
            if self.discord:
                self.discord.update(
                    track,
                    self.player.is_playing,
                    self.player.is_paused,
                    self.player.playback_pos,
                )

            if track is None:
                self._flush_listening_stats()
                self._pending_listen_track = None
                self._current_artwork_track_id = None
                self._current_lyrics_track_id = None
                self._current_artwork_img = None
                self._artwork_seq += 1
                self._lyrics_seq += 1
                bar.set_artwork(None)
                np.set_artwork(None)
                if self._dynamic_tint:
                    self._apply_dynamic_art_palette(None, notify_user=False)
                return

            if track.id != self._current_artwork_track_id:
                self._flush_listening_stats()
                self._pending_listen_track = track
                try:
                    self.stats.record_play(track)
                    self.recent_history.record(track)
                    self.query_one("#view_trending", TrendingView).set_recent(
                        self.recent_history.all()
                    )
                    self._refresh_favourites()
                except Exception:
                    pass

                self._current_artwork_track_id = track.id
                self._artwork_seq += 1
                cached = self.artwork_cache.get_cached(track.id, track.thumbnail)
                if cached is not None:
                    self._current_artwork_img = cached
                    bar.set_artwork(cached)
                    np.set_artwork(cached)
                    if self._dynamic_tint:
                        self._apply_dynamic_art_palette(cached, notify_user=False)
                else:
                    self._current_artwork_img = None
                    bar.set_artwork(None)
                    np.set_artwork(None)
                    if track.thumbnail:
                        self.load_artwork_worker(track, self._artwork_seq)

            if track.id != self._current_lyrics_track_id:
                self._current_lyrics_track_id = track.id
                self._lyrics_seq += 1
                cached_lyr = self.lyrics_manager.get_cached(track.id, track.title, track.artist)
                if cached_lyr is not None:
                    np.set_lyrics(cached_lyr, loading=False)
                else:
                    np.set_lyrics_loading(True)
                    self.load_lyrics_worker(track, self._lyrics_seq)
        self._safe_call(_update)

    @work(group="artwork_loader", exclusive=True, thread=True)
    def load_artwork_worker(self, track: Track, seq: int) -> None:
        img = self.artwork_cache.fetch(track.id, track.thumbnail)

        def _apply() -> None:
            if seq != self._artwork_seq:
                return
            cur = self.player.current_track
            if cur is None or cur.id != track.id:
                return
            self._current_artwork_img = img
            self.query_one("#player_bar", PlayerBar).set_artwork(img)
            self.query_one("#view_now_playing", NowPlayingView).set_artwork(img)
            if self._dynamic_tint and img is not None:
                self._apply_dynamic_art_palette(img, notify_user=False)

        self._safe_call(_apply)

    @work(group="lyrics_loader", exclusive=True, thread=True)
    def load_lyrics_worker(self, track: Track, seq: int) -> None:
        res = self.lyrics_manager.fetch(
            track.id,
            track.title,
            track.artist,
            album=track.album,
            duration_seconds=track.duration_seconds,
        )

        def _apply() -> None:
            if seq != self._lyrics_seq:
                return
            cur = self.player.current_track
            if cur is None or cur.id != track.id:
                return
            self.query_one("#view_now_playing", NowPlayingView).set_lyrics(res, loading=False)

        self._safe_call(_apply)

    def _on_player_state_change(self, is_playing: bool, is_paused: bool, is_buffering: bool) -> None:
        def _update():
            self.query_one("#player_bar", PlayerBar).set_state(
                is_playing, is_paused, is_buffering
            )
            self.query_one("#view_now_playing", NowPlayingView).set_state(
                is_playing, is_paused, is_buffering
            )
            top = self.query_one("#top_bar", TopBar)
            top.is_playing = is_playing
            top.is_paused = is_paused
            top.is_buffering = is_buffering
            self._sync_visualizer_visibility(is_playing, is_paused)
            if self.mpris:
                self.mpris.update_state(is_playing, is_paused)
            if self.discord:
                self.discord.update(
                    self.player.current_track,
                    is_playing,
                    is_paused,
                    self.player.playback_pos,
                )
        self._safe_call(_update)

    def _on_player_progress(self, pos: float, duration: float) -> None:
        def _update():
            self.query_one("#player_bar", PlayerBar).set_progress(pos, duration)
            self.query_one("#view_now_playing", NowPlayingView).set_progress(pos, duration)
        self._safe_call(_update)

    def _on_player_queue_change(self) -> None:
        def _update():
            queue_copy = list(self.player.queue)
            top = self.query_one("#top_bar", TopBar)
            top.queue_len = len(queue_copy)
            self.query_one("#view_now_playing", NowPlayingView).set_queue_preview(queue_copy)
            try:
                self.query_one("#view_queue", QueueView).set_queue(queue_copy)
            except Exception:
                pass
        self._safe_call(_update)

    def _on_player_autoplay_trigger(self, last_track: Track) -> None:
        self.run_autoplay_worker(last_track)

    def _on_player_message(self, msg: str) -> None:
        self._safe_call(self.notify, msg, severity="warning", timeout=4)

    def _on_player_volume_change(self, volume: int) -> None:
        def _update():
            self.query_one("#top_bar", TopBar).volume = volume
            if self.mpris:
                self.mpris.update_volume(volume)
        self._safe_call(_update)

    def _on_player_crossfade_change(self, seconds: int) -> None:
        def _update():
            self.query_one("#player_bar", PlayerBar).set_crossfade(seconds)
            self.query_one("#view_now_playing", NowPlayingView).set_crossfade(seconds)
        self._safe_call(_update)

    def _on_player_speed_change(self, speed: float) -> None:
        def _update():
            self.query_one("#top_bar", TopBar).speed = speed
            self.query_one("#player_bar", PlayerBar).set_speed(speed)
            self.query_one("#view_now_playing", NowPlayingView).set_speed(speed)
            if self.mpris:
                self.mpris.update_speed(speed)
        self._safe_call(_update)

    def _on_spectrum_bands(self, bands) -> None:
        self._safe_call(self._set_spectrum_bands, bands)

    def _set_spectrum_bands(self, bands) -> None:
        self.query_one("#spectrum", Spectrum).set_bands(bands)

    def _sync_visualizer_visibility(
        self, is_playing: Optional[bool] = None, is_paused: Optional[bool] = None
    ) -> None:
        """Show the equalizer and run cava only while audio is actually playing."""
        if is_playing is None:
            is_playing = self.player.is_playing
        if is_paused is None:
            is_paused = self.player.is_paused

        # Hide the bottom 1-line EQ strip only in very compact Now Playing terminals
        compact_np = self.active_view_id == "view_now_playing" and self.size.height > 0 and self.size.height < 26
        visible = self._visualizer_on and bool(is_playing) and not bool(is_paused) and not compact_np
        if self.spectrum is not None:
            if visible:
                self.spectrum.start()
            else:
                self.spectrum.stop()
        try:
            self.query_one("#spectrum", Spectrum).display = visible
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Background workers
    # ------------------------------------------------------------------ #
    def _fetch_home_bundle(self):
        """Fetch tailored Home tracks, curated mixes & trending."""
        favs = self.favourites.all()
        recent = self.recent_history.all()
        summary = self.stats.summary("month")
        trending = self.api.get_trending_songs()
        recs = self.api.get_recommendations(
            favourites=favs,
            history=self.player.history,
            trending_seed=trending,
            recent_history=recent,
            stats_top_artists=summary.top_artists,
        )
        playlists = self.api.get_recommended_playlists(
            favourites=favs,
            recent_history=recent,
            history=self.player.history,
            stats_top_artists=summary.top_artists,
        )
        if not isinstance(playlists, list) or not playlists:
            _, playlists = self.api.get_charts(include_tracks=False)
        return recs, trending, playlists, recent

    @work(group="initial_loader", exclusive=True, thread=True)
    def load_initial_data(self) -> None:
        self._safe_call(self._set_status, "loading personalized picks & trending…")
        try:
            recs, trending, playlists, recent = self._fetch_home_bundle()
            def _apply() -> None:
                self.query_one("#view_trending", TrendingView).populate_data(
                    recs, trending, playlists, recent=recent
                )
                if self.active_view_id == "view_trending":
                    self._focus_content()
            self._safe_call(_apply)
        except Exception as e:
            self._safe_call(lambda: self.query_one("#view_trending", TrendingView).hide_loading())
            self._safe_call(self.notify, f"could not load recommendations: {e}", severity="error")
        finally:
            self._safe_call(self._set_status, "")

    @work(group="rec_loader", exclusive=True, thread=True)
    def refresh_recommendations_worker(self) -> None:
        self._safe_call(self._set_status, "refreshing personalized picks…")
        try:
            recs, trending, playlists, recent = self._fetch_home_bundle()
            def _apply() -> None:
                self.query_one("#view_trending", TrendingView).populate_data(
                    recs, trending, playlists, recent=recent
                )
                if self.active_view_id == "view_trending":
                    self._focus_content()
            self._safe_call(_apply)
            self._safe_call(self.notify, "recommendations updated")
        except Exception as e:
            self._safe_call(lambda: self.query_one("#view_trending", TrendingView).hide_loading())
            self._safe_call(self.notify, f"could not refresh recommendations: {e}", severity="error")
        finally:
            self._safe_call(self._set_status, "")
        self.check_update_quietly()

    @work(group="explore_loader", exclusive=True, thread=True)
    def load_explore_worker(self) -> None:
        self._safe_call(self._set_status, "loading new releases & moods…")
        try:
            data = self.api.get_explore_sections()
            def _apply() -> None:
                ev = self.query_one("#view_explore", ExploreView)
                ev.set_explore(
                    data.get("new_releases", []),
                    data.get("trending", []),
                    data.get("moods", []),
                )
                if self.active_view_id == "view_explore":
                    self._focus_content()
            self._safe_call(_apply)
        except Exception as e:
            self._safe_call(lambda: self.query_one("#view_explore", ExploreView).hide_loading())
            self._safe_call(self.notify, f"could not load explore: {e}", severity="error")
        finally:
            self._safe_call(self._set_status, "")

    @work(group="mood_loader", exclusive=True, thread=True)
    def load_mood_worker(self, category: Category) -> None:
        def _open() -> None:
            mv = self.query_one("#view_mood_detail", MoodDetailView)
            mv.show_loading(
                f"LOADING {category.title.upper()}",
                f"fetching {category.title} playlists…",
            )
            self.switch_view("view_mood_detail", focus=False)
        self._safe_call(_open)
        self._safe_call(self._set_status, f"loading {category.title}…")
        try:
            playlists = self.api.get_mood_playlists(category.params) if category.params else []
            def _update() -> None:
                mv = self.query_one("#view_mood_detail", MoodDetailView)
                mv.set_playlists(category, playlists)
                if self.active_view_id == "view_mood_detail":
                    self._focus_content()
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(lambda: self.query_one("#view_mood_detail", MoodDetailView).hide_loading())
            self._safe_call(self.notify, f"could not load mood: {e}", severity="error")
        finally:
            self._safe_call(self._set_status, "")

    @work(group="quiet_update", exclusive=True, thread=True)
    def check_update_quietly(self) -> None:
        try:
            import subprocess
            from pathlib import Path
            script = Path(__file__).resolve().parent / "update.sh"
            res = subprocess.run(
                ["bash", str(script), "--check"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            if "Update available" in res.stdout:
                self._safe_call(
                    self.notify,
                    "update available · run 'ame --update' to upgrade",
                    timeout=5,
                )
        except Exception:
            pass

    @work(group="album_loader", exclusive=True, thread=True)
    def load_album_worker(self, album_id: str, title: str = "") -> None:
        label = title or "album"
        self._safe_call(self._set_status, f"opening {label}…")
        def _open():
            self.query_one("#view_playlist_detail", PlaylistDetailView).show_loading(
                "LOADING ALBUM", f"fetching tracks for {label}…"
            )
            self.switch_view("view_playlist_detail")
        self._safe_call(_open)
        try:
            album_info, tracks = self.api.get_album(album_id)
            def _update():
                self.query_one("#view_playlist_detail", PlaylistDetailView).set_album(
                    album_info, tracks
                )
                if self.active_view_id == "view_playlist_detail":
                    self._focus_content()
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(lambda: self.query_one("#view_playlist_detail", PlaylistDetailView).hide_loading())
            self._safe_call(self.notify, f"could not open album: {e}", severity="error")
        finally:
            self._safe_call(self._set_status, "")

    @work(group="artist_loader", exclusive=True, thread=True)
    def load_artist_worker(self, artist_id: str, name: str = "") -> None:
        label = name or "artist"
        self._safe_call(self._set_status, f"opening {label}…")
        def _open():
            self.query_one("#view_artist_detail", ArtistDetailView).show_loading(
                "LOADING ARTIST", f"fetching discography for {label}…"
            )
            self.switch_view("view_artist_detail")
        self._safe_call(_open)
        try:
            artist_info, songs, albums = self.api.get_artist(artist_id)
            def _update():
                self.query_one("#view_artist_detail", ArtistDetailView).set_artist(
                    artist_info, songs, albums
                )
                if self.active_view_id == "view_artist_detail":
                    self._focus_content()
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(lambda: self.query_one("#view_artist_detail", ArtistDetailView).hide_loading())
            self._safe_call(self.notify, f"could not open artist: {e}", severity="error")
        finally:
            self._safe_call(self._set_status, "")

    @work(group="playlist_loader", exclusive=True, thread=True)
    def load_playlist_worker(self, playlist: Playlist) -> None:
        self._safe_call(self._set_status, f"opening {playlist.title}…")
        def _open():
            self.query_one("#view_playlist_detail", PlaylistDetailView).show_loading(
                "LOADING PLAYLIST", f"fetching tracks for {playlist.title}…"
            )
            self.switch_view("view_playlist_detail")
        self._safe_call(_open)
        try:
            pl_info, tracks = self.api.get_playlist(playlist.id)

            def _update():
                self.query_one("#view_playlist_detail", PlaylistDetailView).set_playlist(
                    pl_info, tracks
                )
                if self.active_view_id == "view_playlist_detail":
                    self._focus_content()
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(lambda: self.query_one("#view_playlist_detail", PlaylistDetailView).hide_loading())
            self._safe_call(self.notify, f"could not open playlist: {e}", severity="error")
        finally:
            self._safe_call(self._set_status, "")

    @work(group="radio_loader", exclusive=True, thread=True)
    def start_radio_worker(self, seed_track: Track) -> None:
        self._safe_call(self._set_status, f"building radio for {seed_track.title}…")
        def _show():
            self.query_one("#view_radio", RadioView).show_loading(
                "BUILDING RADIO", f"generating radio station for {seed_track.title}…"
            )
            self.switch_view("view_radio")
        self._safe_call(_show)
        try:
            recommendations = self.api.get_radio(
                seed_track.id,
                limit=35,
                existing=[seed_track],
                prefer_videos=is_likely_video(seed_track),
            )
            recs = [t for t in recommendations if t.id != seed_track.id] or recommendations

            def _update():
                self.query_one("#view_radio", RadioView).set_radio_tracks(seed_track, recs)
                self.player.play_with_queue(seed_track, recs)
                self._open_now_playing()
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(lambda: self.query_one("#view_radio", RadioView).hide_loading())
            self._safe_call(self.notify, f"could not build radio: {e}", severity="error")
        finally:
            self._safe_call(self._set_status, "")

    @work(group="autoplay_loader", exclusive=True, thread=True)
    def run_autoplay_worker(self, last_track: Track) -> None:
        self._safe_call(self._set_status, "finding more music…")
        try:
            existing = list(self.player.history[-15:]) + [last_track]
            recs = self.api.get_radio(
                last_track.id,
                limit=20,
                existing=existing,
                prefer_videos=is_likely_video(last_track),
            )
            recs = [t for t in recs if t.id != last_track.id] or recs
            if recs:
                def _play():
                    if (
                        self.player.current_track is None
                        and not self.player.is_playing
                        and not self.player.queue
                    ):
                        self.player.play_with_queue(recs[0], recs[1:])
                self._safe_call(_play)
        except Exception as e:
            self._safe_call(self.notify, f"autoplay failed: {e}", severity="warning")
        finally:
            self._safe_call(self._set_status, "")

    @work(group="related_queue_loader", exclusive=True, thread=True)
    def load_related_queue_worker(self, seed_track: Track) -> None:
        self._safe_call(self._set_status, "finding related music…")
        try:
            recommendations = self.api.get_radio(
                seed_track.id,
                limit=35,
                existing=[seed_track],
                prefer_videos=is_likely_video(seed_track),
            )
            recs = [t for t in recommendations if t.id != seed_track.id] or recommendations
            if recs:
                def _update():
                    if self.player.current_track is None or self.player.current_track.id == seed_track.id:
                        self.player.append_queue(recs)
                        self.notify(f"queued {len(recs)} related tracks", timeout=3)
                self._safe_call(_update)
        except Exception as e:
            self._safe_call(self.notify, f"could not load related tracks: {e}", severity="warning")
        finally:
            self._safe_call(self._set_status, "")

    @work(group="search_loader", exclusive=True, thread=True)
    def search_worker(self, query: str, live: bool = False, token: int = 0) -> None:
        # Live suggestions stay quiet (no status flicker, no error popups) and
        # fetch a smaller batch; an explicit submit gets the full result set.
        if not live:
            self._safe_call(self._set_status, f"searching “{query}”…")
            self._safe_call(
                lambda: self.query_one("#view_search", SearchView).show_loading(
                    "SEARCHING", f"searching YouTube Music for “{query}”…"
                )
            )
        try:
            song_lim = 10 if live else 30
            other_lim = 6 if live else 20
            with ThreadPoolExecutor(max_workers=4) as pool:
                f_songs = pool.submit(self.api.search_songs, query, song_lim)
                f_albums = pool.submit(self.api.search_albums, query, other_lim)
                f_artists = pool.submit(self.api.search_artists, query, other_lim)
                f_playlists = pool.submit(self.api.search_playlists, query, other_lim)
                songs = f_songs.result()
                albums = f_albums.result() if hasattr(self.api, "search_albums") else []
                artists = f_artists.result() if hasattr(self.api, "search_artists") else []
                playlists = f_playlists.result()

            def _apply() -> None:
                if token and token != self._search_seq:
                    return
                self.query_one("#view_search", SearchView).set_results(
                    songs, playlists, albums=albums, artists=artists
                )

            self._safe_call(_apply)
        except Exception as e:
            if not live:
                self._safe_call(lambda: self.query_one("#view_search", SearchView).hide_loading())
                self._safe_call(self.notify, f"search failed: {e}", severity="error")
        finally:
            if not live:
                self._safe_call(self._set_status, "")

    # ------------------------------------------------------------------ #
    # Navigation & Now Playing window
    # ------------------------------------------------------------------ #
    def switch_view(self, view_id: str, focus: bool = True) -> None:
        if self.active_view_id != view_id:
            if self.active_view_id != "view_now_playing":
                self.previous_view_id = self.active_view_id
                self._browse_view_id = self.active_view_id
            self.active_view_id = view_id

        self.query_one("#content_switcher", ContentSwitcher).current = view_id
        self.query_one("#top_bar", TopBar).active_view_id = view_id
        try:
            self.query_one("#sidebar", Sidebar).active_view_id = view_id
        except Exception:
            pass

        is_np = view_id == "view_now_playing"
        try:
            self.query_one("#player_bar", PlayerBar).display = not is_np
        except Exception:
            pass
        if is_np:
            try:
                np = self.query_one("#view_now_playing", NowPlayingView)
                np.set_queue_preview(list(self.player.queue))
                np._resize_art_box()
            except Exception:
                pass
        self._sync_sidebar_visibility()
        self._sync_visualizer_visibility()

        if view_id == "view_queue":
            self.query_one("#view_queue", QueueView).set_queue(list(self.player.queue))
        elif view_id == "view_stats":
            self._refresh_stats_view()
        elif view_id == "view_favourites":
            self._refresh_favourites()
        elif view_id == "view_settings":
            self._refresh_settings_view()
        elif view_id == "view_explore":
            try:
                ev = self.query_one("#view_explore", ExploreView)
                if not ev._loaded:
                    ev.show_loading(
                        "LOADING EXPLORE",
                        "fetching new releases, trending & moods…",
                    )
                    self.load_explore_worker()
            except Exception:
                pass

        if focus and not is_np:
            self._focus_content()

    def _sync_sidebar_visibility(self) -> None:
        try:
            sb = self.query_one("#sidebar", Sidebar)
            wide_enough = self.size.width == 0 or self.size.width >= 94
            sb.display = (
                self.settings.data.show_sidebar
                and self.active_view_id != "view_now_playing"
                and wide_enough
            )
        except Exception:
            pass

    def _refresh_settings_view(self) -> None:
        try:
            s = self.settings.data
            rows = [
                (
                    "autoplay",
                    "autoplay related tracks",
                    "on" if self.player.autoplay else "off",
                    "automatically queue similar tracks when queue finishes",
                ),
                (
                    "crossfade",
                    "crossfade duration [x]",
                    f"{self.player.crossfade_seconds}s"
                    if self.player.crossfade_seconds > 0
                    else "off (gapless)",
                    "equal-power dual-mpv crossfade between tracks",
                ),
                (
                    "loudness",
                    "loudness normalization [N]",
                    "on" if self.player.loudness_normalization else "off",
                    "balance track volume using YouTube loudnessDb metadata",
                ),
                (
                    "speed",
                    "playback speed [C]",
                    f"{self.player.speed:.2g}×",
                    "playback rate across mpv decks (0.75× – 2×)",
                ),
                (
                    "show_nerd_stats",
                    "nerd stats (codec / cache / gain)",
                    "on" if s.show_nerd_stats else "off",
                    "show technical audio telemetry inside Now Playing",
                ),
                (
                    "dynamic_art_tint",
                    "dynamic album accent tint [D]",
                    "on" if self._dynamic_tint else "off",
                    "blend dominant cover-art color into active theme",
                ),
                (
                    "show_sidebar",
                    "navigation sidebar [b]",
                    "on" if s.show_sidebar else "off",
                    "collapsible left rail when terminal width >= 94",
                ),
                (
                    "visualizer_enabled",
                    "equalizer strip [v]",
                    "on" if self._visualizer_on else "off",
                    "sleek 1-row graphic equalizer above the player bar",
                ),
                (
                    "discord_rpc",
                    "discord rich presence",
                    "on" if s.discord_rpc else "off",
                    "broadcast current track to local Discord IPC socket",
                ),
                (
                    "mpris_enabled",
                    "mpris2 desktop media bridge",
                    "on" if s.mpris_enabled else "off",
                    "expose playerctl / Waybar / desktop media controls",
                ),
            ]
            self.query_one("#view_settings", SettingsView).set_rows(rows)
        except Exception:
            pass

    def _refresh_stats_view(self, period: Optional[str] = None) -> None:
        try:
            sv = self.query_one("#view_stats", StatsView)
            target_period = period or sv.period or "month"
            sv.set_summary(self.stats.summary(target_period))
        except Exception:
            pass

    def _open_now_playing(self) -> None:
        if self.active_view_id != "view_now_playing":
            self.switch_view("view_now_playing", focus=False)

    def _minimize_now_playing(self) -> None:
        target = self._browse_view_id or "view_trending"
        if target == "view_now_playing":
            target = "view_trending"
        self.switch_view(target)

    def action_toggle_now_playing(self) -> None:
        if self.active_view_id == "view_now_playing":
            self._minimize_now_playing()
        else:
            self._open_now_playing()

    def on_resize(self, event: events.Resize) -> None:
        self._sync_sidebar_visibility()
        self._sync_visualizer_visibility()

    def switch_to_previous_view(self) -> None:
        self.switch_view(self.previous_view_id)

    def _focus_content(self) -> None:
        try:
            view = self.query_one(f"#{self.active_view_id}")
            widget = view.primary_widget() if hasattr(view, "primary_widget") else None
            if widget is not None:
                widget.focus()
        except Exception:
            pass

    def on_top_bar_tab_clicked(self, message: TopBar.TabClicked) -> None:
        self.switch_view(message.view_id)

    def _current_view(self):
        try:
            return self.query_one(f"#{self.active_view_id}")
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Messages from views & player controls
    # ------------------------------------------------------------------ #
    def on_play_track_msg(self, msg: PlayTrackMsg) -> None:
        if getattr(msg, "auto_related", False):
            self.player.play_with_queue(msg.track, [])
            self.load_related_queue_worker(msg.track)
        else:
            self.player.play_with_queue(msg.track, msg.remaining_tracks)
        self._open_now_playing()

    def on_play_queue_index_msg(self, msg: PlayQueueIndexMsg) -> None:
        self.player.play_from_queue(msg.index)
        self._open_now_playing()

    def on_jump_queue_index_msg(self, msg: JumpQueueIndexMsg) -> None:
        self.player.play_from_queue(msg.index)

    def on_seek_to_msg(self, msg: SeekToMsg) -> None:
        self.player.seek_to(msg.seconds)

    def on_move_queue_item_msg(self, msg: MoveQueueItemMsg) -> None:
        self.player.move_queue_item(msg.old_index, msg.new_index)

    def on_maximize_now_playing_msg(self, msg: MaximizeNowPlayingMsg) -> None:
        self._open_now_playing()

    def on_minimize_now_playing_msg(self, msg: MinimizeNowPlayingMsg) -> None:
        self._minimize_now_playing()

    def on_player_control_msg(self, msg: PlayerControlMsg) -> None:
        action = msg.action
        if action == "toggle_play":
            self.action_toggle_play()
        elif action == "prev":
            self.action_prev_track()
        elif action == "next":
            self.action_next_track()
        elif action == "fav":
            self._toggle_current_track_favourite()
        elif action == "loop":
            self.action_cycle_loop()
        elif action == "crossfade":
            self.action_cycle_crossfade()
        elif action == "loudness":
            self.action_toggle_loudness()
        elif action == "speed":
            self.action_cycle_speed()
        elif action == "sleep":
            self.action_cycle_sleep_timer()
        elif action == "toggle_lyrics":
            self.action_toggle_lyrics_panel()
        elif action == "toggle_queue":
            self.action_toggle_up_next_panel()
        elif action == "toggle_tint":
            self.action_toggle_dynamic_tint()
        elif action == "close_side":
            try:
                self.query_one("#view_now_playing", NowPlayingView).close_side_panel()
            except Exception:
                pass
        elif action == "open_artist":
            self.action_open_current_artist()
        elif action == "open_album":
            self.action_open_current_album()
        elif action == "minimize":
            self._minimize_now_playing()
        elif action == "maximize":
            self._open_now_playing()
        elif action == "open_queue":
            self.switch_view("view_queue")

    def on_queue_track_msg(self, msg: QueueTrackMsg) -> None:
        self.player.append_queue(msg.track)

    def on_start_radio_msg(self, msg: StartRadioMsg) -> None:
        self.start_radio_worker(msg.track)

    def on_open_playlist_msg(self, msg: OpenPlaylistMsg) -> None:
        self.load_playlist_worker(msg.playlist)

    def on_open_mood_msg(self, msg: OpenMoodMsg) -> None:
        self.load_mood_worker(msg.category)

    def on_open_album_msg(self, msg: OpenAlbumMsg) -> None:
        self.load_album_worker(msg.album_id, msg.title)

    def on_open_artist_msg(self, msg: OpenArtistMsg) -> None:
        self.load_artist_worker(msg.artist_id, msg.name)

    def on_execute_search_msg(self, msg: ExecuteSearchMsg) -> None:
        self._search_seq += 1
        self.search_worker(msg.query, msg.live, self._search_seq)

    def on_sidebar_nav_msg(self, msg: SidebarNavMsg) -> None:
        if msg.target == "view_search":
            self.action_focus_search()
        elif msg.target == "view_now_playing":
            self._open_now_playing()
        else:
            self.switch_view(msg.target)

    def on_toggle_setting_msg(self, msg: ToggleSettingMsg) -> None:
        k = msg.key
        s = self.settings.data
        if k == "autoplay":
            enabled = self.player.toggle_autoplay()
            s.autoplay = enabled
            self.settings.save()
            self.notify(f"autoplay · {'on' if enabled else 'off'}", timeout=2)
        elif k == "crossfade":
            self.action_cycle_crossfade()
        elif k == "loudness":
            self.action_toggle_loudness()
        elif k == "speed":
            self.action_cycle_speed()
        elif k == "show_nerd_stats":
            s.show_nerd_stats = not s.show_nerd_stats
            self.settings.save()
            try:
                self.query_one("#view_now_playing", NowPlayingView).set_show_nerd_stats(
                    s.show_nerd_stats
                )
            except Exception:
                pass
            self.notify(f"nerd stats · {'on' if s.show_nerd_stats else 'off'}", timeout=2)
        elif k == "dynamic_art_tint":
            self.action_toggle_dynamic_tint()
        elif k == "show_sidebar":
            self.action_toggle_sidebar()
        elif k == "visualizer_enabled":
            self.action_toggle_visualizer()
        elif k == "discord_rpc":
            s.discord_rpc = not s.discord_rpc
            self.settings.save()
            if s.discord_rpc:
                if self.discord is None:
                    self.discord = DiscordPresence()
                self.discord.update(
                    self.player.current_track,
                    self.player.is_playing,
                    self.player.is_paused,
                    self.player.playback_pos,
                )
            else:
                if self.discord is not None:
                    self.discord.close()
                    self.discord = None
            self.notify(f"discord rpc · {'on' if s.discord_rpc else 'off'}", timeout=2)
        elif k == "mpris_enabled":
            s.mpris_enabled = not s.mpris_enabled
            self.settings.save()
            if s.mpris_enabled:
                if self.mpris is None:
                    self.mpris = MPRISController(on_command=self._on_mpris_command)
                if self.player.current_track is not None:
                    self.mpris.update_track(self.player.current_track)
            else:
                if self.mpris is not None:
                    self.mpris.close()
                    self.mpris = None
            self.notify(f"mpris2 · {'on' if s.mpris_enabled else 'off'}", timeout=2)
        self._refresh_settings_view()

    # ------------------------------------------------------------------ #
    # Global key handling
    # ------------------------------------------------------------------ #
    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, Input) and event.key == "escape":
            self._focus_content()
            event.prevent_default()

    def action_focus_search(self) -> None:
        self.switch_view("view_search", focus=False)
        try:
            self.query_one("#search_input", Input).focus()
        except Exception:
            pass

    def action_toggle_play(self) -> None:
        self.player.toggle_pause()

    def action_next_track(self) -> None:
        self.player.next_track()

    def action_prev_track(self) -> None:
        self.player.prev_track()

    def action_cycle_loop(self) -> None:
        mode = self.player.cycle_loop_mode()
        try:
            self.query_one("#player_bar", PlayerBar).set_loop_mode(mode.value)
            self.query_one("#view_now_playing", NowPlayingView).set_loop_mode(mode.value)
        except Exception:
            pass

    def action_cycle_crossfade(self) -> None:
        seconds = self.player.cycle_crossfade()
        self.settings.data.crossfade_seconds = seconds
        self.settings.save()
        self._refresh_settings_view()
        try:
            self.query_one("#player_bar", PlayerBar).set_crossfade(seconds)
            self.query_one("#view_now_playing", NowPlayingView).set_crossfade(seconds)
        except Exception:
            pass
        self.notify(
            f"crossfade · {seconds}s" if seconds > 0 else "crossfade · off (gapless)",
            timeout=2,
        )

    def action_toggle_loudness(self) -> None:
        enabled = self.player.toggle_loudness_normalization()
        self.settings.data.loudness_normalization = enabled
        self.settings.save()
        self._refresh_settings_view()
        try:
            self.query_one("#player_bar", PlayerBar).set_loudness(enabled)
            self.query_one("#view_now_playing", NowPlayingView).set_loudness(enabled)
        except Exception:
            pass
        self.notify(
            f"loudness normalization · {'on' if enabled else 'off'}",
            timeout=2,
        )

    def action_cycle_speed(self) -> None:
        rate = self.player.cycle_speed()
        self.settings.data.speed = rate
        self.settings.save()
        self._refresh_settings_view()
        self.notify(f"playback speed · {rate:.2g}×", timeout=2)

    def action_cycle_sleep_timer(self) -> None:
        idx = (
            SLEEP_TIMER_PRESETS_MIN.index(self._sleep_minutes)
            if self._sleep_minutes in SLEEP_TIMER_PRESETS_MIN
            else 0
        )
        nxt = SLEEP_TIMER_PRESETS_MIN[(idx + 1) % len(SLEEP_TIMER_PRESETS_MIN)]
        self._sleep_minutes = nxt
        if nxt <= 0:
            self._sleep_deadline = None
            saved_vol = self._sleep_fade_base_vol
            self._sleep_fade_base_vol = None
            if saved_vol is not None:
                self.player.set_volume(saved_vol)
            self._update_sleep_ui(0)
            self.notify("sleep timer · off", timeout=2)
        else:
            self._sleep_fade_base_vol = None
            self._sleep_deadline = time.monotonic() + nxt * 60.0
            self._update_sleep_ui(nxt * 60)
            self.notify(f"sleep timer · {nxt} min", timeout=2)

    def action_toggle_lyrics_panel(self) -> None:
        if self.active_view_id != "view_now_playing":
            self._open_now_playing()
        try:
            self.query_one("#view_now_playing", NowPlayingView).toggle_side_panel("lyrics")
        except Exception:
            pass

    def action_toggle_up_next_panel(self) -> None:
        if self.active_view_id != "view_now_playing":
            self._open_now_playing()
        try:
            self.query_one("#view_now_playing", NowPlayingView).toggle_side_panel("queue")
        except Exception:
            pass

    def action_toggle_dynamic_tint(self) -> None:
        self._dynamic_tint = not self._dynamic_tint
        self.settings.data.dynamic_art_tint = self._dynamic_tint
        self.settings.save()
        self._refresh_settings_view()
        try:
            self.query_one("#view_now_playing", NowPlayingView).set_dynamic_tint(self._dynamic_tint)
        except Exception:
            pass
        if self._dynamic_tint and self._current_artwork_img is not None:
            self._apply_dynamic_art_palette(self._current_artwork_img, notify_user=False)
        else:
            self._apply_theme(self.theme_manager.apply_artwork_tint(None), notify_user=False)
        self.notify(f"dynamic album tint · {'on' if self._dynamic_tint else 'off'}", timeout=2)

    def action_open_current_artist(self) -> None:
        tr = self._get_highlighted_track() or self.player.current_track
        if tr and tr.artist_id:
            self.load_artist_worker(tr.artist_id, tr.artist)
        else:
            self.notify("no artist page link for this track", severity="warning", timeout=2)

    def action_open_current_album(self) -> None:
        tr = self._get_highlighted_track() or self.player.current_track
        if tr and tr.album_id:
            self.load_album_worker(tr.album_id, tr.album)
        else:
            self.notify("no album page link for this track", severity="warning", timeout=2)

    def action_play_next_selected(self) -> None:
        track = self._get_highlighted_track()
        if not track:
            return
        self.player.play_next(track)
        self.notify(f"playing next · {track.title}", timeout=2)

    def action_move_queue_down(self) -> None:
        if self.active_view_id == "view_queue":
            try:
                self.query_one("#view_queue", QueueView).move_highlighted(1)
            except Exception:
                pass

    def action_move_queue_up(self) -> None:
        if self.active_view_id == "view_queue":
            try:
                self.query_one("#view_queue", QueueView).move_highlighted(-1)
            except Exception:
                pass

    def action_cursor_down(self) -> None:
        focused = self.focused
        if focused is not None and hasattr(focused, "action_cursor_down"):
            focused.action_cursor_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if focused is not None and hasattr(focused, "action_cursor_up"):
            focused.action_cursor_up()

    @work(group="queue_playlist", thread=True)
    def _queue_playlist_worker(self, playlist: Playlist) -> None:
        try:
            _, tracks = self.api.get_playlist(playlist.id)
            self.player.append_queue(tracks)
            self._safe_call(self.notify, f"queued {len(tracks)} tracks from {playlist.title}", timeout=2)
        except Exception as e:
            self._safe_call(self.notify, f"could not queue playlist: {e}", severity="error")

    def action_queue_selected(self) -> None:
        was_idle = not self.player.is_playing and self.player.current_track is None and not self.player.queue
        track = self._get_highlighted_track()
        if track:
            self.player.append_queue(track)
            if was_idle:
                self._open_now_playing()
            return

        playlist = self._get_highlighted_playlist()
        if playlist:
            self.notify(f"adding {playlist.title}…", timeout=2)
            self._queue_playlist_worker(playlist)

    def action_queue_all(self) -> None:
        was_idle = not self.player.is_playing and self.player.current_track is None and not self.player.queue
        view = self._current_view()
        tracks = view.get_all_tracks() if hasattr(view, "get_all_tracks") else []
        if not tracks:
            self.notify("nothing to queue here", severity="warning", timeout=2)
            return
        self.player.append_queue(tracks)
        if was_idle:
            self._open_now_playing()

    def action_play_all(self) -> None:
        view = self._current_view()
        tracks = view.get_all_tracks() if hasattr(view, "get_all_tracks") else []
        if not tracks:
            self.notify("nothing to play here", severity="warning", timeout=2)
            return
        self.player.play_with_queue(tracks[0], tracks[1:])
        self._open_now_playing()

    def action_remove_selected(self) -> None:
        if self.active_view_id == "view_favourites":
            track = self._get_highlighted_track()
            if track is not None:
                self.favourites.remove(track.id)
                self._refresh_favourites()
                self._sync_current_favourite_badge()
                self.notify(f"removed {track.title} from favourites", timeout=2)
            return

        if self.active_view_id != "view_queue":
            return
        try:
            table = self.query_one("#queue_table")
            index = table.cursor_row
            self.player.remove_from_queue(index)
        except Exception:
            pass

    def _sync_current_favourite_badge(self) -> None:
        cur = self.player.current_track
        is_fav = bool(cur and self.favourites.contains(cur.id))
        try:
            self.query_one("#player_bar", PlayerBar).set_favourite(is_fav)
            self.query_one("#view_now_playing", NowPlayingView).set_favourite(is_fav)
        except Exception:
            pass

    def _toggle_current_track_favourite(self) -> None:
        track = self.player.current_track
        if track is None:
            self.notify("nothing to favourite", severity="warning", timeout=2)
            return
        now_favourite = self.favourites.toggle(track)
        self._refresh_favourites()
        self._sync_current_favourite_badge()
        verb = "added to" if now_favourite else "removed from"
        self.notify(f"{track.title} {verb} favourites", timeout=2)

    def action_toggle_favourite(self) -> None:
        if self.active_view_id == "view_now_playing":
            self._toggle_current_track_favourite()
            return
        track = self._get_highlighted_track() or self.player.current_track
        if track is None:
            self.notify("nothing to favourite", severity="warning", timeout=2)
            return
        now_favourite = self.favourites.toggle(track)
        self._refresh_favourites()
        self._sync_current_favourite_badge()
        verb = "added to" if now_favourite else "removed from"
        self.notify(f"{track.title} {verb} favourites", timeout=2)

    def _refresh_favourites(self) -> None:
        try:
            fv = self.query_one("#view_favourites", FavouritesView)
            fv.set_favourites(self.favourites.all())
            artists = self.stats.summary("all").top_artists
            fv.set_library_extra(self.recent_history.all(), artists)
        except Exception:
            pass

    def action_switch_view_favourites(self) -> None:
        self._refresh_favourites()
        self.switch_view("view_favourites")

    def action_radio_selected(self) -> None:
        track = self._get_highlighted_track() or self.player.current_track
        if track:
            self.start_radio_worker(track)
        else:
            self.notify("highlight or play a song first", severity="warning", timeout=2)

    def _get_highlighted_track(self) -> Optional[Track]:
        view = self._current_view()
        if view is not None and hasattr(view, "get_selected_track"):
            return view.get_selected_track()
        return None

    def _get_highlighted_playlist(self) -> Optional[Playlist]:
        view = self._current_view()
        if view is not None and hasattr(view, "get_selected_playlist"):
            return view.get_selected_playlist()
        return None

    def action_volume_up(self) -> None:
        self.player.adjust_volume(5)
        self.query_one("#top_bar", TopBar).volume = self.player.volume

    def action_volume_down(self) -> None:
        self.player.adjust_volume(-5)
        self.query_one("#top_bar", TopBar).volume = self.player.volume

    def action_seek_forward(self) -> None:
        self.player.seek(5.0)

    def action_seek_backward(self) -> None:
        self.player.seek(-5.0)

    def action_shuffle_queue(self) -> None:
        self.player.shuffle_queue()

    def action_clear_queue(self) -> None:
        self.player.clear_queue()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen(self.theme_manager.current_theme.name))

    def action_toggle_visualizer(self) -> None:
        if not CavaSpectrum.available():
            self.notify("visualizer needs cava installed", severity="warning", timeout=2)
            return
        self._visualizer_on = not self._visualizer_on
        self.settings.data.visualizer_enabled = self._visualizer_on
        self.settings.save()
        self._refresh_settings_view()
        if self._visualizer_on and self.spectrum is None:
            self.spectrum = CavaSpectrum(on_update=self._on_spectrum_bands)
            self.spectrum.start()
        self._sync_visualizer_visibility()
        if self._visualizer_on and not self.player.is_playing:
            self.notify("visualizer shows while playing", timeout=2)

    def action_toggle_sidebar(self) -> None:
        s = self.settings.data
        s.show_sidebar = not s.show_sidebar
        self.settings.save()
        self._sync_sidebar_visibility()
        self._refresh_settings_view()
        if s.show_sidebar and self.size.width > 0 and self.size.width < 94:
            self.notify("sidebar shows when width >= 94", timeout=2)

    def action_handle_escape(self) -> None:
        if isinstance(self.focused, Input):
            self._focus_content()
            return
        if self.active_view_id == "view_now_playing":
            self._minimize_now_playing()
            return
        if self.active_view_id in ("view_playlist_detail", "view_artist_detail", "view_mood_detail"):
            self.switch_to_previous_view()
            return
        self._focus_content()

    def action_switch_view_home(self) -> None:
        self.switch_view("view_trending")

    def action_switch_view_explore(self) -> None:
        self.switch_view("view_explore")

    def action_switch_view_settings(self) -> None:
        self.switch_view("view_settings")

    def action_switch_view_trending(self) -> None:
        if self.active_view_id == "view_trending":
            try:
                self.query_one("#view_trending", TrendingView).show_loading(
                    "REFRESHING RECOMMENDATIONS",
                    "fetching fresh AI recommendations & live charts…",
                )
            except Exception:
                pass
            self.refresh_recommendations_worker()
        else:
            self.switch_view("view_trending")

    def action_refresh_recommendations(self) -> None:
        try:
            self.query_one("#view_trending", TrendingView).show_loading(
                "REFRESHING RECOMMENDATIONS",
                "fetching fresh AI recommendations & live charts…",
            )
        except Exception:
            pass
        self.refresh_recommendations_worker()

    def action_next_tab(self) -> None:
        try:
            view = self.query_one(f"#{self.active_view_id}")
            tabs = view.query_one(TabbedContent)
            panes = [p.id for p in tabs.query(TabPane)]
            if panes and tabs.active in panes:
                curr_idx = panes.index(tabs.active)
                next_idx = (curr_idx + 1) % len(panes)
                tabs.active = panes[next_idx]
        except Exception:
            pass

    def action_prev_tab(self) -> None:
        try:
            view = self.query_one(f"#{self.active_view_id}")
            tabs = view.query_one(TabbedContent)
            panes = [p.id for p in tabs.query(TabPane)]
            if panes and tabs.active in panes:
                curr_idx = panes.index(tabs.active)
                prev_idx = (curr_idx - 1) % len(panes)
                tabs.active = panes[prev_idx]
        except Exception:
            pass

    @work(group="update_checker", exclusive=True, thread=True)
    def action_check_app_update(self) -> None:
        self._safe_call(self._set_status, "checking for updates…")
        try:
            import subprocess
            from pathlib import Path
            script = Path(__file__).resolve().parent / "update.sh"
            res = subprocess.run(
                ["bash", str(script), "--check"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=6,
            )
            out = res.stdout
            if "Update available" in out:
                self._safe_call(
                    self.notify,
                    "update available! Run 'ame --update' in terminal",
                    severity="information",
                    timeout=5,
                )
            elif "up to date" in out:
                self._safe_call(self.notify, "ame is up to date", timeout=3)
            else:
                self._safe_call(self.notify, "checked for updates", timeout=2)
        except Exception:
            self._safe_call(self.notify, "could not check for updates", severity="warning", timeout=3)
        self._safe_call(self._set_status, "")

    def action_switch_view_radio(self) -> None:
        self.switch_view("view_radio")

    def action_switch_view_search(self) -> None:
        self.action_focus_search()

    def action_switch_view_queue(self) -> None:
        self.switch_view("view_queue")

    def action_switch_view_stats(self) -> None:
        if self.active_view_id == "view_stats":
            order = ("month", "all", "today")
            sv = self.query_one("#view_stats", StatsView)
            nxt = order[(order.index(sv.period) + 1) % len(order)] if sv.period in order else "month"
            self._refresh_stats_view(nxt)
        else:
            self.switch_view("view_stats")

    def action_quit_app(self) -> None:
        self._flush_listening_stats()
        self._stop_spectrum()
        if self.mpris:
            self.mpris.close()
        if self.discord:
            self.discord.close()
        self.player.cleanup()
        self.exit()

    def _stop_spectrum(self) -> None:
        if self.spectrum is not None:
            self.spectrum.stop()
            self.spectrum = None

    def on_unmount(self) -> None:
        self._flush_listening_stats()
        self._stop_spectrum()
        if self.mpris:
            self.mpris.close()
        if self.discord:
            self.discord.close()
        self.player.cleanup()


if __name__ == "__main__":
    AmeApp().run()
