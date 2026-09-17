"""
spectrum.py - Real-time audio spectrum source for ame.

Spawns ``cava`` with a private raw-output config, reads its ASCII band values
from stdout on a background thread and forwards them to a callback. cava taps
the PipeWire/PulseAudio monitor, so it visualizes playback without touching
mpv's audio path.
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
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._config_path: Optional[str] = None

    @staticmethod
    def available() -> bool:
        return shutil.which("cava") is not None

    def start(self) -> bool:
        """Launch cava. Returns False when it is unavailable or fails to start."""
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
            return False

        self._running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        return True

    def _write_config(self) -> None:
        fd, path = tempfile.mkstemp(prefix="ame_cava_", suffix=".conf")
        os.close(fd)
        self._config_path = path
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_CAVA_CONFIG.format(bars=self.bars, framerate=self.framerate))

    def _read_loop(self) -> None:
        stream = self.proc.stdout if self.proc else None
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

            if self.on_update:
                try:
                    self.on_update(values)
                except Exception:
                    pass

    def stop(self) -> None:
        self._running = False
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
        if self._config_path and os.path.exists(self._config_path):
            try:
                os.remove(self._config_path)
            except OSError:
                pass
        self._config_path = None
