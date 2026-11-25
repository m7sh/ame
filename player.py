"""
player.py - Headless MPV IPC Client, Audio Queue, Gapless & Crossfade Controller.

Playback reliability features for headless mpv:
- Thread-safe RLock queue/state coordination & play generation cancellation
- Matched per-client HTTP headers + User-Agent passed cleanly to mpv
- Mid-playback error/403 recovery resuming at current timestamp
- Per-track loudness normalization from YouTube's loudnessDb metadata
- On-disk LRU AudioCache integration & lookahead prefetching
- True gapless playback (mpv playlist append) + dual-mpv equal-power crossfade
"""

from __future__ import annotations

import atexit
import json
import math
import os
import random
import socket
import subprocess
import threading
import time
import uuid
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from api import Track
from audio_cache import AudioCache, CachedAudio
from stream_resolver import (
    PermanentlyUnplayableError,
    ResolvedStream,
    StreamResolver,
)

# Loudness gain bounds (dB) for safe headroom window
MIN_LOUDNESS_GAIN_DB = -12.0
MAX_LOUDNESS_GAIN_DB = 6.0

# Crossfade options cycled with `x` (0 = native gapless)
CROSSFADE_PRESETS = (0, 3, 6)
CROSSFADE_TICK_SECONDS = 0.04
MAX_MID_PLAYBACK_RECOVERIES = 2
SPEED_PRESETS = (1.0, 1.25, 1.5, 2.0, 0.75)


class LoopMode(Enum):
    """Playback loop behavior when a track ends."""

    OFF = "off"
    LOOP_ONE = "loop_one"
    STOP_AFTER_ONE = "stop_after_one"

    def next_mode(self) -> "LoopMode":
        order = [LoopMode.OFF, LoopMode.LOOP_ONE, LoopMode.STOP_AFTER_ONE]
        idx = order.index(self)
        return order[(idx + 1) % len(order)]


def _compute_loudness_gain_db(loudness_db: Optional[float]) -> Optional[float]:
    if loudness_db is None:
        return None
    gain = -float(loudness_db)
    return max(MIN_LOUDNESS_GAIN_DB, min(MAX_LOUDNESS_GAIN_DB, gain))


def _format_mpv_options(
    media_title: str,
    headers: Optional[Dict[str, str]] = None,
    start_pos: float = 0.0,
    loudness_db: Optional[float] = None,
    normalize_loudness: bool = True,
) -> Dict[str, Any]:
    """Build per-file mpv loadfile options without leaking unrelated headers."""
    opts: Dict[str, Any] = {
        "force-media-title": media_title,
    }
    if start_pos > 0.5:
        opts["start"] = f"{start_pos:.2f}"

    if headers:
        ua = headers.get("User-Agent")
        if ua:
            opts["user-agent"] = ua
        extra = [
            f"{k}: {v}"
            for k, v in headers.items()
            if k.lower() != "user-agent" and v
        ]
        if extra:
            opts["http-header-fields"] = ",".join(extra)

    if normalize_loudness:
        gain_db = _compute_loudness_gain_db(loudness_db)
        if gain_db is not None and abs(gain_db) >= 0.05:
            opts["af"] = f"volume=volume={gain_db:.2f}dB"

    return opts


