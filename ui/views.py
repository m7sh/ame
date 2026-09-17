"""
ui/views.py - Single-pane views for ytmusic-tui.

Each view fills the content area with a cliamp-style section divider and a
track list, keeping the chrome to a minimum.
"""

from typing import Optional, List, Dict

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import (
    Static,
    DataTable,
    ListView,
    ListItem,
    Input,
    TabbedContent,
    TabPane,
)
from textual.containers import Horizontal, Vertical
from textual.message import Message

from api import Track, Playlist, Category, format_seconds
from theme import ThemeColors
from ui.widgets import SectionHeader


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
class TrackActionMessage(Message):
    """Base message for track actions."""

    def __init__(self, track: Track, remaining_tracks: Optional[List[Track]] = None):
        super().__init__()
        self.track = track
        self.remaining_tracks = remaining_tracks or []


class PlayTrackMsg(TrackActionMessage):
    """Fired when a track should be played immediately."""


class QueueTrackMsg(TrackActionMessage):
    """Fired when a track should be appended to the queue."""


class StartRadioMsg(TrackActionMessage):
    """Fired when radio should be generated from a track."""


class OpenPlaylistMsg(Message):
    """Fired when a playlist is selected for inspection."""

    def __init__(self, playlist: Playlist):
        super().__init__()
        self.playlist = playlist


class SelectCategoryMsg(Message):
    """Fired when a mood/genre category is selected."""

    def __init__(self, category: Category):
        super().__init__()
        self.category = category


class ExecuteSearchMsg(Message):
    """Fired when a search query is submitted."""

    def __init__(self, query: str):
        super().__init__()
        self.query = query


