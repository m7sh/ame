"""
ui/views.py - Single-pane views for ame.

Each view fills the content area with a cliamp-style section divider and a
track list, keeping the chrome to a minimum.
"""

from typing import Optional, List, Dict, Tuple

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
    """Fired when a search query is submitted or updated while typing."""

    def __init__(self, query: str, live: bool = False):
        super().__init__()
        self.query = query
        self.live = live


def _theme(widget: Widget) -> ThemeColors:
    if hasattr(widget.app, "theme_manager"):
        return widget.app.theme_manager.current_theme
    return ThemeColors()


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count(n: int, singular: str, plural: Optional[str] = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _apply_fixed_widths(table: DataTable, widths: List[Tuple[str, int]]) -> None:
    """Pin column widths so the table never overflows its widget."""
    try:
        for key, width in widths:
            column = table.columns.get(key)
            if column is None:
                continue
            column.auto_width = False
            column.width = max(1, width)
        table.clear_cached_dimensions()
        table._require_update_dimensions = True
        table.refresh(layout=True)
    except Exception:
        pass


def _track_widths(table: DataTable) -> List[Tuple[str, int]]:
    """# | TITLE (flex) | ARTIST | TIME, fitted to the current width."""
    total = table.size.width
    if total <= 0:
        return []
    available = total - 2 * table.cell_padding * 4
    num_w, time_w = 3, 7
    artist_w = max(12, min(30, available // 4))
    title_w = max(16, available - num_w - time_w - artist_w)
    return [("num", num_w), ("title", title_w), ("artist", artist_w), ("time", time_w)]


def _playlist_widths(table: DataTable) -> List[Tuple[str, int]]:
    """# | NAME (flex) | META, fitted to the current width."""
    total = table.size.width
    if total <= 0:
        return []
    available = total - 2 * table.cell_padding * 3
    num_w = 3
    meta_w = max(12, min(30, available // 3))
    title_w = max(16, available - num_w - meta_w)
    return [("num", num_w), ("title", title_w), ("meta", meta_w)]


class TableFitMixin:
    """Re-pins table column widths on mount, after loading and on resize."""

    def _fitted_tables(self) -> List[Tuple[DataTable, str]]:
        return []

    def _fit_tables(self) -> None:
        for table, kind in self._fitted_tables():
            widths = _track_widths(table) if kind == "track" else _playlist_widths(table)
            if widths:
                _apply_fixed_widths(table, widths)

    def on_resize(self, event) -> None:
        self._fit_tables()


# --------------------------------------------------------------------------- #
# 1. Trending
# --------------------------------------------------------------------------- #
class TrendingView(TableFitMixin, Widget):
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
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#trending_playlists_table", DataTable).add_columns(
            ("#", "num"), ("PLAYLIST", "title"), ("CURATOR", "meta")
        )
        self.query_one("#trending_header", SectionHeader).set_content("trending")
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [
                (self.query_one("#trending_tracks_table", DataTable), "track"),
                (self.query_one("#trending_playlists_table", DataTable), "playlist"),
            ]
        except Exception:
            return []

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
        self._fit_tables()

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
class RadioView(TableFitMixin, Widget):
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
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#radio_header", SectionHeader).set_content(
            "radio", [("press r on a song", False)]
        )
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [(self.query_one("#radio_tracks_table", DataTable), "track")]
        except Exception:
            return []

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
        self._fit_tables()

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
class MoodsPlaylistsView(TableFitMixin, Widget):
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
            ("#", "num"), ("PLAYLIST", "title"), ("TRACKS", "meta")
        )
        self.query_one("#moods_header", SectionHeader).set_content("moods")
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [(self.query_one("#mood_playlists_table", DataTable), "playlist")]
        except Exception:
            return []

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
        self._fit_tables()

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
class PlaylistDetailView(TableFitMixin, Widget):
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
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#pl_header", SectionHeader).set_content("playlist")
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [(self.query_one("#pl_detail_table", DataTable), "track")]
        except Exception:
            return []

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
        self._fit_tables()

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
class SearchView(TableFitMixin, Widget):
    """Search songs and public playlists, with live suggestions while typing."""

    # Wait this long after the last keystroke before querying YouTube Music,
    # so a burst of typing triggers a single request instead of one per key.
    SEARCH_DEBOUNCE = 0.35
    MIN_QUERY_LENGTH = 2

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.song_results: List[Track] = []
        self.playlist_results: List[Playlist] = []
        self._search_timer = None
        self._last_query = ""

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
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#search_playlists_table", DataTable).add_columns(
            ("#", "num"), ("PLAYLIST", "title"), ("CURATOR", "meta")
        )
        self.query_one("#search_header", SectionHeader).set_content("search")
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [
                (self.query_one("#search_songs_table", DataTable), "track"),
                (self.query_one("#search_playlists_table", DataTable), "playlist"),
            ]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        try:
            tabs = self.query_one("#search_tabs", TabbedContent)
            if tabs.active == "tab_search_playlists":
                return self.query_one("#search_playlists_table", DataTable)
            return self.query_one("#search_songs_table", DataTable)
        except Exception:
            return None

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        if query == self._last_query:
            return
        self._last_query = query

        self._cancel_search_timer()

        if not query:
            self._clear_results()
            return

        if len(query) < self.MIN_QUERY_LENGTH:
            return

        self._search_timer = self.set_timer(
            self.SEARCH_DEBOUNCE,
            lambda: self.post_message(ExecuteSearchMsg(query, live=True)),
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        self._last_query = query
        self._cancel_search_timer()
        if query:
            self.post_message(ExecuteSearchMsg(query, live=False))

    def _cancel_search_timer(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None

    def _clear_results(self) -> None:
        self.song_results = []
        self.playlist_results = []
        self.query_one("#search_songs_table", DataTable).clear()
        self.query_one("#search_playlists_table", DataTable).clear()
        self.query_one("#search_header", SectionHeader).set_content("search")

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
        self._fit_tables()

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
class QueueView(TableFitMixin, Widget):
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
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#queue_header", SectionHeader).set_content(
            "queue", [("empty", False)]
        )
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [(self.query_one("#queue_table", DataTable), "track")]
        except Exception:
            return []

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
        self._fit_tables()

    def get_all_tracks(self) -> List[Track]:
        return list(self.queue_tracks)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.queue_tracks) and hasattr(self.app, "player"):
            self.app.player.play_from_queue(event.cursor_row)

    def refresh_theme(self) -> None:
        self.query_one("#queue_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 7. Favourites
# --------------------------------------------------------------------------- #
class FavouritesView(TableFitMixin, Widget):
    """Songs the user has starred, persisted across sessions."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tracks: List[Track] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="favourites_header", classes="section_header")
            yield DataTable(id="favourites_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#favourites_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#favourites_header", SectionHeader).set_content(
            "favourites", [("press f on a song to add", False)]
        )
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [(self.query_one("#favourites_table", DataTable), "track")]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        try:
            return self.query_one("#favourites_table", DataTable)
        except Exception:
            return None

    def set_favourites(self, tracks: List[Track]) -> None:
        self.tracks = list(tracks)

        if self.tracks:
            total = sum(tr.duration_seconds for tr in self.tracks if tr.duration_seconds)
            chips = [(_count(len(self.tracks), "song"), True)]
            if total:
                chips.append((f"~{format_seconds(total)}", False))
        else:
            chips = [("press f on a song to add", False)]
        self.query_one("#favourites_header", SectionHeader).set_content("favourites", chips)

        table = self.query_one("#favourites_table", DataTable)
        table.clear()
        for idx, tr in enumerate(self.tracks, 1):
            table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                          key=f"fav_{tr.id}_{idx}")
        self._fit_tables()

    def get_selected_track(self) -> Optional[Track]:
        try:
            table = self.query_one("#favourites_table", DataTable)
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
        self.query_one("#favourites_header", SectionHeader).refresh()