class _MpvDeck:
    """Single headless mpv daemon process managed over a UNIX IPC socket."""

    def __init__(
        self,
        name: str,
        initial_volume: int,
        on_event: Callable[["_MpvDeck", Dict[str, Any]], None],
    ) -> None:
        self.name = name
        self.sock_path = f"/tmp/ame_mpv_{name}_{os.getpid()}_{uuid.uuid4().hex[:6]}.sock"
        self.on_event = on_event

        self.proc: Optional[subprocess.Popen] = None
        self.sock: Optional[socket.socket] = None
        self.sock_file = None
        self._io_lock = threading.Lock()
        self._resp_lock = threading.Lock()
        self._pending: Dict[int, Tuple[threading.Event, Dict[str, Any]]] = {}
        self._next_req_id = 100
        self._running = False
        self._reader_thread: Optional[threading.Thread] = None

        self.active_entry_id: Optional[int] = None
        self.armed_entry_id: Optional[int] = None
        self.retired_entry_ids: Set[int] = set()
        self._last_volume = initial_volume

        self.start(initial_volume)

    def start(self, volume: int) -> None:
        if os.path.exists(self.sock_path):
            try:
                os.remove(self.sock_path)
            except OSError:
                pass

        cmd = [
            "mpv",
            "--no-video",
            "--no-config",
            "--idle",
            f"--input-ipc-server={self.sock_path}",
            "--gapless-audio=yes",
            f"--volume={max(0, min(150, int(round(volume))))}",
            "--ytdl=no",
            "--audio-client-name=ame",
        ]
        ao_override = os.environ.get("AME_MPV_AO", "").strip()
        if ao_override:
            cmd.append(f"--ao={ao_override}")

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("mpv executable not found. Please ensure mpv is installed.") from exc

        connected = False
        for _ in range(60):
            if os.path.exists(self.sock_path):
                try:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(self.sock_path)
                    self.sock_file = self.sock.makefile("r", encoding="utf-8", errors="replace")
                    connected = True
                    break
                except OSError:
                    if self.sock:
                        try:
                            self.sock.close()
                        except OSError:
                            pass
                        self.sock = None
                    time.sleep(0.05)
            time.sleep(0.05)

        if not connected or not self.sock:
            self.cleanup()
            raise RuntimeError("Failed to connect to headless mpv IPC socket.")

        self._running = True
        self._reader_thread = threading.Thread(target=self._ipc_listener, daemon=True)
        self._reader_thread.start()

        self.send_command(["observe_property", 1, "pause"])
        self.send_command(["observe_property", 2, "time-pos"])
        self.send_command(["observe_property", 3, "duration"])
        self.send_command(["observe_property", 4, "volume"])

    def is_alive(self) -> bool:
        return (
            self._running
            and self.proc is not None
            and self.proc.poll() is None
            and self.sock is not None
        )

    def ensure_alive(self, volume: int) -> None:
        if self.is_alive():
            return
        self.cleanup()
        self.start(volume)

    def send_command(self, cmd: List[Any], request_id: Optional[int] = None) -> bool:
        with self._io_lock:
            if not self.sock or not self._running:
                return False
            payload: Dict[str, Any] = {"command": cmd}
            if request_id is not None:
                payload["request_id"] = request_id
            try:
                data = json.dumps(payload).encode("utf-8") + b"\n"
                self.sock.sendall(data)
                return True
            except OSError:
                return False

    def request_command(self, cmd: List[Any], timeout: float = 1.5) -> Optional[Dict[str, Any]]:
        with self._resp_lock:
            req_id = self._next_req_id
            self._next_req_id += 1
            ev = threading.Event()
            bucket: Dict[str, Any] = {}
            self._pending[req_id] = (ev, bucket)

        try:
            if not self.send_command(cmd, request_id=req_id):
                return None
            if not ev.wait(timeout=timeout):
                return None
            return bucket.get("msg")
        finally:
            with self._resp_lock:
                self._pending.pop(req_id, None)

    def load_file(self, source: str, mode: str, options: Dict[str, Any]) -> Optional[int]:
        """Load or append a track into mpv and return its playlist_entry_id."""
        if mode == "replace" and self.active_entry_id is not None:
            self.retired_entry_ids.add(self.active_entry_id)
            if len(self.retired_entry_ids) > 32:
                self.retired_entry_ids = set(list(self.retired_entry_ids)[-16:])
            self.active_entry_id = None
        resp = self.request_command(["loadfile", source, mode, -1, options])
        if resp is None or resp.get("error") != "success":
            # Compatibility fallback for older mpv < 0.38 (3-arg loadfile)
            resp = self.request_command(["loadfile", source, mode, options])
        if isinstance(resp, dict) and resp.get("error") == "success":
            data = resp.get("data")
            if isinstance(data, dict) and "playlist_entry_id" in data:
                return int(data["playlist_entry_id"])
        return None

    def set_volume(self, vol: float) -> None:
        self.send_command(["set_property", "volume", max(0.0, min(150.0, float(vol)))])

    def set_pause(self, paused: bool) -> None:
        self.send_command(["set_property", "pause", bool(paused)])

    def clear_upcoming_playlist(self) -> None:
        """Remove queued (non-active) entries from mpv's internal playlist."""
        self.armed_entry_id = None
        resp = self.request_command(["get_property", "playlist"], timeout=0.6)
        if not isinstance(resp, dict) or resp.get("error") != "success":
            return
        items = resp.get("data")
        if not isinstance(items, list):
            return
        # Remove from highest index downward so indices remain valid
        for idx in range(len(items) - 1, -1, -1):
            entry = items[idx]
            if isinstance(entry, dict) and not entry.get("current"):
                self.send_command(["playlist-remove", idx])

    def _ipc_listener(self) -> None:
        while self._running and self.sock_file:
            try:
                line = self.sock_file.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                msg = json.loads(line)
            except (json.JSONDecodeError, OSError, ValueError):
                break

            req_id = msg.get("request_id")
            if req_id is not None and "error" in msg:
                with self._resp_lock:
                    waiter = self._pending.get(req_id)
                if waiter is not None:
                    ev, bucket = waiter
                    bucket["msg"] = msg
                    ev.set()
                continue

            try:
                self.on_event(self, msg)
            except Exception:
                pass

    def cleanup(self) -> None:
        self._running = False
        try:
            self.send_command(["quit"])
        except Exception:
            pass

        with self._resp_lock:
            for ev, _ in self._pending.values():
                ev.set()
            self._pending.clear()

        if self.sock_file:
            try:
                self.sock_file.close()
            except Exception:
                pass
            self.sock_file = None

        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

        if self._reader_thread and self._reader_thread.is_alive():
            if threading.current_thread() is not self._reader_thread:
                self._reader_thread.join(timeout=0.5)
        self._reader_thread = None

        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1.0)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            self.proc = None

        if os.path.exists(self.sock_path):
            try:
                os.remove(self.sock_path)
            except OSError:
                pass


