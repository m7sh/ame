"""
spectrum.py - Real-time audio spectrum source for ame.

Spawns ``cava`` with a private raw-output config, reads its ASCII band values
from stdout on a background thread and forwards them to a callback. cava taps
the PipeWire/PulseAudio monitor, so it visualizes playback without touching
mpv's audio path. Start/stop are idempotent so cava idles with 0 CPU whenever
audio is paused/stopped or the visualizer is hidden.
"""

import os
import shutil
import subprocess
import tempfile
import threading
from typing import Callable, List, Optional


_CAVA_CONFIG = """\
[general]
bars = {bars}
framerate = {framerate}
autosens = 1
sensitivity = 100
lower_cutoff_freq = 50
higher_cutoff_freq = 12000

[output]
method = raw
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = 1000

[input]
method = pipewire
source = auto
"""


class CavaSpectrum:
    """Thin wrapper around a cava subprocess producing normalized bands."""

    def __init__(
        self,
        bars: int = 48,
        framerate: int = 30,
        on_update: Optional[Callable[[List[float]], None]] = None,
    ) -> None:
        self.bars = bars
        self.framerate = framerate
        self.on_update = on_update
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._config_path: Optional[str] = None

    @staticmethod
    def available() -> bool:
        return shutil.which("cava") is not None

    @property
    def running(self) -> bool:
        return self._running and self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        """Launch cava if not already running. Returns False when unavailable."""
        with self._lock:
            if self.running:
                return True
            if not self.available():
                return False

            self._write_config()
            try:
                self.proc = subprocess.Popen(
                    ["cava", "-p", self._config_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except Exception:
                self._remove_config()
                return False

            self._running = True
            self._thread = threading.Thread(target=self._read_loop, daemon=True)
            self._thread.start()
            return True

    def _write_config(self) -> None:
        self._remove_config()
        fd, path = tempfile.mkstemp(prefix="ame_cava_", suffix=".conf")
        os.close(fd)
        self._config_path = path
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_CAVA_CONFIG.format(bars=self.bars, framerate=self.framerate))

    def _remove_config(self) -> None:
        if self._config_path and os.path.exists(self._config_path):
            try:
                os.remove(self._config_path)
            except OSError:
                pass
        self._config_path = None

    def _read_loop(self) -> None:
        proc = self.proc
        stream = proc.stdout if proc else None
        while self._running and stream is not None:
            try:
                line = stream.readline()
            except Exception:
                break
            if not line:
                break

            line = line.strip().rstrip(";")
            if not line:
                continue

            try:
                values = [max(0.0, min(1.0, int(part) / 1000.0)) for part in line.split(";")]
            except ValueError:
                continue

            if self._running and self.on_update:
                try:
                    self.on_update(values)
                except Exception:
                    pass

    def stop(self) -> None:
        with self._lock:
            self._running = False
            proc = self.proc
            self.proc = None
            thread = self._thread
            self._thread = None

        if proc:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if thread and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=0.5)

        self._remove_config()
