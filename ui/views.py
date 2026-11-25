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
    Input,
    TabbedContent,
    TabPane,
)
from textual.containers import Vertical
from textual.message import Message

from api import Album, Artist, Category, Playlist, Track, format_seconds
from stats import ArtistStat, StatsSummary
from theme import ThemeColors
from ui.widgets import SectionHeader, AnimatedLoader


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
class TrackActionMessage(Message):
    """Base message for track actions."""

    def __init__(
        self,
        track: Track,
        remaining_tracks: Optional[List[Track]] = None,
        auto_related: bool = False,
    ):
        super().__init__()
        self.track = track
        self.remaining_tracks = remaining_tracks or []
        self.auto_related = auto_related


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


class PlayQueueIndexMsg(Message):
    """Fired when a track in the Queue view is selected to play."""

    def __init__(self, index: int):
        super().__init__()
        self.index = index


class OpenAlbumMsg(Message):
    """Fired when an album should be opened."""

    def __init__(self, album_id: str, title: str = ""):
        super().__init__()
        self.album_id = album_id
        self.title = title


class OpenArtistMsg(Message):
    """Fired when an artist page should be opened."""

    def __init__(self, artist_id: str, name: str = ""):
        super().__init__()
        self.artist_id = artist_id
        self.name = name


class MoveQueueItemMsg(Message):
    """Fired when a queued track should move up or down."""

    def __init__(self, old_index: int, new_index: int):
        super().__init__()
        self.old_index = old_index
        self.new_index = new_index


class OpenMoodMsg(Message):
    """Fired when a mood/genre category is selected."""

    def __init__(self, category: Category):
        super().__init__()
        self.category = category


