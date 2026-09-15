"""
app.py - Main Textual Application for YouTube Music TUI.

Modern, Unauthenticated YouTube Music TUI Player.
Built with Textual, ytmusicapi (guest mode), yt-dlp, and headless mpv IPC.
"""

import os
import sys
import threading
from typing import Optional, List, Dict, Any

from textual.app import App, ComposeResult
from textual.widgets import ContentSwitcher, Input, ListView, DataTable, Static
from textual.containers import Container, Horizontal, Vertical
from textual.binding import Binding
from textual import work
from textual import events

from api import YTMusicAPI, Track, Playlist, Category
from player import MPVPlayer
from ui.widgets import HeaderBar, SidebarNav, BottomPlayerBar
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
    QueuePlaylistMsg,
    SelectCategoryMsg,
    ExecuteSearchMsg,
)


class YTMusicApp(App):
    """Modern, Unauthenticated YouTube Music TUI Player."""

    CSS_PATH = "styles.tcss"
    TITLE = "YouTube Music TUI"
    SUB_TITLE = "Guest Mode • Zero Auth"

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
        Binding("r", "radio_selected", "Radio", show=False),
        Binding("plus", "volume_up", "Vol +", show=False),
        Binding("equals", "volume_up", "Vol +", show=False),
        Binding("minus", "volume_down", "Vol -", show=False),
        Binding("underscore", "volume_down", "Vol -", show=False),
        Binding("s", "shuffle_queue", "Shuffle", show=False),
        Binding("c", "clear_queue", "Clear", show=False),
        Binding("left", "seek_backward", "Seek -5s", show=False),
        Binding("right", "seek_forward", "Seek +5s", show=False),
        Binding("tab", "toggle_focus", "Switch Focus", show=False),
        Binding("escape", "handle_escape", "Back / Defocus", show=False),
        Binding("q", "quit_app", "Quit", show=False),
        # Number shortcuts for view switching
        Binding("1", "switch_view_1", "Trending", show=False),
        Binding("2", "switch_view_2", "Radio", show=False),
        Binding("3", "switch_view_3", "Moods", show=False),
        Binding("4", "switch_view_4", "Search", show=False),
        Binding("5", "switch_view_5", "Queue", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.api = YTMusicAPI()
        self.player = MPVPlayer(resolver=self.api.resolver)
        self.active_view_id: str = "view_trending"
        self.previous_view_id: str = "view_trending"
        self._is_muted = False
        self._pre_mute_volume = 80

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="app_header")
        with Horizontal(id="main_body_container"):
            yield SidebarNav(id="app_sidebar")
            with ContentSwitcher(initial="view_trending", id="content_switcher"):
                yield TrendingView(id="view_trending")
                yield RadioView(id="view_radio")
                yield MoodsPlaylistsView(id="view_moods")
                yield PlaylistDetailView(id="view_playlist_detail")
                yield SearchView(id="view_search")
                yield QueueView(id="view_queue")
        yield BottomPlayerBar(id="app_player_bar")

    def on_mount(self) -> None:
        """Initialize player event callbacks and launch background discovery loader."""
        # Setup player callbacks
        self.player.on_track_change = self._on_player_track_change
        self.player.on_state_change = self._on_player_state_change
        self.player.on_progress = self._on_player_progress
        self.player.on_queue_change = self._on_player_queue_change
        self.player.on_autoplay_trigger = self._on_player_autoplay_trigger
        self.player.on_message = self._on_player_message

        # Periodic progress ticker for ultra-smooth seek bar and duration
        self.set_interval(0.25, self._tick_progress)

        # Kick off background data loading without blocking UI startup
        self.load_initial_data()

    def _tick_progress(self) -> None:
        """Periodic timer to update seek bar dynamically while playing."""
        if self.player.is_playing and not self.player.is_paused:
            bar = self.query_one("#app_player_bar", BottomPlayerBar)
            bar.update_progress(self.player.playback_pos, self.player.duration)

    def _safe_call(self, callback: Any, *args: Any, **kwargs: Any) -> None:
        """Call callback directly if in main app thread, or via call_from_thread if in background thread."""
        try:
            if getattr(self, "_thread_id", None) == threading.get_ident():
                callback(*args, **kwargs)
            else:
                self.call_from_thread(callback, *args, **kwargs)
        except Exception:
            pass

    # --- Player Callback Handlers ---

    def _on_player_track_change(self, track: Optional[Track]) -> None:
        def _update():
            bar = self.query_one("#app_player_bar", BottomPlayerBar)
            bar.update_track(track, self.player.current_stream_quality, self.player.autoplay)
            if track:
                self.notify(f"Playing: {track.title} • {track.artist}", title="Now Playing", timeout=3)
        self._safe_call(_update)

    def _on_player_state_change(self, is_playing: bool, is_paused: bool, is_buffering: bool) -> None:
        def _update():
            bar = self.query_one("#app_player_bar", BottomPlayerBar)
            bar.update_state(
                is_playing,
                is_paused,
                is_buffering,
                self.player.volume,
                len(self.player.queue),
            )
            header = self.query_one("#app_header", HeaderBar)
            header.is_buffering = is_buffering
            if is_buffering:
                header.status_text = "Buffering stream"
            elif is_playing:
                header.status_text = "Playing"
            elif is_paused:
                header.status_text = "Paused"
            else:
                header.status_text = "Ready"
        self._safe_call(_update)

    def _on_player_progress(self, pos: float, duration: float) -> None:
        def _update():
            bar = self.query_one("#app_player_bar", BottomPlayerBar)
            bar.update_progress(pos, duration)
        self._safe_call(_update)

    def _on_player_queue_change(self) -> None:
        def _update():
            bar = self.query_one("#app_player_bar", BottomPlayerBar)
            bar.update_state(
                self.player.is_playing,
                self.player.is_paused,
                self.player.is_buffering,
                self.player.volume,
                len(self.player.queue),
            )
            # If QueueView is mounted, refresh its data
            try:
                qv = self.query_one("#view_queue", QueueView)
                qv.set_queue(list(self.player.queue))
            except Exception:
                pass
        self._safe_call(_update)

    def _on_player_autoplay_trigger(self, last_track: Track) -> None:
        """Triggered when queue ends and continuous autoplay is active."""
        self.run_autoplay_worker(last_track)

    def _on_player_message(self, msg: str) -> None:
        def _notify():
            self.notify(msg, title="Player Notice", severity="warning", timeout=4)
        self._safe_call(_notify)

    # --- Background Workers (@work) ---

    @work(group="initial_loader", exclusive=True, thread=True)
    def load_initial_data(self) -> None:
        """Load charts and mood categories in parallel without freezing UI."""
        self._safe_call(self._set_header_status, "Fetching charts...")
        try:
            tracks, playlists = self.api.get_charts()
            def _update_trending():
                tv = self.query_one("#view_trending", TrendingView)
                tv.populate_data(tracks, playlists)
            self._safe_call(_update_trending)
        except Exception as e:
            self._safe_call(self.notify, f"Error loading charts: {e}", severity="error")

        try:
            categories = self.api.get_mood_categories()
            def _update_moods():
                mv = self.query_one("#view_moods", MoodsPlaylistsView)
                mv.populate_categories(categories)
            self._safe_call(_update_moods)

            # Auto load first category playlists if present
            first_cat = None
            for group, cats in categories.items():
                if cats:
                    first_cat = cats[0]
                    break
            if first_cat:
                self.load_category_playlists_worker(first_cat)
        except Exception as e:
            pass

        self._safe_call(self._set_header_status, "Ready")

    @work(group="category_loader", exclusive=True, thread=True)
    def load_category_playlists_worker(self, category: Category) -> None:
        """Fetch playlists for a mood/genre category."""
        self._safe_call(self._set_header_status, f"Loading {category.title} playlists...")
        try:
            playlists = self.api.get_mood_playlists(category.params)
            def _update():
                mv = self.query_one("#view_moods", MoodsPlaylistsView)
                mv.populate_playlists(category.title, playlists)
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(self.notify, f"Failed to load playlists: {e}", severity="warning")
        self._safe_call(self._set_header_status, "Ready")

    @work(group="playlist_loader", exclusive=True, thread=True)
    def load_playlist_worker(self, playlist: Playlist) -> None:
        """Fetch all tracks for a selected playlist and switch to detail view."""
        self._safe_call(self._set_header_status, f"Loading {playlist.title}...")
        try:
            pl_info, tracks = self.api.get_playlist(playlist.id)
            def _update():
                pv = self.query_one("#view_playlist_detail", PlaylistDetailView)
                pv.set_playlist(pl_info, tracks)
                self.switch_view("view_playlist_detail", f"📂 {playlist.title}")
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(self.notify, f"Error loading playlist: {e}", severity="error")
        self._safe_call(self._set_header_status, "Ready")

    @work(group="radio_loader", exclusive=True, thread=True)
    def start_radio_worker(self, seed_track: Track) -> None:
        """Fetch algorithmic recommendations based on seed track."""
        self._safe_call(self._set_header_status, f"Generating radio for {seed_track.title}...")
        try:
            recommendations = self.api.get_radio(seed_track.id, limit=35)
            # Filter out seed track from recommendations list if present
            recs_filtered = [t for t in recommendations if t.id != seed_track.id]
            if not recs_filtered:
                recs_filtered = recommendations

            def _update():
                rv = self.query_one("#view_radio", RadioView)
                rv.set_radio_tracks(seed_track, recs_filtered)
                self.switch_view("view_radio", f"📻 Radio: {seed_track.title}")
                # Play seed track and queue recommendations
                self.player.play(seed_track)
                self.player.append_queue(recs_filtered)
                self.notify(f"Infinite Radio started! Queued {len(recs_filtered)} tracks.", title="Radio Active")

            self._safe_call(_update)
        except Exception as e:
            self._safe_call(self.notify, f"Failed to generate radio: {e}", severity="error")
        self._safe_call(self._set_header_status, "Ready")

    @work(group="autoplay_loader", exclusive=True, thread=True)
    def run_autoplay_worker(self, last_track: Track) -> None:
        """Continuous autoplay worker: fetch recommendations for last played song."""
        self._safe_call(self._set_header_status, f"Autoplay: Finding recs for {last_track.title}...")
        try:
            recs = self.api.get_radio(last_track.id, limit=20)
            recs_filtered = [t for t in recs if t.id != last_track.id]
            if not recs_filtered:
                recs_filtered = recs

            if recs_filtered:
                first = recs_filtered[0]
                rest = recs_filtered[1:]
                def _play():
                    self.player.play(first)
                    self.player.append_queue(rest)
                    self.notify(
                        f"Autoplay: Queued {len(recs_filtered)} recommendations from {last_track.title}",
                        title="Continuous Autoplay",
                    )
                self._safe_call(_play)
        except Exception as e:
            self._safe_call(self.notify, f"Autoplay fetch failed: {e}", severity="warning")
        self._safe_call(self._set_header_status, "Ready")

    @work(group="search_loader", exclusive=True, thread=True)
    def search_worker(self, query: str) -> None:
        """Execute unauthenticated search across songs and playlists."""
        self._safe_call(self._set_header_status, f"Searching for '{query}'...")
        try:
            songs = self.api.search_songs(query, limit=30)
            playlists = self.api.search_playlists(query, limit=20)
            def _update():
                sv = self.query_one("#view_search", SearchView)
                sv.set_results(songs, playlists)
                self.notify(f"Found {len(songs)} songs, {len(playlists)} playlists for '{query}'.")
            self._safe_call(_update)
        except Exception as e:
            self._safe_call(self.notify, f"Search failed: {e}", severity="error")
        self._safe_call(self._set_header_status, "Ready")

    # --- View Navigation & Helpers ---

    def _set_header_status(self, text: str) -> None:
        header = self.query_one("#app_header", HeaderBar)
        header.status_text = text

    def switch_view(self, view_id: str, view_label: str) -> None:
        """Switch the main content switcher view."""
        if self.active_view_id != view_id:
            self.previous_view_id = self.active_view_id
            self.active_view_id = view_id

        switcher = self.query_one("#content_switcher", ContentSwitcher)
        switcher.current = view_id

        header = self.query_one("#app_header", HeaderBar)
        header.active_view = view_label

    def switch_to_previous_view(self) -> None:
        """Return to the previously active view."""
        label_map = {
            "view_trending": "🎵 Trending / Charts",
            "view_radio": "📻 Song Radio & Recs",
            "view_moods": "📂 Moods & Playlists",
            "view_search": "🔍 Search",
            "view_queue": "📋 Current Queue",
        }
        self.switch_view(self.previous_view_id, label_map.get(self.previous_view_id, "Trending"))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Sidebar item selected."""
        item_id = event.item.id or ""
        if item_id == "nav_trending":
            self.switch_view("view_trending", "🎵 Trending / Charts")
        elif item_id == "nav_radio":
            self.switch_view("view_radio", "📻 Song Radio & Recs")
        elif item_id == "nav_moods":
            self.switch_view("view_moods", "📂 Moods & Playlists")
        elif item_id == "nav_search":
            self.switch_view("view_search", "🔍 Search")
            self.action_focus_search()
        elif item_id == "nav_queue":
            self.switch_view("view_queue", "📋 Current Queue")
            qv = self.query_one("#view_queue", QueueView)
            qv.set_queue(list(self.player.queue))

    # --- Custom UI Message Handlers ---

    def on_play_track_msg(self, msg: PlayTrackMsg) -> None:
        self.player.play(msg.track)
        if msg.remaining_tracks:
            self.player.queue.clear()
            self.player.append_queue(msg.remaining_tracks)

    def on_queue_track_msg(self, msg: QueueTrackMsg) -> None:
        self.player.append_queue(msg.track)
        self.notify(f"Queued: {msg.track.title} • {msg.track.artist}", title="Added to Queue")

    def on_start_radio_msg(self, msg: StartRadioMsg) -> None:
        self.start_radio_worker(msg.track)

    def on_open_playlist_msg(self, msg: OpenPlaylistMsg) -> None:
        self.load_playlist_worker(msg.playlist)

    def on_select_category_msg(self, msg: SelectCategoryMsg) -> None:
        self.load_category_playlists_worker(msg.category)

    def on_execute_search_msg(self, msg: ExecuteSearchMsg) -> None:
        self.search_worker(msg.query)

    # --- Global Keybindings & Actions ---

    def on_key(self, event: events.Key) -> None:
        """Handle keyboard events without interfering with typing in Input boxes."""
        if isinstance(self.focused, Input):
            if event.key == "escape":
                self.set_focus(None)
                event.prevent_default()
            return

    def action_focus_search(self) -> None:
        """Focus the search input bar directly."""
        self.switch_view("view_search", "🔍 Search")
        try:
            inp = self.query_one("#search_input", Input)
            inp.focus()
        except Exception:
            pass

    def action_toggle_play(self) -> None:
        """Toggle play / pause."""
        self.player.toggle_pause()

    def action_next_track(self) -> None:
        """Advance to next track in queue."""
        self.player.next_track()

    def action_prev_track(self) -> None:
        """Previous track or restart."""
        self.player.prev_track()

    def action_cursor_down(self) -> None:
        """Vim 'j' or Down arrow cursor navigation."""
        focused = self.focused
        if focused and hasattr(focused, "action_cursor_down"):
            focused.action_cursor_down()

    def action_cursor_up(self) -> None:
        """Vim 'k' or Up arrow cursor navigation."""
        focused = self.focused
        if focused and hasattr(focused, "action_cursor_up"):
            focused.action_cursor_up()

    def action_queue_selected(self) -> None:
        """Append currently highlighted track or playlist to queue ('a')."""
        track = self._get_highlighted_track()
        if track:
            self.player.append_queue(track)
            self.notify(f"Queued: {track.title} • {track.artist}", title="Added to Queue")
            return

        # Check for playlist
        playlist = self._get_highlighted_playlist()
        if playlist:
            self.notify(f"Fetching tracks to queue for {playlist.title}...")

            def _fetch_and_queue():
                try:
                    _, tracks = self.api.get_playlist(playlist.id)
                    self.player.append_queue(tracks)
                    self.call_from_thread(
                        self.notify,
                        f"Queued {len(tracks)} tracks from {playlist.title}",
                        title="Playlist Queued",
                    )
                except Exception as e:
                    self.call_from_thread(self.notify, f"Error queuing playlist: {e}", severity="error")

            threading.Thread(target=_fetch_and_queue, daemon=True).start()

    def action_radio_selected(self) -> None:
        """Generate song radio from highlighted track ('r')."""
        track = self._get_highlighted_track()
        if not track:
            track = self.player.current_track
        if track:
            self.start_radio_worker(track)
        else:
            self.notify("Highlight or play a song first to start a radio station.", severity="warning")

    def _get_highlighted_track(self) -> Optional[Track]:
        """Extract currently highlighted track from active view."""
        switcher = self.query_one("#content_switcher", ContentSwitcher)
        current = switcher.current
        try:
            if current == "view_trending":
                return self.query_one("#view_trending", TrendingView).get_selected_track()
            elif current == "view_radio":
                return self.query_one("#view_radio", RadioView).get_selected_track()
            elif current == "view_playlist_detail":
                return self.query_one("#view_playlist_detail", PlaylistDetailView).get_selected_track()
            elif current == "view_search":
                return self.query_one("#view_search", SearchView).get_selected_track()
        except Exception:
            pass
        return None

    def _get_highlighted_playlist(self) -> Optional[Playlist]:
        """Extract currently highlighted playlist from active view."""
        switcher = self.query_one("#content_switcher", ContentSwitcher)
        current = switcher.current
        try:
            if current == "view_trending":
                return self.query_one("#view_trending", TrendingView).get_selected_playlist()
            elif current == "view_moods":
                return self.query_one("#view_moods", MoodsPlaylistsView).get_selected_playlist()
            elif current == "view_search":
                return self.query_one("#view_search", SearchView).get_selected_playlist()
        except Exception:
            pass
        return None

    def action_volume_up(self) -> None:
        """Increase volume by 5%."""
        self.player.adjust_volume(5)
        self.notify(f"Volume: {self.player.volume}%", timeout=1.5)

    def action_volume_down(self) -> None:
        """Decrease volume by 5%."""
        self.player.adjust_volume(-5)
        self.notify(f"Volume: {self.player.volume}%", timeout=1.5)

    def action_seek_forward(self) -> None:
        """Seek forward 5 seconds."""
        self.player.seek(5.0)

    def action_seek_backward(self) -> None:
        """Seek backward 5 seconds."""
        self.player.seek(-5.0)

    def action_shuffle_queue(self) -> None:
        """Shuffle remaining queue."""
        self.player.shuffle_queue()
        self.notify("Queue shuffled 🔀")

    def action_clear_queue(self) -> None:
        """Clear queue."""
        self.player.clear_queue()
        self.notify("Queue cleared 🗑️")

    def action_toggle_focus(self) -> None:
        """Toggle focus between Sidebar and Main Content Area (Tab)."""
        sidebar_list = self.query_one("#nav_list", ListView)
        if self.focused == sidebar_list:
            # Switch to active table in content switcher
            try:
                table = self.query_one(f"#{self.active_view_id} DataTable", DataTable)
                table.focus()
            except Exception:
                self.set_focus(None)
        else:
            sidebar_list.focus()

    def action_handle_escape(self) -> None:
        """Escape handles going back from playlist detail or unfocusing inputs."""
        if isinstance(self.focused, Input):
            self.set_focus(None)
            return
        if self.active_view_id == "view_playlist_detail":
            self.switch_to_previous_view()

    def action_switch_view_1(self) -> None:
        self.switch_view("view_trending", "🎵 Trending / Charts")

    def action_switch_view_2(self) -> None:
        self.switch_view("view_radio", "📻 Song Radio & Recs")

    def action_switch_view_3(self) -> None:
        self.switch_view("view_moods", "📂 Moods & Playlists")

    def action_switch_view_4(self) -> None:
        self.switch_view("view_search", "🔍 Search")
        self.action_focus_search()

    def action_switch_view_5(self) -> None:
        self.switch_view("view_queue", "📋 Current Queue")
        qv = self.query_one("#view_queue", QueueView)
        qv.set_queue(list(self.player.queue))

    def action_quit_app(self) -> None:
        """Safely stop audio, clean IPC sockets/processes, and exit."""
        self.player.cleanup()
        self.exit()


if __name__ == "__main__":
    app = YTMusicApp()
    app.run()
