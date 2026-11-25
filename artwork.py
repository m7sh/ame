"""
artwork.py - Album artwork URL upgrading, caching, and terminal image rendering.

Uses size-aware Google artwork scaling (``sized_artwork_url``) paired with
a bounded on-disk + in-memory PIL cache and ``textual-image`` rendering:
- Native Sixel in foot / xterm / mlterm / wezterm
- Terminal Graphics Protocol (TGP) in Kitty / Ghostty
- 24-bit Truecolor Unicode half-cells (▀) fallback inside tmux or plain PTYs
"""

from __future__ import annotations

import colorsys
import hashlib
import io
import os
import re
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests
from PIL import Image as PILImage

# Guard textual_image's import-time terminal probe in non-interactive or dumb
# terminals (e.g. pytest, CI, pipes) so startup never blocks 2s on DA1 timeout.
_HAS_TEXTUAL_IMAGE = False
_IMAGE_BACKEND_NAME = "placeholder"

try:
    from textual_image import _terminal as _ti_terminal

    if (
        os.environ.get("TERM") == "dumb"
        or os.environ.get("AME_DISABLE_TERMINAL_PROBE") == "1"
        or not sys.__stdout__
        or not sys.__stdout__.isatty()
    ):
        _ti_terminal.probe_terminal._result = _ti_terminal.TerminalCapabilities(  # type: ignore[attr-defined]
            _ti_terminal.CellSize(10, 20),
            sixel=False,
            tgp=False,
        )

    from textual_image._terminal import get_cell_size, probe_terminal
    from textual_image.widget import HalfcellImage as _HalfcellWidget
    from textual_image.widget import Image as _AutoImageWidget
    from textual_image.widget.sixel import Image as _SixelWidget

    _HAS_TEXTUAL_IMAGE = True
    _caps = probe_terminal()
    if _caps.sixel:
        _IMAGE_BACKEND_NAME = "sixel"
    elif _caps.tgp:
        _IMAGE_BACKEND_NAME = "tgp"
    else:
        _IMAGE_BACKEND_NAME = "halfcell"
except Exception:
    _AutoImageWidget = None  # type: ignore[assignment]
    _HalfcellWidget = None  # type: ignore[assignment]
    _SixelWidget = None  # type: ignore[assignment]


_GOOGLE_SIZE_RE = re.compile(r"=([ws]\d+[^/?#&]*)")
_DEFAULT_ART_DIR = Path.home() / ".cache" / "ame" / "artwork"
_MAX_DISK_FILES = 160
_MAX_MEM_ITEMS = 32


def sized_artwork_url(url: str, size: int = 600) -> str:
    """Return a high-resolution square URL when the host supports Google sizing params.

    Scale artwork URL to ``size`` px for ``lh3.googleusercontent`` / ``ggpht``.
    and ``yt3.ggpht`` thumbnails while leaving ``i.ytimg.com`` URLs intact (or
    upgrading ``default``/``mqdefault`` to ``hqdefault``).
    """
    if not url:
        return ""
    clean = url.strip()
    if not clean:
        return ""

    if "googleusercontent.com" in clean or "ggpht.com" in clean:
        if _GOOGLE_SIZE_RE.search(clean):
            return _GOOGLE_SIZE_RE.sub(f"=w{size}-h{size}-l90-rj", clean, count=1)
        if "=" not in clean.rsplit("/", 1)[-1]:
            return f"{clean}=w{size}-h{size}-l90-rj"
        return clean

    if "i.ytimg.com" in clean or "img.youtube.com" in clean:
        for low in ("/default.jpg", "/mqdefault.jpg"):
            if low in clean:
                return clean.replace(low, "/hqdefault.jpg")
    return clean


def image_backend_name() -> str:
    """Return active terminal image backend: 'sixel', 'tgp', 'halfcell', or 'placeholder'."""
    return _IMAGE_BACKEND_NAME


def cell_aspect_ratio() -> float:
    """Return terminal cell height / width ratio (typically ~2.0) for square sizing."""
    if not _HAS_TEXTUAL_IMAGE:
        return 2.0
    try:
        cs = get_cell_size()
        if cs.width > 0 and cs.height > 0:
            return cs.height / cs.width
    except Exception:
        pass
    return 2.0


