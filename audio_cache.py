"""
audio_cache.py - Bounded LRU on-disk audio cache.

Caches complete audio files under ~/.cache/ame/audio/ keyed by YouTube videoId:
- Downloads in 2 MiB Range chunks using the shared HTTP session so googlevideo
  does not pace transfers down to real-time playback speed.
- Writes atomically through a .part file and promotes only complete files so a
  partial download never corrupts playback or seeking.
- Evicts least-recently-used entries past `max_bytes` (512 MiB default), while
  protecting currently playing or armed next-up tracks.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

import requests

from stream_resolver import ResolvedStream

DEFAULT_CACHE_LIMIT_BYTES = 512 * 1024 * 1024  # 512 MiB default cache cap
CHUNK_BYTES = 2 * 1024 * 1024                  # 2 MiB bounded range chunks
DOWNLOAD_TIMEOUT_SECONDS = 12.0


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "ame" / "audio"


@dataclass
class CachedAudio:
    video_id: str
    path: Path
    quality: str
    codec: str
    kbps: int
    mime_type: str
    loudness_db: Optional[float]
    size_bytes: int


class AudioCache:
    """Thread-safe, bounded LRU disk cache for complete resolved audio tracks."""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        max_bytes: int = DEFAULT_CACHE_LIMIT_BYTES,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.max_bytes = max(0, int(max_bytes))
        self._session = session or requests.Session()
        self._lock = threading.RLock()
        self._in_flight: Set[str] = set()
        self._clean_stale_parts()

    def _clean_stale_parts(self) -> None:
        if not self.cache_dir.exists():
            return
        try:
            for p in self.cache_dir.glob("*.part"):
                p.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _safe_id(video_id: str) -> str:
        return "".join(ch for ch in video_id if ch.isalnum() or ch in ("-", "_"))

    def _meta_path(self, safe_id: str) -> Path:
        return self.cache_dir / f"{safe_id}.json"

    def _find_audio_file(self, safe_id: str) -> Optional[Path]:
        for ext in ("webm", "m4a", "opus", "mp3"):
            candidate = self.cache_dir / f"{safe_id}.{ext}"
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        return None

    def get(self, video_id: str) -> Optional[CachedAudio]:
        """Return complete cached track metadata and touch its LRU timestamp."""
        if self.max_bytes <= 0:
            return None
        safe_id = self._safe_id(video_id)
        if not safe_id:
            return None

        with self._lock:
            audio_file = self._find_audio_file(safe_id)
            if audio_file is None:
                return None

            meta: Dict[str, object] = {}
            meta_file = self._meta_path(safe_id)
            if meta_file.is_file():
                try:
                    meta = json.loads(meta_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    meta = {}

            try:
                size = audio_file.stat().st_size
                expected_len = meta.get("content_length")
                if isinstance(expected_len, int) and expected_len > 0 and size != expected_len:
                    # Incomplete file: discard so playback fetches cleanly.
                    audio_file.unlink(missing_ok=True)
                    meta_file.unlink(missing_ok=True)
                    return None
                os.utime(audio_file, None)
            except OSError:
                return None

            return CachedAudio(
                video_id=video_id,
                path=audio_file,
                quality=str(meta.get("quality") or "Cached Audio"),
                codec=str(meta.get("codec") or audio_file.suffix.lstrip(".")),
                kbps=int(meta.get("kbps") or 0),
                mime_type=str(meta.get("mime_type") or "audio/webm"),
                loudness_db=float(meta["loudness_db"]) if isinstance(meta.get("loudness_db"), (int, float)) else None,
                size_bytes=size,
            )

    def ensure_cached_async(
        self,
        stream: ResolvedStream,
        protected_ids: Optional[Iterable[str]] = None,
    ) -> None:
        """Download ``stream`` to disk in a background daemon thread if not already cached."""
        if self.max_bytes <= 0 or stream.is_live or stream.cached_path:
            return
        if not stream.url.startswith(("http://", "https://")):
            return
        safe_id = self._safe_id(stream.video_id)
        if not safe_id:
            return

        with self._lock:
            if self._find_audio_file(safe_id) is not None or safe_id in self._in_flight:
                return
            self._in_flight.add(safe_id)

        protected = set(protected_ids or ())
        protected.add(stream.video_id)

        def _worker() -> None:
            try:
                self.download(stream, protected_ids=protected)
            finally:
                with self._lock:
                    self._in_flight.discard(safe_id)

        threading.Thread(target=_worker, daemon=True).start()

    def download(
        self,
        stream: ResolvedStream,
        protected_ids: Optional[Iterable[str]] = None,
    ) -> Optional[CachedAudio]:
        """Download complete stream via 2 MiB bounded Range chunks and commit atomically."""
        if self.max_bytes <= 0 or stream.is_live:
            return None
        safe_id = self._safe_id(stream.video_id)
        if not safe_id:
            return None

        existing = self.get(stream.video_id)
        if existing is not None:
            return existing

        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        ext = stream.download_extension
        final_path = self.cache_dir / f"{safe_id}.{ext}"
        part_path = self.cache_dir / f"{safe_id}.{ext}.part"
        meta_path = self._meta_path(safe_id)

        total_len = stream.content_length
        if total_len is not None and total_len > self.max_bytes:
            return None

        written = 0
        try:
            with open(part_path, "wb") as out:
                if total_len and total_len > 0:
                    offset = 0
                    while offset < total_len:
                        end = min(total_len - 1, offset + CHUNK_BYTES - 1)
                        req_headers = dict(stream.headers)
                        req_headers["Range"] = f"bytes={offset}-{end}"
                        resp = self._session.get(
                            stream.url,
                            headers=req_headers,
                            stream=True,
                            timeout=DOWNLOAD_TIMEOUT_SECONDS,
                        )
                        if not resp.ok and resp.status_code != 206:
                            raise OSError(f"HTTP {resp.status_code}")
                        chunk_bytes = 0
                        for buf in resp.iter_content(chunk_size=65536):
                            if buf:
                                out.write(buf)
                                chunk_bytes += len(buf)
                        if chunk_bytes == 0:
                            raise OSError("empty range chunk")
                        written += chunk_bytes
                        offset += chunk_bytes
                else:
                    # Unknown length fallback: single streamed download
                    resp = self._session.get(
                        stream.url,
                        headers=dict(stream.headers),
                        stream=True,
                        timeout=DOWNLOAD_TIMEOUT_SECONDS,
                    )
                    if not resp.ok:
                        raise OSError(f"HTTP {resp.status_code}")
                    for buf in resp.iter_content(chunk_size=65536):
                        if buf:
                            out.write(buf)
                            written += len(buf)
                            if written > self.max_bytes:
                                raise OSError("exceeded cache limit")
                    total_len = written

            if written <= 0 or (total_len and written != total_len):
                part_path.unlink(missing_ok=True)
                return None

            with self._lock:
                self._evict_for(written, protected_ids=protected_ids)
                part_path.replace(final_path)
                meta_payload = {
                    "video_id": stream.video_id,
                    "quality": f"{stream.quality} · cached" if "cached" not in stream.quality else stream.quality,
                    "codec": stream.codec,
                    "kbps": stream.kbps,
                    "mime_type": stream.mime_type,
                    "loudness_db": stream.loudness_db,
                    "content_length": written,
                    "cached_at": time.time(),
                }
                tmp_meta = meta_path.with_suffix(".json.tmp")
                tmp_meta.write_text(json.dumps(meta_payload), encoding="utf-8")
                tmp_meta.replace(meta_path)

            return self.get(stream.video_id)
        except Exception:
            part_path.unlink(missing_ok=True)
            return None

    def discard(self, video_id: str) -> None:
        safe_id = self._safe_id(video_id)
        if not safe_id:
            return
        with self._lock:
            for ext in ("webm", "m4a", "opus", "mp3", "json", "webm.part", "m4a.part"):
                (self.cache_dir / f"{safe_id}.{ext}").unlink(missing_ok=True)

    def clear(self) -> None:
        with self._lock:
            if not self.cache_dir.exists():
                return
            for p in self.cache_dir.iterdir():
                if p.is_file():
                    p.unlink(missing_ok=True)

    def total_bytes(self) -> int:
        if not self.cache_dir.exists():
            return 0
        total = 0
        for p in self.cache_dir.iterdir():
            if p.is_file() and p.suffix in (".webm", ".m4a", ".opus", ".mp3"):
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total

    def _evict_for(self, needed_bytes: int, protected_ids: Optional[Iterable[str]] = None) -> None:
        if not self.cache_dir.exists():
            return
        protected_safe = {self._safe_id(vid) for vid in (protected_ids or ()) if vid}

        entries = []
        total = 0
        for p in self.cache_dir.iterdir():
            if p.is_file() and p.suffix in (".webm", ".m4a", ".opus", ".mp3"):
                try:
                    st = p.stat()
                    total += st.st_size
                    entries.append((st.st_mtime, st.st_size, p))
                except OSError:
                    pass

        if total + needed_bytes <= self.max_bytes:
            return

        # Evict oldest non-protected entries first; fall back to oldest overall if still over budget.
        entries.sort(key=lambda item: item[0])
        for pass_protected in (False, True):
            for _, size, path in entries:
                if total + needed_bytes <= self.max_bytes:
                    return
                if not pass_protected and path.stem in protected_safe:
                    continue
                if not path.exists():
                    continue
                try:
                    path.unlink(missing_ok=True)
                    self._meta_path(path.stem).unlink(missing_ok=True)
                    total -= size
                except OSError:
                    pass
