"""
api.py - Unauthenticated YouTube Music API Layer & Stream Resolver.

Uses ytmusicapi in guest mode (YTMusic()) with zero login/auth required.
Resolves direct high-fidelity audio streams using yt-dlp with in-memory caching.
"""

import time
import subprocess
from dataclasses import dataclass, field
from typing import List, Dict, Any, Tuple
from ytmusicapi import YTMusic
import yt_dlp


def parse_duration(val: Any) -> Tuple[str, int]:
    """Parse various duration formats into (formatted_str, seconds)."""
    if val is None:
        return "--:--", 0

    if isinstance(val, (int, float)):
        seconds = int(val)
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
            elif len(parts) == 3:
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
                names.append(a["name"])
            elif isinstance(a, str):
                names.append(a)
        if names:
            return ", ".join(names)
    elif isinstance(artists, dict) and "name" in artists:
        return artists["name"]
    return "Unknown Artist"


@dataclass
class Track:
    id: str
    title: str
    artist: str
    album: str = ""
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
class Category:
    title: str
    params: str
    category_type: str = "Mood"


class StreamResolver:
    """Resolves and caches audio stream URLs using yt-dlp."""

    def __init__(self):
        self._cache: Dict[str, Tuple[str, str, float]] = {}  # video_id -> (url, quality, expire_ts)
        self._ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "extract_flat": False,
        }

    def resolve(self, video_id: str) -> Tuple[str, str]:
        """
        Resolve audio stream URL and quality info for a video_id.
        Returns: (stream_url, quality_info_str)
        """
        now = time.time()
        # Check cache (expire after 4 hours = 14400s)
        if video_id in self._cache:
            cached_url, quality, exp = self._cache[video_id]
            if now < exp:
                return cached_url, quality

        # Extract using yt_dlp python library
        try:
            with yt_dlp.YoutubeDL(self._ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                stream_url = info.get("url")
                if stream_url:
                    acodec = (info.get("acodec") or "audio").replace("none", "").strip().upper()
                    abr = info.get("abr")
                    quality = f"{acodec} {int(abr)}kbps" if abr else (acodec or "Best Audio")
                    # Cache valid for 4 hours
                    self._cache[video_id] = (stream_url, quality, now + 14400)
                    return stream_url, quality
        except Exception as e:
            # Fallback to CLI command
            try:
                cmd = ["yt-dlp", "-f", "bestaudio", "-g", video_id]
                res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
                if res.returncode == 0 and res.stdout.strip():
                    stream_url = res.stdout.strip().split("\n")[0]
                    quality = "Opus Best Audio"
                    self._cache[video_id] = (stream_url, quality, now + 14400)
                    return stream_url, quality
            except Exception:
                pass
            raise RuntimeError(f"Failed to resolve audio stream for video {video_id}: {e}")

        raise RuntimeError(f"No audio stream URL found for video {video_id}")


class YTMusicAPI:
    """Guest-mode YouTube Music API client with helper parsing methods."""

    def __init__(self):
        # Guest mode - zero auth required!
        self.yt = YTMusic()
        self.resolver = StreamResolver()

    def get_charts(self, country: str = "US") -> Tuple[List[Track], List[Playlist]]:
        """
        Fetch top trending songs and chart playlists.
        Returns: (top_tracks, chart_playlists)
        """
        top_tracks: List[Track] = []
        chart_playlists: List[Playlist] = []

        try:
            charts = self.yt.get_charts(country=country)
        except Exception:
            charts = self.yt.get_charts()

        # Collect chart playlists
        playlists_raw = charts.get("videos", []) + charts.get("genres", [])
        for p in playlists_raw:
            pid = p.get("playlistId")
            if pid:
                thumbs = p.get("thumbnails", [])
                thumb_url = thumbs[-1]["url"] if thumbs else ""
                chart_playlists.append(
                    Playlist(
                        id=pid,
                        title=p.get("title", "Chart Playlist"),
                        author="YouTube Charts",
                        track_count="Chart",
                        thumbnail=thumb_url,
                        raw=p,
                    )
                )

        # Use the first chart playlist (usually Daily Top Music Videos or Top 100) to populate tracks
        primary_playlist_id = "PL4fGSI1pDJn6t3TXLGiiJdD-sZbrG3tG0"  # Default global top
        if chart_playlists:
            primary_playlist_id = chart_playlists[0].id

        try:
            _, tracks = self.get_playlist(primary_playlist_id, limit=50)
            top_tracks = tracks
        except Exception:
            pass

        return top_tracks, chart_playlists

    def get_mood_categories(self) -> Dict[str, List[Category]]:
        """
        Fetch mood and genre categories with their exploration tokens.
        """
        categories: Dict[str, List[Category]] = {}
        raw = self.yt.get_mood_categories()

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
        """
        Fetch curated playlists for a specific mood/genre params token.
        """
        playlists: List[Playlist] = []
        raw_list = self.yt.get_mood_playlists(params)

        for p in raw_list:
            pid = p.get("playlistId")
            if pid:
                thumbs = p.get("thumbnails", [])
                thumb_url = thumbs[-1]["url"] if thumbs else ""
                playlists.append(
                    Playlist(
                        id=pid,
                        title=p.get("title", "Playlist"),
                        author=p.get("author", "Curated"),
                        track_count=p.get("itemCount", ""),
                        description=p.get("description", ""),
                        thumbnail=thumb_url,
                        raw=p,
                    )
                )

        return playlists

    def get_playlist(self, playlist_id: str, limit: int = 100) -> Tuple[Playlist, List[Track]]:
        """
        Fetch playlist metadata and its track list.
        """
        # Handle VL prefix if present
        clean_id = playlist_id
        if clean_id.startswith("VL"):
            clean_id = clean_id[2:]

        data = self.yt.get_playlist(clean_id, limit=limit)

        author_raw = data.get("author")
        author_name = "YouTube Music"
        if isinstance(author_raw, dict):
            author_name = author_raw.get("name", "YouTube Music")
        elif isinstance(author_raw, str):
            author_name = author_raw

        thumbs = data.get("thumbnails", [])
        thumb_url = thumbs[-1]["url"] if thumbs else ""

        playlist = Playlist(
            id=data.get("id") or clean_id,
            title=data.get("title", "Playlist"),
            author=author_name,
            track_count=str(data.get("trackCount") or len(data.get("tracks", []))),
            description=data.get("description", ""),
            thumbnail=thumb_url,
            raw=data,
        )

        tracks: List[Track] = []
        for t in data.get("tracks", []):
            vid = t.get("videoId")
            if not vid:
                continue

            dur_str, dur_sec = parse_duration(t.get("duration") or t.get("duration_seconds"))
            artists_str = extract_artists_str(t.get("artists"))

            album_name = ""
            album_raw = t.get("album")
            if isinstance(album_raw, dict):
                album_name = album_raw.get("name", "")
            elif isinstance(album_raw, str):
                album_name = album_raw

            t_thumbs = t.get("thumbnails", [])
            t_thumb_url = t_thumbs[-1]["url"] if t_thumbs else ""

            tracks.append(
                Track(
                    id=vid,
                    title=t.get("title", "Unknown Track"),
                    artist=artists_str,
                    album=album_name,
                    duration=dur_str,
                    duration_seconds=dur_sec,
                    thumbnail=t_thumb_url,
                    raw=t,
                )
            )

        return playlist, tracks

    def search_songs(self, query: str, limit: int = 30) -> List[Track]:
        """
        Search for songs matching query.
        """
        tracks: List[Track] = []
        results = self.yt.search(query, filter="songs", limit=limit)

        for s in results:
            vid = s.get("videoId")
            if not vid:
                continue

            dur_str, dur_sec = parse_duration(s.get("duration"))
            artists_str = extract_artists_str(s.get("artists"))

            album_name = ""
            album_raw = s.get("album")
            if isinstance(album_raw, dict):
                album_name = album_raw.get("name", "")
            elif isinstance(album_raw, str):
                album_name = album_raw

            thumbs = s.get("thumbnails", [])
            thumb_url = thumbs[-1]["url"] if thumbs else ""

            tracks.append(
                Track(
                    id=vid,
                    title=s.get("title", "Unknown Track"),
                    artist=artists_str,
                    album=album_name,
                    duration=dur_str,
                    duration_seconds=dur_sec,
                    thumbnail=thumb_url,
                    raw=s,
                )
            )

        return tracks

    def search_playlists(self, query: str, limit: int = 20) -> List[Playlist]:
        """
        Search for public playlists matching query.
        """
        playlists: List[Playlist] = []
        results = self.yt.search(query, filter="playlists", limit=limit)

        for p in results:
            pid = p.get("browseId") or p.get("playlistId")
            if not pid:
                continue

            author_raw = p.get("author")
            author_name = "Community"
            if isinstance(author_raw, dict):
                author_name = author_raw.get("name", "Community")
            elif isinstance(author_raw, str):
                author_name = author_raw

            thumbs = p.get("thumbnails", [])
            thumb_url = thumbs[-1]["url"] if thumbs else ""

            playlists.append(
                Playlist(
                    id=pid,
                    title=p.get("title", "Playlist"),
                    author=author_name,
                    track_count=str(p.get("itemCount", "")),
                    description=p.get("description", ""),
                    thumbnail=thumb_url,
                    raw=p,
                )
            )

        return playlists

    def get_radio(self, video_id: str, limit: int = 25) -> List[Track]:
        """
        Fetch algorithmic recommendations / watch playlist based on a track's video_id.
        """
        tracks: List[Track] = []
        wp = self.yt.get_watch_playlist(videoId=video_id, limit=limit)

        for t in wp.get("tracks", []):
            vid = t.get("videoId")
            if not vid:
                continue

            dur_str, dur_sec = parse_duration(t.get("length") or t.get("duration"))
            artists_str = extract_artists_str(t.get("artists"))

            album_name = ""
            album_raw = t.get("album")
            if isinstance(album_raw, dict):
                album_name = album_raw.get("name", "")
            elif isinstance(album_raw, str):
                album_name = album_raw

            thumbs = t.get("thumbnail", [])
            thumb_url = thumbs[-1]["url"] if thumbs else ""

            tracks.append(
                Track(
                    id=vid,
                    title=t.get("title", "Unknown Track"),
                    artist=artists_str,
                    album=album_name,
                    duration=dur_str,
                    duration_seconds=dur_sec,
                    thumbnail=thumb_url,
                    raw=t,
                )
            )

        return tracks
