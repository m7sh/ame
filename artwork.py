"""
artwork.py - Album artwork URL upgrading, caching, and crisp terminal rendering.

Ports BitChord's artwork resolution ladder and fallback pipeline:
- Standard YouTube Music sizes (``PLAYER_ART_PX = 1200`` for Now Playing)
- Host-aware Google ``=wN-hN`` / ``=sN`` rewriting preserving CDN flags
- YouTube ``i.ytimg.com`` / ``img.youtube.com`` upgrades to ``maxresdefault`` /
  ``sddefault`` with query-signature stripping and 404 fallback to ``hqdefault``
- Letterbox bar trimming on 4:3 YouTube thumbnails before square cropping
- High-quality Lanczos scaling and HiDPI-aware TGP / Sixel / Halfcell rendering
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
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

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

    from textual_image._pixeldata import PixelData as _TIPixelData
    from textual_image._sixel import SixelOptions as _TISixelOptions
    from textual_image._terminal import get_cell_size, probe_terminal
    from textual_image.renderable.halfcell import Image as _HalfcellRenderable
    from textual_image.renderable.tgp import Image as _TGPRenderable
    from textual_image.widget._base import Image as _TIBaseImage
    from textual_image.widget import HalfcellImage as _HalfcellWidget
    from textual_image.widget import Image as _AutoImageWidget
    from textual_image.widget.sixel import Image as _SixelWidget
    from textual_image.widget.sixel import _ImageSixelImpl as _TISixelImpl
    from textual_image.widget.sixel import _NoopRenderable as _TINoopRenderable

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
    _TIPixelData = None  # type: ignore[assignment]
    _TISixelOptions = None  # type: ignore[assignment]
    _HalfcellRenderable = None  # type: ignore[assignment]
    _TGPRenderable = None  # type: ignore[assignment]
    _TIBaseImage = None  # type: ignore[assignment]
    _TISixelImpl = None  # type: ignore[assignment]
    _TINoopRenderable = None  # type: ignore[assignment]


# BitChord resolution ladder (Models.kt / artwork.ts)
ROW_ART_PX = 160
CARD_ART_PX = 480
NOTIFICATION_ART_PX = 544
HEADER_ART_PX = 720
PLAYER_ART_PX = 1200
SQUARE_SIZES: Tuple[int, ...] = (60, 120, 226, 544, 1200)

_SIZE_HINT_RE = re.compile(r"w\d+-h\d+")
_PARAM_SIZE_HINT_RE = re.compile(r"=s\d+")
_YT_THUMB_NAME_RE = re.compile(
    r"/(default|mqdefault|hqdefault|sddefault|hq720|maxresdefault)\.([a-zA-Z0-9]+)"
)
_YT_UPGRADED_RENDITIONS = frozenset({"sddefault", "hq720", "maxresdefault"})

_DEFAULT_ART_DIR = Path.home() / ".cache" / "ame" / "artwork"
_MAX_DISK_FILES = 160
_MAX_MEM_ITEMS = 32
_TGP_MIN_PIXEL_SIDE = 960


def _extract_host(url: str) -> str:
    """Return lowercased hostname without port for a URL or protocol-relative URL."""
    clean = url.strip()
    if clean.startswith("//"):
        clean = "https:" + clean
    elif "://" not in clean:
        return ""
    try:
        return (urlsplit(clean).hostname or "").lower()
    except Exception:
        return ""


def is_google_image_host(url: str) -> bool:
    """True when URL points to Google's resizable image CDN (ggpht / googleusercontent)."""
    host = _extract_host(url)
    return (
        host == "ggpht.com"
        or host.endswith(".ggpht.com")
        or host == "googleusercontent.com"
        or host.endswith(".googleusercontent.com")
    )


def is_youtube_thumbnail_host(url: str) -> bool:
    """True when URL points to YouTube's static thumbnail host (i.ytimg / img.youtube)."""
    host = _extract_host(url)
    return (
        host == "i.ytimg.com"
        or host.endswith(".ytimg.com")
        or host == "img.youtube.com"
        or host.endswith(".youtube.com")
    )


def _with_yt_rendition(url: str, match: re.Match[str], rendition: str) -> str:
    """Rewrite a YouTube thumbnail rendition and drop the signed ``?sqp=...`` query."""
    ext = match.group(2)
    rewritten = f"{url[: match.start()]}/{rendition}.{ext}{url[match.end() :]}"
    return rewritten.split("?", 1)[0]