def square_cell_dimensions(
    max_cols: int,
    max_rows: int,
    aspect_ratio: Optional[float] = None,
) -> Tuple[int, int]:
    """Compute (cols, rows) that form a visually square box in terminal cells."""
    if max_cols <= 0 or max_rows <= 0:
        return (0, 0)
    ratio = aspect_ratio if aspect_ratio and aspect_ratio > 0 else cell_aspect_ratio()
    # A square of `rows` rows has pixel height `rows * ch`, so it needs
    # `cols = round(rows * (ch / cw))` columns.
    rows = max_rows
    cols = max(2, int(round(rows * ratio)))
    if cols > max_cols:
        cols = max_cols
        rows = max(1, int(round(cols / ratio)))
    return (cols, rows)


def create_album_image_widget(**kwargs: Any) -> Any:
    """Create the best terminal image widget for the maximized album window."""
    if not _HAS_TEXTUAL_IMAGE or _AutoImageWidget is None:
        return None
    return _AutoImageWidget(**kwargs)


def create_mini_image_widget(**kwargs: Any) -> Any:
    """Create a flicker-free Halfcell widget for the compact bottom mini-player."""
    if not _HAS_TEXTUAL_IMAGE or _HalfcellWidget is None:
        return None
    return _HalfcellWidget(**kwargs)


def is_sixel_backend() -> bool:
    return _IMAGE_BACKEND_NAME == "sixel"


