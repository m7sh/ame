"""
favourites.py - Persistent store for liked songs.

Favourites are kept as a small JSON file under the user's data directory so
they survive restarts. Only the fields needed to rebuild a Track are stored;
the heavyweight ``raw`` payload from ytmusicapi is dropped.
"""

import json
import threading
from pathlib import Path
from typing import List, Optional

from api import Track


def _default_path() -> Path:
    return Path.home() / ".local" / "share" / "ame" / "favourites.json"


class FavouritesStore:
    """Thread-safe, JSON-backed set of favourite tracks (ordered, newest last)."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else _default_path()
        self._lock = threading.Lock()
        self._tracks: List[Track] = []
        self.load()

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            self._tracks = []
            return

        if isinstance(data, dict):
            data = data.get("tracks", [])

        tracks: List[Track] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("id"):
                    tracks.append(_track_from_dict(item))
        self._tracks = tracks

    def save(self) -> None:
        payload = {
            "version": 1,
            "tracks": [_track_to_dict(t) for t in self._tracks],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def all(self) -> List[Track]:
        with self._lock:
            return list(self._tracks)

    def count(self) -> int:
        with self._lock:
            return len(self._tracks)

    def contains(self, track_id: str) -> bool:
        with self._lock:
            return any(t.id == track_id for t in self._tracks)

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def add(self, track: Track) -> bool:
        """Add a track. Returns False if it was already a favourite."""
        with self._lock:
            if any(t.id == track.id for t in self._tracks):
                return False
            self._tracks.append(track)
            self.save()
        return True

    def remove(self, track_id: str) -> bool:
        """Remove a track by id. Returns True if something was removed."""
        with self._lock:
            before = len(self._tracks)
            self._tracks = [t for t in self._tracks if t.id != track_id]
            if len(self._tracks) == before:
                return False
            self.save()
        return True

    def toggle(self, track: Track) -> bool:
        """Add or remove a track. Returns True if it is now a favourite."""
        if self.remove(track.id):
            return False
        self.add(track)
        return True


def _track_to_dict(track: Track) -> dict:
    return {
        "id": track.id,
        "title": track.title,
        "artist": track.artist,
        "album": track.album,
        "duration": track.duration,
        "duration_seconds": track.duration_seconds,
        "thumbnail": track.thumbnail,
    }


def _track_from_dict(data: dict) -> Track:
    return Track(
        id=str(data.get("id", "")),
        title=str(data.get("title", "")),
        artist=str(data.get("artist", "")),
        album=str(data.get("album", "")),
        duration=str(data.get("duration", "")),
        duration_seconds=int(data.get("duration_seconds", 0) or 0),
        thumbnail=str(data.get("thumbnail", "")),
    )
