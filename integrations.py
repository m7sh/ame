"""
integrations.py - Desktop Linux integrations: MPRIS2 (playerctl/Waybar) & Discord Rich Presence.

Both integrations degrade gracefully when D-Bus or Discord IPC is absent,
so ``ame`` stays completely zero-config and portable.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests

from api import Track
from artwork import NOTIFICATION_ART_PX, PLAYER_ART_PX, sized_artwork_url

# Default Discord Application ID fallback; user can override via env.
DEFAULT_DISCORD_CLIENT_ID = os.environ.get("AME_DISCORD_CLIENT_ID", "1341440284083650660")


class MPRISController:
    """Spawns ``mpris_bridge.py`` under system python3 when dbus/gi are available."""

    def __init__(
        self,
        on_command: Optional[Callable[[Dict[str, Any]], None]] = None,
        enabled: bool = True,
    ) -> None:
        self.on_command = on_command
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._started = False
        if enabled:
            self.start()

    def start(self) -> bool:
        if os.environ.get("AME_DISABLE_MPRIS") == "1":
            return False
        bridge = Path(__file__).resolve().parent / "mpris_bridge.py"
        if not bridge.exists() or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            return False
        py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3"
        try:
            proc = subprocess.Popen(
                [py, str(bridge)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except Exception:
            return False
        with self._lock:
            self._proc = proc
            self._started = True
        threading.Thread(target=self._read_loop, args=(proc,), daemon=True).start()
        return True

    def _read_loop(self, proc: subprocess.Popen) -> None:
        if proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if self.on_command and isinstance(msg, dict):
                    self.on_command(msg)
        except Exception:
            pass

    def _send(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
        except Exception:
            pass

    def update_track(self, track: Optional[Track]) -> None:
        if track is None:
            self._send({"track": None})
            return
        self._send(
            {
                "track": {
                    "id": track.id,
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "duration_s": track.duration_seconds,
                    "art_url": sized_artwork_url(track.thumbnail, NOTIFICATION_ART_PX) if track.thumbnail else "",
                }
            }
        )

    def update_state(self, is_playing: bool, is_paused: bool) -> None:
        status = "Paused" if is_paused else ("Playing" if is_playing else "Stopped")
        self._send({"status": status})

    def update_position(self, pos_seconds: float) -> None:
        self._send({"position_us": int(max(0.0, pos_seconds) * 1_000_000)})

    def update_volume(self, volume: int) -> None:
        self._send({"volume": volume})

    def update_loop(self, loop_mode: str) -> None:
        mpris_loop = "Track" if loop_mode == "loop_one" else "None"
        self._send({"loop": mpris_loop})

    def update_speed(self, speed: float) -> None:
        self._send({"speed": speed})

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class DiscordPresence:
    """Zero-dependency local Discord IPC Rich Presence client."""

    OP_HANDSHAKE = 0
    OP_FRAME = 1
    OP_CLOSE = 2

    def __init__(self, client_id: str = DEFAULT_DISCORD_CLIENT_ID) -> None:
        self.client_id = client_id
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._last_reconnect_attempt = 0.0

    @staticmethod
    def _candidate_sockets() -> list[str]:
        roots = []
        for env in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP"):
            val = os.environ.get(env)
            if val:
                roots.append(Path(val))
        roots.append(Path("/tmp"))
        candidates: list[str] = []
        for root in roots:
            for idx in range(10):
                candidates.append(str(root / f"discord-ipc-{idx}"))
                candidates.append(str(root / "app" / "com.discordapp.Discord" / f"discord-ipc-{idx}"))
        return candidates

    @staticmethod
    def encode_packet(opcode: int, payload: Dict[str, Any]) -> bytes:
        data = json.dumps(payload).encode("utf-8")
        return struct.pack("<II", opcode, len(data)) + data

    def _connect_locked(self) -> bool:
        if os.environ.get("AME_DISABLE_DISCORD") == "1":
            return False
        if self._sock is not None:
            return True
        now = time.monotonic()
        if now - self._last_reconnect_attempt < 15.0:
            return False
        self._last_reconnect_attempt = now

        for path in self._candidate_sockets():
            if not os.path.exists(path):
                continue
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(1.0)
                sock.connect(path)
                sock.sendall(
                    self.encode_packet(
                        self.OP_HANDSHAKE,
                        {"v": 1, "client_id": self.client_id},
                    )
                )
                # Read handshake reply header + body if available
                hdr = sock.recv(8)
                if len(hdr) == 8:
                    _, length = struct.unpack("<II", hdr)
                    if 0 < length < 65536:
                        sock.recv(length)
                self._sock = sock
                return True
            except OSError:
                try:
                    sock.close()
                except Exception:
                    pass
        return False

    def update(
        self,
        track: Optional[Track],
        is_playing: bool,
        is_paused: bool,
        pos_seconds: float = 0.0,
    ) -> None:
        """Update Discord Rich Presence activity asynchronously."""
        threading.Thread(
            target=self._update_sync,
            args=(track, is_playing, is_paused, pos_seconds),
            daemon=True,
        ).start()

    def _update_sync(
        self,
        track: Optional[Track],
        is_playing: bool,
        is_paused: bool,
        pos_seconds: float,
    ) -> None:
        with self._lock:
            if not self._connect_locked() or self._sock is None:
                return
            activity: Optional[Dict[str, Any]] = None
            if track is not None and (is_playing or is_paused):
                details = track.title[:128]
                state = f"by {track.artist}"[:128] if track.artist else "ame"
                assets: Dict[str, Any] = {
                    "large_text": (track.album or track.title)[:128],
                }
                art_url = sized_artwork_url(track.thumbnail, PLAYER_ART_PX) if track.thumbnail else ""
                if art_url:
                    assets["large_image"] = art_url
                activity = {
                    "type": 2,  # Listening
                    "details": details,
                    "state": state,
                    "assets": assets,
                }
                if is_playing and not is_paused and track.duration_seconds > 0:
                    now = int(time.time())
                    start_ts = max(0, now - int(pos_seconds))
                    end_ts = start_ts + int(track.duration_seconds)
                    activity["timestamps"] = {"start": start_ts, "end": end_ts}

            frame = {
                "cmd": "SET_ACTIVITY",
                "args": {
                    "pid": os.getpid(),
                    "activity": activity,
                },
                "nonce": uuid.uuid4().hex,
            }
            try:
                self._sock.sendall(self.encode_packet(self.OP_FRAME, frame))
            except OSError:
                try:
                    self._sock.close()
                except Exception:
                    pass
                self._sock = None

    def close(self) -> None:
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class ListenBrainzScrobbler:
    """Zero-OAuth ListenBrainz ``playing_now`` & ``single`` listen submitter."""

    API_URL = "https://api.listenbrainz.org/1/submit-listens"

    def __init__(
        self,
        token: str = "",
        enabled: bool = True,
        session: Optional[requests.Session] = None,
    ) -> None:
        env_token = os.environ.get("AME_LISTENBRAINZ_TOKEN", "").strip()
        self.token = (token or env_token).strip()
        self.enabled = bool(enabled)
        self._session = session or requests.Session()
        self._scrobbled_track_key: Optional[str] = None
        self._lock = threading.Lock()

    def configure(self, token: str = "", enabled: bool = True) -> None:
        env_token = os.environ.get("AME_LISTENBRAINZ_TOKEN", "").strip()
        with self._lock:
            self.token = (token or env_token).strip()
            self.enabled = bool(enabled)

    def _is_active(self) -> bool:
        if os.environ.get("AME_DISABLE_SCROBBLE") == "1":
            return False
        return bool(self.enabled and self.token)

    @staticmethod
    def build_payload(
        listen_type: str,
        track: Track,
        listened_at: Optional[int] = None,
        pos_seconds: float = 0.0,
    ) -> Dict[str, Any]:
        primary_artist = (track.artist or "Unknown Artist").split(",")[0].strip() or "Unknown Artist"
        info: Dict[str, Any] = {"submission_client": "ame"}
        if track.duration_seconds > 0:
            info["duration_ms"] = int(track.duration_seconds) * 1000
        if listen_type == "playing_now":
            info["position_ms"] = int(max(0.0, pos_seconds) * 1000)

        meta: Dict[str, Any] = {
            "artist_name": primary_artist,
            "track_name": track.title,
            "additional_info": info,
        }
        if track.album:
            meta["release_name"] = track.album

        entry: Dict[str, Any] = {"track_metadata": meta}
        if listen_type == "single":
            entry["listened_at"] = int(listened_at or time.time())
        return {"listen_type": listen_type, "payload": [entry]}

    def _post_async(self, payload: Dict[str, Any], token: str) -> None:
        def _run() -> None:
            try:
                self._session.post(
                    self.API_URL,
                    json=payload,
                    headers={
                        "Authorization": f"Token {token}",
                        "Content-Type": "application/json",
                    },
                    timeout=5.0,
                )
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def submit_playing_now(self, track: Optional[Track], pos_seconds: float = 0.0) -> bool:
        if track is None or not self._is_active():
            return False
        with self._lock:
            self._scrobbled_track_key = None
            token = self.token
        payload = self.build_payload("playing_now", track, pos_seconds=pos_seconds)
        self._post_async(payload, token)
        return True

    def maybe_scrobble(
        self,
        track: Optional[Track],
        listened_seconds: float,
        duration_seconds: float = 0.0,
    ) -> bool:
        if track is None or not self._is_active():
            return False
        dur = duration_seconds if duration_seconds > 0 else float(track.duration_seconds or 0)
        threshold = max(30.0, min(dur * 0.5, 240.0)) if dur > 0 else 60.0
        if listened_seconds < threshold:
            return False

        key = f"{track.id}:{int(time.time() // 300)}"
        with self._lock:
            if self._scrobbled_track_key == track.id:
                return False
            self._scrobbled_track_key = track.id
            token = self.token
        _ = key
        payload = self.build_payload(
            "single",
            track,
            listened_at=int(time.time() - listened_seconds),
        )
        self._post_async(payload, token)
        return True
