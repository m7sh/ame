"""
api.py - Unauthenticated YouTube Music API Layer & Stream Resolver bridge.

Uses ytmusicapi in guest mode (YTMusic()) with zero login/auth required.
Stream resolution is delegated to stream_resolver.StreamResolver (InnerTube
client walk + 1 MiB byte-range probing + yt-dlp failsafe).
"""

import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from ytmusicapi import YTMusic

from stream_resolver import (  # noqa: F401
    PermanentlyUnplayableError,
    ResolvedStream,
    StreamResolver,
    media_headers_for_url,
)


def parse_duration(val: Any) -> Tuple[str, int]:
    """Parse various duration formats into (formatted_str, seconds)."""
    if val is None:
        return "--:--", 0

    if isinstance(val, (int, float)):
        seconds = max(0, int(val))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}", seconds
        return f"{m:02d}:{s:02d}", seconds

    if isinstance(val, str):
        val = val.strip()
        parts = val.split(":")
        try:
            if len(parts) == 2:
                seconds = int(parts[0]) * 60 + int(parts[1])
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}", seconds
            if len(parts) == 3:
                seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}", seconds
        except ValueError:
            pass
        return val, 0

    return "--:--", 0


def format_seconds(seconds: float | int) -> str:
    """Format seconds into MM:SS or HH:MM:SS."""
    sec = max(0, int(seconds))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def extract_artists_str(artists: Any) -> str:
    """Extract comma-separated artist names from YTMusic artist list or dict."""
    if not artists:
        return "Unknown Artist"
    if isinstance(artists, str):
        return artists
    if isinstance(artists, list):
        names = []
        for a in artists:
            if isinstance(a, dict) and "name" in a:
                names.append(str(a["name"]))
            elif isinstance(a, str):
                names.append(a)
        if names:
            return ", ".join(names)
    elif isinstance(artists, dict) and "name" in artists:
        return str(artists["name"])
    return "Unknown Artist"


def _extract_thumbnail_url(item: Dict[str, Any]) -> str:
    """Safely extract the highest-resolution thumbnail URL from diverse ytmusicapi shapes."""
    thumbs = item.get("thumbnails")
    if not thumbs:
        thumbs = item.get("thumbnail")
    if isinstance(thumbs, dict):
        thumbs = thumbs.get("thumbnails", [])
    if isinstance(thumbs, list) and thumbs:
        last = thumbs[-1]
        if isinstance(last, dict):
            return str(last.get("url") or "")
    return ""


def _extract_album_name(item: Dict[str, Any]) -> str:
    album_raw = item.get("album")
    if isinstance(album_raw, dict):
        return str(album_raw.get("name") or "")
    if isinstance(album_raw, str):
        return album_raw
    return ""


def _extract_album_id(item: Dict[str, Any]) -> str:
    album_raw = item.get("album")
    if isinstance(album_raw, dict):
        return str(album_raw.get("id") or "")
    return ""


def _extract_artist_id(item: Dict[str, Any]) -> str:
    artists = item.get("artists")
    if isinstance(artists, list):
        for a in artists:
            if isinstance(a, dict) and a.get("id"):
                return str(a["id"])
    elif isinstance(artists, dict) and artists.get("id"):
        return str(artists["id"])
    return ""


def _parse_track(item: Dict[str, Any]) -> Optional["Track"]:
    vid = item.get("videoId")
    if not vid:
        return None
    dur_raw = item.get("duration") or item.get("length") or item.get("duration_seconds")
    dur_str, dur_sec = parse_duration(dur_raw)
    return Track(
        id=str(vid),
        title=str(item.get("title") or "Unknown Track"),
        artist=extract_artists_str(item.get("artists")),
        album=_extract_album_name(item),
        album_id=_extract_album_id(item),
        artist_id=_extract_artist_id(item),
        duration=dur_str,
        duration_seconds=dur_sec,
        thumbnail=_extract_thumbnail_url(item),
        raw=item,
    )