def _theme(widget: Widget) -> ThemeColors:
    if hasattr(widget.app, "theme_manager"):
        return widget.app.theme_manager.current_theme
    return ThemeColors()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count(n: int, singular: str, plural: Optional[str] = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


# --------------------------------------------------------------------------- #
# 1. Trending
# --------------------------------------------------------------------------- #
class TrendingView(Widget):
    """Top trending tracks and chart playlists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tracks: List[Track] = []
        self.playlists: List[Playlist] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="trending_header", classes="section_header")
            with TabbedContent(initial="tab_trending_tracks", id="trending_tabs"):
                with TabPane("songs", id="tab_trending_tracks"):
                    yield DataTable(id="trending_tracks_table", cursor_type="row")
                with TabPane("playlists", id="tab_chart_playlists"):
                    yield DataTable(id="trending_playlists_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#trending_tracks_table", DataTable).add_columns(
            "#", "TITLE", "ARTIST", "TIME"
        )
        self.query_one("#trending_playlists_table", DataTable).add_columns(
            "#", "PLAYLIST", "CURATOR"
        )
        self.query_one("#trending_header", SectionHeader).set_content("trending")

    def primary_widget(self) -> Optional[Widget]:
        try:
            tabs = self.query_one("#trending_tabs", TabbedContent)
            if tabs.active == "tab_chart_playlists":
                return self.query_one("#trending_playlists_table", DataTable)
            return self.query_one("#trending_tracks_table", DataTable)
        except Exception:
            return None

    def populate_data(self, tracks: List[Track], playlists: List[Playlist]) -> None:
        self.tracks = tracks
        self.playlists = playlists

        table = self.query_one("#trending_tracks_table", DataTable)
        table.clear()
        for idx, tr in enumerate(tracks, 1):
            table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                          key=f"track_{tr.id}_{idx}")

        pl_table = self.query_one("#trending_playlists_table", DataTable)
        pl_table.clear()
        for idx, p in enumerate(playlists, 1):
            pl_table.add_row(str(idx), p.title, p.author or "YouTube Music",
                             key=f"pl_{p.id}_{idx}")

        self.query_one("#trending_header", SectionHeader).set_content(
            "trending",
            [(_count(len(tracks), "song"), False), (_count(len(playlists), "chart"), False)],
        )

    def get_selected_track(self) -> Optional[Track]:
        try:
            table = self.query_one("#trending_tracks_table", DataTable)
            if 0 <= table.cursor_row < len(self.tracks):
                return self.tracks[table.cursor_row]
        except Exception:
            pass
        return None

    def get_selected_playlist(self) -> Optional[Playlist]:
        try:
            table = self.query_one("#trending_playlists_table", DataTable)
            if 0 <= table.cursor_row < len(self.playlists):
                return self.playlists[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        return list(self.tracks)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "trending_tracks_table":
            if 0 <= event.cursor_row < len(self.tracks):
                tr = self.tracks[event.cursor_row]
                self.post_message(PlayTrackMsg(tr, self.tracks[event.cursor_row + 1:]))
        elif event.data_table.id == "trending_playlists_table":
            if 0 <= event.cursor_row < len(self.playlists):
                self.post_message(OpenPlaylistMsg(self.playlists[event.cursor_row]))

    def refresh_theme(self) -> None:
        self.query_one("#trending_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 2. Radio
# --------------------------------------------------------------------------- #
class RadioView(Widget):
    """Algorithmic song radio recommendations."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tracks: List[Track] = []
        self.seed_track: Optional[Track] = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="radio_header", classes="section_header")
            yield DataTable(id="radio_tracks_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#radio_tracks_table", DataTable).add_columns(
            "#", "TITLE", "ARTIST", "TIME"
        )
        self.query_one("#radio_header", SectionHeader).set_content(
            "radio", [("press r on a song", False)]
        )

    def primary_widget(self) -> Optional[Widget]:
        try:
            return self.query_one("#radio_tracks_table", DataTable)
        except Exception:
            return None

    def set_radio_tracks(self, seed: Track, tracks: List[Track]) -> None:
        self.seed_track = seed
        self.tracks = tracks

        self.query_one("#radio_header", SectionHeader).set_content(
            "radio",
            [(_clip(seed.title, 40), True), (_count(len(tracks), "track"), False)],
        )

        table = self.query_one("#radio_tracks_table", DataTable)
        table.clear()
        for idx, tr in enumerate(tracks, 1):
            table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                          key=f"radio_{tr.id}_{idx}")

    def get_selected_track(self) -> Optional[Track]:
        try:
            table = self.query_one("#radio_tracks_table", DataTable)
            if 0 <= table.cursor_row < len(self.tracks):
                return self.tracks[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        return list(self.tracks)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.tracks):
            tr = self.tracks[event.cursor_row]
            self.post_message(PlayTrackMsg(tr, self.tracks[event.cursor_row + 1:]))

    def refresh_theme(self) -> None:
        self.query_one("#radio_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 3. Moods & Playlists
# --------------------------------------------------------------------------- #
class MoodsPlaylistsView(Widget):
    """Categories on the left, curated playlists on the right."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.categories: List[Category] = []
        self.current_playlists: List[Playlist] = []
        self.current_category: str = ""

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="moods_header", classes="section_header")
            with Horizontal(id="moods_split"):
                with Vertical(id="categories_pane"):
                    yield ListView(id="categories_list")
                with Vertical(id="playlists_pane"):
                    yield DataTable(id="mood_playlists_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#mood_playlists_table", DataTable).add_columns(
            "#", "PLAYLIST", "TRACKS"
        )
        self.query_one("#moods_header", SectionHeader).set_content("moods")

    def primary_widget(self) -> Optional[Widget]:
        try:
            return self.query_one("#categories_list", ListView)
        except Exception:
            return None

    def populate_categories(self, categories: Dict[str, List[Category]]) -> None:
        self.categories.clear()
        cat_list = self.query_one("#categories_list", ListView)
        cat_list.clear()

        for _group, items in categories.items():
            for c in items:
                self.categories.append(c)
                cat_list.append(ListItem(Static(c.title), id=f"cat_{len(self.categories) - 1}"))

        if self.categories:
            cat_list.index = 0

        self.query_one("#moods_header", SectionHeader).set_content(
            "moods", [(_count(len(self.categories), "category", "categories"), False)]
        )

    def populate_playlists(self, category_name: str, playlists: List[Playlist]) -> None:
        self.current_playlists = playlists
        self.current_category = category_name

        self.query_one("#moods_header", SectionHeader).set_content(
            "moods",
            [(category_name, True), (_count(len(playlists), "playlist"), False)],
        )

        table = self.query_one("#mood_playlists_table", DataTable)
        table.clear()
        for idx, p in enumerate(playlists, 1):
            table.add_row(str(idx), p.title, p.track_count or "—",
                          key=f"m_pl_{p.id}_{idx}")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx_str = event.item.id or ""
        if idx_str.startswith("cat_"):
            try:
                idx = int(idx_str.replace("cat_", ""))
                if 0 <= idx < len(self.categories):
                    self.post_message(SelectCategoryMsg(self.categories[idx]))
            except ValueError:
                pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.current_playlists):
            self.post_message(OpenPlaylistMsg(self.current_playlists[event.cursor_row]))

    def get_selected_playlist(self) -> Optional[Playlist]:
        try:
            table = self.query_one("#mood_playlists_table", DataTable)
            if 0 <= table.cursor_row < len(self.current_playlists):
                return self.current_playlists[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        return []

    def refresh_theme(self) -> None:
        self.query_one("#moods_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 4. Playlist detail
# --------------------------------------------------------------------------- #
class PlaylistDetailView(Widget):
    """Inspect a playlist's tracks."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.playlist: Optional[Playlist] = None
        self.tracks: List[Track] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="pl_header", classes="section_header")
            yield DataTable(id="pl_detail_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#pl_detail_table", DataTable).add_columns(
            "#", "TITLE", "ARTIST", "TIME"
        )
        self.query_one("#pl_header", SectionHeader).set_content("playlist")

    def primary_widget(self) -> Optional[Widget]:
        try:
            return self.query_one("#pl_detail_table", DataTable)
        except Exception:
            return None

    def set_playlist(self, playlist: Playlist, tracks: List[Track]) -> None:
        self.playlist = playlist
        self.tracks = tracks

        self.query_one("#pl_header", SectionHeader).set_content(
            "playlist",
            [
                (_clip(playlist.title, 40), True),
                (_clip(playlist.author or "YouTube Music", 24), False),
                (_count(len(tracks), "track"), False),
            ],
        )

        table = self.query_one("#pl_detail_table", DataTable)
        table.clear()
        for idx, tr in enumerate(tracks, 1):
            table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                          key=f"plt_{tr.id}_{idx}")

    def get_selected_track(self) -> Optional[Track]:
        try:
            table = self.query_one("#pl_detail_table", DataTable)
            if 0 <= table.cursor_row < len(self.tracks):
                return self.tracks[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        return list(self.tracks)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.tracks):
            tr = self.tracks[event.cursor_row]
            self.post_message(PlayTrackMsg(tr, self.tracks[event.cursor_row + 1:]))

    def refresh_theme(self) -> None:
        self.query_one("#pl_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 5. Search
# --------------------------------------------------------------------------- #
class SearchView(Widget):
    """Search songs and public playlists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.song_results: List[Track] = []
        self.playlist_results: List[Playlist] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield Input(placeholder="search songs, artists or playlists…", id="search_input")
            yield SectionHeader(id="search_header", classes="section_header")
            with TabbedContent(initial="tab_search_songs", id="search_tabs"):
                with TabPane("songs", id="tab_search_songs"):
                    yield DataTable(id="search_songs_table", cursor_type="row")
                with TabPane("playlists", id="tab_search_playlists"):
                    yield DataTable(id="search_playlists_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#search_songs_table", DataTable).add_columns(
            "#", "TITLE", "ARTIST", "TIME"
        )
        self.query_one("#search_playlists_table", DataTable).add_columns(
            "#", "PLAYLIST", "CURATOR"
        )
        self.query_one("#search_header", SectionHeader).set_content("search")

    def primary_widget(self) -> Optional[Widget]:
        try:
            tabs = self.query_one("#search_tabs", TabbedContent)
            if tabs.active == "tab_search_playlists":
                return self.query_one("#search_playlists_table", DataTable)
            return self.query_one("#search_songs_table", DataTable)
        except Exception:
            return None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            self.post_message(ExecuteSearchMsg(query))

    def set_results(self, songs: List[Track], playlists: List[Playlist]) -> None:
        self.song_results = songs
        self.playlist_results = playlists

        s_table = self.query_one("#search_songs_table", DataTable)
        s_table.clear()
        for idx, tr in enumerate(songs, 1):
            s_table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                            key=f"s_{tr.id}_{idx}")

        p_table = self.query_one("#search_playlists_table", DataTable)
        p_table.clear()
        for idx, p in enumerate(playlists, 1):
            p_table.add_row(str(idx), p.title, p.author or "Community",
                            key=f"sp_{p.id}_{idx}")

        self.query_one("#search_header", SectionHeader).set_content(
            "search",
            [(_count(len(songs), "song"), False), (_count(len(playlists), "playlist"), False)],
        )

    def get_selected_track(self) -> Optional[Track]:
        try:
            table = self.query_one("#search_songs_table", DataTable)
            if 0 <= table.cursor_row < len(self.song_results):
                return self.song_results[table.cursor_row]
        except Exception:
            pass
        return None

    def get_selected_playlist(self) -> Optional[Playlist]:
        try:
            table = self.query_one("#search_playlists_table", DataTable)
            if 0 <= table.cursor_row < len(self.playlist_results):
                return self.playlist_results[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        return list(self.song_results)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "search_songs_table":
            if 0 <= event.cursor_row < len(self.song_results):
                tr = self.song_results[event.cursor_row]
                self.post_message(PlayTrackMsg(tr, self.song_results[event.cursor_row + 1:]))
        elif event.data_table.id == "search_playlists_table":
            if 0 <= event.cursor_row < len(self.playlist_results):
                self.post_message(OpenPlaylistMsg(self.playlist_results[event.cursor_row]))

    def refresh_theme(self) -> None:
        self.query_one("#search_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 6. Queue
# --------------------------------------------------------------------------- #
class QueueView(Widget):
    """The active playback queue."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.queue_tracks: List[Track] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="queue_header", classes="section_header")
            yield DataTable(id="queue_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#queue_table", DataTable).add_columns(
            "#", "TITLE", "ARTIST", "TIME"
        )
        self.query_one("#queue_header", SectionHeader).set_content(
            "queue", [("empty", False)]
        )

    def primary_widget(self) -> Optional[Widget]:
        try:
            return self.query_one("#queue_table", DataTable)
        except Exception:
            return None

    def set_queue(self, queue: List[Track]) -> None:
        self.queue_tracks = queue

        if queue:
            total = sum(tr.duration_seconds for tr in queue if tr.duration_seconds)
            chips = [(_count(len(queue), "track"), True)]
            if total:
                chips.append((f"~{format_seconds(total)}", False))
        else:
            chips = [("empty", False)]
        self.query_one("#queue_header", SectionHeader).set_content("queue", chips)

        table = self.query_one("#queue_table", DataTable)
        table.clear()
        for idx, tr in enumerate(queue, 1):
            table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                          key=f"q_{tr.id}_{idx}")

    def get_all_tracks(self) -> List[Track]:
        return list(self.queue_tracks)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.queue_tracks) and hasattr(self.app, "player"):
            self.app.player.play_from_queue(event.cursor_row)

    def refresh_theme(self) -> None:
        self.query_one("#queue_header", SectionHeader).refresh()