def _upgrade_youtube_thumbnail(url: str, size: int) -> str:
    """Select the BitChord YouTube thumbnail rendition for ``size`` pixels."""
    match = _YT_THUMB_NAME_RE.search(url)
    if not match:
        return url
    current = match.group(1)
    if size >= 480:
        target = "maxresdefault"
    elif size >= 240:
        target = "sddefault"
    elif current in _YT_UPGRADED_RENDITIONS:
        target = "hqdefault"
    else:
        return url
    return url if target == current else _with_yt_rendition(url, match, target)


def youtube_thumbnail_fallback(url: str) -> Optional[str]:
    """Return ``hqdefault`` fallback for an upgraded YouTube thumbnail, or None."""
    if not url or not is_youtube_thumbnail_host(url):
        return None
    match = _YT_THUMB_NAME_RE.search(url)
    if not match or match.group(1) not in _YT_UPGRADED_RENDITIONS:
        return None
    return _with_yt_rendition(url, match, "hqdefault")


def youtube_thumbnail_candidates(url: str) -> List[str]:
    """Return ordered fallback URLs for a YouTube thumbnail (highest res first)."""
    if not url or not is_youtube_thumbnail_host(url):
        return [url] if url else []
    match = _YT_THUMB_NAME_RE.search(url)
    if not match:
        return [url]
    current = match.group(1)
    if current == "maxresdefault":
        order = ("maxresdefault", "sddefault", "hqdefault")
    elif current in ("sddefault", "hq720"):
        order = (current, "hqdefault")
    else:
        return [url]
    candidates: List[str] = []
    for rendition in order:
        cand = url if rendition == current else _with_yt_rendition(url, match, rendition)
        if cand not in candidates:
            candidates.append(cand)
    return candidates


def snap_artwork_size(size: int) -> int:
    """Snap a requested pixel dimension onto YouTube Music's cached ``SQUARE_SIZES`` ladder."""
    for candidate in SQUARE_SIZES:
        if candidate >= size:
            return candidate
    return SQUARE_SIZES[-1]


def sized_artwork_url(url: str, size: int = PLAYER_ART_PX) -> str:
    """Return a high-resolution square URL matching BitChord's ``artworkAt`` rules.

    - Replaces ``wN-hN`` hints while preserving CDN flags (e.g. ``-l90-rj``).
    - On Google image hosts, also replaces ``=sN`` hints (preserving flags like
      ``-c-k-c0x00ffffff-no-rj``) or appends ``=w{size}-h{size}-l90-rj`` when absent.
    - On YouTube thumbnail hosts (``i.ytimg.com`` / ``img.youtube.com``), upgrades
      to ``maxresdefault`` (>=480px) or ``sddefault`` (>=240px) and drops ``?sqp=...``.
    """
    if not url:
        return ""
    clean = url.strip()
    if not clean:
        return ""

    sized = _SIZE_HINT_RE.sub(f"w{size}-h{size}", clean)
    if is_google_image_host(clean):
        updated = _PARAM_SIZE_HINT_RE.sub(f"=s{size}", sized)
        if updated == clean and "=" not in clean.rsplit("/", 1)[-1]:
            return f"{clean}=w{size}-h{size}-l90-rj"
        return updated

    if is_youtube_thumbnail_host(clean):
        return _upgrade_youtube_thumbnail(sized, size)

    return sized


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


