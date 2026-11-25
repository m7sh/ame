"""
settings.py - Persistent user settings and Recently Played history for ame.

Persistent user preferences & recently played history:
- ``~/.local/share/ame/settings.json``: playback, appearance, and integration prefs
- ``~/.local/share/ame/recent.json``: last 50 played tracks across sessions
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

from api import Track
from favourites import _track_from_dict, _track_to_dict


def _default_settings_path() -> Path:
    return Path.home() / ".local" / "share" / "ame" / "settings.json"


def _default_recent_path() -> Path:
    return Path.home() / ".local" / "share" / "ame" / "recent.json"


@dataclass
class AppSettings:
    autoplay: bool = True
    crossfade_seconds: int = 0
    loudness_normalization: bool = True
    speed: float = 1.0
    show_nerd_stats: bool = True
    dynamic_art_tint: bool = True
    show_sidebar: bool = True
    visualizer_enabled: bool = True
    discord_rpc: bool = True
    mpris_enabled: bool = True


class AppSettingsStore:
    """Thread-safe JSON store for user preferences."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else _default_settings_path()
        self._lock = threading.Lock()
        self.data = AppSettings()
        self.load()

    def load(self) -> AppSettings:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = AppSettings(
                        autoplay=bool(raw.get("autoplay", True)),
                        crossfade_seconds=int(raw.get("crossfade_seconds", 0) or 0),
                        loudness_normalization=bool(raw.get("loudness_normalization", True)),
                        speed=max(0.5, min(2.5, float(raw.get("speed", 1.0) or 1.0))),
                        show_nerd_stats=bool(raw.get("show_nerd_stats", True)),
                        dynamic_art_tint=bool(raw.get("dynamic_art_tint", True)),
                        show_sidebar=bool(raw.get("show_sidebar", True)),
                        visualizer_enabled=bool(raw.get("visualizer_enabled", True)),
                        discord_rpc=bool(raw.get("discord_rpc", True)),
                        mpris_enabled=bool(raw.get("mpris_enabled", True)),
                    )
            except Exception:
                self.data = AppSettings()
            return self.data

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(asdict(self.data), indent=2), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass


class RecentHistoryStore:
    """Persists Recently Played tracks (newest first, deduplicated by track id)."""

    MAX_ITEMS = 50

    def __init__(self, path: Optional[Path] = None, max_items: int = MAX_ITEMS) -> None:
        self.path = Path(path) if path else _default_recent_path()
        self.max_items = max(1, int(max_items))
        self._lock = threading.Lock()
        self._tracks: List[Track] = []
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                items = raw.get("tracks", []) if isinstance(raw, dict) else raw
                loaded: List[Track] = []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and item.get("id"):
                            loaded.append(_track_from_dict(item))
                self._tracks = loaded[: self.max_items]
            except Exception:
                self._tracks = []

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = {"version": 1, "tracks": [_track_to_dict(t) for t in self._tracks]}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def all(self) -> List[Track]:
        with self._lock:
            return list(self._tracks)

    def record(self, track: Track) -> None:
        with self._lock:
            self._tracks = [t for t in self._tracks if t.id != track.id]
            self._tracks.insert(0, track)
            self._tracks = self._tracks[: self.max_items]
            self.save()
