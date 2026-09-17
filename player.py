"""
player.py - Headless MPV IPC Client & Audio Queue Controller.

Manages headless mpv playback via JSON-IPC socket, track queue state machine,
stream prefetching, volume control, seeking, and continuous autoplay triggers.
"""

import os
import time
import json
import uuid
import random
import socket
import atexit
import threading
import subprocess
from typing import Optional, List, Callable, Dict, Any
from api import Track, StreamResolver


class MPVPlayer:
    """
    Headless mpv background audio player controlled over UNIX domain socket IPC.
    """

    def __init__(self, resolver: Optional[StreamResolver] = None):
        self.resolver = resolver or StreamResolver()
        self.sock_path = f"/tmp/ame_mpv_{os.getpid()}_{uuid.uuid4().hex[:6]}.sock"
        self.proc: Optional[subprocess.Popen] = None
        self.sock: Optional[socket.socket] = None
        self.sock_file = None

        self._lock = threading.Lock()
        self._running = False
        self._reader_thread: Optional[threading.Thread] = None

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

        # UI Callbacks
        self.on_track_change: Optional[Callable[[Optional[Track]], None]] = None
        self.on_state_change: Optional[Callable[[bool, bool, bool], None]] = None  # playing, paused, buffering
        self.on_progress: Optional[Callable[[float, float], None]] = None  # pos, duration
        self.on_queue_change: Optional[Callable[[], None]] = None
        self.on_autoplay_trigger: Optional[Callable[[Track], None]] = None
        self.on_message: Optional[Callable[[str], None]] = None

        # Start mpv process and IPC connection
        self._start_mpv()
        atexit.register(self.cleanup)

    def _start_mpv(self) -> None:
        """Spawn headless mpv daemon and connect IPC socket."""
        if os.path.exists(self.sock_path):
            try:
                os.remove(self.sock_path)
            except OSError:
                pass

        cmd = [
            "mpv",
            "--no-video",
            "--idle",
            f"--input-ipc-server={self.sock_path}",
            "--gapless-audio=yes",
            f"--volume={self.volume}",
            "--ytdl=no",  # We provide direct stream URLs from yt-dlp
        ]

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise RuntimeError("mpv executable not found. Please ensure mpv is installed.")

        # Wait for socket to become available
        connected = False
        for _ in range(50):
            if os.path.exists(self.sock_path):
                try:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(self.sock_path)
                    self.sock_file = self.sock.makefile("r", encoding="utf-8")
                    connected = True
                    break
                except (socket.error, OSError):
                    time.sleep(0.05)
            time.sleep(0.05)

        if not connected or not self.sock:
            raise RuntimeError("Failed to connect to headless mpv IPC socket.")

        self._running = True
        self._reader_thread = threading.Thread(target=self._ipc_listener, daemon=True)
        self._reader_thread.start()

        # Observe needed properties
        self._send_command(["observe_property", 1, "pause"])
        self._send_command(["observe_property", 2, "time-pos"])
        self._send_command(["observe_property", 3, "duration"])
        self._send_command(["observe_property", 4, "volume"])

    def _send_command(self, cmd: List[Any], request_id: Optional[int] = None) -> None:
        """Send JSON command to mpv over IPC socket."""
        with self._lock:
            if not self.sock or not self._running:
                return
            payload: Dict[str, Any] = {"command": cmd}
            if request_id is not None:
                payload["request_id"] = request_id
            try:
                data = json.dumps(payload).encode("utf-8") + b"\n"
                self.sock.sendall(data)
            except (socket.error, BrokenPipeError, OSError):
                pass

    def _ipc_listener(self) -> None:
        """Background thread reading events and property updates from mpv."""
        while self._running and self.sock_file:
            try:
                line = self.sock_file.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                msg = json.loads(line)
                event = msg.get("event")

                if event == "property-change":
                    prop_name = msg.get("name")
                    data = msg.get("data")

                    if prop_name == "time-pos" and data is not None:
                        self.playback_pos = float(data)
                        if self.on_progress:
                            self.on_progress(self.playback_pos, self.duration)

                    elif prop_name == "duration" and data is not None:
                        self.duration = float(data)
                        if self.on_progress:
                            self.on_progress(self.playback_pos, self.duration)

                    elif prop_name == "pause" and data is not None:
                        self.is_paused = bool(data)
                        if self.on_state_change:
                            self.on_state_change(self.is_playing, self.is_paused, self.is_buffering)

                    elif prop_name == "volume" and data is not None:
                        self.volume = int(round(float(data)))

                elif event == "start-file":
                    self.is_buffering = True
                    self.is_playing = True
                    if self.on_state_change:
                        self.on_state_change(self.is_playing, self.is_paused, self.is_buffering)

                elif event == "file-loaded":
                    self.is_buffering = False
                    self.is_playing = True
                    if self.on_state_change:
                        self.on_state_change(self.is_playing, self.is_paused, self.is_buffering)

                elif event == "end-file":
                    reason = msg.get("reason")
                    if reason == "eof":
                        # Song finished naturally
                        threading.Thread(target=self._handle_track_finished, daemon=True).start()

            except (json.JSONDecodeError, socket.error, OSError):
                break

    def _handle_track_finished(self) -> None:
        """Called when a track finishes naturally."""
        self.playback_pos = 0.0
        self.duration = 0.0
        if self.queue:
            next_t = self.queue.pop(0)
            if self.on_queue_change:
                self.on_queue_change()
            self._play_track_sync(next_t)
        else:
            # Queue is empty - check autoplay
            last_track = self.current_track
            self.is_playing = False
            self.current_track = None
            if self.on_state_change:
                self.on_state_change(False, False, False)
            if self.on_track_change:
                self.on_track_change(None)

            if self.autoplay and last_track and self.on_autoplay_trigger:
                self.on_autoplay_trigger(last_track)

    def _play_track_sync(self, track: Track) -> None:
        """Resolve stream and load into mpv (runs in thread)."""
        if self.current_track and self.current_track.id != track.id:
            self.history.append(self.current_track)

        self.current_track = track
        self.playback_pos = 0.0
        self.duration = track.duration_seconds or 0.0
        self.is_buffering = True
        self.is_playing = True
        self.is_paused = False

        if self.on_track_change:
            self.on_track_change(track)
        if self.on_state_change:
            self.on_state_change(True, False, True)

        try:
            stream_url, quality = self.resolver.resolve(track.id)
            self.current_stream_quality = quality

            # Send loadfile command to mpv
            self._send_command(["loadfile", stream_url, "replace"])

            # Pre-resolve stream for next track in queue if available
            self._prefetch_next_track()

        except Exception as e:
            self.is_buffering = False
            self.is_playing = False
            if self.on_state_change:
                self.on_state_change(False, False, False)
            if self.on_message:
                self.on_message(f"Playback error: {e}")

    def _prefetch_next_track(self) -> None:
        """Prefetch stream URL for the first item in queue in background."""
        if self.queue:
            next_t = self.queue[0]

            def _prefetch():
                try:
                    self.resolver.resolve(next_t.id)
                except Exception:
                    pass

            threading.Thread(target=_prefetch, daemon=True).start()

    # --- Public Player Controls ---

    def play(self, track: Track) -> None:
        """Play a track immediately."""
        threading.Thread(target=self._play_track_sync, args=(track,), daemon=True).start()

    def append_queue(self, tracks: Any) -> None:
        """Append one or more tracks to the queue."""
        track_list: List[Track] = tracks if isinstance(tracks, list) else [tracks]
        if not track_list:
            return

        with self._lock:
            # If nothing currently playing and queue empty, start playing the first track
            if not self.is_playing and self.current_track is None and not self.queue:
                first = track_list[0]
                self.queue.extend(track_list[1:])
                if self.on_queue_change:
                    self.on_queue_change()
                self.play(first)
                return

            self.queue.extend(track_list)

        if self.on_queue_change:
            self.on_queue_change()
        self._prefetch_next_track()

    def play_next(self, tracks: Any) -> None:
        """Insert track(s) at the front of the queue."""
        track_list: List[Track] = tracks if isinstance(tracks, list) else [tracks]
        if not track_list:
            return

        with self._lock:
            for t in reversed(track_list):
                self.queue.insert(0, t)

        if self.on_queue_change:
            self.on_queue_change()
        self._prefetch_next_track()

    def play_from_queue(self, index: int) -> None:
        """Play a specific track from the queue by index."""
        with self._lock:
            if 0 <= index < len(self.queue):
                # Remove up to index and play
                track = self.queue.pop(index)
                if self.on_queue_change:
                    self.on_queue_change()
                self.play(track)

    def next_track(self) -> None:
        """Advance to next track in queue or trigger autoplay."""
        with self._lock:
            if self.queue:
                next_t = self.queue.pop(0)
                if self.on_queue_change:
                    self.on_queue_change()
                self.play(next_t)
                return

        # Queue empty
        last_track = self.current_track
        if self.autoplay and last_track and self.on_autoplay_trigger:
            self.on_autoplay_trigger(last_track)
        else:
            self.stop()

    def prev_track(self) -> None:
        """Seek to beginning if >3s elapsed; otherwise go to previous history track."""
        if self.playback_pos > 3.0:
            self.seek_to(0.0)
            return

        with self._lock:
            if self.history:
                prev_t = self.history.pop()
                if self.current_track:
                    self.queue.insert(0, self.current_track)
                if self.on_queue_change:
                    self.on_queue_change()
                self.play(prev_t)
                return

        self.seek_to(0.0)

    def toggle_pause(self) -> None:
        """Toggle play / pause state."""
        if not self.is_playing and self.queue:
            self.next_track()
            return
        self._send_command(["cycle", "pause"])

    def stop(self) -> None:
        """Stop playback and clear current track."""
        self._send_command(["stop"])
        self.is_playing = False
        self.is_paused = False
        self.is_buffering = False
        self.current_track = None
        self.playback_pos = 0.0
        self.duration = 0.0
        if self.on_track_change:
            self.on_track_change(None)
        if self.on_state_change:
            self.on_state_change(False, False, False)

    def seek(self, delta_seconds: float) -> None:
        """Relative seek forward (+) or backward (-)."""
        self._send_command(["seek", delta_seconds, "relative"])

    def seek_to(self, seconds: float) -> None:
        """Absolute seek to seconds."""
        self._send_command(["seek", seconds, "absolute"])

    def seek_percent(self, percent: float) -> None:
        """Absolute seek to percentage (0.0 - 100.0)."""
        clamped = max(0.0, min(100.0, percent))
        self._send_command(["seek", clamped, "absolute-percent"])

    def set_volume(self, vol: int) -> None:
        """Set volume clamped between 0 and 100."""
        self.volume = max(0, min(100, vol))
        self._send_command(["set_property", "volume", self.volume])

    def adjust_volume(self, delta: int) -> None:
        """Adjust volume by +/- delta."""
        self.set_volume(self.volume + delta)

    def shuffle_queue(self) -> None:
        """Shuffle remaining queue."""
        with self._lock:
            random.shuffle(self.queue)
        if self.on_queue_change:
            self.on_queue_change()

    def clear_queue(self) -> None:
        """Clear all tracks from queue."""
        with self._lock:
            self.queue.clear()
        if self.on_queue_change:
            self.on_queue_change()

    def remove_from_queue(self, index: int) -> None:
        """Remove a track at given index."""
        with self._lock:
            if 0 <= index < len(self.queue):
                self.queue.pop(index)
        if self.on_queue_change:
            self.on_queue_change()

    def toggle_autoplay(self) -> bool:
        """Toggle continuous autoplay."""
        self.autoplay = not self.autoplay
        return self.autoplay

    def cleanup(self) -> None:
        """Clean up sockets and terminate mpv daemon."""
        self._running = False
        try:
            self._send_command(["quit"])
        except Exception:
            pass

        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

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