if _HAS_TEXTUAL_IMAGE and _TIPixelData is not None:

    class _CrispPixelData(_TIPixelData):  # type: ignore[misc,valid-type]
        """PixelData subclass using Lanczos resampling for sharper terminal artwork."""

        def scaled(self, width: int, height: int) -> "_CrispPixelData":
            scaled_image = self._image.resize(
                (max(1, width), max(1, height)),
                resample=PILImage.Resampling.LANCZOS,
                reducing_gap=3.0,
            )
            return _CrispPixelData(scaled_image)

    class _CrispHalfcellRenderable(_HalfcellRenderable):  # type: ignore[misc,valid-type]
        def __init__(self, image: Any, width: Any = None, height: Any = None) -> None:
            super().__init__(image, width=width, height=height)
            self._image_data = _CrispPixelData(self._image_data.pil_image)

    class _CrispTGPRenderable(_TGPRenderable):  # type: ignore[misc,valid-type]
        def __init__(self, image: Any, width: Any = None, height: Any = None) -> None:
            super().__init__(image, width=width, height=height)
            self._image_data = _CrispPixelData(self._image_data.pil_image)

        def _send_image_to_terminal(self, width: int, height: int) -> None:
            # Preserve HiDPI / high-resolution source detail instead of clamping down
            # to a fallback 10x20 cell pixel estimate before Kitty/Ghostty placement.
            src_w = max(1, self._image_data.width)
            src_h = max(1, self._image_data.height)
            target_w = max(1, width)
            target_h = max(1, height)
            max_upscale = min(src_w / target_w, src_h / target_h)
            desired_scale = max(2.0, _TGP_MIN_PIXEL_SIDE / max(target_w, target_h))
            scale = min(max_upscale, desired_scale)
            if scale > 1.0:
                target_w = max(target_w, int(round(target_w * scale)))
                target_h = max(target_h, int(round(target_h * scale)))
            super()._send_image_to_terminal(target_w, target_h)

    class _CrispSixelImpl(_TISixelImpl):  # type: ignore[misc,valid-type]
        def _scale_image(self, image_data: Any, terminal_sizes: Any) -> Any:
            scaled = super()._scale_image(image_data, terminal_sizes)
            # Re-sample directly from original image_data using Lanczos
            try:
                from textual_image._geometry import ImageSize

                styled_width, styled_height = self.parent._get_styled_size()  # type: ignore[attr-defined]
                image_size = ImageSize(
                    image_data.width,
                    image_data.height,
                    width=styled_width,
                    height=styled_height,
                )
                pixel_width, pixel_height = image_size.get_pixel_size(
                    self.content_size.width, self.content_size.height, terminal_sizes
                )
                return _CrispPixelData(image_data.pil_image).scaled(pixel_width, pixel_height)
            except Exception:
                return scaled

    class _CrispSixelWidget(_SixelWidget, Renderable=_TINoopRenderable):  # type: ignore[misc,valid-type,call-arg]
        DEFAULT_OPTIONS = _TISixelOptions(colors=256, quantize="adaptive")

        def compose(self) -> Any:
            yield _CrispSixelImpl(self.image, self._sixel_options)

    class _CrispTGPWidget(_TIBaseImage, Renderable=_CrispTGPRenderable):  # type: ignore[misc,call-arg]
        pass

    class _CrispHalfcellWidget(_TIBaseImage, Renderable=_CrispHalfcellRenderable):  # type: ignore[misc,call-arg]
        pass

else:
    _CrispSixelWidget = None  # type: ignore[assignment]
    _CrispTGPWidget = None  # type: ignore[assignment]
    _CrispHalfcellWidget = None  # type: ignore[assignment]


def create_album_image_widget(**kwargs: Any) -> Any:
    """Create the highest-quality terminal image widget for the Now Playing album window."""
    if not _HAS_TEXTUAL_IMAGE:
        return None
    if _IMAGE_BACKEND_NAME == "sixel" and _CrispSixelWidget is not None:
        return _CrispSixelWidget(**kwargs)
    if _IMAGE_BACKEND_NAME == "tgp" and _CrispTGPWidget is not None:
        return _CrispTGPWidget(**kwargs)
    if _IMAGE_BACKEND_NAME == "halfcell" and _CrispHalfcellWidget is not None:
        return _CrispHalfcellWidget(**kwargs)
    if _AutoImageWidget is None:
        return None
    return _AutoImageWidget(**kwargs)


def create_mini_image_widget(**kwargs: Any) -> Any:
    """Create a flicker-free Lanczos Halfcell widget for the compact bottom mini-player."""
    if not _HAS_TEXTUAL_IMAGE:
        return None
    if _CrispHalfcellWidget is not None:
        return _CrispHalfcellWidget(**kwargs)
    if _HalfcellWidget is None:
        return None
    return _HalfcellWidget(**kwargs)


def is_sixel_backend() -> bool:
    return _IMAGE_BACKEND_NAME == "sixel"