class ToggleSettingMsg(Message):
    """Fired when a setting row is activated in SettingsView."""

    def __init__(self, key: str):
        super().__init__()
        self.key = key


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
# 1. Home (For You, Recently Played, Quick Picks, Charts & Mixes)
# --------------------------------------------------------------------------- #
class TrendingView(TableFitMixin, Widget):
    """Home feed: For You, Recently Played, Quick Picks, Charts & Mixes."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.recommended_tracks: List[Track] = []
        self.recent_tracks: List[Track] = []
        self.trending_tracks: List[Track] = []
        self.playlists: List[Playlist] = []
        self._is_loading: bool = True

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="trending_header", classes="section_header")
            yield AnimatedLoader(
                title="LOADING HOME FEED",
                subtitle="fetching personalized picks & charts from YouTube Music…",
                id="trending_loader",
            )
            with TabbedContent(initial="tab_recommended", id="trending_tabs"):
                with TabPane("for you", id="tab_recommended"):
                    yield DataTable(id="recommended_tracks_table", cursor_type="row")
                with TabPane("recently played", id="tab_recent_tracks"):
                    yield DataTable(id="recent_tracks_table", cursor_type="row")
                with TabPane("quick picks", id="tab_trending_tracks"):
                    yield DataTable(id="trending_tracks_table", cursor_type="row")
                with TabPane("charts & mixes", id="tab_chart_playlists"):
                    yield DataTable(id="trending_playlists_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#recommended_tracks_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#recent_tracks_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#trending_tracks_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#trending_playlists_table", DataTable).add_columns(
            ("#", "num"), ("PLAYLIST", "title"), ("CURATOR", "meta")
        )
        self.query_one("#trending_header", SectionHeader).set_content("home")
        self.show_loading("LOADING HOME FEED", "fetching personalized picks & charts from YouTube Music…")
        self._fit_tables()

    def show_loading(self, title: str = "LOADING HOME FEED", subtitle: str = "") -> None:
        self._is_loading = True
        try:
            loader = self.query_one("#trending_loader", AnimatedLoader)
            loader.set_message(title, subtitle)
            loader.display = True
            self.query_one("#trending_tabs", TabbedContent).display = False
            self.query_one("#trending_header", SectionHeader).set_content(
                "home", [("loading…", True)]
            )
        except Exception:
            pass

    def hide_loading(self) -> None:
        self._is_loading = False
        try:
            self.query_one("#trending_loader", AnimatedLoader).display = False
            self.query_one("#trending_tabs", TabbedContent).display = True
        except Exception:
            pass

    def _fitted_tables(self):
        try:
            return [
                (self.query_one("#recommended_tracks_table", DataTable), "track"),
                (self.query_one("#recent_tracks_table", DataTable), "track"),
                (self.query_one("#trending_tracks_table", DataTable), "track"),
                (self.query_one("#trending_playlists_table", DataTable), "playlist"),
            ]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        if getattr(self, "_is_loading", False):
            return None
        try:
            tabs = self.query_one("#trending_tabs", TabbedContent)
            if tabs.active == "tab_chart_playlists":
                return self.query_one("#trending_playlists_table", DataTable)
            elif tabs.active == "tab_trending_tracks":
                return self.query_one("#trending_tracks_table", DataTable)
            elif tabs.active == "tab_recent_tracks":
                return self.query_one("#recent_tracks_table", DataTable)
            return self.query_one("#recommended_tracks_table", DataTable)
        except Exception:
            return None

    def set_recent(self, recent: List[Track]) -> None:
        self.recent_tracks = list(recent)
        try:
            rt = self.query_one("#recent_tracks_table", DataTable)
            rt.clear()
            for idx, tr in enumerate(self.recent_tracks, 1):
                rt.add_row(str(idx), tr.title, tr.artist, tr.display_duration, key=f"hrec_{tr.id}_{idx}")
            self._fit_tables()
        except Exception:
            pass

    def populate_data(
        self,
        recommended: List[Track],
        trending_or_playlists: Any,
        playlists: Optional[List[Playlist]] = None,
        recent: Optional[List[Track]] = None,
    ) -> None:
        self.hide_loading()
        if playlists is None:
            self.recommended_tracks = recommended
            self.trending_tracks = recommended
            self.playlists = trending_or_playlists or []
        else:
            self.recommended_tracks = recommended
            self.trending_tracks = trending_or_playlists or []
            self.playlists = playlists or []

        if recent is not None:
            self.set_recent(recent)

        rec_table = self.query_one("#recommended_tracks_table", DataTable)
        rec_table.clear()
        for idx, tr in enumerate(self.recommended_tracks, 1):
            rec_table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                              key=f"rec_{tr.id}_{idx}")

        tr_table = self.query_one("#trending_tracks_table", DataTable)
        tr_table.clear()
        for idx, tr in enumerate(self.trending_tracks, 1):
            tr_table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                             key=f"tr_{tr.id}_{idx}")

        pl_table = self.query_one("#trending_playlists_table", DataTable)
        pl_table.clear()
        for idx, p in enumerate(self.playlists, 1):
            pl_table.add_row(str(idx), p.title, p.author or "YouTube Music",
                             key=f"pl_{p.id}_{idx}")

        self.query_one("#trending_header", SectionHeader).set_content(
            "home",
            [
                (_count(len(self.recommended_tracks), "pick"), False),
                (_count(len(self.recent_tracks), "recent"), False),
                (_count(len(self.trending_tracks), "trending"), False),
                (_count(len(self.playlists), "chart"), False),
            ],
        )
        self._fit_tables()

    def get_selected_track(self) -> Optional[Track]:
        try:
            tabs = self.query_one("#trending_tabs", TabbedContent)
            if tabs.active == "tab_trending_tracks":
                table = self.query_one("#trending_tracks_table", DataTable)
                if 0 <= table.cursor_row < len(self.trending_tracks):
                    return self.trending_tracks[table.cursor_row]
            elif tabs.active == "tab_recent_tracks":
                table = self.query_one("#recent_tracks_table", DataTable)
                if 0 <= table.cursor_row < len(self.recent_tracks):
                    return self.recent_tracks[table.cursor_row]
            elif tabs.active == "tab_recommended":
                table = self.query_one("#recommended_tracks_table", DataTable)
                if 0 <= table.cursor_row < len(self.recommended_tracks):
                    return self.recommended_tracks[table.cursor_row]
        except Exception:
            pass
        return None

    def get_selected_playlist(self) -> Optional[Playlist]:
        try:
            tabs = self.query_one("#trending_tabs", TabbedContent)
            if tabs.active != "tab_chart_playlists":
                return None
            table = self.query_one("#trending_playlists_table", DataTable)
            if 0 <= table.cursor_row < len(self.playlists):
                return self.playlists[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        try:
            tabs = self.query_one("#trending_tabs", TabbedContent)
            if tabs.active == "tab_trending_tracks":
                return list(self.trending_tracks)
            if tabs.active == "tab_recent_tracks":
                return list(self.recent_tracks)
        except Exception:
            pass
        return list(self.recommended_tracks)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "recommended_tracks_table":
            if 0 <= event.cursor_row < len(self.recommended_tracks):
                tr = self.recommended_tracks[event.cursor_row]
                self.post_message(PlayTrackMsg(tr, self.recommended_tracks[event.cursor_row + 1:]))
        elif event.data_table.id == "recent_tracks_table":
            if 0 <= event.cursor_row < len(self.recent_tracks):
                tr = self.recent_tracks[event.cursor_row]
                self.post_message(PlayTrackMsg(tr, self.recent_tracks[event.cursor_row + 1:]))
        elif event.data_table.id == "trending_tracks_table":
            if 0 <= event.cursor_row < len(self.trending_tracks):
                tr = self.trending_tracks[event.cursor_row]
                self.post_message(PlayTrackMsg(tr, self.trending_tracks[event.cursor_row + 1:]))
        elif event.data_table.id == "trending_playlists_table":
            if 0 <= event.cursor_row < len(self.playlists):
                self.post_message(OpenPlaylistMsg(self.playlists[event.cursor_row]))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._fit_tables()
        w = self.primary_widget()
        if w:
            w.focus()

    def refresh_theme(self) -> None:
        self.query_one("#trending_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 1b. Explore / New Releases & Moods
# --------------------------------------------------------------------------- #
class ExploreView(TableFitMixin, Widget):
    """Explore: New album releases, trending songs, and moods & genres."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.new_releases: List[Album] = []
        self.trending: List[Track] = []
        self.moods: List[Category] = []
        self._is_loading: bool = False
        self._loaded: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="explore_header", classes="section_header")
            yield AnimatedLoader(
                title="LOADING NEW & EXPLORE",
                subtitle="fetching new releases, charts & moods…",
                id="explore_loader",
            )
            with TabbedContent(initial="tab_explore_releases", id="explore_tabs"):
                with TabPane("new releases", id="tab_explore_releases"):
                    yield DataTable(id="explore_releases_table", cursor_type="row")
                with TabPane("trending", id="tab_explore_trending"):
                    yield DataTable(id="explore_trending_table", cursor_type="row")
                with TabPane("moods & genres", id="tab_explore_moods"):
                    yield DataTable(id="explore_moods_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#explore_releases_table", DataTable).add_columns(
            ("#", "num"), ("ALBUM / SINGLE", "title"), ("ARTIST", "meta")
        )
        self.query_one("#explore_trending_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#explore_moods_table", DataTable).add_columns(
            ("#", "num"), ("MOOD / GENRE", "title"), ("SECTION", "meta")
        )
        self.query_one("#explore_header", SectionHeader).set_content("new & explore")
        self.query_one("#explore_loader", AnimatedLoader).display = False
        self._fit_tables()

    def show_loading(self, title: str = "LOADING NEW & EXPLORE", subtitle: str = "") -> None:
        self._is_loading = True
        try:
            loader = self.query_one("#explore_loader", AnimatedLoader)
            loader.set_message(title, subtitle)
            loader.display = True
            self.query_one("#explore_tabs", TabbedContent).display = False
            self.query_one("#explore_header", SectionHeader).set_content("new & explore", [("loading…", True)])
        except Exception:
            pass

    def hide_loading(self) -> None:
        self._is_loading = False
        try:
            self.query_one("#explore_loader", AnimatedLoader).display = False
            self.query_one("#explore_tabs", TabbedContent).display = True
        except Exception:
            pass

    def _fitted_tables(self):
        try:
            return [
                (self.query_one("#explore_releases_table", DataTable), "playlist"),
                (self.query_one("#explore_trending_table", DataTable), "track"),
                (self.query_one("#explore_moods_table", DataTable), "playlist"),
            ]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        if getattr(self, "_is_loading", False):
            return None
        try:
            tabs = self.query_one("#explore_tabs", TabbedContent)
            if tabs.active == "tab_explore_trending":
                return self.query_one("#explore_trending_table", DataTable)
            if tabs.active == "tab_explore_moods":
                return self.query_one("#explore_moods_table", DataTable)
            return self.query_one("#explore_releases_table", DataTable)
        except Exception:
            return None

    def set_explore(self, releases: List[Album], trending: List[Track], moods: List[Category]) -> None:
        self.hide_loading()
        self._loaded = True
        self.new_releases = list(releases)
        self.trending = list(trending)
        self.moods = list(moods)

        rt = self.query_one("#explore_releases_table", DataTable)
        rt.clear()
        for idx, al in enumerate(self.new_releases, 1):
            meta = f"{al.artist} · {al.year}" if al.year else al.artist
            rt.add_row(str(idx), al.title, meta, key=f"er_{al.id}_{idx}")

        tt = self.query_one("#explore_trending_table", DataTable)
        tt.clear()
        for idx, tr in enumerate(self.trending, 1):
            tt.add_row(str(idx), tr.title, tr.artist, tr.display_duration, key=f"et_{tr.id}_{idx}")

        mt = self.query_one("#explore_moods_table", DataTable)
        mt.clear()
        for idx, m in enumerate(self.moods, 1):
            mt.add_row(str(idx), m.title, m.category_type, key=f"em_{idx}")

        self.query_one("#explore_header", SectionHeader).set_content(
            "new & explore",
            [
                (_count(len(self.new_releases), "release"), False),
                (_count(len(self.trending), "trending"), False),
                (_count(len(self.moods), "mood"), False),
            ],
        )
        self._fit_tables()

    def get_selected_track(self) -> Optional[Track]:
        try:
            tabs = self.query_one("#explore_tabs", TabbedContent)
            if tabs.active != "tab_explore_trending":
                return None
            table = self.query_one("#explore_trending_table", DataTable)
            if 0 <= table.cursor_row < len(self.trending):
                return self.trending[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        return list(self.trending)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "explore_releases_table":
            if 0 <= event.cursor_row < len(self.new_releases):
                al = self.new_releases[event.cursor_row]
                self.post_message(OpenAlbumMsg(al.id, al.title))
        elif event.data_table.id == "explore_trending_table":
            if 0 <= event.cursor_row < len(self.trending):
                tr = self.trending[event.cursor_row]
                self.post_message(PlayTrackMsg(tr, self.trending[event.cursor_row + 1:]))
        elif event.data_table.id == "explore_moods_table":
            if 0 <= event.cursor_row < len(self.moods):
                self.post_message(OpenMoodMsg(self.moods[event.cursor_row]))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._fit_tables()
        w = self.primary_widget()
        if w:
            w.focus()

    def refresh_theme(self) -> None:
        self.query_one("#explore_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 1c. Mood / Genre Playlists Detail
# --------------------------------------------------------------------------- #
class MoodDetailView(TableFitMixin, Widget):
    """Curated playlists inside a selected Mood / Genre."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.category: Optional[Category] = None
        self.playlists: List[Playlist] = []
        self._is_loading: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="mood_header", classes="section_header")
            yield AnimatedLoader(
                title="LOADING MOOD",
                subtitle="fetching curated playlists…",
                id="mood_loader",
            )
            yield DataTable(id="mood_playlists_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#mood_playlists_table", DataTable).add_columns(
            ("#", "num"), ("PLAYLIST", "title"), ("CURATOR", "meta")
        )
        self.query_one("#mood_header", SectionHeader).set_content("moods & genres")
        self.query_one("#mood_loader", AnimatedLoader).display = False
        self._fit_tables()

    def show_loading(self, title: str = "LOADING MOOD", subtitle: str = "") -> None:
        self._is_loading = True
        try:
            loader = self.query_one("#mood_loader", AnimatedLoader)
            loader.set_message(title, subtitle)
            loader.display = True
            self.query_one("#mood_playlists_table", DataTable).display = False
            self.query_one("#mood_header", SectionHeader).set_content("moods & genres", [("loading…", True)])
        except Exception:
            pass

    def hide_loading(self) -> None:
        self._is_loading = False
        try:
            self.query_one("#mood_loader", AnimatedLoader).display = False
            self.query_one("#mood_playlists_table", DataTable).display = True
        except Exception:
            pass

    def _fitted_tables(self):
        try:
            return [(self.query_one("#mood_playlists_table", DataTable), "playlist")]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        if getattr(self, "_is_loading", False):
            return None
        try:
            return self.query_one("#mood_playlists_table", DataTable)
        except Exception:
            return None

    def set_playlists(self, category: Category, playlists: List[Playlist]) -> None:
        self.hide_loading()
        self.category = category
        self.playlists = list(playlists)
        self.query_one("#mood_header", SectionHeader).set_content(
            "mood & genre",
            [(_clip(category.title, 36), True), (_count(len(self.playlists), "playlist"), False)],
        )
        table = self.query_one("#mood_playlists_table", DataTable)
        table.clear()
        for idx, p in enumerate(self.playlists, 1):
            table.add_row(str(idx), p.title, p.author or "Curated", key=f"mp_{p.id}_{idx}")
        self._fit_tables()

    def get_selected_playlist(self) -> Optional[Playlist]:
        try:
            table = self.query_one("#mood_playlists_table", DataTable)
            if 0 <= table.cursor_row < len(self.playlists):
                return self.playlists[table.cursor_row]
        except Exception:
            pass
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.playlists):
            self.post_message(OpenPlaylistMsg(self.playlists[event.cursor_row]))

    def refresh_theme(self) -> None:
        self.query_one("#mood_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 2. Radio
# --------------------------------------------------------------------------- #
class RadioView(TableFitMixin, Widget):
    """Algorithmic song radio recommendations."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tracks: List[Track] = []
        self.seed_track: Optional[Track] = None
        self._is_loading: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="radio_header", classes="section_header")
            yield AnimatedLoader(
                title="BUILDING RADIO",
                subtitle="generating algorithmic station…",
                id="radio_loader",
            )
            yield DataTable(id="radio_tracks_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#radio_tracks_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#radio_header", SectionHeader).set_content(
            "radio", [("press r on a song", False)]
        )
        self.query_one("#radio_loader", AnimatedLoader).display = False
        self._fit_tables()

    def show_loading(self, title: str = "BUILDING RADIO", subtitle: str = "") -> None:
        self._is_loading = True
        try:
            loader = self.query_one("#radio_loader", AnimatedLoader)
            loader.set_message(title, subtitle)
            loader.display = True
            self.query_one("#radio_tracks_table", DataTable).display = False
            self.query_one("#radio_header", SectionHeader).set_content("radio", [("building…", True)])
        except Exception:
            pass

    def hide_loading(self) -> None:
        self._is_loading = False
        try:
            self.query_one("#radio_loader", AnimatedLoader).display = False
            self.query_one("#radio_tracks_table", DataTable).display = True
        except Exception:
            pass

    def _fitted_tables(self):
        try:
            return [(self.query_one("#radio_tracks_table", DataTable), "track")]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        if getattr(self, "_is_loading", False):
            return None
        try:
            return self.query_one("#radio_tracks_table", DataTable)
        except Exception:
            return None

    def set_radio_tracks(self, seed: Track, tracks: List[Track]) -> None:
        self.hide_loading()
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
# 3. Playlist detail
# --------------------------------------------------------------------------- #
class PlaylistDetailView(TableFitMixin, Widget):
    """Inspect a playlist's tracks."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.playlist: Optional[Playlist] = None
        self.tracks: List[Track] = []
        self._is_loading: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="pl_header", classes="section_header")
            yield AnimatedLoader(
                title="LOADING PLAYLIST",
                subtitle="fetching tracks from YouTube Music…",
                id="pl_loader",
            )
            yield DataTable(id="pl_detail_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#pl_detail_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#pl_header", SectionHeader).set_content("playlist")
        self.query_one("#pl_loader", AnimatedLoader).display = False
        self._fit_tables()

    def show_loading(self, title: str = "LOADING PLAYLIST", subtitle: str = "") -> None:
        self._is_loading = True
        try:
            loader = self.query_one("#pl_loader", AnimatedLoader)
            loader.set_message(title, subtitle)
            loader.display = True
            self.query_one("#pl_detail_table", DataTable).display = False
            self.query_one("#pl_header", SectionHeader).set_content("playlist", [("loading…", True)])
        except Exception:
            pass

    def hide_loading(self) -> None:
        self._is_loading = False
        try:
            self.query_one("#pl_loader", AnimatedLoader).display = False
            self.query_one("#pl_detail_table", DataTable).display = True
        except Exception:
            pass

    def _fitted_tables(self):
        try:
            return [(self.query_one("#pl_detail_table", DataTable), "track")]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        if getattr(self, "_is_loading", False):
            return None
        try:
            return self.query_one("#pl_detail_table", DataTable)
        except Exception:
            return None

    def set_playlist(self, playlist: Playlist, tracks: List[Track]) -> None:
        self.hide_loading()
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

    def set_album(self, album: Album, tracks: List[Track]) -> None:
        self.hide_loading()
        self.playlist = Playlist(
            id=album.id,
            title=album.title,
            author=album.artist,
            track_count=album.track_count,
            thumbnail=album.thumbnail,
        )
        self.tracks = tracks

        chips = [(_clip(album.title, 36), True)]
        if album.artist:
            chips.append((_clip(album.artist, 24), False))
        if album.year:
            chips.append((album.year, False))
        chips.append((_count(len(tracks), "track"), False))
        self.query_one("#pl_header", SectionHeader).set_content("album", chips)

        table = self.query_one("#pl_detail_table", DataTable)
        table.clear()
        for idx, tr in enumerate(tracks, 1):
            table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                          key=f"alb_{tr.id}_{idx}")
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
# 4. Search
# --------------------------------------------------------------------------- #
class SearchView(TableFitMixin, Widget):
    """Search songs, albums, artists, and playlists with live suggestions."""

    # Wait this long after the last keystroke before querying YouTube Music,
    # so a burst of typing triggers a single request instead of one per key.
    SEARCH_DEBOUNCE = 0.35
    MIN_QUERY_LENGTH = 2

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.song_results: List[Track] = []
        self.album_results: List[Album] = []
        self.artist_results: List[Artist] = []
        self.playlist_results: List[Playlist] = []
        self._search_timer = None
        self._last_query = ""
        self._is_loading: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield Input(placeholder="search songs, albums, artists or playlists…", id="search_input")
            yield SectionHeader(id="search_header", classes="section_header")
            yield AnimatedLoader(
                title="SEARCHING",
                subtitle="searching YouTube Music…",
                id="search_loader",
            )
            with TabbedContent(initial="tab_search_songs", id="search_tabs"):
                with TabPane("songs", id="tab_search_songs"):
                    yield DataTable(id="search_songs_table", cursor_type="row")
                with TabPane("albums", id="tab_search_albums"):
                    yield DataTable(id="search_albums_table", cursor_type="row")
                with TabPane("artists", id="tab_search_artists"):
                    yield DataTable(id="search_artists_table", cursor_type="row")
                with TabPane("playlists", id="tab_search_playlists"):
                    yield DataTable(id="search_playlists_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#search_songs_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#search_albums_table", DataTable).add_columns(
            ("#", "num"), ("ALBUM", "title"), ("ARTIST / YEAR", "meta")
        )
        self.query_one("#search_artists_table", DataTable).add_columns(
            ("#", "num"), ("ARTIST", "title"), ("SUBSCRIBERS", "meta")
        )
        self.query_one("#search_playlists_table", DataTable).add_columns(
            ("#", "num"), ("PLAYLIST", "title"), ("CURATOR", "meta")
        )
        self.query_one("#search_header", SectionHeader).set_content("search")
        self.query_one("#search_loader", AnimatedLoader).display = False
        self._fit_tables()

    def show_loading(self, title: str = "SEARCHING", subtitle: str = "") -> None:
        self._is_loading = True
        try:
            loader = self.query_one("#search_loader", AnimatedLoader)
            loader.set_message(title, subtitle)
            loader.display = True
            self.query_one("#search_tabs", TabbedContent).display = False
            self.query_one("#search_header", SectionHeader).set_content("search", [("searching…", True)])
        except Exception:
            pass

    def hide_loading(self) -> None:
        self._is_loading = False
        try:
            self.query_one("#search_loader", AnimatedLoader).display = False
            self.query_one("#search_tabs", TabbedContent).display = True
        except Exception:
            pass

    def _fitted_tables(self):
        try:
            return [
                (self.query_one("#search_songs_table", DataTable), "track"),
                (self.query_one("#search_albums_table", DataTable), "playlist"),
                (self.query_one("#search_artists_table", DataTable), "playlist"),
                (self.query_one("#search_playlists_table", DataTable), "playlist"),
            ]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        try:
            if getattr(self, "_is_loading", False):
                return self.query_one("#search_input", Input)
            tabs = self.query_one("#search_tabs", TabbedContent)
            if tabs.active == "tab_search_albums":
                return self.query_one("#search_albums_table", DataTable)
            if tabs.active == "tab_search_artists":
                return self.query_one("#search_artists_table", DataTable)
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
        self.hide_loading()
        self.song_results = []
        self.album_results = []
        self.artist_results = []
        self.playlist_results = []
        self.query_one("#search_songs_table", DataTable).clear()
        self.query_one("#search_albums_table", DataTable).clear()
        self.query_one("#search_artists_table", DataTable).clear()
        self.query_one("#search_playlists_table", DataTable).clear()
        self.query_one("#search_header", SectionHeader).set_content("search")

    def set_results(
        self,
        songs: List[Track],
        playlists: List[Playlist],
        albums: Optional[List[Album]] = None,
        artists: Optional[List[Artist]] = None,
    ) -> None:
        self.hide_loading()
        self.song_results = songs
        self.album_results = albums or []
        self.artist_results = artists or []
        self.playlist_results = playlists

        s_table = self.query_one("#search_songs_table", DataTable)
        s_table.clear()
        for idx, tr in enumerate(songs, 1):
            s_table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                            key=f"s_{tr.id}_{idx}")

        al_table = self.query_one("#search_albums_table", DataTable)
        al_table.clear()
        for idx, al in enumerate(self.album_results, 1):
            meta = f"{al.artist} ({al.year})" if al.year else al.artist
            al_table.add_row(str(idx), al.title, meta, key=f"sal_{al.id}_{idx}")

        ar_table = self.query_one("#search_artists_table", DataTable)
        ar_table.clear()
        for idx, ar in enumerate(self.artist_results, 1):
            ar_table.add_row(str(idx), ar.name, ar.subscribers or "Artist", key=f"sar_{ar.id}_{idx}")

        p_table = self.query_one("#search_playlists_table", DataTable)
        p_table.clear()
        for idx, p in enumerate(playlists, 1):
            p_table.add_row(str(idx), p.title, p.author or "Community",
                            key=f"sp_{p.id}_{idx}")

        self.query_one("#search_header", SectionHeader).set_content(
            "search",
            [
                (_count(len(songs), "song"), False),
                (_count(len(self.album_results), "album"), False),
                (_count(len(self.artist_results), "artist"), False),
                (_count(len(playlists), "playlist"), False),
            ],
        )
        self._fit_tables()

    def get_selected_track(self) -> Optional[Track]:
        try:
            tabs = self.query_one("#search_tabs", TabbedContent)
            if tabs.active != "tab_search_songs":
                return None
            table = self.query_one("#search_songs_table", DataTable)
            if 0 <= table.cursor_row < len(self.song_results):
                return self.song_results[table.cursor_row]
        except Exception:
            pass
        return None

    def get_selected_playlist(self) -> Optional[Playlist]:
        try:
            tabs = self.query_one("#search_tabs", TabbedContent)
            if tabs.active != "tab_search_playlists":
                return None
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
                self.post_message(PlayTrackMsg(tr, auto_related=True))
        elif event.data_table.id == "search_albums_table":
            if 0 <= event.cursor_row < len(self.album_results):
                al = self.album_results[event.cursor_row]
                self.post_message(OpenAlbumMsg(al.id, al.title))
        elif event.data_table.id == "search_artists_table":
            if 0 <= event.cursor_row < len(self.artist_results):
                ar = self.artist_results[event.cursor_row]
                self.post_message(OpenArtistMsg(ar.id, ar.name))
        elif event.data_table.id == "search_playlists_table":
            if 0 <= event.cursor_row < len(self.playlist_results):
                self.post_message(OpenPlaylistMsg(self.playlist_results[event.cursor_row]))

    def refresh_theme(self) -> None:
        self.query_one("#search_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 4b. Artist Detail
# --------------------------------------------------------------------------- #
class ArtistDetailView(TableFitMixin, Widget):
    """Inspect an artist's top songs and releases."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.artist: Optional[Artist] = None
        self.songs: List[Track] = []
        self.albums: List[Album] = []
        self._is_loading: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="artist_header", classes="section_header")
            yield AnimatedLoader(
                title="LOADING ARTIST",
                subtitle="fetching artist discography…",
                id="artist_loader",
            )
            with TabbedContent(initial="tab_artist_songs", id="artist_tabs"):
                with TabPane("top songs", id="tab_artist_songs"):
                    yield DataTable(id="artist_songs_table", cursor_type="row")
                with TabPane("albums & singles", id="tab_artist_albums"):
                    yield DataTable(id="artist_albums_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#artist_songs_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#artist_albums_table", DataTable).add_columns(
            ("#", "num"), ("ALBUM", "title"), ("YEAR", "meta")
        )
        self.query_one("#artist_header", SectionHeader).set_content("artist")
        self.query_one("#artist_loader", AnimatedLoader).display = False
        self._fit_tables()

    def show_loading(self, title: str = "LOADING ARTIST", subtitle: str = "") -> None:
        self._is_loading = True
        try:
            loader = self.query_one("#artist_loader", AnimatedLoader)
            loader.set_message(title, subtitle)
            loader.display = True
            self.query_one("#artist_tabs", TabbedContent).display = False
            self.query_one("#artist_header", SectionHeader).set_content("artist", [("loading…", True)])
        except Exception:
            pass

    def hide_loading(self) -> None:
        self._is_loading = False
        try:
            self.query_one("#artist_loader", AnimatedLoader).display = False
            self.query_one("#artist_tabs", TabbedContent).display = True
        except Exception:
            pass

    def _fitted_tables(self):
        try:
            return [
                (self.query_one("#artist_songs_table", DataTable), "track"),
                (self.query_one("#artist_albums_table", DataTable), "playlist"),
            ]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        if getattr(self, "_is_loading", False):
            return None
        try:
            tabs = self.query_one("#artist_tabs", TabbedContent)
            if tabs.active == "tab_artist_albums":
                return self.query_one("#artist_albums_table", DataTable)
            return self.query_one("#artist_songs_table", DataTable)
        except Exception:
            return None

    def set_artist(self, artist: Artist, songs: List[Track], albums: List[Album]) -> None:
        self.hide_loading()
        self.artist = artist
        self.songs = songs
        self.albums = albums

        chips = [(_clip(artist.name, 36), True)]
        if artist.subscribers:
            chips.append((artist.subscribers, False))
        chips.append((_count(len(songs), "song"), False))
        chips.append((_count(len(albums), "release"), False))
        self.query_one("#artist_header", SectionHeader).set_content("artist", chips)

        st = self.query_one("#artist_songs_table", DataTable)
        st.clear()
        for idx, tr in enumerate(songs, 1):
            st.add_row(str(idx), tr.title, tr.artist, tr.display_duration, key=f"ars_{tr.id}_{idx}")

        at = self.query_one("#artist_albums_table", DataTable)
        at.clear()
        for idx, al in enumerate(albums, 1):
            at.add_row(str(idx), al.title, al.year or "Release", key=f"ara_{al.id}_{idx}")
        self._fit_tables()

    def get_selected_track(self) -> Optional[Track]:
        try:
            tabs = self.query_one("#artist_tabs", TabbedContent)
            if tabs.active != "tab_artist_songs":
                return None
            table = self.query_one("#artist_songs_table", DataTable)
            if 0 <= table.cursor_row < len(self.songs):
                return self.songs[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        return list(self.songs)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "artist_songs_table":
            if 0 <= event.cursor_row < len(self.songs):
                tr = self.songs[event.cursor_row]
                self.post_message(PlayTrackMsg(tr, self.songs[event.cursor_row + 1:]))
        elif event.data_table.id == "artist_albums_table":
            if 0 <= event.cursor_row < len(self.albums):
                al = self.albums[event.cursor_row]
                self.post_message(OpenAlbumMsg(al.id, al.title))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._fit_tables()
        w = self.primary_widget()
        if w:
            w.focus()

    def refresh_theme(self) -> None:
        self.query_one("#artist_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 5. Queue
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
        prev_row = table.cursor_row
        table.clear()
        for idx, tr in enumerate(queue, 1):
            table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                          key=f"q_{tr.id}_{idx}")
        if queue:
            target_row = max(0, min(prev_row, len(queue) - 1))
            table.move_cursor(row=target_row)
        self._fit_tables()

    def get_selected_track(self) -> Optional[Track]:
        try:
            table = self.query_one("#queue_table", DataTable)
            if 0 <= table.cursor_row < len(self.queue_tracks):
                return self.queue_tracks[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        return list(self.queue_tracks)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.queue_tracks):
            self.post_message(PlayQueueIndexMsg(event.cursor_row))

    def move_highlighted(self, delta: int) -> None:
        try:
            table = self.query_one("#queue_table", DataTable)
            row = table.cursor_row
            target = row + delta
            if 0 <= row < len(self.queue_tracks) and 0 <= target < len(self.queue_tracks):
                self.post_message(MoveQueueItemMsg(row, target))
                table.move_cursor(row=target)
        except Exception:
            pass

    def refresh_theme(self) -> None:
        self.query_one("#queue_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 6. Library (Liked Songs, Recently Played, Artists)
# --------------------------------------------------------------------------- #
class FavouritesView(TableFitMixin, Widget):
    """Library: liked songs, recently played, and top artists."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tracks: List[Track] = []
        self.recent_tracks: List[Track] = []
        self.top_artists: List[ArtistStat] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="favourites_header", classes="section_header")
            with TabbedContent(initial="tab_lib_liked", id="library_tabs"):
                with TabPane("liked songs", id="tab_lib_liked"):
                    yield DataTable(id="favourites_table", cursor_type="row")
                with TabPane("recently played", id="tab_lib_recent"):
                    yield DataTable(id="library_recent_table", cursor_type="row")
                with TabPane("artists", id="tab_lib_artists"):
                    yield DataTable(id="library_artists_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#favourites_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#library_recent_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("TIME", "time")
        )
        self.query_one("#library_artists_table", DataTable).add_columns(
            ("#", "num"), ("ARTIST", "title"), ("PLAYS / TIME", "meta")
        )
        self.query_one("#favourites_header", SectionHeader).set_content(
            "library", [("press f on a song to like", False)]
        )
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [
                (self.query_one("#favourites_table", DataTable), "track"),
                (self.query_one("#library_recent_table", DataTable), "track"),
                (self.query_one("#library_artists_table", DataTable), "playlist"),
            ]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        try:
            tabs = self.query_one("#library_tabs", TabbedContent)
            if tabs.active == "tab_lib_recent":
                return self.query_one("#library_recent_table", DataTable)
            if tabs.active == "tab_lib_artists":
                return self.query_one("#library_artists_table", DataTable)
            return self.query_one("#favourites_table", DataTable)
        except Exception:
            return None

    def set_favourites(self, tracks: List[Track]) -> None:
        self.tracks = list(tracks)
        self._update_header()

        table = self.query_one("#favourites_table", DataTable)
        table.clear()
        for idx, tr in enumerate(self.tracks, 1):
            table.add_row(str(idx), tr.title, tr.artist, tr.display_duration,
                          key=f"fav_{tr.id}_{idx}")
        self._fit_tables()

    def set_library_extra(self, recent: List[Track], artists: List[ArtistStat]) -> None:
        self.recent_tracks = list(recent)
        self.top_artists = list(artists)
        self._update_header()

        try:
            rt = self.query_one("#library_recent_table", DataTable)
            rt.clear()
            for idx, tr in enumerate(self.recent_tracks, 1):
                rt.add_row(str(idx), tr.title, tr.artist, tr.display_duration, key=f"lrec_{tr.id}_{idx}")

            at = self.query_one("#library_artists_table", DataTable)
            at.clear()
            for idx, item in enumerate(self.top_artists, 1):
                mins = int(item.listen_seconds // 60)
                at.add_row(str(idx), item.name, f"{item.play_count}× · {mins}m", key=f"lart_{idx}")
            self._fit_tables()
        except Exception:
            pass

    def _update_header(self) -> None:
        chips: List[Tuple[str, bool]] = []
        if self.tracks:
            chips.append((_count(len(self.tracks), "liked"), True))
        if self.recent_tracks:
            chips.append((_count(len(self.recent_tracks), "recent"), False))
        if self.top_artists:
            chips.append((_count(len(self.top_artists), "artist"), False))
        if not chips:
            chips = [("press f on a song to like", False)]
        try:
            self.query_one("#favourites_header", SectionHeader).set_content("library", chips)
        except Exception:
            pass

    def get_selected_track(self) -> Optional[Track]:
        try:
            tabs = self.query_one("#library_tabs", TabbedContent)
            if tabs.active == "tab_lib_recent":
                table = self.query_one("#library_recent_table", DataTable)
                if 0 <= table.cursor_row < len(self.recent_tracks):
                    return self.recent_tracks[table.cursor_row]
            elif tabs.active == "tab_lib_liked":
                table = self.query_one("#favourites_table", DataTable)
                if 0 <= table.cursor_row < len(self.tracks):
                    return self.tracks[table.cursor_row]
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        try:
            tabs = self.query_one("#library_tabs", TabbedContent)
            if tabs.active == "tab_lib_recent":
                return list(self.recent_tracks)
        except Exception:
            pass
        return list(self.tracks)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "favourites_table":
            if 0 <= event.cursor_row < len(self.tracks):
                tr = self.tracks[event.cursor_row]
                self.post_message(PlayTrackMsg(tr, self.tracks[event.cursor_row + 1:]))
        elif event.data_table.id == "library_recent_table":
            if 0 <= event.cursor_row < len(self.recent_tracks):
                tr = self.recent_tracks[event.cursor_row]
                self.post_message(PlayTrackMsg(tr, self.recent_tracks[event.cursor_row + 1:]))
        elif event.data_table.id == "library_artists_table":
            if 0 <= event.cursor_row < len(self.top_artists):
                name = self.top_artists[event.cursor_row].name
                self.post_message(ExecuteSearchMsg(name, live=False))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._fit_tables()
        w = self.primary_widget()
        if w:
            w.focus()

    def refresh_theme(self) -> None:
        self.query_one("#favourites_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 7. Listening Stats / Replay
# --------------------------------------------------------------------------- #
class StatsView(TableFitMixin, Widget):
    """Local Replay summary: top tracks, artists & hourly activity chart."""

    _BARS = " ▁▂▃▄▅▆▇█"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._summary: Optional[StatsSummary] = None
        self.period: str = "month"

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="stats_header", classes="section_header")
            yield Static("", id="stats_overview")
            yield Static("", id="stats_histogram")
            with TabbedContent(initial="tab_stats_tracks", id="stats_tabs"):
                with TabPane("top tracks", id="tab_stats_tracks"):
                    yield DataTable(id="stats_tracks_table", cursor_type="row")
                with TabPane("top artists", id="tab_stats_artists"):
                    yield DataTable(id="stats_artists_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#stats_tracks_table", DataTable).add_columns(
            ("#", "num"), ("TITLE", "title"), ("ARTIST", "artist"), ("PLAYS", "time")
        )
        self.query_one("#stats_artists_table", DataTable).add_columns(
            ("#", "num"), ("ARTIST", "title"), ("LISTENS", "meta")
        )
        self.query_one("#stats_header", SectionHeader).set_content("replay & stats")
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [
                (self.query_one("#stats_tracks_table", DataTable), "track"),
                (self.query_one("#stats_artists_table", DataTable), "playlist"),
            ]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        try:
            tabs = self.query_one("#stats_tabs", TabbedContent)
            if tabs.active == "tab_stats_artists":
                return self.query_one("#stats_artists_table", DataTable)
            return self.query_one("#stats_tracks_table", DataTable)
        except Exception:
            return None

    def set_summary(self, summary: StatsSummary) -> None:
        self._summary = summary
        self.period = summary.period
        t = _theme(self)

        period_chips = [
            ("today", summary.period == "today"),
            ("this month", summary.period == "month"),
            ("all time", summary.period == "all"),
        ]
        self.query_one("#stats_header", SectionHeader).set_content("replay & stats", period_chips)

        mins = int(summary.total_seconds // 60)
        hrs, rem_m = divmod(mins, 60)
        dur_str = f"{hrs}h {rem_m}m" if hrs else f"{mins}m"
        top_artist = summary.top_artists[0].name if summary.top_artists else "—"
        self.query_one("#stats_overview", Static).update(
            f"  [bold {t.accent}]{dur_str}[/] [{t.muted}]listened[/]  ·  "
            f"[bold {t.foreground}]{summary.total_plays}[/] [{t.muted}]plays[/]  ·  "
            f"[bold {t.foreground}]{summary.unique_tracks}[/] [{t.muted}]tracks[/]  ·  "
            f"[{t.muted}]top artist:[/] [bold {t.secondary}]{top_artist}[/]"
        )

        peak = max(summary.hourly_seconds) if summary.hourly_seconds else 0.0
        bars = ""
        for val in summary.hourly_seconds:
            if peak <= 0 or val <= 0:
                bars += "▁"
            else:
                idx = max(1, min(len(self._BARS) - 1, int(round((val / peak) * (len(self._BARS) - 1)))))
                bars += self._BARS[idx]
        self.query_one("#stats_histogram", Static).update(
            f"  [{t.muted}]00h [/][bold {t.primary}]{bars}[/][{t.muted}] 23h[/]"
        )

        tt = self.query_one("#stats_tracks_table", DataTable)
        tt.clear()
        for idx, item in enumerate(summary.top_tracks, 1):
            tt.add_row(
                str(idx),
                item.track.title,
                item.track.artist,
                f"{item.play_count}×",
                key=f"stt_{item.track.id}_{idx}",
            )

        at = self.query_one("#stats_artists_table", DataTable)
        at.clear()
        for idx, item in enumerate(summary.top_artists, 1):
            m = int(item.listen_seconds // 60)
            at.add_row(str(idx), item.name, f"{item.play_count}× · {m}m", key=f"sta_{idx}")
        self._fit_tables()

    def get_selected_track(self) -> Optional[Track]:
        if not self._summary:
            return None
        try:
            tabs = self.query_one("#stats_tabs", TabbedContent)
            if tabs.active != "tab_stats_tracks":
                return None
            table = self.query_one("#stats_tracks_table", DataTable)
            if 0 <= table.cursor_row < len(self._summary.top_tracks):
                return self._summary.top_tracks[table.cursor_row].track
        except Exception:
            pass
        return None

    def get_all_tracks(self) -> List[Track]:
        if not self._summary:
            return []
        return [s.track for s in self._summary.top_tracks]

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "stats_tracks_table" and self._summary:
            if 0 <= event.cursor_row < len(self._summary.top_tracks):
                tracks = [s.track for s in self._summary.top_tracks]
                self.post_message(PlayTrackMsg(tracks[event.cursor_row], tracks[event.cursor_row + 1:]))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._fit_tables()
        w = self.primary_widget()
        if w:
            w.focus()

    def refresh_theme(self) -> None:
        if self._summary:
            self.set_summary(self._summary)
        else:
            self.query_one("#stats_header", SectionHeader).refresh()


# --------------------------------------------------------------------------- #
# 8. Settings
# --------------------------------------------------------------------------- #
class SettingsView(TableFitMixin, Widget):
    """Interactive settings panel for playback, UI & integrations."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._rows: List[Tuple[str, str, str, str]] = []  # (key, section, label, value)

    def compose(self) -> ComposeResult:
        with Vertical(classes="view_content_container"):
            yield SectionHeader(id="settings_header", classes="section_header")
            yield DataTable(id="settings_table", cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#settings_table", DataTable).add_columns(
            ("#", "num"), ("SETTING", "title"), ("VALUE", "meta")
        )
        self.query_one("#settings_header", SectionHeader).set_content(
            "settings", [("enter to toggle or cycle", False)]
        )
        self._fit_tables()

    def _fitted_tables(self):
        try:
            return [(self.query_one("#settings_table", DataTable), "playlist")]
        except Exception:
            return []

    def primary_widget(self) -> Optional[Widget]:
        try:
            return self.query_one("#settings_table", DataTable)
        except Exception:
            return None

    def set_rows(self, rows: List[Tuple[str, str, str, str]]) -> None:
        """Update displayed settings rows: list of (key, section, label, value)."""
        self._rows = list(rows)
        table = self.query_one("#settings_table", DataTable)
        prev_row = table.cursor_row
        table.clear()
        for idx, (key, section, label, value) in enumerate(self._rows, 1):
            table.add_row(str(idx), f"{section} · {label}", value, key=f"set_{key}")
        if self._rows:
            table.move_cursor(row=max(0, min(prev_row, len(self._rows) - 1)))
        self._fit_tables()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._rows):
            key = self._rows[event.cursor_row][0]
            self.post_message(ToggleSettingMsg(key))

    def refresh_theme(self) -> None:
        self.query_one("#settings_header", SectionHeader).refresh()
