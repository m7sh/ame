"""
lyrics.py - Synced and plain lyrics fetcher & LRC parser.

Queries LRCLIB (word/line-synced LRC + plain lyrics) with YouTube Music's
built-in lyrics endpoint as fallback, and caches responses on disk under
``~/.cache/ame/lyrics``.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

_LRC_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_DEFAULT_LYRICS_DIR = Path.home() / ".cache" / "ame" / "lyrics"
_MAX_DISK_FILES = 200
_MAX_MEM_ITEMS = 48


@dataclass(frozen=True)
class LyricLine:
    """Single lyric line with optional start timestamp in milliseconds."""

    time_ms: Optional[int]
    text: str


@dataclass
class LyricsResult:
    """Normalized lyrics response for a track."""

    lines: List[LyricLine] = field(default_factory=list)
    synced: bool = False
    source: str = ""


def parse_lrc(lrc_text: str) -> List[LyricLine]:
    """Parse LRC formatted text into sorted ``LyricLine`` objects."""
    if not lrc_text:
        return []
    parsed: List[LyricLine] = []
    for raw_line in lrc_text.splitlines():
        matches = list(_LRC_TAG_RE.finditer(raw_line))
        if not matches:
            continue
        lyric = _LRC_TAG_RE.sub("", raw_line).strip()
        for m in matches:
            mins = int(m.group(1))
            secs = int(m.group(2))
            frac = m.group(3) or "0"
            if len(frac) == 1:
                ms = int(frac) * 100
            elif len(frac) == 2:
                ms = int(frac) * 10
            else:
                ms = int(frac[:3])
            total_ms = (mins * 60 + secs) * 1000 + ms
            parsed.append(LyricLine(time_ms=total_ms, text=lyric or "♪"))
    parsed.sort(key=lambda line: line.time_ms or 0)
    return parsed


def parse_plain_lyrics(text: str) -> List[LyricLine]:
    """Split plain unsynced lyrics into lines."""
    if not text:
        return []
    return [LyricLine(time_ms=None, text=line.strip()) for line in text.splitlines() if line.strip()]


def active_lyric_index(lines: List[LyricLine], pos_seconds: float) -> int:
    """Return the index of the active synced lyric line for ``pos_seconds``, or -1."""
    if not lines:
        return -1
    pos_ms = int(max(0.0, pos_seconds) * 1000) + 120
    active = -1
    for idx, line in enumerate(lines):
        if line.time_ms is None:
            return -1
        if line.time_ms <= pos_ms:
            active = idx
        else:
            break
    return active


class LyricsManager:
    """Fetches and caches synced or plain lyrics for tracks."""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        session: Optional[requests.Session] = None,
        ytm_fetcher: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self.cache_dir = cache_dir or _DEFAULT_LYRICS_DIR
        self._session = session or requests.Session()
        self._ytm_fetcher = ytm_fetcher
        self._lock = threading.Lock()
        self._mem: OrderedDict[str, LyricsResult] = OrderedDict()

    def _cache_key(self, track_id: str, title: str, artist: str) -> str:
        raw = f"{track_id}:{title.lower()}:{artist.lower()}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]

    def _disk_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def get_cached(self, track_id: str, title: str, artist: str) -> Optional[LyricsResult]:
        key = self._cache_key(track_id, title, artist)
        with self._lock:
            hit = self._mem.get(key)
            if hit is not None:
                self._mem.move_to_end(key)
                return hit

        path = self._disk_path(key)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                lines = [
                    LyricLine(time_ms=item.get("time_ms"), text=str(item.get("text", "")))
                    for item in data.get("lines", [])
                ]
                res = LyricsResult(
                    lines=lines,
                    synced=bool(data.get("synced", False)),
                    source=str(data.get("source", "")),
                )
                self._store_mem(key, res)
                return res
            except Exception:
                pass
        return None

    def fetch(
        self,
        track_id: str,
        title: str,
        artist: str,
        album: str = "",
        duration_seconds: int = 0,
    ) -> LyricsResult:
        """Fetch lyrics from cache, LRCLIB, or YouTube Music fallback."""
        cached = self.get_cached(track_id, title, artist)
        if cached is not None:
            return cached

        res = self._fetch_lrclib(title, artist, album, duration_seconds)
        if (not res.lines or not res.synced) and self._ytm_fetcher is not None:
            ytm_res = self._fetch_ytm(track_id)
            if ytm_res.lines and (ytm_res.synced or not res.lines):
                res = ytm_res

        key = self._cache_key(track_id, title, artist)
        if res.lines:
            self._store_mem(key, res)
            self._store_disk(key, res)
        return res

    def _fetch_lrclib(
        self,
        title: str,
        artist: str,
        album: str,
        duration_seconds: int,
    ) -> LyricsResult:
        clean_title = re.sub(r"\s*[\(\[](?:official|audio|video|lyrics).*?[\)\]]", "", title, flags=re.I).strip()
        params: Dict[str, Any] = {
            "track_name": clean_title or title,
            "artist_name": artist.split(",")[0].strip(),
        }
        if album:
            params["album_name"] = album
        if duration_seconds > 0:
            params["duration"] = duration_seconds

        try:
            r = self._session.get(
                "https://lrclib.net/api/get",
                params=params,
                timeout=5.0,
                headers={"User-Agent": "ame/2.2 (https://github.com/m7sh/ame)"},
            )
            if r.status_code == 200:
                res = self._parse_lrclib_payload(r.json())
                if res.lines:
                    return res
        except Exception:
            pass

        try:
            r = self._session.get(
                "https://lrclib.net/api/search",
                params={"q": f"{clean_title or title} {artist.split(',')[0].strip()}"},
                timeout=5.0,
                headers={"User-Agent": "ame/2.2 (https://github.com/m7sh/ame)"},
            )
            if r.status_code == 200:
                items = r.json()
                if isinstance(items, list):
                    for item in items:
                        res = self._parse_lrclib_payload(item)
                        if res.lines and res.synced:
                            return res
                    for item in items:
                        res = self._parse_lrclib_payload(item)
                        if res.lines:
                            return res
        except Exception:
            pass

        return LyricsResult()

    def _parse_lrclib_payload(self, data: Dict[str, Any]) -> LyricsResult:
        if not isinstance(data, dict):
            return LyricsResult()
        synced_raw = str(data.get("syncedLyrics") or "").strip()
        if synced_raw:
            lines = parse_lrc(synced_raw)
            if lines:
                return LyricsResult(lines=lines, synced=True, source="LRCLIB")
        plain_raw = str(data.get("plainLyrics") or "").strip()
        if plain_raw:
            lines = parse_plain_lyrics(plain_raw)
            if lines:
                return LyricsResult(lines=lines, synced=False, source="LRCLIB")
        return LyricsResult()

    def _fetch_ytm(self, video_id: str) -> LyricsResult:
        if self._ytm_fetcher is None:
            return LyricsResult()
        try:
            payload = self._ytm_fetcher(video_id)
        except Exception:
            return LyricsResult()
        if not isinstance(payload, dict):
            return LyricsResult()

        raw_lyrics = payload.get("lyrics")
        if isinstance(raw_lyrics, list):
            lines: List[LyricLine] = []
            has_timestamps = bool(payload.get("hasTimestamps"))
            for item in raw_lyrics:
                if isinstance(item, dict):
                    txt = str(item.get("text") or "").strip()
                    st = item.get("start_time")
                    if txt:
                        lines.append(
                            LyricLine(
                                time_ms=int(st) if (has_timestamps and st is not None) else None,
                                text=txt,
                            )
                        )
                elif hasattr(item, "text"):
                    txt = str(getattr(item, "text", "") or "").strip()
                    st = getattr(item, "start_time", None)
                    if txt:
                        lines.append(
                            LyricLine(
                                time_ms=int(st) if (has_timestamps and st is not None) else None,
                                text=txt,
                            )
                        )
            if lines:
                return LyricsResult(
                    lines=lines,
                    synced=any(l.time_ms is not None for l in lines),
                    source="YouTube Music",
                )
        elif isinstance(raw_lyrics, str) and raw_lyrics.strip():
            return LyricsResult(
                lines=parse_plain_lyrics(raw_lyrics),
                synced=False,
                source="YouTube Music",
            )
        return LyricsResult()

    def _store_mem(self, key: str, res: LyricsResult) -> None:
        with self._lock:
            self._mem[key] = res
            self._mem.move_to_end(key)
            while len(self._mem) > _MAX_MEM_ITEMS:
                self._mem.popitem(last=False)

    def _store_disk(self, key: str, res: LyricsResult) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._disk_path(key)
            payload = {
                "source": res.source,
                "synced": res.synced,
                "lines": [{"time_ms": l.time_ms, "text": l.text} for l in res.lines],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            files = sorted(self.cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            excess = len(files) - _MAX_DISK_FILES
            for old in files[: max(0, excess)]:
                old.unlink(missing_ok=True)
        except Exception:
            pass
