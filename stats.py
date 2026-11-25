"""
stats.py - Local-only monthly listening stats (Replay summary).

Aggregates listening duration, play counts, top tracks, top artists, daily totals,
and 24-hour listening distribution into monthly JSON files under
``~/.local/share/ame/stats/YYYY-MM.json``. Nothing ever leaves the machine.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from api import Track


def _default_stats_dir() -> Path:
    return Path.home() / ".local" / "share" / "ame" / "stats"


@dataclass
class TrackStat:
    track: Track
    play_count: int = 0
    listen_seconds: float = 0.0


@dataclass
class ArtistStat:
    name: str
    play_count: int = 0
    listen_seconds: float = 0.0


@dataclass
class StatsSummary:
    period: str
    total_seconds: float = 0.0
    total_plays: int = 0
    unique_tracks: int = 0
    unique_artists: int = 0
    top_tracks: List[TrackStat] = field(default_factory=list)
    top_artists: List[ArtistStat] = field(default_factory=list)
    hourly_seconds: List[float] = field(default_factory=lambda: [0.0] * 24)


class ListeningStatsStore:
    """Thread-safe monthly-bucketed listening analytics store."""

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = Path(directory) if directory else _default_stats_dir()
        self._lock = threading.Lock()
        self._buckets: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _empty_bucket() -> Dict[str, Any]:
        return {
            "version": 1,
            "tracks": {},
            "artists": {},
            "days": {},
            "hours": [0.0] * 24,
            "total_seconds": 0.0,
            "total_plays": 0,
        }

    def _month_path(self, ym: str) -> Path:
        return self.directory / f"{ym}.json"

    def _load_bucket(self, ym: str) -> Dict[str, Any]:
        if ym in self._buckets:
            return self._buckets[ym]
        path = self._month_path(ym)
        data: Optional[Dict[str, Any]] = None
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                pass
        if data is None:
            data = self._empty_bucket()
        else:
            data.setdefault("tracks", {})
            data.setdefault("artists", {})
            data.setdefault("days", {})
            hours = data.get("hours")
            if not isinstance(hours, list) or len(hours) != 24:
                data["hours"] = [0.0] * 24
            data.setdefault("total_seconds", 0.0)
            data.setdefault("total_plays", 0)
        self._buckets[ym] = data
        return data

    def _save_bucket(self, ym: str, bucket: Dict[str, Any]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._month_path(ym)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(bucket, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    def record_play(self, track: Track, when: Optional[datetime] = None) -> None:
        """Increment play count for a newly started track."""
        now = when or datetime.now()
        ym = now.strftime("%Y-%m")
        day_key = now.strftime("%Y-%m-%d")

        with self._lock:
            b = self._load_bucket(ym)
            b["total_plays"] = int(b.get("total_plays", 0)) + 1

            tracks = b["tracks"]
            t_entry = tracks.get(track.id)
            if not isinstance(t_entry, dict):
                t_entry = {
                    "id": track.id,
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "album_id": track.album_id,
                    "artist_id": track.artist_id,
                    "duration": track.duration,
                    "duration_seconds": track.duration_seconds,
                    "thumbnail": track.thumbnail,
                    "plays": 0,
                    "seconds": 0.0,
                }
                tracks[track.id] = t_entry
            t_entry["plays"] = int(t_entry.get("plays", 0)) + 1

            artist_name = (track.artist.split(",")[0] if track.artist else "Unknown Artist").strip() or "Unknown Artist"
            artists = b["artists"]
            a_entry = artists.get(artist_name) or {"plays": 0, "seconds": 0.0}
            a_entry["plays"] = int(a_entry.get("plays", 0)) + 1
            artists[artist_name] = a_entry

            days = b["days"]
            d_entry = days.get(day_key) or {"plays": 0, "seconds": 0.0}
            d_entry["plays"] = int(d_entry.get("plays", 0)) + 1
            days[day_key] = d_entry

            self._save_bucket(ym, b)

    def record_listening(self, track: Track, delta_seconds: float, when: Optional[datetime] = None) -> None:
        """Accumulate active listening seconds for ``track``."""
        if delta_seconds <= 0:
            return
        now = when or datetime.now()
        ym = now.strftime("%Y-%m")
        day_key = now.strftime("%Y-%m-%d")
        hour = now.hour

        with self._lock:
            b = self._load_bucket(ym)
            b["total_seconds"] = float(b.get("total_seconds", 0.0)) + delta_seconds

            tracks = b["tracks"]
            t_entry = tracks.get(track.id)
            if not isinstance(t_entry, dict):
                t_entry = {
                    "id": track.id,
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "album_id": track.album_id,
                    "artist_id": track.artist_id,
                    "duration": track.duration,
                    "duration_seconds": track.duration_seconds,
                    "thumbnail": track.thumbnail,
                    "plays": 0,
                    "seconds": 0.0,
                }
                tracks[track.id] = t_entry
            t_entry["seconds"] = float(t_entry.get("seconds", 0.0)) + delta_seconds

            artist_name = (track.artist.split(",")[0] if track.artist else "Unknown Artist").strip() or "Unknown Artist"
            artists = b["artists"]
            a_entry = artists.get(artist_name) or {"plays": 0, "seconds": 0.0}
            a_entry["seconds"] = float(a_entry.get("seconds", 0.0)) + delta_seconds
            artists[artist_name] = a_entry

            days = b["days"]
            d_entry = days.get(day_key) or {"plays": 0, "seconds": 0.0}
            d_entry["seconds"] = float(d_entry.get("seconds", 0.0)) + delta_seconds
            days[day_key] = d_entry

            b["hours"][hour] = float(b["hours"][hour]) + delta_seconds
            self._save_bucket(ym, b)

    def summary(self, period: str = "month", today: Optional[date] = None) -> StatsSummary:
        """Aggregate stats for 'today', 'month', or 'all'."""
        ref = today or date.today()
        cur_ym = ref.strftime("%Y-%m")
        cur_day = ref.strftime("%Y-%m-%d")

        with self._lock:
            if period == "all":
                month_keys: List[str] = []
                if self.directory.exists():
                    for p in sorted(self.directory.glob("????-??.json")):
                        month_keys.append(p.stem)
                if cur_ym not in month_keys:
                    month_keys.append(cur_ym)
            else:
                month_keys = [cur_ym]

            buckets = [self._load_bucket(ym) for ym in month_keys]

            if period == "today":
                # Filter today's stats from the current month's day summary + hours
                cur_b = buckets[0] if buckets else self._empty_bucket()
                day_info = cur_b.get("days", {}).get(cur_day, {})
                total_sec = float(day_info.get("seconds", 0.0))
                total_plays = int(day_info.get("plays", 0))
                # Today uses current month's track/artist breakdown when today has plays
                if total_plays == 0 and total_sec == 0:
                    return StatsSummary(period="today")
                buckets_to_merge = [cur_b]
            else:
                buckets_to_merge = buckets
                total_sec = sum(float(b.get("total_seconds", 0.0)) for b in buckets_to_merge)
                total_plays = sum(int(b.get("total_plays", 0)) for b in buckets_to_merge)

            merged_tracks: Dict[str, TrackStat] = {}
            merged_artists: Dict[str, ArtistStat] = {}
            hours = [0.0] * 24

            for b in buckets_to_merge:
                for tid, raw in b.get("tracks", {}).items():
                    if not isinstance(raw, dict):
                        continue
                    tr = Track(
                        id=str(raw.get("id") or tid),
                        title=str(raw.get("title") or "Unknown Track"),
                        artist=str(raw.get("artist") or "Unknown Artist"),
                        album=str(raw.get("album") or ""),
                        album_id=str(raw.get("album_id") or ""),
                        artist_id=str(raw.get("artist_id") or ""),
                        duration=str(raw.get("duration") or ""),
                        duration_seconds=int(raw.get("duration_seconds") or 0),
                        thumbnail=str(raw.get("thumbnail") or ""),
                    )
                    existing = merged_tracks.get(tid)
                    plays = int(raw.get("plays", 0))
                    secs = float(raw.get("seconds", 0.0))
                    if existing is None:
                        merged_tracks[tid] = TrackStat(track=tr, play_count=plays, listen_seconds=secs)
                    else:
                        existing.play_count += plays
                        existing.listen_seconds += secs

                for name, raw in b.get("artists", {}).items():
                    if not isinstance(raw, dict):
                        continue
                    existing_a = merged_artists.get(name)
                    plays = int(raw.get("plays", 0))
                    secs = float(raw.get("seconds", 0.0))
                    if existing_a is None:
                        merged_artists[name] = ArtistStat(name=name, play_count=plays, listen_seconds=secs)
                    else:
                        existing_a.play_count += plays
                        existing_a.listen_seconds += secs

                b_hours = b.get("hours", [])
                for i in range(min(24, len(b_hours))):
                    hours[i] += float(b_hours[i])

            top_tracks = sorted(
                merged_tracks.values(),
                key=lambda s: (s.play_count, s.listen_seconds),
                reverse=True,
            )[:25]
            top_artists = sorted(
                merged_artists.values(),
                key=lambda s: (s.listen_seconds, s.play_count),
                reverse=True,
            )[:20]

            return StatsSummary(
                period=period,
                total_seconds=total_sec,
                total_plays=total_plays,
                unique_tracks=len(merged_tracks),
                unique_artists=len(merged_artists),
                top_tracks=top_tracks,
                top_artists=top_artists,
                hourly_seconds=hours,
            )