class ArtworkCache:
    """Thread-safe bounded in-memory + on-disk artwork cache."""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        session: Optional[requests.Session] = None,
        max_disk_files: int = _MAX_DISK_FILES,
        max_mem_items: int = _MAX_MEM_ITEMS,
    ) -> None:
        self.cache_dir = cache_dir or _DEFAULT_ART_DIR
        self.max_disk_files = max_disk_files
        self.max_mem_items = max_mem_items
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._mem: OrderedDict[str, PILImage.Image] = OrderedDict()
        self._inflight: Dict[str, threading.Event] = {}

    def _key(self, track_id: str, url: str) -> str:
        digest = hashlib.sha256(f"{track_id}:{url}".encode("utf-8")).hexdigest()[:24]
        return digest

    def _disk_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.jpg"

    def get_cached(self, track_id: str, url: str, size: int = 600) -> Optional[PILImage.Image]:
        """Return cached PIL Image from memory or disk without network I/O."""
        target_url = sized_artwork_url(url, size=size)
        if not target_url:
            return None
        key = self._key(track_id, target_url)

        with self._lock:
            img = self._mem.get(key)
            if img is not None:
                self._mem.move_to_end(key)
                return img

        path = self._disk_path(key)
        if path.exists():
            try:
                with PILImage.open(path) as loaded:
                    rgb = loaded.convert("RGB")
                try:
                    os.utime(path, None)
                except OSError:
                    pass
                self._store_mem(key, rgb)
                return rgb
            except Exception:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        return None

    def fetch(self, track_id: str, url: str, size: int = 600) -> Optional[PILImage.Image]:
        """Fetch artwork from cache or HTTP, coalescing concurrent requests."""
        target_url = sized_artwork_url(url, size=size)
        if not target_url:
            return None

        cached = self.get_cached(track_id, url, size=size)
        if cached is not None:
            return cached

        key = self._key(track_id, target_url)
        waiter: Optional[threading.Event] = None
        is_leader = False

        with self._lock:
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
            if key in self._inflight:
                waiter = self._inflight[key]
            else:
                waiter = threading.Event()
                self._inflight[key] = waiter
                is_leader = True

        if not is_leader:
            waiter.wait(timeout=8.0)
            return self.get_cached(track_id, url, size=size)

        try:
            resp = self._session.get(
                target_url,
                timeout=6.0,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code != 200 or not resp.content:
                return None

            with PILImage.open(io.BytesIO(resp.content)) as raw:
                rgb = raw.convert("RGB")
                # Center-crop non-square 16:9 YouTube thumbs only if mildly wide,
                # preserving full artwork when nearly square.
                w, h = rgb.size
                if w > 0 and h > 0 and w / h > 1.25:
                    side = min(w, h)
                    left = (w - side) // 2
                    top = (h - side) // 2
                    rgb = rgb.crop((left, top, left + side, top + side))

            self._store_disk(key, rgb)
            self._store_mem(key, rgb)
            return rgb
        except Exception:
            return None
        finally:
            with self._lock:
                ev = self._inflight.pop(key, None)
                if ev is not None:
                    ev.set()

    def _store_mem(self, key: str, img: PILImage.Image) -> None:
        with self._lock:
            self._mem[key] = img
            self._mem.move_to_end(key)
            while len(self._mem) > self.max_mem_items:
                self._mem.popitem(last=False)

    def _store_disk(self, key: str, img: PILImage.Image) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._disk_path(key)
            tmp = path.with_suffix(f".tmp.{os.getpid()}")
            img.save(tmp, format="JPEG", quality=90)
            tmp.replace(path)
            self._prune_disk()
        except Exception:
            pass

    def _prune_disk(self) -> None:
        try:
            files = sorted(
                self.cache_dir.glob("*.jpg"),
                key=lambda p: p.stat().st_mtime,
            )
            excess = len(files) - self.max_disk_files
            for old in files[: max(0, excess)]:
                old.unlink(missing_ok=True)
        except Exception:
            pass


def _relative_luminance(r: int, g: int, b: int) -> float:
    def _chan(v: int) -> float:
        x = v / 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * _chan(r) + 0.7152 * _chan(g) + 0.0722 * _chan(b)


def _contrast_corrected_rgb(r: int, g: int, b: int, dark_mode: bool = True) -> Tuple[int, int, int]:
    """Ensure extracted album color stays legible against dark or light terminal backgrounds."""
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    if dark_mode:
        v = max(0.68, min(0.96, v))
        s = max(0.35, min(0.92, s))
    else:
        v = max(0.25, min(0.62, v))
        s = max(0.40, min(0.92, s))
    nr, ng, nb = colorsys.hsv_to_rgb(h, s, v)
    return int(round(nr * 255)), int(round(ng * 255)), int(round(nb * 255))


def extract_artwork_colors(
    img: Optional[PILImage.Image],
    dark_mode: bool = True,
) -> Optional[Tuple[str, str]]:
    """Extract a vibrant (accent_hex, secondary_hex) pair from album art."""
    if img is None:
        return None
    try:
        small = img.convert("RGB").resize((24, 24), PILImage.Resampling.BILINEAR)
        pixels = list(
            small.get_flattened_data()
            if hasattr(small, "get_flattened_data")
            else small.getdata()
        )
    except Exception:
        return None

    # Bucket quantized hues and score by saturation + mid-brightness.
    buckets: Dict[int, Tuple[float, int, int, int]] = {}
    for r, g, b in pixels:
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        if v < 0.14 or (s < 0.15 and v > 0.90):
            continue
        score = (s * 1.4 + 0.25) * (1.0 - abs(v - 0.58))
        hue_bin = int(h * 12) % 12
        prev_score, pr, pg, pb = buckets.get(hue_bin, (0.0, r, g, b))
        total_score = prev_score + score
        if score >= prev_score * 0.2:
            buckets[hue_bin] = (total_score, r, g, b)
        else:
            buckets[hue_bin] = (total_score, pr, pg, pb)

    if not buckets:
        return None

    ranked = sorted(buckets.items(), key=lambda kv: kv[1][0], reverse=True)
    _, r1, g1, b1 = ranked[0][1]
    cr1, cg1, cb1 = _contrast_corrected_rgb(r1, g1, b1, dark_mode=dark_mode)
    accent = f"#{cr1:02x}{cg1:02x}{cb1:02x}"

    secondary = accent
    for hue_bin, (_, r2, g2, b2) in ranked[1:]:
        if abs(hue_bin - ranked[0][0]) >= 2:
            cr2, cg2, cb2 = _contrast_corrected_rgb(r2, g2, b2, dark_mode=dark_mode)
            secondary = f"#{cr2:02x}{cg2:02x}{cb2:02x}"
            break

    return accent, secondary

