"""
ui/views.py - Screens/Views for Trending, Radio, Moods & Playlists, Playlist Detail, Search, and Queue.
Refined with Omarchy theme color integration.
"""

from typing import Optional, List, Dict, Any
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import (
    Static,
    DataTable,
    ListView,
    ListItem,
    Input,
    Button,
    TabbedContent,
    TabPane,
)
from textual.containers import Container, Horizontal, Vertical
from textual.message import Message

from api import Track, Playlist, Category, format_seconds
from theme import ThemeColors


# Custom UI Event Messages
class TrackActionMessage(Message):
    """Base message for track actions."""
    def __init__(self, track: Track, remaining_tracks: Optional[List[Track]] = None):
        super().__init__()
        self.track = track
        self.remaining_tracks = remaining_tracks or []


class PlayTrackMsg(TrackActionMessage):
    """Fired when track should be played immediately."""
    pass


class QueueTrackMsg(TrackActionMessage):
    """Fired when track should be appended to queue."""
    pass


class StartRadioMsg(TrackActionMessage):
    """Fired when radio should be generated from track."""
    pass


class OpenPlaylistMsg(Message):
    """Fired when a playlist is selected for inspection."""
    def __init__(self, playlist: Playlist):
        super().__init__()
        self.playlist = playlist


class QueuePlaylistMsg(Message):
    """Fired when an entire playlist is queued."""
    def __init__(self, playlist: Playlist):
        super().__init__()
        self.playlist = playlist


class SelectCategoryMsg(Message):
    """Fired when a mood/genre category is selected."""
    def __init__(self, category: Category):
        super().__init__()
        self.category = category


class ExecuteSearchMsg(Message):
    """Fired when search query is submitted."""
    def __init__(self, query: str, filter_type: str = "songs"):
        super().__init__()
        self.query = query
        self.filter_type = filter_type


def _theme(widget: Widget) -> ThemeColors:
    if hasattr(widget.app, "theme_manager"):
        return widget.app.theme_manager.current_theme
    return ThemeColors()


