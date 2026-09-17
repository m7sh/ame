"""
app.py - Main Textual application for ame.

An unauthenticated, keyboard-driven YouTube Music player for the terminal.
Built with Textual, ytmusicapi (guest mode), yt-dlp and a headless mpv daemon.
"""

import threading
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import ContentSwitcher, Input
from textual import work
from textual import events

from api import YTMusicAPI, Track, Playlist, Category
from player import MPVPlayer
from spectrum import CavaSpectrum
from theme import OmarchyThemeManager, ThemeColors
from ui.widgets import TopBar, PlayerBar, HelpScreen, Spectrum
from ui.views import (
    TrendingView,
    RadioView,
    MoodsPlaylistsView,
    PlaylistDetailView,
    SearchView,
    QueueView,
    PlayTrackMsg,
    QueueTrackMsg,
    StartRadioMsg,
    OpenPlaylistMsg,
    SelectCategoryMsg,
    ExecuteSearchMsg,
)


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
        Binding("A", "queue_all", "Queue All", show=False),
        Binding("P", "play_all", "Play All", show=False),
        Binding("d", "remove_selected", "Remove", show=False),
        Binding("R", "radio_selected", "Radio", show=False),
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
        Binding("Q", "quit_app", "Quit", show=False),
        Binding("ctrl+c", "quit_app", "Quit", show=False),
        Binding("t", "switch_view_trending", "Trending", show=False),
        Binding("r", "switch_view_radio", "Radio", show=False),
        Binding("m", "switch_view_moods", "Moods", show=False),
        Binding("s", "switch_view_search", "Search", show=False),
        Binding("q", "switch_view_queue", "Queue", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.theme_manager = OmarchyThemeManager()
        self._theme_version = 0
        self._install_theme()

        self.api = YTMusicAPI()
        self.player = MPVPlayer(resolver=self.api.resolver)
        self.spectrum: Optional[CavaSpectrum] = None
        self._visualizer_on = True

        self.active_view_id = "view_trending"
        self.previous_view_id = "view_trending"

        # Incremented on every search request so stale results from an
        # in-flight worker can be discarded when the query has moved on.
        self._search_seq = 0

    # ------------------------------------------------------------------ #
    # Theme plumbing
    # ------------------------------------------------------------------ #
    def _install_theme(self) -> None:
        """Register the current palette as a fresh Textual theme and apply it."""
        self._theme_version += 1
        theme = OmarchyThemeManager.build_textual_theme(
            self.theme_manager.current_theme, self._theme_version
        )
        self.register_theme(theme)
        self.theme = theme.name

    def _refresh_widgets_theme(self) -> None:
        try:
            self.query_one("#top_bar", TopBar).refresh_theme()
            self.query_one("#player_bar", PlayerBar).refresh_theme()
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
        yield Spectrum(id="spectrum")
        with ContentSwitcher(initial="view_trending", id="content_switcher"):
            yield TrendingView(id="view_trending")
            yield RadioView(id="view_radio")
            yield MoodsPlaylistsView(id="view_moods")
            yield PlaylistDetailView(id="view_playlist_detail")
            yield SearchView(id="view_search")
            yield QueueView(id="view_queue")
        yield PlayerBar(id="player_bar")

    def on_mount(self) -> None:
        self.player.on_track_change = self._on_player_track_change
        self.player.on_state_change = self._on_player_state_change
        self.player.on_progress = self._on_player_progress
        self.player.on_queue_change = self._on_player_queue_change
        self.player.on_autoplay_trigger = self._on_player_autoplay_trigger
        self.player.on_message = self._on_player_message

        self.query_one("#top_bar", TopBar).theme_name = self.theme_manager.current_theme.name

        self.set_interval(0.25, self._tick_progress)
        self.set_interval(1.5, self._check_theme_sync)

        if CavaSpectrum.available():
            self.spectrum = CavaSpectrum(on_update=self._on_spectrum_bands)
            self.spectrum.start()
        else:
            self._visualizer_on = False
        self._sync_visualizer_visibility(False, False)

        self._focus_content()
        self.load_initial_data()

    # ------------------------------------------------------------------ #
    # Periodic timers
    # ------------------------------------------------------------------ #
    def _tick_progress(self) -> None:
        if self.player.is_playing and not self.player.is_paused:
            self.query_one("#player_bar", PlayerBar).set_progress(
                self.player.playback_pos, self.player.duration
            )

    def _check_theme_sync(self) -> None:
        new_theme = self.theme_manager.reload()
        if new_theme:
            self._apply_theme(new_theme)

    def _apply_theme(self, colors: ThemeColors) -> None:
        self._install_theme()
        self.query_one("#top_bar", TopBar).theme_name = colors.name
        self._refresh_widgets_theme()
        self.refresh(layout=True)
        self.notify(f"theme · {colors.name}", timeout=2)

    def action_reload_theme(self) -> None:
        self.theme_manager.load_theme()
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
    # Player callbacks
    # ------------------------------------------------------------------ #
    def _on_player_track_change(self, track: Optional[Track]) -> None:
        def _update():
            bar = self.query_one("#player_bar", PlayerBar)
            bar.set_track(track, self.player.current_stream_quality)
        self._safe_call(_update)

    def _on_player_state_change(self, is_playing: bool, is_paused: bool, is_buffering: bool) -> None:
        def _update():
            self.query_one("#player_bar", PlayerBar).set_state(
                is_playing, is_paused, is_buffering
            )
            top = self.query_one("#top_bar", TopBar)
            top.is_playing = is_playing
            top.is_paused = is_paused
            top.is_buffering = is_buffering
            self._sync_visualizer_visibility(is_playing, is_paused)
        self._safe_call(_update)

    def _on_player_progress(self, pos: float, duration: float) -> None:
        def _update():
            self.query_one("#player_bar", PlayerBar).set_progress(pos, duration)
        self._safe_call(_update)

    def _on_player_queue_change(self) -> None:
        def _update():
            top = self.query_one("#top_bar", TopBar)
            top.queue_len = len(self.player.queue)
            try:
                self.query_one("#view_queue", QueueView).set_queue(list(self.player.queue))
            except Exception:
                pass
        self._safe_call(_update)

    def _on_player_autoplay_trigger(self, last_track: Track) -> None:
        self.run_autoplay_worker(last_track)

    def _on_player_message(self, msg: str) -> None:
        self._safe_call(self.notify, msg, severity="warning", timeout=4)

    def _on_spectrum_bands(self, bands) -> None:
        self._safe_call(self._set_spectrum_bands, bands)

    def _set_spectrum_bands(self, bands) -> None:
        self.query_one("#spectrum", Spectrum).set_bands(bands)

    def _sync_visualizer_visibility(
        self, is_playing: Optional[bool] = None, is_paused: Optional[bool] = None
    ) -> None:
        """Show the equalizer only while audio is actually playing."""
        if is_playing is None:
            is_playing = self.player.is_playing
        if is_paused is None:
            is_paused = self.player.is_paused

        visible = self._visualizer_on and bool(is_playing) and not bool(is_paused)
        try:
            self.query_one("#spectrum", Spectrum).display = visible
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Background workers
    # ------------------------------------------------------------------ #
    @work(group="initial_loader", exclusive=True, thread=True)
    def load_initial_data(self) -> None:
        self._safe_call(self._set_status, "loading charts…")
        try:
            tracks, playlists = self.api.get_charts()
            self._safe_call(
                lambda: self.query_one("#view_trending", TrendingView).populate_data(
                    tracks, playlists
                )
            )
        except Exception as e:
            self._safe_call(self.notify, f"could not load charts: {e}", severity="error")

        self._safe_call(self._set_status, "loading moods…")
        try:
            categories = self.api.get_mood_categories()
            self._safe_call(
                lambda: self.query_one("#view_moods", MoodsPlaylistsView).populate_categories(
                    categories
                )
            )
            first_cat = next((c for cats in categories.values() for c in cats), None)
            if first_cat:
                self.load_category_playlists_worker(first_cat)
        except Exception:
            pass

        self._safe_call(self._set_status, "")

    @work(group="category_loader", exclusive=True, thread=True)
    def load_category_playlists_worker(self, category: Category) -> None:
        self._safe_call(self._set_status, f"loading {category.title}…")
        try:
            playlists = self.api.get_mood_playlists(category.params)
            self._safe_call(
                lambda: self.query_one("#view_moods", MoodsPlaylistsView).populate_playlists(
                    category.title, playlists
                )
            )
        except Exception as e:
            self._safe_call(self.notify, f"could not load playlists: {e}", severity="warning")
        self._safe_call(self._set_status, "")

    @work(group="playlist_loader", exclusive=True, thread=True)
    def load_playlist_worker(self, playlist: Playlist) -> None:
        self._safe_call(self._set_status, f"opening {playlist.title}…")
        try:
            pl_info, tracks = self.api.get_playlist(playlist.id)

            def _update():
                self.query_one("#view_playlist_detail", PlaylistDetailView).set_playlist(
                    pl_info, tracks
                )
                self.switch_view("view_playlist_detail")
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(self.notify, f"could not open playlist: {e}", severity="error")
        self._safe_call(self._set_status, "")

    @work(group="radio_loader", exclusive=True, thread=True)
    def start_radio_worker(self, seed_track: Track) -> None:
        self._safe_call(self._set_status, f"building radio for {seed_track.title}…")
        try:
            recommendations = self.api.get_radio(seed_track.id, limit=35)
            recs = [t for t in recommendations if t.id != seed_track.id] or recommendations

            def _update():
                self.query_one("#view_radio", RadioView).set_radio_tracks(seed_track, recs)
                self.switch_view("view_radio")
                self.player.play(seed_track)
                self.player.append_queue(recs)
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(self.notify, f"could not build radio: {e}", severity="error")
        self._safe_call(self._set_status, "")

    @work(group="autoplay_loader", exclusive=True, thread=True)
    def run_autoplay_worker(self, last_track: Track) -> None:
        self._safe_call(self._set_status, "finding more music…")
        try:
            recs = self.api.get_radio(last_track.id, limit=20)
            recs = [t for t in recs if t.id != last_track.id] or recs
            if recs:
                def _play():
                    self.player.play(recs[0])
                    self.player.append_queue(recs[1:])
                self._safe_call(_play)
        except Exception as e:
            self._safe_call(self.notify, f"autoplay failed: {e}", severity="warning")
        self._safe_call(self._set_status, "")

    @work(group="search_loader", exclusive=True, thread=True)
    def search_worker(self, query: str, live: bool = False, token: int = 0) -> None:
        # Live suggestions stay quiet (no status flicker, no error popups) and
        # fetch a smaller batch; an explicit submit gets the full result set.
        if not live:
            self._safe_call(self._set_status, f"searching “{query}”…")
        try:
            songs = self.api.search_songs(query, limit=10 if live else 30)
            playlists = self.api.search_playlists(query, limit=8 if live else 20)

            def _apply() -> None:
                if token and token != self._search_seq:
                    return
                self.query_one("#view_search", SearchView).set_results(songs, playlists)

            self._safe_call(_apply)
        except Exception as e:
            if not live:
                self._safe_call(self.notify, f"search failed: {e}", severity="error")
        finally:
            if not live:
                self._safe_call(self._set_status, "")

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #
    def switch_view(self, view_id: str, focus: bool = True) -> None:
        if self.active_view_id != view_id:
            self.previous_view_id = self.active_view_id
            self.active_view_id = view_id

        self.query_one("#content_switcher", ContentSwitcher).current = view_id
        self.query_one("#top_bar", TopBar).active_view_id = view_id

        if view_id == "view_queue":
            self.query_one("#view_queue", QueueView).set_queue(list(self.player.queue))

        if focus:
            self._focus_content()

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
    # Messages from views
    # ------------------------------------------------------------------ #
    def on_play_track_msg(self, msg: PlayTrackMsg) -> None:
        self.player.play(msg.track)
        if msg.remaining_tracks:
            self.player.queue.clear()
            self.player.append_queue(msg.remaining_tracks)

    def on_queue_track_msg(self, msg: QueueTrackMsg) -> None:
        self.player.append_queue(msg.track)

    def on_start_radio_msg(self, msg: StartRadioMsg) -> None:
        self.start_radio_worker(msg.track)

    def on_open_playlist_msg(self, msg: OpenPlaylistMsg) -> None:
        self.load_playlist_worker(msg.playlist)

    def on_select_category_msg(self, msg: SelectCategoryMsg) -> None:
        self.load_category_playlists_worker(msg.category)

    def on_execute_search_msg(self, msg: ExecuteSearchMsg) -> None:
        self._search_seq += 1
        self.search_worker(msg.query, msg.live, self._search_seq)

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

    def action_cursor_down(self) -> None:
        focused = self.focused
        if focused is not None and hasattr(focused, "action_cursor_down"):
            focused.action_cursor_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if focused is not None and hasattr(focused, "action_cursor_up"):
            focused.action_cursor_up()

    def action_queue_selected(self) -> None:
        track = self._get_highlighted_track()
        if track:
            self.player.append_queue(track)
            return

        playlist = self._get_highlighted_playlist()
        if playlist:
            self.notify(f"adding {playlist.title}…", timeout=2)

            def _fetch_and_queue():
                try:
                    _, tracks = self.api.get_playlist(playlist.id)
                    self.player.append_queue(tracks)
                except Exception as e:
                    self.call_from_thread(
                        self.notify, f"could not queue playlist: {e}", severity="error"
                    )

            threading.Thread(target=_fetch_and_queue, daemon=True).start()

    def action_queue_all(self) -> None:
        view = self._current_view()
        tracks = view.get_all_tracks() if hasattr(view, "get_all_tracks") else []
        if not tracks:
            self.notify("nothing to queue here", severity="warning", timeout=2)
            return
        self.player.append_queue(tracks)

    def action_play_all(self) -> None:
        view = self._current_view()
        tracks = view.get_all_tracks() if hasattr(view, "get_all_tracks") else []
        if not tracks:
            self.notify("nothing to play here", severity="warning", timeout=2)
            return
        self.player.play(tracks[0])
        self.player.queue.clear()
        self.player.append_queue(tracks[1:])

    def action_remove_selected(self) -> None:
        if self.active_view_id != "view_queue":
            return
        try:
            table = self.query_one("#queue_table")
            index = table.cursor_row
            self.player.remove_from_queue(index)
        except Exception:
            pass

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

    def action_volume_down(self) -> None:
        self.player.adjust_volume(-5)

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
        if self._visualizer_on and self.spectrum is None:
            self.spectrum = CavaSpectrum(on_update=self._on_spectrum_bands)
            self.spectrum.start()
        self._sync_visualizer_visibility()
        if self._visualizer_on and not self.player.is_playing:
            self.notify("visualizer shows while playing", timeout=2)

    def action_handle_escape(self) -> None:
        if isinstance(self.focused, Input):
            self._focus_content()
            return
        if self.active_view_id == "view_playlist_detail":
            self.switch_to_previous_view()
            return
        self._focus_content()

    def action_switch_view_trending(self) -> None:
        self.switch_view("view_trending")

    def action_switch_view_radio(self) -> None:
        self.switch_view("view_radio")

    def action_switch_view_moods(self) -> None:
        self.switch_view("view_moods")

    def action_switch_view_search(self) -> None:
        self.action_focus_search()

    def action_switch_view_queue(self) -> None:
        self.switch_view("view_queue")

    def action_quit_app(self) -> None:
        self._stop_spectrum()
        self.player.cleanup()
        self.exit()

    def _stop_spectrum(self) -> None:
        if self.spectrum is not None:
            self.spectrum.stop()
            self.spectrum = None

    def on_unmount(self) -> None:
        self._stop_spectrum()


if __name__ == "__main__":
    AmeApp().run()