def _trim_letterbox_bars(img: PILImage.Image) -> PILImage.Image:
    """Trim uniform near-black top/bottom letterbox bars on 4:3 YouTube thumbnails."""
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    ratio = w / h
    # Only target mildly wide/4:3 frames (e.g. 480x360 hqdefault or 640x480 sddefault)
    if not (1.20 <= ratio <= 1.50):
        return img

    gray = img.convert("L")
    sample_w = min(w, 96)
    small = gray.resize((sample_w, h), PILImage.Resampling.BILINEAR)
    raw = small.tobytes()
    max_trim = max(0, int(h * 0.20))

    def _row_is_black(y: int) -> bool:
        row = raw[y * sample_w : (y + 1) * sample_w]
        return max(row) <= 22

    top = 0
    while top < max_trim and _row_is_black(top):
        top += 1

    bottom = h
    while (h - bottom) < max_trim and bottom > top + 1 and _row_is_black(bottom - 1):
        bottom -= 1

    # Require balanced top+bottom bars and a non-black interior before cropping
    if top < 4 or (h - bottom) < 4:
        return img
    trimmed_h = bottom - top
    if trimmed_h < int(h * 0.55):
        return img
    mid_y = top + trimmed_h // 2
    mid_row = raw[mid_y * sample_w : (mid_y + 1) * sample_w]
    if max(mid_row) <= 24:
        return img
    return img.crop((0, top, w, bottom))


def _prepare_square_artwork(img: PILImage.Image) -> PILImage.Image:
    """Convert to RGB, strip YouTube 4:3 letterbox bars if present, and center-crop wide thumbs."""
    rgb = _trim_letterbox_bars(img.convert("RGB"))
    w, h = rgb.size
    if w > 0 and h > 0 and w / h > 1.25:
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        rgb = rgb.crop((left, top, left + side, top + side))
    return rgb


class ArtworkCache:
    """Thread-safe bounded in-memory + on-disk artwork cache with BitChord YouTube fallback."""

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
        self._missing_yt_urls: Set[str] = set()

    def _key(self, track_id: str, url: str) -> str:
        digest = hashlib.sha256(f"{track_id}:{url}".encode("utf-8")).hexdigest()[:24]
        return digest

    def _disk_path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.jpg"

    def _candidate_urls(self, target_url: str) -> List[str]:
        candidates = youtube_thumbnail_candidates(target_url)
        with self._lock:
            missing = set(self._missing_yt_urls)
        filtered = [u for u in candidates if u not in missing]
        return filtered or (candidates[-1:] if candidates else [])

    def _get_cached_by_key(self, key: str) -> Optional[PILImage.Image]:
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

    def get_cached(
        self,
        track_id: str,
        url: str,
        size: int = PLAYER_ART_PX,
    ) -> Optional[PILImage.Image]:
        """Return cached PIL Image from memory or disk without network I/O."""
        target_url = sized_artwork_url(url, size=size)
        if not target_url:
            return None
        for candidate in self._candidate_urls(target_url):
            hit = self._get_cached_by_key(self._key(track_id, candidate))
            if hit is not None:
                return hit
        return None

    def fetch(
        self,
        track_id: str,
        url: str,
        size: int = PLAYER_ART_PX,
    ) -> Optional[PILImage.Image]:
        """Fetch artwork from cache or HTTP, coalescing concurrent requests and falling back on 404."""
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
            candidates = self._candidate_urls(target_url)
            for cand in candidates:
                cand_key = self._key(track_id, cand)
                existing = self._get_cached_by_key(cand_key)
                if existing is not None:
                    return existing
                try:
                    resp = self._session.get(
                        cand,
                        timeout=6.0,
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                except Exception:
                    continue

                status = getattr(resp, "status_code", 0)
                content = getattr(resp, "content", b"")
                if status == 404 and youtube_thumbnail_fallback(cand) is not None:
                    with self._lock:
                        self._missing_yt_urls.add(cand)
                    continue
                if status != 200 or not content:
                    continue

                try:
                    with PILImage.open(io.BytesIO(content)) as raw:
                        rgb = _prepare_square_artwork(raw)
                except Exception:
                    continue

                self._store_disk(cand_key, rgb)
                self._store_mem(cand_key, rgb)
                return rgb
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
            img.save(tmp, format="JPEG", quality=95, subsampling=0)
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