def _parse_playlist(
    item: Dict[str, Any],
    default_author: str = "YouTube Music",
    default_count: str = "",
) -> Optional["Playlist"]:
    pid = item.get("browseId") or item.get("playlistId")
    if not pid:
        return None
    author_raw = item.get("author")
    author_name = default_author
    if isinstance(author_raw, dict):
        author_name = str(author_raw.get("name") or default_author)
    elif isinstance(author_raw, str) and author_raw:
        author_name = author_raw

    return Playlist(
        id=str(pid),
        title=str(item.get("title") or "Playlist"),
        author=author_name,
        track_count=str(item.get("itemCount") or default_count),
        description=str(item.get("description") or ""),
        thumbnail=_extract_thumbnail_url(item),
        raw=item,
    )


@dataclass
class Track:
    id: str
    title: str
    artist: str
    album: str = ""
    album_id: str = ""
    artist_id: str = ""
    duration: str = ""
    duration_seconds: int = 0
    thumbnail: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_duration(self) -> str:
        return self.duration or format_seconds(self.duration_seconds)

    def to_row(self, index: int) -> Tuple[str, str, str, str, str]:
        """Return formatted tuple for DataTable display."""
        return (
            str(index),
            self.title,
            self.artist,
            self.album or "Single",
            self.display_duration,
        )


@dataclass
class Playlist:
    id: str
    title: str
    author: str = ""
    track_count: str = ""
    description: str = ""
    thumbnail: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Album:
    id: str
    title: str
    artist: str = ""
    year: str = ""
    track_count: str = ""
    thumbnail: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Artist:
    id: str
    name: str
    subscribers: str = ""
    thumbnail: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Category:
    title: str
    params: str
    category_type: str = "Mood"