# -----------------------------------------------------------------------------
# 1. Trending View
# -----------------------------------------------------------------------------
class TrendingView(Widget):
    """View displaying top trending tracks and chart playlists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tracks: List[Track] = []
        self.playlists: List[Playlist] = []

    def compose(self) -> ComposeResult:
        t = _theme(self)
        with Vertical(classes="view_content_container"):
            yield Static(
                f"🔥  [bold {t.primary}]Trending & Global Charts[/]  [dim {t.muted}]— Top songs updated daily[/]",
                id="trending_banner",
                classes="view_banner",
            )
            with TabbedContent(initial="tab_trending_tracks", id="trending_tabs"):
                with TabPane("🎵 Trending Tracks", id="tab_trending_tracks"):
                    yield DataTable(id="trending_tracks_table", cursor_type="row", zebra_stripes=True)
                with TabPane("🏆 Chart Playlists", id="tab_chart_playlists"):
                    yield DataTable(id="trending_playlists_table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        tracks_table = self.query_one("#trending_tracks_table", DataTable)
        tracks_table.add_columns("#", "Title", "Artist", "Album", "Duration")

        pl_table = self.query_one("#trending_playlists_table", DataTable)
        pl_table.add_columns("#", "Chart Playlist Title", "Curator", "Type")

    def populate_data(self, tracks: List[Track], playlists: List[Playlist]) -> None:
        self.tracks = tracks
        self.playlists = playlists

        tracks_table = self.query_one("#trending_tracks_table", DataTable)
        tracks_table.clear()
        for idx, t in enumerate(tracks, 1):
            tracks_table.add_row(
                str(idx),
                t.title,
                t.artist,
                t.album or "Single",
                t.display_duration,
                key=f"track_{t.id}_{idx}",
            )

        pl_table = self.query_one("#trending_playlists_table", DataTable)
        pl_table.clear()
        for idx, p in enumerate(playlists, 1):
            pl_table.add_row(
                str(idx),
                p.title,
                p.author or "YouTube Music",
                "Official Chart",
                key=f"pl_{p.id}_{idx}",
            )

    def get_selected_track(self) -> Optional[Track]:
        try:
            tabs = self.query_one("#trending_tabs", TabbedContent)
            if tabs.active == "tab_trending_tracks":
                table = self.query_one("#trending_tracks_table", DataTable)
                if 0 <= table.cursor_row < len(self.tracks):
                    return self.tracks[table.cursor_row]
        except Exception:
            pass
        try:
            table = self.query_one("#trending_tracks_table", DataTable)
            if 0 <= table.cursor_row < len(self.tracks):
                return self.tracks[table.cursor_row]
        except Exception:
            pass
        return None

    def get_selected_playlist(self) -> Optional[Playlist]:
        try:
            tabs = self.query_one("#trending_tabs", TabbedContent)
            if tabs.active == "tab_chart_playlists":
                table = self.query_one("#trending_playlists_table", DataTable)
                if 0 <= table.cursor_row < len(self.playlists):
                    return self.playlists[table.cursor_row]
        except Exception:
            pass
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "trending_tracks_table":
            if 0 <= event.cursor_row < len(self.tracks):
                t = self.tracks[event.cursor_row]
                remaining = self.tracks[event.cursor_row + 1 :]
                self.post_message(PlayTrackMsg(t, remaining))
        elif event.data_table.id == "trending_playlists_table":
            if 0 <= event.cursor_row < len(self.playlists):
                p = self.playlists[event.cursor_row]
                self.post_message(OpenPlaylistMsg(p))

    def refresh_theme(self) -> None:
        t = _theme(self)
        try:
            self.query_one("#trending_banner", Static).update(
                f"🔥  [bold {t.primary}]Trending & Global Charts[/]  [dim {t.muted}]— Top songs updated daily[/]"
            )
        except Exception:
            pass


# -----------------------------------------------------------------------------
# 2. Radio View
# -----------------------------------------------------------------------------
class RadioView(Widget):
    """View displaying algorithmic infinite radio recommendations."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tracks: List[Track] = []
        self.seed_track: Optional[Track] = None

    def compose(self) -> ComposeResult:
        t = _theme(self)
        with Vertical(classes="view_content_container"):
            with Container(id="radio_seed_card"):
                yield Static(
                    f"📻  [bold {t.primary}]Song Radio & Algorithmic Recommendations[/]",
                    id="radio_title",
                )
                yield Static(
                    f"[dim {t.muted}]No active seed track. Highlight any song and press [bold {t.primary}][r][/] to launch infinite radio.[/]",
                    id="radio_seed_info",
                )
                with Horizontal(id="radio_action_buttons"):
                    yield Button("🔄 Regenerate (r)", id="btn_radio_regen", variant="primary")
                    yield Button("➕ Queue All Recs (a)", id="btn_radio_queue_all")

            yield DataTable(id="radio_tracks_table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#radio_tracks_table", DataTable)
        table.add_columns("#", "Title", "Artist", "Album", "Duration")

    def set_radio_tracks(self, seed: Track, tracks: List[Track]) -> None:
        self.seed_track = seed
        self.tracks = tracks
        t = _theme(self)

        try:
            info = self.query_one("#radio_seed_info", Static)
            info.update(
                f"Generated from: [bold {t.accent}]{seed.title}[/]  "
                f"[dim {t.muted}]by[/]  [bold {t.secondary}]{seed.artist}[/]  "
                f"[dim {t.muted}]({len(tracks)} related recommendations)[/]"
            )
        except Exception:
            pass

        table = self.query_one("#radio_tracks_table", DataTable)
        table.clear()
        for idx, tr in enumerate(tracks, 1):
            table.add_row(
                str(idx),
                tr.title,
                tr.artist,
                tr.album or "Single",
                tr.display_duration,
                key=f"radio_{tr.id}_{idx}",
            )

    def get_selected_track(self) -> Optional[Track]:
        try:
            table = self.query_one("#radio_tracks_table", DataTable)
            if 0 <= table.cursor_row < len(self.tracks):
                return self.tracks[table.cursor_row]
        except Exception:
            pass
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.tracks):
            tr = self.tracks[event.cursor_row]
            remaining = self.tracks[event.cursor_row + 1 :]
            self.post_message(PlayTrackMsg(tr, remaining))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_radio_regen" and self.seed_track:
            self.post_message(StartRadioMsg(self.seed_track))
        elif event.button.id == "btn_radio_queue_all" and self.tracks:
            for tr in self.tracks:
                self.post_message(QueueTrackMsg(tr))

    def refresh_theme(self) -> None:
        t = _theme(self)
        try:
            self.query_one("#radio_title", Static).update(
                f"📻  [bold {t.primary}]Song Radio & Algorithmic Recommendations[/]"
            )
            if self.seed_track:
                self.set_radio_tracks(self.seed_track, self.tracks)
        except Exception:
            pass