class MPVPlayer:
    """
    Headless mpv audio player controller with:
    - Multi-client InnerTube stream resolution, 1 MiB probing, and mid-playback recovery
    - Thread-safe queue state machine (RLock + generation tokens)
    - On-disk LRU AudioCache prefetching
    - Native gapless playback and dual-mpv equal-power crossfade
    - Per-track loudness normalization
    """

    def __init__(
        self,
        resolver: Optional[StreamResolver] = None,
        cache: Optional[AudioCache | bool] = None,
    ) -> None:
        self.resolver = resolver or StreamResolver()
        if cache is False:
            self.cache: Optional[AudioCache] = None
        elif isinstance(cache, AudioCache):
            self.cache = cache
        elif isinstance(self.resolver, StreamResolver):
            self.cache = AudioCache(session=self.resolver._session)
        else:
            self.cache = None

        self._lock = threading.RLock()
        self._running = True
        self._cleaned_up = False

        # Generation counter prevents stale resolve/end-file threads from mutating newer playback
        self._play_gen: int = 0
        self._active_deck: Optional[_MpvDeck] = None
        self._standby_deck: Optional[_MpvDeck] = None
        self._current_stream: Optional[ResolvedStream] = None
        self._armed_next: Optional[Tuple[int, Track, ResolvedStream, Optional[int]]] = None

        # Crossfade state
        self.crossfade_seconds: int = 0
        self._crossfade_active: bool = False
        self._crossfade_stop_ev = threading.Event()
        self._crossfade_thread: Optional[threading.Thread] = None
        self._crossfade_triggered_gen: int = -1

        # Loudness normalization & playback speed
        self.loudness_normalization: bool = True
        self.speed: float = 1.0

        # Queue & playback state
        self.queue: List[Track] = []
        self.history: List[Track] = []
        self.current_track: Optional[Track] = None
        self.current_stream_quality: str = "Opus Audio"

        self.is_playing: bool = False
        self.is_paused: bool = False
        self.is_buffering: bool = False
        self.autoplay: bool = True
        self.volume: int = 80
        self.playback_pos: float = 0.0
        self.duration: float = 0.0
        self.loop_mode: LoopMode = LoopMode.OFF

        # UI Callbacks
        self.on_track_change: Optional[Callable[[Optional[Track]], None]] = None
        self.on_state_change: Optional[Callable[[bool, bool, bool], None]] = None
        self.on_progress: Optional[Callable[[float, float], None]] = None
        self.on_queue_change: Optional[Callable[[], None]] = None
        self.on_autoplay_trigger: Optional[Callable[[Track], None]] = None
        self.on_message: Optional[Callable[[str], None]] = None
        self.on_volume_change: Optional[Callable[[int], None]] = None
        self.on_crossfade_change: Optional[Callable[[int], None]] = None
        self.on_loudness_change: Optional[Callable[[bool], None]] = None
        self.on_speed_change: Optional[Callable[[float], None]] = None

        self._start_mpv()
        atexit.register(self.cleanup)

    @property
    def sock_path(self) -> str:
        return self._active_deck.sock_path if self._active_deck else ""

    @property
    def current_stream_info(self) -> Optional[ResolvedStream]:
        """Thread-safe snapshot of the active stream metadata for NerdStats."""
        with self._lock:
            return self._current_stream

    def _start_mpv(self) -> None:
        """Spawn the primary headless mpv daemon."""
        self._active_deck = _MpvDeck("primary", self.volume, self._on_deck_event)

    def _ensure_standby_deck(self) -> _MpvDeck:
        if self._standby_deck is None:
            self._standby_deck = _MpvDeck("standby", 0, self._on_deck_event)
        else:
            self._standby_deck.ensure_alive(0)
        return self._standby_deck

    def _send_command(self, cmd: List[Any], request_id: Optional[int] = None) -> None:
        deck = self._active_deck
        if deck is not None:
            deck.send_command(cmd, request_id=request_id)

    # ------------------------------------------------------------------ #
    # MPV Event Handling
    # ------------------------------------------------------------------ #

    def _on_deck_event(self, deck: _MpvDeck, msg: Dict[str, Any]) -> None:
        with self._lock:
            if not self._running or deck is not self._active_deck:
                return
            gen = self._play_gen
            active_entry_id = deck.active_entry_id
            armed = self._armed_next
            crossfading = self._crossfade_active

        event = msg.get("event")

        if event == "property-change":
            prop_name = msg.get("name")
            data = msg.get("data")

            if prop_name == "time-pos" and data is not None:
                pos = float(data)
                with self._lock:
                    if self._play_gen != gen:
                        return
                    self.playback_pos = pos
                    dur = self.duration
                if self.on_progress:
                    self.on_progress(pos, dur)
                self._maybe_trigger_crossfade(gen, pos, dur)

            elif prop_name == "duration" and data is not None:
                dur = float(data)
                with self._lock:
                    if self._play_gen != gen:
                        return
                    self.duration = dur
                    pos = self.playback_pos
                if self.on_progress:
                    self.on_progress(pos, dur)

            elif prop_name == "pause" and data is not None:
                paused = bool(data)
                with self._lock:
                    if not self.is_playing and paused:
                        return
                    self.is_paused = paused
                    playing = self.is_playing
                    buffering = self.is_buffering
                if self.on_state_change:
                    self.on_state_change(playing, paused, buffering)

            elif prop_name == "volume" and data is not None:
                if crossfading:
                    return
                vol = int(round(float(data)))
                with self._lock:
                    self.volume = vol
                if self.on_volume_change:
                    self.on_volume_change(vol)

        elif event == "start-file":
            entry_id = msg.get("playlist_entry_id")
            if entry_id is not None and entry_id in deck.retired_entry_ids:
                return
            # Gapless transition to armed next-up track inside mpv's playlist
            if armed is not None and entry_id is not None and armed[3] == entry_id:
                self._promote_gapless_armed(armed, deck)
                return

            with self._lock:
                if active_entry_id is not None and entry_id is not None and entry_id != active_entry_id:
                    return
                self.is_buffering = True
                self.is_playing = True
                paused = self.is_paused
            if self.on_state_change:
                self.on_state_change(True, paused, True)

        elif event == "file-loaded":
            with self._lock:
                self.is_buffering = False
                self.is_playing = True
                paused = self.is_paused
                cur_gen = self._play_gen
            if self.on_state_change:
                self.on_state_change(True, paused, False)
            self._schedule_lookahead(cur_gen)

        elif event == "end-file":
            reason = msg.get("reason")
            entry_id = msg.get("playlist_entry_id")

            if entry_id is not None and entry_id in deck.retired_entry_ids:
                deck.retired_entry_ids.discard(entry_id)
                return
            # Ignore end-file for non-active entries
            if active_entry_id is not None and entry_id is not None and entry_id != active_entry_id:
                return

            if reason == "eof":
                with self._lock:
                    # If an armed gapless entry already exists in mpv, wait for its start-file
                    if self._armed_next is not None and self.loop_mode == LoopMode.OFF:
                        return
                    if crossfading:
                        return
                threading.Thread(
                    target=self._handle_track_finished, args=(gen,), daemon=True
                ).start()
            elif reason == "error":
                threading.Thread(
                    target=self._recover_mid_playback, args=(gen,), daemon=True
                ).start()

    def _promote_gapless_armed(
        self,
        armed: Tuple[int, Track, ResolvedStream, Optional[int]],
        deck: _MpvDeck,
    ) -> None:
        """Promote a gaplessly-transitioned armed track in mpv's playlist."""
        armed_gen, next_track, next_stream, entry_id = armed
        with self._lock:
            if self._armed_next is not armed or self._play_gen != armed_gen:
                return
            self._armed_next = None
            if self.current_track and self.current_track.id != next_track.id:
                self.history.append(self.current_track)
            if self.queue and self.queue[0].id == next_track.id:
                self.queue.pop(0)
            else:
                for i, q_track in enumerate(self.queue):
                    if q_track.id == next_track.id:
                        self.queue.pop(i)
                        break

            self._play_gen += 1
            new_gen = self._play_gen
            deck.active_entry_id = entry_id
            deck.armed_entry_id = None
            self.current_track = next_track
            self._current_stream = next_stream
            self.current_stream_quality = next_stream.quality
            self.playback_pos = 0.0
            self.duration = float(next_track.duration_seconds or 0)
            self.is_playing = True
            self.is_paused = False
            self.is_buffering = False

        # Clean up finished entry 0 without interrupting entry 1
        deck.send_command(["playlist-remove", 0])
        if self.on_queue_change:
            self.on_queue_change()
        if self.on_track_change:
            self.on_track_change(next_track)
        if self.on_state_change:
            self.on_state_change(True, False, False)
        self._schedule_lookahead(new_gen)

    # ------------------------------------------------------------------ #
    # Track End & Mid-Playback Recovery
    # ------------------------------------------------------------------ #

    def _handle_track_finished(self, expected_gen: Optional[int] = None) -> None:
        """Called when a track finishes naturally (Issue #7 fixed: queue mutations under _lock)."""
        next_to_play: Optional[Track] = None
        autoplay_track: Optional[Track] = None
        stopped_state = False
        queue_changed = False
        track_cleared = False

        with self._lock:
            if expected_gen is not None and expected_gen != self._play_gen:
                return

            self.playback_pos = 0.0
            self.duration = 0.0
            self._armed_next = None

            if self.loop_mode == LoopMode.LOOP_ONE and self.current_track is not None:
                next_to_play = self.current_track
            elif self.loop_mode == LoopMode.STOP_AFTER_ONE:
                self.is_playing = False
                self.is_paused = False
                self.is_buffering = False
                stopped_state = True
            elif self.queue:
                next_to_play = self.queue.pop(0)
                queue_changed = True
            else:
                autoplay_track = self.current_track
                self.is_playing = False
                self.is_paused = False
                self.is_buffering = False
                self.current_track = None
                self._current_stream = None
                stopped_state = True
                track_cleared = True

        # Emit callbacks & play outside _lock to avoid holding lock across I/O
        if queue_changed and self.on_queue_change:
            self.on_queue_change()
        if stopped_state and self.on_state_change:
            self.on_state_change(False, False, False)
        if track_cleared and self.on_track_change:
            self.on_track_change(None)

        if next_to_play is not None:
            self._play_track_sync(next_to_play)
        elif autoplay_track is not None and self.autoplay and self.on_autoplay_trigger:
            self.on_autoplay_trigger(autoplay_track)

    def _recover_mid_playback(self, failed_gen: int) -> None:
        """
        Mid-playback error/403 recovery:
        bench failing client, drop stale cache, resolve next client, and resume at position.
        """
        with self._lock:
            if failed_gen != self._play_gen or not self.current_track:
                return
            track = self.current_track
            failed_stream = self._current_stream
            resume_pos = max(0.0, self.playback_pos - 1.0)
            self._play_gen += 1
            recovery_gen = self._play_gen

        skip_clients: Set[str] = set()
        if failed_stream is not None:
            if failed_stream.client_id:
                skip_clients.add(failed_stream.client_id)
            if hasattr(self.resolver, "on_playback_refused"):
                self.resolver.on_playback_refused(
                    track.id, url=failed_stream.url, client_id=failed_stream.client_id
                )
            if failed_stream.cached_path and self.cache is not None:
                self.cache.discard(track.id)

        for _ in range(MAX_MID_PLAYBACK_RECOVERIES):
            if not self._is_gen_current(recovery_gen):
                return
            try:
                stream = self._resolve_stream_or_cache(track.id, skip_clients=skip_clients)
                if not self._is_gen_current(recovery_gen):
                    return
                if self._load_into_active_deck(track, stream, recovery_gen, start_pos=resume_pos):
                    return
                if stream.client_id:
                    skip_clients.add(stream.client_id)
            except PermanentlyUnplayableError as exc:
                self._fail_playback(recovery_gen, str(exc))
                return
            except Exception:
                break

        self._fail_playback(recovery_gen, f"Stream interrupted for {track.title}")

    def _fail_playback(self, gen: int, message: str) -> None:
        with self._lock:
            if gen != self._play_gen:
                return
            self.is_buffering = False
            self.is_playing = False
            self.is_paused = False
        if self.on_state_change:
            self.on_state_change(False, False, False)
        if self.on_message:
            self.on_message(message)

    # ------------------------------------------------------------------ #
    # Stream Resolution, Caching & Deck Loading
    # ------------------------------------------------------------------ #

    def _is_gen_current(self, gen: int) -> bool:
        with self._lock:
            return self._running and gen == self._play_gen

    def _resolve_stream_or_cache(
        self,
        video_id: str,
        skip_clients: Optional[Set[str]] = None,
    ) -> ResolvedStream:
        # 1. Serve complete cached file when available and not retrying after cache error
        if self.cache is not None and not skip_clients:
            cached: Optional[CachedAudio] = self.cache.get(video_id)
            if cached is not None:
                return ResolvedStream(
                    video_id=video_id,
                    url=str(cached.path),
                    quality=cached.quality,
                    codec=cached.codec,
                    kbps=cached.kbps,
                    mime_type=cached.mime_type,
                    client_id="CACHE",
                    headers={},
                    loudness_db=cached.loudness_db,
                    content_length=cached.size_bytes,
                    cached_path=str(cached.path),
                )

        # 2. Resolve via StreamResolver (or mock in tests)
        if hasattr(self.resolver, "resolve_stream"):
            return self.resolver.resolve_stream(video_id, skip_clients=skip_clients)

        url, quality = self.resolver.resolve(video_id)
        return ResolvedStream(
            video_id=video_id,
            url=url,
            quality=quality,
            client_id="RESOLVER",
        )

    def _load_into_active_deck(
        self,
        track: Track,
        stream: ResolvedStream,
        gen: int,
        start_pos: float = 0.0,
    ) -> bool:
        with self._lock:
            if gen != self._play_gen or not self._running:
                return False
            self._armed_next = None
            self._current_stream = stream
            self.current_stream_quality = stream.quality
            vol = self.volume
            norm = self.loudness_normalization
            spd = self.speed
            deck = self._active_deck

        if deck is None:
            return False

        deck.ensure_alive(vol)
        opts = _format_mpv_options(
            media_title=self._media_title(track),
            headers=stream.headers,
            start_pos=start_pos,
            loudness_db=stream.loudness_db,
            normalize_loudness=norm,
        )
        deck.set_volume(vol)
        deck.send_command(["set_property", "speed", spd])
        deck.set_pause(False)
        entry_id = deck.load_file(stream.url, "replace", opts)
        if entry_id is not None:
            deck.active_entry_id = entry_id
        deck.armed_entry_id = None

        # Refresh PlayerBar now that quality & stream metadata are resolved!
        if self._is_gen_current(gen) and self.on_track_change:
            self.on_track_change(track)
        return True

    def _play_track_sync(
        self,
        track: Track,
        gen: Optional[int] = None,
        start_pos: float = 0.0,
        record_history: bool = True,
    ) -> None:
        """Resolve stream and load into mpv (runs in background thread)."""
        with self._lock:
            if not self._running:
                return
            if gen is None:
                self._play_gen += 1
                gen = self._play_gen
            elif gen != self._play_gen:
                return

            if record_history and self.current_track and self.current_track.id != track.id:
                self.history.append(self.current_track)

            self.current_track = track
            self._current_stream = None
            self._armed_next = None
            self.playback_pos = start_pos
            self.duration = float(track.duration_seconds or 0)
            self.is_buffering = True
            self.is_playing = True
            self.is_paused = False
            self.current_stream_quality = "Resolving…"

        if self.on_track_change:
            self.on_track_change(track)
        if self.on_state_change:
            self.on_state_change(True, False, True)

        try:
            stream = self._resolve_stream_or_cache(track.id)
            if not self._is_gen_current(gen):
                return
            self._load_into_active_deck(track, stream, gen, start_pos=start_pos)
            self._prefetch_next_track()
        except PermanentlyUnplayableError as exc:
            self._fail_playback(gen, str(exc))
        except Exception as exc:
            self._fail_playback(gen, f"Playback error: {exc}")

    @staticmethod
    def _media_title(track: Track) -> str:
        """Human-readable title exposed to mpv/MPRIS for a track."""
        if track.artist:
            return f"{track.title} · {track.artist}"
        return track.title

    def _protected_cache_ids(self) -> Set[str]:
        with self._lock:
            ids: Set[str] = set()
            if self.current_track:
                ids.add(self.current_track.id)
            if self.queue:
                ids.add(self.queue[0].id)
            return ids

    def _schedule_lookahead(self, gen: int) -> None:
        """Warm AudioCache for current + next track, and arm mpv gapless playlist when appropriate."""
        def _run() -> None:
            time.sleep(2.5)
            if not self._is_gen_current(gen):
                return
            with self._lock:
                cur_stream = self._current_stream
                next_t = self.queue[0] if self.queue else None
                loop_mode = self.loop_mode
                xfade = self.crossfade_seconds
                deck = self._active_deck
                protected = self._protected_cache_ids()

            if cur_stream is not None and self.cache is not None:
                self.cache.ensure_cached_async(cur_stream, protected_ids=protected)

            if next_t is None:
                return

            try:
                next_stream = self._resolve_stream_or_cache(next_t.id)
            except Exception:
                return

            if not self._is_gen_current(gen):
                return

            if self.cache is not None:
                self.cache.ensure_cached_async(next_stream, protected_ids=protected)

            # Arm gapless mpv playlist entry when crossfade is off and loop is OFF
            if xfade == 0 and loop_mode == LoopMode.OFF and deck is not None:
                with self._lock:
                    if (
                        gen != self._play_gen
                        or not self.queue
                        or self.queue[0].id != next_t.id
                        or self.loop_mode != LoopMode.OFF
                        or self.crossfade_seconds != 0
                    ):
                        return
                    if self._armed_next and self._armed_next[1].id == next_t.id:
                        return
                    norm = self.loudness_normalization

                opts = _format_mpv_options(
                    media_title=self._media_title(next_t),
                    headers=next_stream.headers,
                    loudness_db=next_stream.loudness_db,
                    normalize_loudness=norm,
                )
                entry_id = deck.load_file(next_stream.url, "append", opts)
                if entry_id is not None:
                    with self._lock:
                        if gen == self._play_gen and self.queue and self.queue[0].id == next_t.id:
                            self._armed_next = (gen, next_t, next_stream, entry_id)
                            deck.armed_entry_id = entry_id
                        else:
                            deck.clear_upcoming_playlist()

        threading.Thread(target=_run, daemon=True).start()

    def _prefetch_next_track(self) -> None:
        """Prefetch stream URL for the first item in queue safely under lock."""
        with self._lock:
            next_t = self.queue[0] if self.queue else None
        if next_t is None:
            return

        def _prefetch() -> None:
            try:
                stream = self._resolve_stream_or_cache(next_t.id)
                if self.cache is not None:
                    self.cache.ensure_cached_async(
                        stream, protected_ids=self._protected_cache_ids()
                    )
            except Exception:
                pass

        threading.Thread(target=_prefetch, daemon=True).start()

    def _disarm_upcoming_locked(self) -> None:
        self._armed_next = None
        if self._active_deck is not None:
            threading.Thread(
                target=self._active_deck.clear_upcoming_playlist, daemon=True
            ).start()

    # ------------------------------------------------------------------ #
    # Dual-MPV Equal-Power Crossfade controller
    # ------------------------------------------------------------------ #

    def _cancel_crossfade(self) -> None:
        self._crossfade_stop_ev.set()
        with self._lock:
            was_active = self._crossfade_active
            self._crossfade_active = False
            standby = self._standby_deck
            active = self._active_deck
            vol = self.volume
        if standby is not None and was_active:
            standby.send_command(["stop"])
            standby.set_volume(0)
        if active is not None and was_active:
            active.set_volume(vol)

    def _maybe_trigger_crossfade(self, gen: int, pos: float, dur: float) -> None:
        with self._lock:
            xfade = self.crossfade_seconds
            if (
                xfade <= 0
                or self._crossfade_active
                or self._crossfade_triggered_gen == gen
                or gen != self._play_gen
                or self.loop_mode != LoopMode.OFF
                or self.is_paused
                or not self.is_playing
                or not self.queue
                or dur <= xfade + 3.0
            ):
                return
            remaining = dur - pos
            if not (0.25 < remaining <= float(xfade)):
                return
            self._crossfade_triggered_gen = gen
            self._crossfade_active = True
            self._crossfade_stop_ev.clear()
            next_t = self.queue[0]
            outgoing_deck = self._active_deck
            outgoing_track = self.current_track

        if outgoing_deck is None:
            with self._lock:
                self._crossfade_active = False
            return

        self._crossfade_thread = threading.Thread(
            target=self._run_crossfade,
            args=(gen, xfade, outgoing_deck, outgoing_track, next_t),
            daemon=True,
        )
        self._crossfade_thread.start()

    def _run_crossfade(
        self,
        gen: int,
        xfade: int,
        outgoing_deck: _MpvDeck,
        outgoing_track: Optional[Track],
        incoming_track: Track,
    ) -> None:
        try:
            incoming_stream = self._resolve_stream_or_cache(incoming_track.id)
            if not self._is_gen_current(gen) or self._crossfade_stop_ev.is_set():
                return

            incoming_deck = self._ensure_standby_deck()
            with self._lock:
                norm = self.loudness_normalization
                user_vol = self.volume

            opts = _format_mpv_options(
                media_title=self._media_title(incoming_track),
                headers=incoming_stream.headers,
                loudness_db=incoming_stream.loudness_db,
                normalize_loudness=norm,
            )
            incoming_deck.set_volume(0.0)
            incoming_deck.set_pause(False)
            entry_id = incoming_deck.load_file(incoming_stream.url, "replace", opts)
            if entry_id is not None:
                incoming_deck.active_entry_id = entry_id

            # Allow incoming decoder a brief window to open before starting the gain ramp
            time.sleep(0.15)
            elapsed = 0.0
            handed_off = False
            total = float(max(1, xfade))

            while elapsed < total:
                if self._crossfade_stop_ev.is_set() or not self._running:
                    incoming_deck.send_command(["stop"])
                    incoming_deck.set_volume(0.0)
                    outgoing_deck.set_volume(self.volume)
                    return

                with self._lock:
                    paused = self.is_paused
                    user_vol = self.volume
                    if not handed_off and gen != self._play_gen:
                        incoming_deck.send_command(["stop"])
                        return

                if paused:
                    time.sleep(0.05)
                    continue

                t = min(1.0, elapsed / total)
                # Equal-power law (cos^2 + sin^2 = 1) + headroom trim (1 / sqrt(in + out))
                out_gain = math.cos(t * math.pi / 2.0)
                in_gain = math.sin(t * math.pi / 2.0)
                headroom = 1.0 / math.sqrt(max(1.0, out_gain + in_gain))

                outgoing_deck.set_volume(user_vol * out_gain * headroom)
                incoming_deck.set_volume(user_vol * in_gain * headroom)

                if not handed_off and t >= 0.5:
                    handed_off = True
                    dur_resp = incoming_deck.request_command(["get_property", "duration"], timeout=0.3)
                    in_dur = (
                        float(dur_resp["data"])
                        if isinstance(dur_resp, dict) and isinstance(dur_resp.get("data"), (int, float))
                        else float(incoming_track.duration_seconds or 0)
                    )
                    with self._lock:
                        if gen == self._play_gen:
                            if outgoing_track and outgoing_track.id != incoming_track.id:
                                self.history.append(outgoing_track)
                            if self.queue and self.queue[0].id == incoming_track.id:
                                self.queue.pop(0)
                            self._play_gen += 1
                            new_gen = self._play_gen
                            self._active_deck = incoming_deck
                            self._standby_deck = outgoing_deck
                            self.current_track = incoming_track
                            self._current_stream = incoming_stream
                            self.current_stream_quality = incoming_stream.quality
                            self.playback_pos = elapsed
                            self.duration = in_dur
                            self._armed_next = None
                        else:
                            new_gen = -1

                    if new_gen != -1:
                        if self.on_queue_change:
                            self.on_queue_change()
                        if self.on_track_change:
                            self.on_track_change(incoming_track)
                        self._schedule_lookahead(new_gen)

                time.sleep(CROSSFADE_TICK_SECONDS)
                elapsed += CROSSFADE_TICK_SECONDS

            # Complete blend: stop outgoing deck and restore full user volume on active deck
            outgoing_deck.send_command(["stop"])
            outgoing_deck.set_volume(0.0)
            incoming_deck.set_volume(self.volume)
        except Exception:
            outgoing_deck.set_volume(self.volume)
        finally:
            with self._lock:
                self._crossfade_active = False

    # ------------------------------------------------------------------ #
    # Public Player Controls
    # ------------------------------------------------------------------ #

    def play(self, track: Track) -> None:
        """Play a track immediately."""
        self._cancel_crossfade()
        with self._lock:
            self._play_gen += 1
            gen = self._play_gen
            self._disarm_upcoming_locked()
        threading.Thread(
            target=self._play_track_sync, args=(track,), kwargs={"gen": gen}, daemon=True
        ).start()

    def play_with_queue(self, track: Track, remaining: Optional[List[Track]] = None) -> None:
        """Atomically replace the queue and play ``track`` immediately."""
        self._cancel_crossfade()
        with self._lock:
            self.queue.clear()
            if remaining:
                self.queue.extend(remaining)
            self._play_gen += 1
            gen = self._play_gen
            self._disarm_upcoming_locked()
        if self.on_queue_change:
            self.on_queue_change()
        threading.Thread(
            target=self._play_track_sync, args=(track,), kwargs={"gen": gen}, daemon=True
        ).start()

    def append_queue(self, tracks: Any) -> None:
        """Append one or more tracks to the queue."""
        track_list: List[Track] = tracks if isinstance(tracks, list) else [tracks]
        if not track_list:
            return

        start_first: Optional[Track] = None
        gen: int = 0
        with self._lock:
            if not self.is_playing and self.current_track is None and not self.queue:
                start_first = track_list[0]
                self.queue.extend(track_list[1:])
                self._play_gen += 1
                gen = self._play_gen
            else:
                was_empty = len(self.queue) == 0
                self.queue.extend(track_list)
                if was_empty:
                    self._disarm_upcoming_locked()

        if self.on_queue_change:
            self.on_queue_change()

        if start_first is not None:
            threading.Thread(
                target=self._play_track_sync, args=(start_first,), kwargs={"gen": gen}, daemon=True
            ).start()
        else:
            self._prefetch_next_track()

    def play_next(self, tracks: Any) -> None:
        """Insert track(s) at the front of the queue."""
        track_list: List[Track] = tracks if isinstance(tracks, list) else [tracks]
        if not track_list:
            return

        with self._lock:
            for t in reversed(track_list):
                self.queue.insert(0, t)
            self._disarm_upcoming_locked()
            cur_gen = self._play_gen

        if self.on_queue_change:
            self.on_queue_change()
        self._prefetch_next_track()
        if self.is_playing:
            self._schedule_lookahead(cur_gen)

    def play_from_queue(self, index: int) -> None:
        """Play a specific track from the queue by index."""
        self._cancel_crossfade()
        track: Optional[Track] = None
        gen: int = 0
        with self._lock:
            if 0 <= index < len(self.queue):
                track = self.queue.pop(index)
                self._play_gen += 1
                gen = self._play_gen
                self._disarm_upcoming_locked()
        if track is not None:
            if self.on_queue_change:
                self.on_queue_change()
            threading.Thread(
                target=self._play_track_sync, args=(track,), kwargs={"gen": gen}, daemon=True
            ).start()

    def cycle_loop_mode(self) -> LoopMode:
        """Cycle loop mode: OFF -> LOOP_ONE -> STOP_AFTER_ONE -> OFF."""
        with self._lock:
            self.loop_mode = self.loop_mode.next_mode()
            mode = self.loop_mode
            if mode != LoopMode.OFF:
                self._disarm_upcoming_locked()
            cur_gen = self._play_gen
        if mode == LoopMode.OFF and self.is_playing:
            self._schedule_lookahead(cur_gen)
        return mode

    def cycle_crossfade(self) -> int:
        """Cycle crossfade duration (0 -> 3 -> 6 -> 0 seconds)."""
        with self._lock:
            idx = CROSSFADE_PRESETS.index(self.crossfade_seconds) if self.crossfade_seconds in CROSSFADE_PRESETS else 0
            self.crossfade_seconds = CROSSFADE_PRESETS[(idx + 1) % len(CROSSFADE_PRESETS)]
            xfade = self.crossfade_seconds
            if xfade > 0:
                self._disarm_upcoming_locked()
            cur_gen = self._play_gen
        if xfade == 0 and self.is_playing:
            self._schedule_lookahead(cur_gen)
        if self.on_crossfade_change:
            self.on_crossfade_change(xfade)
        return xfade

    def toggle_loudness_normalization(self) -> bool:
        """Toggle YouTube loudnessDb normalization on or off."""
        with self._lock:
            self.loudness_normalization = not self.loudness_normalization
            enabled = self.loudness_normalization
            stream = self._current_stream
            deck = self._active_deck
        if deck is not None:
            if enabled and stream is not None:
                gain_db = _compute_loudness_gain_db(stream.loudness_db)
                if gain_db is not None and abs(gain_db) >= 0.05:
                    deck.send_command(["af", "set", f"volume=volume={gain_db:.2f}dB"])
                else:
                    deck.send_command(["af", "set", ""])
            else:
                deck.send_command(["af", "set", ""])
        if self.on_loudness_change:
            self.on_loudness_change(enabled)
        return enabled

    def set_speed(self, rate: float) -> float:
        """Set playback speed clamped between 0.5x and 2.5x."""
        clamped = max(0.5, min(2.5, round(float(rate), 2)))
        with self._lock:
            self.speed = clamped
            active = self._active_deck
            standby = self._standby_deck
        if active is not None:
            active.send_command(["set_property", "speed", clamped])
        if standby is not None:
            standby.send_command(["set_property", "speed", clamped])
        if self.on_speed_change:
            self.on_speed_change(clamped)
        return clamped

    def cycle_speed(self) -> float:
        """Cycle playback speed through presets."""
        with self._lock:
            cur = self.speed
        idx = SPEED_PRESETS.index(cur) if cur in SPEED_PRESETS else 0
        return self.set_speed(SPEED_PRESETS[(idx + 1) % len(SPEED_PRESETS)])

    def next_track(self) -> None:
        """Advance to next track in queue or trigger autoplay."""
        self._cancel_crossfade()
        next_t: Optional[Track] = None
        last_track: Optional[Track] = None
        gen: int = 0
        with self._lock:
            if self.queue:
                next_t = self.queue.pop(0)
                self._play_gen += 1
                gen = self._play_gen
                self._disarm_upcoming_locked()
            else:
                last_track = self.current_track

        if next_t is not None:
            if self.on_queue_change:
                self.on_queue_change()
            threading.Thread(
                target=self._play_track_sync, args=(next_t,), kwargs={"gen": gen}, daemon=True
            ).start()
            return

        if self.autoplay and last_track and self.on_autoplay_trigger:
            self.on_autoplay_trigger(last_track)
        else:
            self.stop()

    def prev_track(self) -> None:
        """Seek to beginning if >3s elapsed; otherwise go to previous history track."""
        if self.playback_pos > 3.0 and self.is_playing:
            self.seek_to(0.0)
            return

        self._cancel_crossfade()
        prev_t: Optional[Track] = None
        gen: int = 0
        with self._lock:
            if self.history:
                prev_t = self.history.pop()
                if self.current_track:
                    self.queue.insert(0, self.current_track)
                self._play_gen += 1
                gen = self._play_gen
                self._disarm_upcoming_locked()

        if prev_t is not None:
            if self.on_queue_change:
                self.on_queue_change()
            threading.Thread(
                target=self._play_track_sync,
                args=(prev_t,),
                kwargs={"gen": gen, "record_history": False},
                daemon=True,
            ).start()
            return

        if self.is_playing:
            self.seek_to(0.0)

    def toggle_pause(self) -> None:
        """Toggle play / pause state safely (no-op when idle with empty queue)."""
        with self._lock:
            has_track = self.is_playing or self.current_track is not None
            has_queue = bool(self.queue)
        if not has_track:
            if has_queue:
                self.next_track()
            return
        self._send_command(["cycle", "pause"])

    def stop(self) -> None:
        """Stop playback and clear current track."""
        self._cancel_crossfade()
        with self._lock:
            self._play_gen += 1
            self._disarm_upcoming_locked()
            self.is_playing = False
            self.is_paused = False
            self.is_buffering = False
            self.current_track = None
            self._current_stream = None
            self.playback_pos = 0.0
            self.duration = 0.0

        self._send_command(["stop"])
        self._send_command(["set_property", "force-media-title", ""])
        self._send_command(["set_property", "pause", False])
        if self.on_track_change:
            self.on_track_change(None)
        if self.on_state_change:
            self.on_state_change(False, False, False)

    def seek(self, delta_seconds: float) -> None:
        if not self.is_playing:
            return
        self._send_command(["seek", delta_seconds, "relative"])

    def seek_to(self, seconds: float) -> None:
        if not self.is_playing:
            return
        self._send_command(["seek", max(0.0, seconds), "absolute"])

    def seek_percent(self, percent: float) -> None:
        if not self.is_playing:
            return
        clamped = max(0.0, min(100.0, percent))
        self._send_command(["seek", clamped, "absolute-percent"])

    def set_volume(self, vol: int) -> None:
        """Set volume clamped between 0 and 100."""
        with self._lock:
            self.volume = max(0, min(100, int(vol)))
            new_vol = self.volume
            crossfading = self._crossfade_active
        if not crossfading:
            self._send_command(["set_property", "volume", new_vol])
        if self.on_volume_change:
            self.on_volume_change(new_vol)

    def adjust_volume(self, delta: int) -> None:
        self.set_volume(self.volume + delta)

    def shuffle_queue(self) -> None:
        """Shuffle remaining queue."""
        with self._lock:
            random.shuffle(self.queue)
            self._disarm_upcoming_locked()
            cur_gen = self._play_gen
        if self.on_queue_change:
            self.on_queue_change()
        if self.is_playing:
            self._schedule_lookahead(cur_gen)

    def clear_queue(self) -> None:
        """Clear all tracks from queue."""
        with self._lock:
            self.queue.clear()
            self._disarm_upcoming_locked()
        if self.on_queue_change:
            self.on_queue_change()

    def remove_from_queue(self, index: int) -> None:
        """Remove a track at given index."""
        removed = False
        rearm = False
        cur_gen = 0
        with self._lock:
            if 0 <= index < len(self.queue):
                self.queue.pop(index)
                removed = True
                if index == 0:
                    self._disarm_upcoming_locked()
                    rearm = self.is_playing
                    cur_gen = self._play_gen
        if removed and self.on_queue_change:
            self.on_queue_change()
        if rearm:
            self._schedule_lookahead(cur_gen)

    def move_queue_item(self, old_index: int, new_index: int) -> bool:
        """Move a track inside the queue and re-arm gapless if index 0 changed."""
        rearm = False
        cur_gen = 0
        with self._lock:
            n = len(self.queue)
            if not (0 <= old_index < n and 0 <= new_index < n) or old_index == new_index:
                return False
            item = self.queue.pop(old_index)
            self.queue.insert(new_index, item)
            if old_index == 0 or new_index == 0:
                self._disarm_upcoming_locked()
                rearm = self.is_playing
                cur_gen = self._play_gen
        if self.on_queue_change:
            self.on_queue_change()
        if rearm:
            self._schedule_lookahead(cur_gen)
        return True

    def toggle_autoplay(self) -> bool:
        with self._lock:
            self.autoplay = not self.autoplay
            return self.autoplay

    def cleanup(self) -> None:
        """Clean up sockets, crossfade threads, and terminate mpv daemons."""
        with self._lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
            self._running = False
            self._play_gen += 1
            active = self._active_deck
            standby = self._standby_deck
            self._active_deck = None
            self._standby_deck = None

        self._crossfade_stop_ev.set()
        if active is not None:
            active.cleanup()
        if standby is not None:
            standby.cleanup()