class YTMusicAPI:
    """Guest-mode YouTube Music API client with helper parsing methods."""

    def __init__(self) -> None:
        # Guest mode - zero auth required.
        self._yt_local = threading.local()
        self.yt = YTMusic()
        self.resolver = StreamResolver()

    def _client(self) -> YTMusic:
        """Return a thread-local YTMusic instance when called from worker threads."""
        if not isinstance(self.yt, YTMusic):
            return self.yt
        if threading.current_thread() is threading.main_thread():
            return self.yt
        client = getattr(self._yt_local, "yt", None)
        if client is None:
            client = YTMusic()
            self._yt_local.yt = client
        return client

    def get_trending_songs(self, limit: int = 40) -> List[Track]:
        """
        Fetch real-time trending songs from YouTube Music Explore & Charts.
        """
        tracks: List[Track] = []
        seen: set[str] = set()
        yt = self._client()
        errors: List[Exception] = []

        try:
            explore = yt.get_explore()
            trending_raw = explore.get("trending", {})

            pl_id = trending_raw.get("playlist") if isinstance(trending_raw, dict) else None
            if pl_id:
                try:
                    _, pl_tracks = self.get_playlist(pl_id, limit=limit)
                    for tr in pl_tracks:
                        if tr.id not in seen and len(tracks) < limit:
                            seen.add(tr.id)
                            tracks.append(tr)
                except Exception as exc:
                    errors.append(exc)

            items = trending_raw.get("items", []) if isinstance(trending_raw, dict) else trending_raw
            if isinstance(items, list):
                for item in items:
                    if len(tracks) >= limit:
                        break
                    tr = _parse_track(item)
                    if tr and tr.id not in seen:
                        seen.add(tr.id)
                        tracks.append(tr)
        except Exception as exc:
            errors.append(exc)

        if len(tracks) < 10:
            try:
                charts = yt.get_charts()
                playlists_raw = charts.get("videos", [])
                fallback_id = (
                    playlists_raw[0].get("playlistId")
                    if playlists_raw
                    else "PL4fGSI1pDJn6t3TXLGiiJdD-sZbrG3tG0"
                )
                _, fallback_tracks = self.get_playlist(fallback_id, limit=limit)
                for tr in fallback_tracks:
                    if tr.id not in seen and len(tracks) < limit:
                        seen.add(tr.id)
                        tracks.append(tr)
            except Exception as exc:
                errors.append(exc)

        if not tracks and errors:
            raise errors[0]

        return tracks[:limit]

    def _get_home_feed(self) -> Tuple[List[Track], List[Playlist]]:
        """Parse personalized tracks & playlists from YouTube Music's Home shelves.

        Mirrors quick-picks rules: skips stale Listen Again and History shelves,
        prioritizes Quick Picks / Mixed shelves, and filters out podcast/episode shelves.
        """
        home_tracks: List[Track] = []
        quick_tracks: List[Track] = []
        home_playlists: List[Playlist] = []
        seen_t: set[str] = set()
        seen_p: set[str] = set()

        try:
            shelves = self._client().get_home(limit=5) or []
        except Exception:
            return [], []

        for shelf in shelves:
            if not isinstance(shelf, dict):
                continue
            title = str(shelf.get("title") or "").lower()
            if any(skip in title for skip in ("recent", "history", "listen again", "podcast", "episode")):
                continue
            is_priority = any(k in title for k in ("quick", "pick", "mix", "recommend", "discover"))
            for item in shelf.get("contents") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("videoId"):
                    tr = _parse_track(item)
                    if tr and tr.id not in seen_t:
                        seen_t.add(tr.id)
                        if is_priority:
                            quick_tracks.append(tr)
                        else:
                            home_tracks.append(tr)
                elif item.get("playlistId") or (
                    isinstance(item.get("browseId"), str)
                    and item["browseId"].startswith(("VL", "PL", "RD", "OLAK"))
                ):
                    pl = _parse_playlist(item, default_author="Personalized Mix")
                    if pl and pl.id not in seen_p:
                        seen_p.add(pl.id)
                        home_playlists.append(pl)

        return quick_tracks + home_tracks, home_playlists

    def get_recommendations(
        self,
        favourites: Optional[List[Track]] = None,
        history: Optional[List[Track]] = None,
        limit: int = 50,
        trending_seed: Optional[List[Track]] = None,
        recent_history: Optional[List[Track]] = None,
        stats_top_artists: Optional[List[Any]] = None,
        stats_top_tracks: Optional[List[Track]] = None,
        stats_track_entries: Optional[List[Any]] = None,
        all_time_artists: Optional[List[Any]] = None,
    ) -> List[Track]:
        """Generate tailored, listening-history-weighted & category-coherent picks.

        Combines:
        1. Watch radios + ``You might also like`` related feeds from completion-weighted
           listening history seeds (skipping early-skipped tracks).
        2. Top-taste & similar ("Fans might also like") artist tracks for fresh discovery.
        3. Category/language/vibe playlists matching the listener's dominant history profile.
        4. YouTube Music Home Quick Picks / time-of-day mood tracks (when sparse or cold-start).
        """
        from recommendations import (
            ARTIST_POOL,
            build_taste_profile,
            extend_station,
            interleave_buckets,
            moods_for_hour,
            rank_recommendations,
            weighted_sample,
        )

        profile = build_taste_profile(
            favourites=favourites or [],
            recent_history=recent_history or [],
            session_history=history or [],
            stats_top_artists=stats_top_artists or [],
            stats_top_tracks=stats_top_tracks or [],
            stats_track_entries=stats_track_entries or [],
            all_time_artists=all_time_artists or [],
        )

        radio_bucket: List[Track] = []
        related_bucket: List[Track] = []
        artist_bucket: List[Track] = []
        category_bucket: List[Track] = []
        home_bucket: List[Track] = []
        mood_bucket: List[Track] = []

        seed_count = 4 if profile.has_history else 2
        sampled_seeds = (
            weighted_sample(profile.seed_tracks[:15], seed_count)
            if profile.seed_tracks
            else []
        )
        if not sampled_seeds and trending_seed:
            sampled_seeds = random.sample(trending_seed[:15], min(len(trending_seed[:15]), 2))

        artist_count = 3 if profile.has_history else 2
        sampled_artists = (
            weighted_sample(profile.artists[:ARTIST_POOL], artist_count)
            if profile.artists
            else []
        )
        hour_moods = moods_for_hour()

        def _fetch_seed_radio_and_related(seed: Track) -> Tuple[List[Track], List[Track]]:
            rad: List[Track] = []
            rel: List[Track] = []
            try:
                rad = self.get_radio(seed.id, limit=22)
            except Exception:
                pass
            try:
                _, rel_tracks, _, _ = self.get_song_related_bundle(seed.id, include_watch=False)
                rel = rel_tracks
            except Exception:
                pass
            return rad, rel

        def _fetch_artist_discovery(artist_entry: Any) -> List[Track]:
            aid = artist_entry.artist_id
            if not aid:
                try:
                    found = self.search_artists(artist_entry.name, limit=1)
                    if found:
                        aid = found[0].id
                except Exception:
                    return []
            if not aid:
                return []
            try:
                _, top_songs, _ = self.get_artist(aid)
                raw = self._client().get_artist(aid)
                related_tracks: List[Track] = []
                rel = raw.get("related", {}) if isinstance(raw, dict) else {}
                for r in (rel.get("results") or [])[:3]:
                    rid = str(r.get("browseId") or "")
                    if rid.startswith("UC"):
                        try:
                            _, r_songs, _ = self.get_artist(rid)
                            related_tracks.extend(r_songs[:6])
                        except Exception:
                            pass
                return [t for t in (related_tracks + top_songs) if t.id not in profile.heard_ids]
            except Exception:
                return []

        def _fetch_history_category_tracks() -> List[Track]:
            if not profile.has_history:
                return []
            lang = profile.dominant_language
            styles = profile.dominant_styles
            hints: List[str] = []
            if lang and lang != "english":
                hints.append(lang)
            hints.extend(styles[:2])
            if not hints and profile.artists:
                hints.append(profile.artists[0].name)
            query = " ".join(hints[:2]).strip()
            if not query:
                return []
            try:
                pls = self.search_playlists(f"{query} mix", limit=4)
                if not pls:
                    return []
                pick = random.choice(pls[:min(len(pls), 3)])
                _, trs = self.get_playlist(pick.id, limit=20)
                return trs
            except Exception:
                return []

        def _fetch_mood_sample() -> List[Track]:
            if not hour_moods:
                return []
            _, params, _ = hour_moods[0]
            try:
                pls = self.get_mood_playlists(params)
                if not pls:
                    return []
                pick = random.choice(pls[:min(len(pls), 5)])
                _, trs = self.get_playlist(pick.id, limit=20)
                return trs
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=8) as pool:
            f_home = pool.submit(self._get_home_feed)
            f_radios = [pool.submit(_fetch_seed_radio_and_related, s) for s in sampled_seeds]
            f_artists = [pool.submit(_fetch_artist_discovery, a) for a in sampled_artists]
            f_cat = pool.submit(_fetch_history_category_tracks)
            f_mood = pool.submit(_fetch_mood_sample)

            try:
                home_tracks, _ = f_home.result(timeout=8)
                home_bucket.extend(home_tracks)
            except Exception:
                pass
            for f in f_radios:
                try:
                    rad, rel = f.result(timeout=8)
                    radio_bucket.extend(rad)
                    related_bucket.extend(rel)
                except Exception:
                    pass
            for f in f_artists:
                try:
                    artist_bucket.extend(f.result(timeout=8))
                except Exception:
                    pass
            try:
                category_bucket.extend(f_cat.result(timeout=8))
            except Exception:
                pass
            try:
                mood_bucket.extend(f_mood.result(timeout=8))
            except Exception:
                pass

        for bucket in (radio_bucket, related_bucket, artist_bucket, category_bucket, mood_bucket):
            if len(bucket) > 2:
                random.shuffle(bucket)

        if profile.has_history:
            # History-first ordering: related/artist/radio/category dominate; home/mood only supplement
            buckets = [related_bucket, artist_bucket, radio_bucket, category_bucket, home_bucket, mood_bucket]
        else:
            buckets = [home_bucket, mood_bucket, radio_bucket, artist_bucket]

        candidates = interleave_buckets(buckets, limit=max(limit * 2, 60))
        recommended_tracks = rank_recommendations(candidates, profile, limit=limit)

        if len(recommended_tracks) < limit:
            trending_tracks = trending_seed if trending_seed is not None else self.get_trending_songs(limit=25)
            fresh_trending = [t for t in trending_tracks if t.id not in profile.heard_ids]
            recommended_tracks.extend(
                extend_station(
                    recommended_tracks,
                    fresh_trending or trending_tracks,
                    limit=limit - len(recommended_tracks),
                )
            )

        return recommended_tracks[:limit]

    def get_recommended_playlists(
        self,
        favourites: Optional[List[Track]] = None,
        recent_history: Optional[List[Track]] = None,
        history: Optional[List[Track]] = None,
        stats_top_artists: Optional[List[Any]] = None,
        limit: int = 30,
    ) -> List[Playlist]:
        """Curate mixes: Home mixes, artist playlists, time-of-day moods & charts."""
        from recommendations import (
            ARTIST_POOL,
            build_taste_profile,
            moods_for_hour,
            rank_mix_playlists,
            weighted_sample,
        )

        profile = build_taste_profile(
            favourites=favourites or [],
            recent_history=recent_history or [],
            session_history=history or [],
            stats_top_artists=stats_top_artists or [],
        )
        sampled_artists = weighted_sample(profile.artists[:ARTIST_POOL], 2) if profile.artists else []
        hour_moods = moods_for_hour()

        home_playlists: List[Playlist] = []
        artist_playlists: List[Playlist] = []
        mood_playlists: List[Playlist] = []
        chart_playlists: List[Playlist] = []

        def _artist_mixes(name: str) -> List[Playlist]:
            try:
                pls = self.search_playlists(f"{name} mix", limit=4)
                for p in pls:
                    if not p.author or p.author == "Playlist":
                        p.author = f"Mix · {name}"
                return pls
            except Exception:
                return []

        def _mood_mixes(label: str, params: str) -> List[Playlist]:
            try:
                pls = self.get_mood_playlists(params)[:4]
                for p in pls:
                    p.author = f"Mood · {label}"
                return pls
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=6) as pool:
            f_home = pool.submit(self._get_home_feed)
            f_art = [pool.submit(_artist_mixes, a.name) for a in sampled_artists]
            f_mood = [pool.submit(_mood_mixes, name, params) for name, params, _ in hour_moods[:2]]
            f_charts = pool.submit(self.get_charts, "US", False)

            try:
                _, home_pls = f_home.result(timeout=8)
                home_playlists.extend(home_pls)
            except Exception:
                pass
            for f in f_art:
                try:
                    artist_playlists.extend(f.result(timeout=8))
                except Exception:
                    pass
            for f in f_mood:
                try:
                    mood_playlists.extend(f.result(timeout=8))
                except Exception:
                    pass
            try:
                _, c_pls = f_charts.result(timeout=8)
                chart_playlists.extend(c_pls)
            except Exception:
                pass

        return rank_mix_playlists(
            personalized=home_playlists + artist_playlists,
            mood_playlists=mood_playlists,
            charts=chart_playlists,
            limit=limit,
        )

    def get_charts(
        self, country: str = "US", include_tracks: bool = True
    ) -> Tuple[List[Track], List[Playlist]]:
        """
        Fetch chart playlists (and optionally trending tracks).
        Returns: (top_tracks, chart_playlists)
        """
        chart_playlists: List[Playlist] = []
        yt = self._client()

        try:
            charts = yt.get_charts(country=country)
        except Exception:
            charts = yt.get_charts()

        playlists_raw = charts.get("videos", []) + charts.get("genres", [])
        for p in playlists_raw:
            pl = _parse_playlist(p, default_author="YouTube Charts", default_count="Chart")
            if pl is not None:
                chart_playlists.append(pl)

        top_tracks = self.get_trending_songs(limit=50) if include_tracks else []
        return top_tracks, chart_playlists

    def get_mood_categories(self) -> Dict[str, List[Category]]:
        """Fetch mood and genre categories with their exploration tokens."""
        categories: Dict[str, List[Category]] = {}
        raw = self._client().get_mood_categories()

        for group_name, items in raw.items():
            cat_list: List[Category] = []
            for item in items:
                title = item.get("title", "")
                params = item.get("params", "")
                if title and params:
                    cat_list.append(Category(title=title, params=params, category_type=group_name))
            categories[group_name] = cat_list

        return categories

    def get_mood_playlists(self, params: str) -> List[Playlist]:
        """Fetch curated playlists for a specific mood/genre params token."""
        playlists: List[Playlist] = []
        for p in self._client().get_mood_playlists(params):
            pl = _parse_playlist(p, default_author="Curated")
            if pl is not None:
                playlists.append(pl)
        return playlists

    def get_playlist(self, playlist_id: str, limit: int = 100) -> Tuple[Playlist, List[Track]]:
        """Fetch playlist metadata and its track list."""
        clean_id = playlist_id[2:] if playlist_id.startswith("VL") else playlist_id
        data = self._client().get_playlist(clean_id, limit=limit)

        author_raw = data.get("author")
        author_name = "YouTube Music"
        if isinstance(author_raw, dict):
            author_name = str(author_raw.get("name") or "YouTube Music")
        elif isinstance(author_raw, str) and author_raw:
            author_name = author_raw

        playlist = Playlist(
            id=str(data.get("id") or clean_id),
            title=str(data.get("title") or "Playlist"),
            author=author_name,
            track_count=str(data.get("trackCount") or len(data.get("tracks", []))),
            description=str(data.get("description") or ""),
            thumbnail=_extract_thumbnail_url(data),
            raw=data,
        )

        tracks: List[Track] = []
        for t in data.get("tracks", []):
            tr = _parse_track(t)
            if tr is not None:
                tracks.append(tr)

        return playlist, tracks

    def search_songs(self, query: str, limit: int = 30) -> List[Track]:
        """Search for songs matching query."""
        results = self._client().search(query, filter="songs", limit=limit)
        tracks: List[Track] = []
        for s in results:
            tr = _parse_track(s)
            if tr is not None:
                tracks.append(tr)
        return tracks

    def search_playlists(self, query: str, limit: int = 20) -> List[Playlist]:
        """Search for public playlists matching query."""
        results = self._client().search(query, filter="playlists", limit=limit)
        playlists: List[Playlist] = []
        for p in results:
            pl = _parse_playlist(p, default_author="Community")
            if pl is not None:
                playlists.append(pl)
        return playlists

    def search_albums(self, query: str, limit: int = 20) -> List[Album]:
        """Search for albums matching query."""
        results = self._client().search(query, filter="albums", limit=limit)
        albums: List[Album] = []
        for a in results:
            bid = a.get("browseId") or a.get("playlistId")
            if not bid:
                continue
            albums.append(
                Album(
                    id=str(bid),
                    title=str(a.get("title") or "Album"),
                    artist=extract_artists_str(a.get("artists")),
                    year=str(a.get("year") or ""),
                    thumbnail=_extract_thumbnail_url(a),
                    raw=a,
                )
            )
        return albums

    def search_artists(self, query: str, limit: int = 20) -> List[Artist]:
        """Search for artists matching query."""
        results = self._client().search(query, filter="artists", limit=limit)
        artists: List[Artist] = []
        for a in results:
            cid = a.get("browseId") or a.get("channelId")
            if not cid:
                continue
            name = str(a.get("artist") or a.get("title") or a.get("name") or "Unknown Artist")
            artists.append(
                Artist(
                    id=str(cid),
                    name=name,
                    subscribers=str(a.get("subscribers") or ""),
                    thumbnail=_extract_thumbnail_url(a),
                    raw=a,
                )
            )
        return artists

    def get_album(self, browse_id: str) -> Tuple[Album, List[Track]]:
        """Fetch album metadata and tracks."""
        data = self._client().get_album(browse_id)
        album_title = str(data.get("title") or "Album")
        artist_name = extract_artists_str(data.get("artists"))
        thumb = _extract_thumbnail_url(data)
        year = str(data.get("year") or "")
        tracks: List[Track] = []
        for item in data.get("tracks", []):
            tr = _parse_track(item)
            if tr is not None:
                if not tr.album:
                    tr.album = album_title
                if not tr.album_id:
                    tr.album_id = browse_id
                if not tr.thumbnail:
                    tr.thumbnail = thumb
                tracks.append(tr)
        album = Album(
            id=browse_id,
            title=album_title,
            artist=artist_name,
            year=year,
            track_count=str(data.get("trackCount") or len(tracks)),
            thumbnail=thumb,
            raw=data,
        )
        return album, tracks

    def get_artist(self, channel_id: str) -> Tuple[Artist, List[Track], List[Album]]:
        """Fetch artist profile, top songs, and albums."""
        data = self._client().get_artist(channel_id)
        name = str(data.get("name") or "Artist")
        thumb = _extract_thumbnail_url(data)
        artist = Artist(
            id=channel_id,
            name=name,
            subscribers=str(data.get("subscribers") or ""),
            thumbnail=thumb,
            raw=data,
        )

        songs: List[Track] = []
        songs_sec = data.get("songs") or {}
        for item in songs_sec.get("results", []):
            tr = _parse_track(item)
            if tr is not None:
                if not tr.artist_id:
                    tr.artist_id = channel_id
                songs.append(tr)

        albums: List[Album] = []
        for key in ("albums", "singles"):
            sec = data.get(key) or {}
            for a in sec.get("results", []):
                bid = a.get("browseId")
                if not bid:
                    continue
                albums.append(
                    Album(
                        id=str(bid),
                        title=str(a.get("title") or "Release"),
                        artist=name,
                        year=str(a.get("year") or ""),
                        thumbnail=_extract_thumbnail_url(a),
                        raw=a,
                    )
                )
        return artist, songs, albums

    def get_ytm_lyrics(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Fetch timed or plain lyrics directly from YouTube Music."""
        try:
            wp = self._client().get_watch_playlist(videoId=video_id, limit=5)
            lyrics_id = wp.get("lyrics")
            if not lyrics_id:
                return None
            return self._client().get_lyrics(str(lyrics_id), timestamps=True)
        except Exception:
            return None

    def get_explore_sections(self) -> Dict[str, Any]:
        """Fetch Explore data: new releases, trending, moods & genres."""
        yt = self._client()
        explore = yt.get_explore()

        new_releases: List[Album] = []
        for item in explore.get("new_releases", []):
            bid = item.get("browseId")
            if not bid:
                continue
            new_releases.append(
                Album(
                    id=str(bid),
                    title=str(item.get("title") or "Release"),
                    artist=extract_artists_str(item.get("artists")),
                    year=str(item.get("type") or "Release"),
                    thumbnail=_extract_thumbnail_url(item),
                    raw=item,
                )
            )

        trending: List[Track] = []
        seen: set[str] = set()
        tr_raw = explore.get("trending", {})
        items = tr_raw.get("items", []) if isinstance(tr_raw, dict) else tr_raw
        if isinstance(items, list):
            for item in items:
                tr = _parse_track(item)
                if tr and tr.id not in seen:
                    seen.add(tr.id)
                    trending.append(tr)

        moods: List[Category] = []
        for m in explore.get("moods_and_genres", []):
            title = str(m.get("title") or "")
            params = str(m.get("params") or "")
            if title and params:
                moods.append(Category(title=title, params=params, category_type="Mood & Genre"))

        if not moods:
            try:
                cats = self.get_mood_categories()
                for group_list in cats.values():
                    moods.extend(group_list)
            except Exception:
                pass

        return {
            "new_releases": new_releases,
            "trending": trending,
            "moods": moods,
        }

    def get_song_credits(self, video_id: str) -> Optional[Dict[str, Any]]:
        """Fetch official YouTube Music track credits via ``MPTC{video_id}``."""
        if not video_id:
            return None
        try:
            yt = self._client()
            if hasattr(yt, "get_song_credits"):
                return yt.get_song_credits(f"MPTC{video_id}")
        except Exception:
            pass
        return None

    def get_song_related_bundle(
        self,
        video_id: str,
        watch_limit: int = 30,
        include_watch: bool = True,
    ) -> Tuple[List[Track], List[Track], List[Playlist], List[str]]:
        """Fetch watch playlist + YouTube Music's ``Related`` tab (songs, playlists, artists)."""
        watch_tracks: List[Track] = []
        related_tracks: List[Track] = []
        related_playlists: List[Playlist] = []
        similar_artist_ids: List[str] = []
        if not video_id:
            return watch_tracks, related_tracks, related_playlists, similar_artist_ids

        yt = self._client()
        try:
            wp = yt.get_watch_playlist(videoId=video_id, limit=watch_limit) or {}
        except Exception:
            return watch_tracks, related_tracks, related_playlists, similar_artist_ids

        if include_watch:
            for item in wp.get("tracks") or []:
                tr = _parse_track(item)
                if tr is not None:
                    watch_tracks.append(tr)

        related_browse_id = wp.get("related")
        if related_browse_id and hasattr(yt, "get_song_related"):
            try:
                sections = yt.get_song_related(str(related_browse_id)) or []
                for sec in sections:
                    if not isinstance(sec, dict):
                        continue
                    contents = sec.get("contents")
                    if not isinstance(contents, list):
                        continue
                    for item in contents:
                        if not isinstance(item, dict):
                            continue
                        if item.get("videoId"):
                            tr = _parse_track(item)
                            if tr is not None:
                                related_tracks.append(tr)
                        elif item.get("playlistId") or (
                            isinstance(item.get("browseId"), str)
                            and item["browseId"].startswith(("VL", "PL", "RD", "OLAK"))
                        ):
                            pl = _parse_playlist(item, default_author="Related Mix")
                            if pl is not None:
                                related_playlists.append(pl)
                        elif isinstance(item.get("browseId"), str) and item["browseId"].startswith("UC"):
                            similar_artist_ids.append(item["browseId"])
            except Exception:
                pass

        return watch_tracks, related_tracks, related_playlists, similar_artist_ids

    def get_category_queue(
        self,
        seed_track: Track,
        limit: int = 30,
        taste_profile: Optional[Any] = None,
    ) -> List[Track]:
        """Build a queue strictly matching ``seed_track``'s category, language, and vibe."""
        from recommendations import build_category_queue, infer_track_categories, is_likely_video

        watch_tracks, related_tracks, related_pls, sim_artists = self.get_song_related_bundle(
            seed_track.id, watch_limit=30, include_watch=True
        )

        extra_tracks: List[Track] = []

        def _fetch_rel_playlist() -> List[Track]:
            if not related_pls:
                return []
            try:
                _, trs = self.get_playlist(related_pls[0].id, limit=20)
                return trs
            except Exception:
                return []

        def _fetch_similar_artists() -> List[Track]:
            out: List[Track] = []
            ids = list(sim_artists[:2])
            if not ids and seed_track.artist_id:
                ids = [seed_track.artist_id]
            for aid in ids:
                try:
                    _, songs, _ = self.get_artist(aid)
                    out.extend(songs[:8])
                except Exception:
                    pass
            return out

        def _fetch_category_fallback() -> List[Track]:
            # Only search when watch + related returned sparse results
            if len(watch_tracks) + len(related_tracks) >= 15:
                return []
            cat = infer_track_categories(seed_track)
            hint = cat.summary_query_hint()
            primary = (seed_track.artist or "").split(",")[0].strip()
            q = f"{primary} {hint}".strip() or seed_track.title
            try:
                return self.search_songs(q, limit=15)
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_pl = pool.submit(_fetch_rel_playlist)
            f_art = pool.submit(_fetch_similar_artists)
            f_fb = pool.submit(_fetch_category_fallback)
            for fut in (f_pl, f_art, f_fb):
                try:
                    extra_tracks.extend(fut.result(timeout=6))
                except Exception:
                    pass

        all_candidates = related_tracks + watch_tracks + extra_tracks
        return build_category_queue(
            seed_track,
            all_candidates,
            profile=taste_profile,
            limit=limit,
            prefer_videos=is_likely_video(seed_track),
        )

    def get_radio(
        self,
        video_id: str,
        limit: int = 25,
        existing: Optional[List[Track]] = None,
        prefer_videos: bool = False,
    ) -> List[Track]:
        """Fetch watch playlist & run recording-deduped artist-capped station filter."""
        from recommendations import extend_station

        try:
            wp = self._client().get_watch_playlist(videoId=video_id, limit=max(limit + 10, 25))
        except Exception:
            return []

        raw_tracks: List[Track] = []
        for t in wp.get("tracks", []):
            tr = _parse_track(t)
            if tr is not None:
                raw_tracks.append(tr)

        target_limit = limit if limit else len(raw_tracks)
        filtered = extend_station(
            existing=existing or [],
            candidates=raw_tracks,
            limit=target_limit,
            prefer_videos=prefer_videos,
        )
        return filtered if filtered else raw_tracks[:target_limit]