# -----------------------------------------------------------------------------
# 3. Moods & Playlists View
# -----------------------------------------------------------------------------
class MoodsPlaylistsView(Widget):
    """Split view with mood/genre categories on left and playlists on right."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.categories: List[Category] = []
        self.current_playlists: List[Playlist] = []

    def compose(self) -> ComposeResult:
        t = _theme(self)
        with Vertical(classes="view_content_container"):
            yield Static(
                f"📂  [bold {t.primary}]Moods & Curated Playlists[/]  [dim {t.muted}]— Browse curated moods, moments and genres[/]",
                id="moods_banner",
                classes="view_banner",
            )
            with Horizontal(id="moods_split_container"):
                with Vertical(id="moods_left_pane"):
                    yield Static(f"[bold {t.secondary}]CATEGORIES[/]", id="moods_cat_header", classes="pane_title")
                    yield ListView(id="categories_list")
                with Vertical(id="moods_right_pane"):
                    yield Static(
                        f"[bold {t.primary}]CURATED PLAYLISTS[/]  [dim {t.muted}]— Press Enter to inspect[/]",
                        classes="pane_title",
                        id="moods_playlists_title",
                    )
                    yield DataTable(id="mood_playlists_table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#mood_playlists_table", DataTable)
        table.add_columns("#", "Playlist Title", "Creator", "Tracks", "Description")

    def populate_categories(self, categories_dict: Dict[str, List[Category]]) -> None:
        self.categories.clear()
        cat_list = self.query_one("#categories_list", ListView)
        cat_list.clear()

        for group_name, items in categories_dict.items():
            for c in items:
                self.categories.append(c)
                icon = "✨" if "Mood" in group_name else "🎸"
                cat_list.append(ListItem(Static(f"{icon}  {c.title}"), id=f"cat_{len(self.categories)-1}"))

    def populate_playlists(self, category_name: str, playlists: List[Playlist]) -> None:
        self.current_playlists = playlists
        t = _theme(self)
        try:
            title_lbl = self.query_one("#moods_playlists_title", Static)
            title_lbl.update(
                f"[bold {t.primary}]{category_name.upper()} PLAYLISTS[/]  "
                f"[dim {t.muted}]({len(playlists)} found) — Press Enter to inspect[/]"
            )
        except Exception:
            pass

        table = self.query_one("#mood_playlists_table", DataTable)
        table.clear()
        for idx, p in enumerate(playlists, 1):
            desc = p.description.split("\n")[0] if p.description else "Curated YouTube Music Playlist"
            if len(desc) > 60:
                desc = desc[:57] + "..."
            table.add_row(
                str(idx),
                p.title,
                p.author or "YouTube Music",
                p.track_count or "Various",
                desc,
                key=f"m_pl_{p.id}_{idx}",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx_str = event.item.id or ""
        if idx_str.startswith("cat_"):
            try:
                idx = int(idx_str.replace("cat_", ""))
                if 0 <= idx < len(self.categories):
                    cat = self.categories[idx]
                    self.post_message(SelectCategoryMsg(cat))
            except ValueError:
                pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.current_playlists):
            p = self.current_playlists[event.cursor_row]
            self.post_message(OpenPlaylistMsg(p))

    def get_selected_playlist(self) -> Optional[Playlist]:
        try:
            table = self.query_one("#mood_playlists_table", DataTable)
            if 0 <= table.cursor_row < len(self.current_playlists):
                return self.current_playlists[table.cursor_row]
        except Exception:
            pass
        return None

    def refresh_theme(self) -> None:
        t = _theme(self)
        try:
            self.query_one("#moods_banner", Static).update(
                f"📂  [bold {t.primary}]Moods & Curated Playlists[/]  [dim {t.muted}]— Browse curated moods, moments and genres[/]"
            )
            self.query_one("#moods_cat_header", Static).update(f"[bold {t.secondary}]CATEGORIES[/]")
        except Exception:
            pass


# -----------------------------------------------------------------------------
# 4. Playlist Detail View
# -----------------------------------------------------------------------------
class PlaylistDetailView(Widget):
    """Detailed view for inspecting a playlist's tracks and creator info."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.playlist: Optional[Playlist] = None
        self.tracks: List[Track] = []

    def compose(self) -> ComposeResult:
        t = _theme(self)
        with Vertical(classes="view_content_container"):
            with Container(id="playlist_header_card"):
                yield Static(
                    f"📂  [bold {t.primary}]Loading Playlist...[/]",
                    id="pl_detail_title",
                )
                yield Static(
                    f"[dim {t.muted}]Fetching tracks and creator information...[/]",
                    id="pl_detail_subtitle",
                )
                with Horizontal(id="pl_detail_buttons"):
                    yield Button("▶ Play All (P)", id="btn_pl_play_all", variant="primary")
                    yield Button("➕ Queue All (Q)", id="btn_pl_queue_all")
                    yield Button("← Back (Esc)", id="btn_pl_back")

            yield DataTable(id="pl_detail_table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#pl_detail_table", DataTable)
        table.add_columns("#", "Title", "Artist", "Album", "Duration")

    def set_playlist(self, playlist: Playlist, tracks: List[Track]) -> None:
        self.playlist = playlist
        self.tracks = tracks
        t = _theme(self)

        try:
            title_lbl = self.query_one("#pl_detail_title", Static)
            title_lbl.update(f"📂  [bold {t.primary}]{playlist.title}[/]")

            desc = playlist.description.split("\n")[0] if playlist.description else ""
            subtitle = self.query_one("#pl_detail_subtitle", Static)
            subtitle.update(
                f"[bold {t.secondary}]{playlist.author}[/]  [dim {t.muted}]•[/]  "
                f"[bold {t.foreground}]{len(tracks)} tracks[/]  "
                f"[dim {t.muted}]•  {desc}[/]"
            )
        except Exception:
            pass

        table = self.query_one("#pl_detail_table", DataTable)
        table.clear()
        for idx, tr in enumerate(tracks, 1):
            table.add_row(
                str(idx),
                tr.title,
                tr.artist,
                tr.album or "Single",
                tr.display_duration,
                key=f"plt_{tr.id}_{idx}",
            )

    def get_selected_track(self) -> Optional[Track]:
        try:
            table = self.query_one("#pl_detail_table", DataTable)
            if 0 <= table.cursor_row < len(self.tracks):
                return self.tracks[table.cursor_row]
        except Exception:
            pass
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.tracks):
            tr = self.tracks[event.cursor_row]
            remaining = self.tracks[event.cursor_row + 1 :]
            self.post_message(PlayTrackMsg(tr, remaining))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_pl_play_all" and self.tracks:
            self.post_message(PlayTrackMsg(self.tracks[0], self.tracks[1:]))
        elif event.button.id == "btn_pl_queue_all" and self.tracks:
            for tr in self.tracks:
                self.post_message(QueueTrackMsg(tr))
        elif event.button.id == "btn_pl_back":
            if hasattr(self.app, "switch_to_previous_view"):
                self.app.switch_to_previous_view()

    def refresh_theme(self) -> None:
        if self.playlist:
            self.set_playlist(self.playlist, self.tracks)


# -----------------------------------------------------------------------------
# 5. Search View
# -----------------------------------------------------------------------------
class SearchView(Widget):
    """Search interface for finding songs and public playlists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.song_results: List[Track] = []
        self.playlist_results: List[Playlist] = []

    def compose(self) -> ComposeResult:
        t = _theme(self)
        with Vertical(classes="view_content_container"):
            yield Static(
                f"🔍  [bold {t.primary}]Search YouTube Music[/]  [dim {t.muted}]— Zero auth query discovery[/]",
                id="search_banner",
                classes="view_banner",
            )
            with Container(id="search_bar_container"):
                yield Input(
                    placeholder="Type song title, artist name, or playlist and press Enter...",
                    id="search_input",
                )
            with TabbedContent(initial="tab_search_songs", id="search_tabs"):
                with TabPane("🎵 Songs", id="tab_search_songs"):
                    yield DataTable(id="search_songs_table", cursor_type="row", zebra_stripes=True)
                with TabPane("📁 Playlists", id="tab_search_playlists"):
                    yield DataTable(id="search_playlists_table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        s_table = self.query_one("#search_songs_table", DataTable)
        s_table.add_columns("#", "Title", "Artist", "Album", "Duration")

        p_table = self.query_one("#search_playlists_table", DataTable)
        p_table.add_columns("#", "Playlist Title", "Creator", "Track Count")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self.post_message(ExecuteSearchMsg(query, "both"))

    def set_results(self, songs: List[Track], playlists: List[Playlist]) -> None:
        self.song_results = songs
        self.playlist_results = playlists

        s_table = self.query_one("#search_songs_table", DataTable)
        s_table.clear()
        for idx, tr in enumerate(songs, 1):
            s_table.add_row(
                str(idx),
                tr.title,
                tr.artist,
                tr.album or "Single",
                tr.display_duration,
                key=f"s_{tr.id}_{idx}",
            )

        p_table = self.query_one("#search_playlists_table", DataTable)
        p_table.clear()
        for idx, p in enumerate(playlists, 1):
            p_table.add_row(
                str(idx),
                p.title,
                p.author or "Community",
                p.track_count or "Playlist",
                key=f"sp_{p.id}_{idx}",
            )

    def get_selected_track(self) -> Optional[Track]:
        try:
            tabs = self.query_one("#search_tabs", TabbedContent)
            if tabs.active == "tab_search_songs":
                table = self.query_one("#search_songs_table", DataTable)
                if 0 <= table.cursor_row < len(self.song_results):
                    return self.song_results[table.cursor_row]
        except Exception:
            pass
        try:
            table = self.query_one("#search_songs_table", DataTable)
            if 0 <= table.cursor_row < len(self.song_results):
                return self.song_results[table.cursor_row]
        except Exception:
            pass
        return None

    def get_selected_playlist(self) -> Optional[Playlist]:
        try:
            tabs = self.query_one("#search_tabs", TabbedContent)
            if tabs.active == "tab_search_playlists":
                table = self.query_one("#search_playlists_table", DataTable)
                if 0 <= table.cursor_row < len(self.playlist_results):
                    return self.playlist_results[table.cursor_row]
        except Exception:
            pass
        try:
            table = self.query_one("#search_playlists_table", DataTable)
            if 0 <= table.cursor_row < len(self.playlist_results):
                return self.playlist_results[table.cursor_row]
        except Exception:
            pass
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "search_songs_table":
            if 0 <= event.cursor_row < len(self.song_results):
                tr = self.song_results[event.cursor_row]
                remaining = self.song_results[event.cursor_row + 1 :]
                self.post_message(PlayTrackMsg(tr, remaining))
        elif event.data_table.id == "search_playlists_table":
            if 0 <= event.cursor_row < len(self.playlist_results):
                p = self.playlist_results[event.cursor_row]
                self.post_message(OpenPlaylistMsg(p))

    def refresh_theme(self) -> None:
        t = _theme(self)
        try:
            self.query_one("#search_banner", Static).update(
                f"🔍  [bold {t.primary}]Search YouTube Music[/]  [dim {t.muted}]— Zero auth query discovery[/]"
            )
        except Exception:
            pass


# -----------------------------------------------------------------------------
# 6. Queue View
# -----------------------------------------------------------------------------
class QueueView(Widget):
    """View managing the active playback queue and controls."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queue_tracks: List[Track] = []

    def compose(self) -> ComposeResult:
        t = _theme(self)
        with Vertical(classes="view_content_container"):
            with Container(id="queue_header_card"):
                yield Static(
                    f"📋  [bold {t.primary}]Current Playback Queue[/]",
                    id="queue_title",
                )
                yield Static(
                    f"[dim {t.muted}]Manage tracks, shuffle order, or jump directly to any queued song.[/]",
                    id="queue_info",
                )
                with Horizontal(id="queue_action_buttons"):
                    yield Button("🔀 Shuffle (s)", id="btn_queue_shuffle", variant="primary")
                    yield Button("🗑️ Clear Queue (c)", id="btn_queue_clear", variant="error")
                    yield Button("❌ Remove Selected (d)", id="btn_queue_remove")

            yield DataTable(id="queue_table", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        table = self.query_one("#queue_table", DataTable)
        table.add_columns("#", "Title", "Artist", "Album", "Duration")

    def set_queue(self, queue: List[Track]) -> None:
        self.queue_tracks = queue
        t = _theme(self)
        try:
            info = self.query_one("#queue_info", Static)
            if queue:
                total_sec = sum(tr.duration_seconds for tr in queue if tr.duration_seconds)
                total_time_str = f" • Approx {format_seconds(total_sec)}" if total_sec else ""
                info.update(
                    f"[bold {t.foreground}]{len(queue)} tracks queued{total_time_str}[/]  "
                    f"[dim {t.muted}]— Press Enter to play from queue[/]"
                )
            else:
                info.update(f"[dim {t.muted}]Queue is currently empty. Add tracks from Trending, Playlists, or Search![/]")
        except Exception:
            pass

        table = self.query_one("#queue_table", DataTable)
        table.clear()
        for idx, tr in enumerate(queue, 1):
            table.add_row(
                str(idx),
                tr.title,
                tr.artist,
                tr.album or "Single",
                tr.display_duration,
                key=f"q_{tr.id}_{idx}",
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.queue_tracks):
            if hasattr(self.app, "player"):
                self.app.player.play_from_queue(event.cursor_row)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not hasattr(self.app, "player"):
            return

        if event.button.id == "btn_queue_shuffle":
            self.app.player.shuffle_queue()
        elif event.button.id == "btn_queue_clear":
            self.app.player.clear_queue()
        elif event.button.id == "btn_queue_remove":
            table = self.query_one("#queue_table", DataTable)
            if 0 <= table.cursor_row < len(self.queue_tracks):
                self.app.player.remove_from_queue(table.cursor_row)

    def refresh_theme(self) -> None:
        t = _theme(self)
        try:
            self.query_one("#queue_title", Static).update(
                f"📋  [bold {t.primary}]Current Playback Queue[/]"
            )
            self.set_queue(self.queue_tracks)
        except Exception:
            pass
