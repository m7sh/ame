"""
lyrics.py - Multi-provider word-synced & line-synced lyrics engine.

Providers (queried in parallel, picked in priority order):
1. SimpMusic   - YouTube videoId-keyed Enhanced LRC & LRC
2. BetterLyrics - Apple Music TTML (syllable/word-level sync)
3. LyricsPlus  - YouLy+ multi-mirror syllable-timed JSON
4. lrc.red     - Apple Music TTML with strict recording matching
5. LRCLIB      - Fast line-synced LRC & plain lyrics
6. YouTube Music - Built-in timed/plain lyrics fallback

Also provides:
- Enhanced LRC (A2) and Apple Music TTML syllable/word-level parsers
- Instrumental interlude gap markers (``•••``) for >= 4s breaks
- Per-track timing offset persistence (``~/.local/share/ame/lyrics_offsets.json``)
- Bounded LRU disk cache (``~/.cache/ame/lyrics``)
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import unicodedata
import xml.etree.ElementTree as ET
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

_LRC_TAG_RE = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_ENHANCED_LINE_RE = re.compile(r"^\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\](.*)$")
_ENHANCED_WORD_RE = re.compile(r"<(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?>([^<]*)")
_BRACKETED_RE = re.compile(r"[(\[][^)\]]*[)\]]")
_SPACES_RE = re.compile(r"\s+")
_ARTIST_SEP_RE = re.compile(
    r"\s*(?:,|&|;|/|\s+and\s+|\s+x\s+|\s+with\s+|\s+feat\.?\s+|\s+ft\.?\s+)\s*",
    re.IGNORECASE,
)

_DEFAULT_LYRICS_DIR = Path.home() / ".cache" / "ame" / "lyrics"
_DEFAULT_OFFSETS_PATH = Path.home() / ".local" / "share" / "ame" / "lyrics_offsets.json"
_MAX_DISK_FILES = 200
_MAX_MEM_ITEMS = 48
_USER_AGENT = "ame/2.3 (https://github.com/m7sh/ame)"
_MIN_INSTRUMENTAL_GAP_MS = 4000
_TAIL_WORD_MS = 800

_VERSION_WORDS = {
    "live",
    "remix",
    "remixed",
    "mix",
    "acoustic",
    "unplugged",
    "instrumental",
    "karaoke",
    "cappella",
    "acapella",
    "demo",
    "edit",
    "version",
    "cover",
    "sped",
    "slowed",
    "reverb",
    "nightcore",
    "lofi",
    "orchestral",
    "extended",
}

_LYRICSPLUS_MIRRORS = (
    "https://lyricsplus.prjktla.my.id",
    "https://lyricsplus.atomix.one",
    "https://lyricsplus.binimum.org",
    "https://lyricsplus.prjktla.workers.dev",
    "https://lyricsplus-seven.vercel.app",
    "https://lyrics-plus-backend.vercel.app",
)

DEFAULT_PROVIDERS: Tuple[str, ...] = (
    "simpmusic",
    "betterlyrics",
    "lyricsplus",
    "lrcred",
    "lrclib",
    "ytm",
)

_INSTRUMENTAL_NOTICE_RE = re.compile(
    r"(?:pure music|instrumental.*enjoy|no lyrics|纯音乐)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LyricWord:
    """Single word (merged from one or more syllables) with millisecond bounds."""

    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class LyricLine:
    """Single lyric line with optional start timestamp, end bound, and word timings."""

    time_ms: Optional[int]
    text: str
    words: List[LyricWord] = field(default_factory=list)
    sung_until_ms: Optional[int] = None
    is_gap: bool = False

    @property
    def is_word_synced(self) -> bool:
        return bool(self.words)

    @property
    def end_ms(self) -> Optional[int]:
        if self.words:
            return max(w.end_ms for w in self.words)
        return self.sung_until_ms


@dataclass
class LyricsResult:
    """Normalized lyrics response for a track."""

    lines: List[LyricLine] = field(default_factory=list)
    synced: bool = False
    source: str = ""

    @property
    def word_synced(self) -> bool:
        return any(line.is_word_synced for line in self.lines)


def _stamp_to_ms(minutes: str, seconds: str, frac: str) -> int:
    f = frac or "0"
    if len(f) == 1:
        ms = int(f) * 100
    elif len(f) == 2:
        ms = int(f) * 10
    else:
        ms = int(f[:3])
    return (int(minutes) * 60 + int(seconds)) * 1000 + ms


def _is_unspaced_char(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x9FFF
        or 0x3040 <= code <= 0x30FF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
        or 0x0E00 <= code <= 0x0E7F
    )


def words_from_runs(
    runs: Sequence[Tuple[int, int, str]],
    spacing_is_explicit: bool = False,
) -> List[LyricWord]:
    """Glue adjacent syllable runs into whole words unless separated by whitespace."""
    if not runs:
        return []
    glue = spacing_is_explicit or any(any(ch.isspace() for ch in raw) for _, _, raw in runs)

    words: List[LyricWord] = []
    buf: List[str] = []
    start_ms = 0
    end_ms = 0

    def flush() -> None:
        if buf:
            text = "".join(buf)
            words.append(LyricWord(start_ms=start_ms, end_ms=max(start_ms, end_ms), text=text))
            buf.clear()

    for r_start, r_end, raw in runs:
        content = raw.strip()
        if not content:
            flush()
            continue

        prev_char = buf[-1][-1] if buf else ""
        joins = (
            glue
            and bool(buf)
            and not raw[0].isspace()
            and not _is_unspaced_char(prev_char)
            and not _is_unspaced_char(content[0])
        )
        if not joins:
            flush()

        if not buf:
            start_ms = r_start
            end_ms = r_start
        buf.append(content)
        end_ms = max(end_ms, max(r_start, r_end))

        if not glue or raw[-1].isspace():
            flush()

    flush()
    return words


def add_instrumental_gaps(
    lines: List[LyricLine],
    min_gap_ms: int = _MIN_INSTRUMENTAL_GAP_MS,
) -> List[LyricLine]:
    """Insert ``•••`` markers for intro or instrumental breaks >= ``min_gap_ms``."""
    if not lines:
        return lines
    out: List[LyricLine] = []
    first_t = lines[0].time_ms
    if first_t is not None and first_t >= min_gap_ms:
        out.append(LyricLine(time_ms=0, text="•••", is_gap=True))

    for idx, line in enumerate(lines):
        out.append(line)
        if idx + 1 >= len(lines):
            continue
        nxt = lines[idx + 1]
        end = line.end_ms
        if (
            end is not None
            and line.time_ms is not None
            and nxt.time_ms is not None
            and end > line.time_ms
            and (nxt.time_ms - end) >= min_gap_ms
        ):
            out.append(LyricLine(time_ms=end, text="•••", is_gap=True))
    return out


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
            total_ms = _stamp_to_ms(m.group(1), m.group(2), m.group(3) or "0")
            parsed.append(LyricLine(time_ms=total_ms, text=lyric or "♪"))
    parsed.sort(key=lambda line: line.time_ms or 0)
    return parsed


def parse_enhanced_lrc(lrc_text: str) -> List[LyricLine]:
    """Parse Enhanced (A2) word-synced LRC ``[mm:ss.xx]<mm:ss.xx>word ...``."""
    if not lrc_text:
        return []
    rows: List[Tuple[int, List[re.Match[str]], str]] = []
    for raw_line in lrc_text.splitlines():
        m = _ENHANCED_LINE_RE.match(raw_line.strip())
        if not m:
            continue
        t_ms = _stamp_to_ms(m.group(1), m.group(2), m.group(3) or "0")
        body = m.group(4)
        w_matches = list(_ENHANCED_WORD_RE.finditer(body))
        rows.append((t_ms, w_matches, body.strip()))

    if not rows or not any(w for _, w, _ in rows):
        return []

    rows.sort(key=lambda r: r[0])
    out: List[LyricLine] = []
    for idx, (line_ms, w_matches, plain) in enumerate(rows):
        if not w_matches:
            cleaned = html.unescape(plain).strip()
            if cleaned:
                out.append(LyricLine(time_ms=line_ms, text=cleaned))
            continue

        last_word_ms = _stamp_to_ms(
            w_matches[-1].group(1),
            w_matches[-1].group(2),
            w_matches[-1].group(3) or "0",
        )
        line_end = rows[idx + 1][0] if idx + 1 < len(rows) else last_word_ms + _TAIL_WORD_MS
        runs: List[Tuple[int, int, str]] = []
        for wi, wm in enumerate(w_matches):
            w_start = _stamp_to_ms(wm.group(1), wm.group(2), wm.group(3) or "0")
            if wi + 1 < len(w_matches):
                nxt_m = w_matches[wi + 1]
                w_end = _stamp_to_ms(nxt_m.group(1), nxt_m.group(2), nxt_m.group(3) or "0")
            else:
                w_end = line_end
            runs.append((w_start, max(w_start, w_end), html.unescape(wm.group(4))))

        words = words_from_runs(runs)
        if not words:
            continue
        out.append(
            LyricLine(
                time_ms=min(line_ms, words[0].start_ms),
                text=" ".join(w.text for w in words),
                words=words,
                sung_until_ms=max(w.end_ms for w in words),
            )
        )
    return add_instrumental_gaps(out)


def _parse_ttml_time(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith("ms"):
        try:
            return int(round(float(s[:-2])))
        except ValueError:
            return None
    if s.endswith("s"):
        try:
            return int(round(float(s[:-1]) * 1000))
        except ValueError:
            return None
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return int(round((int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])) * 1000))
        if len(parts) == 2:
            return int(round((int(parts[0]) * 60 + float(parts[1])) * 1000))
        return int(round(float(s) * 1000))
    except ValueError:
        return None


def _attr_local(elem: ET.Element, suffix: str) -> str:
    for k, v in elem.attrib.items():
        if k == suffix or k.endswith(f"}}{suffix}") or k.endswith(f":{suffix}"):
            return v
    return ""


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    if ":" in tag:
        return tag.split(":", 1)[1]
    return tag


def parse_ttml(ttml_text: str) -> List[LyricLine]:
    """Parse Apple Music TTML syllable/word/line-synced lyrics."""
    if not ttml_text or "<" not in ttml_text:
        return []
    try:
        root = ET.fromstring(ttml_text.strip())
    except ET.ParseError:
        return []

    lines: List[LyricLine] = []
    for elem in root.iter():
        if _local_tag(elem.tag) != "p":
            continue
        p_begin = _parse_ttml_time(elem.attrib.get("begin"))
        p_end = _parse_ttml_time(elem.attrib.get("end"))

        runs: List[Tuple[int, int, str]] = []

        def collect(node: ET.Element, inside_bg: bool = False) -> None:
            role = _attr_local(node, "role").lower()
            if role in ("x-translation", "x-roman"):
                return
            is_bg = inside_bg or role == "x-bg"

            if _local_tag(node.tag) == "span":
                s_begin = _parse_ttml_time(node.attrib.get("begin"))
                s_end = _parse_ttml_time(node.attrib.get("end"))
                has_child_spans = any(_local_tag(ch.tag) == "span" for ch in node)
                if not is_bg and s_begin is not None and not has_child_spans:
                    txt = "".join(node.itertext())
                    if txt:
                        end_val = s_end if s_end is not None else s_begin
                        # Preserve any trailing whitespace on node.tail as word separator
                        tail = node.tail or ""
                        sep = " " if tail and tail[0].isspace() else ""
                        runs.append((s_begin, max(s_begin, end_val), txt + sep))
                    return

            if not is_bg and node.text and _local_tag(node.tag) != "p":
                pass
            for child in node:
                collect(child, is_bg)
                if not is_bg and child.tail and child.tail.strip() and _local_tag(child.tag) == "span":
                    # Loose text between spans uses parent timing if available
                    if p_begin is not None:
                        runs.append((p_begin, p_end or p_begin, child.tail))

        collect(elem)
        words = words_from_runs(runs)
        if words:
            start_ms = min(p_begin, words[0].start_ms) if p_begin is not None else words[0].start_ms
            end_ms = p_end if p_end is not None else max(w.end_ms for w in words)
            lines.append(
                LyricLine(
                    time_ms=start_ms,
                    text=" ".join(w.text for w in words),
                    words=words,
                    sung_until_ms=end_ms,
                )
            )
            continue

        # Fallback: line-synced <p> without syllable <span>s
        plain_parts: List[str] = []

        def collect_plain(node: ET.Element) -> None:
            role = _attr_local(node, "role").lower()
            if role in ("x-translation", "x-roman", "x-bg"):
                return
            if node.text:
                plain_parts.append(node.text)
            for child in node:
                collect_plain(child)
                if child.tail:
                    plain_parts.append(child.tail)

        collect_plain(elem)
        plain = _SPACES_RE.sub(" ", "".join(plain_parts)).strip()
        if plain and p_begin is not None:
            lines.append(LyricLine(time_ms=p_begin, text=plain, sung_until_ms=p_end))

    lines.sort(key=lambda ln: ln.time_ms or 0)
    return add_instrumental_gaps(lines)


def parse_plain_lyrics(text: str) -> List[LyricLine]:
    """Split plain unsynced lyrics into lines."""
    if not text:
        return []
    return [LyricLine(time_ms=None, text=line.strip()) for line in text.splitlines() if line.strip()]


def active_lyric_index(
    lines: List[LyricLine],
    pos_seconds: float,
    offset_ms: int = 0,
) -> int:
    """Return the index of the active synced lyric line for ``pos_seconds``, or -1."""
    if not lines:
        return -1
    pos_ms = int(max(0.0, pos_seconds) * 1000) + 120 + int(offset_ms)
    active = -1
    for idx, line in enumerate(lines):
        if line.time_ms is None:
            return -1
        if line.time_ms <= pos_ms:
            active = idx
        else:
            break
    return active


def active_word_index(
    line: Optional[LyricLine],
    pos_seconds: float,
    offset_ms: int = 0,
) -> int:
    """Return the active word index within a word-synced ``LyricLine``, or -1."""
    if line is None or not line.words:
        return -1
    pos_ms = int(max(0.0, pos_seconds) * 1000) + 80 + int(offset_ms)
    active = -1
    for idx, word in enumerate(line.words):
        if word.start_ms <= pos_ms:
            active = idx
        else:
            break
    return active


def _clean_search_title(title: str) -> str:
    cleaned = re.sub(
        r"\s*[\(\[](?:official|audio|video|lyrics|lyrical|visuali[sz]er|4k|hd).*?[\)\]]",
        "",
        title or "",
        flags=re.I,
    )
    return cleaned.strip() or (title or "")


def _clean_search_artist(artist: str) -> str:
    return (artist or "").split(",")[0].split("&")[0].strip()


def _norm_for_match(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value or "")
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    alnum = "".join(ch if ch.isalnum() else " " for ch in stripped.lower())
    return _SPACES_RE.sub(" ", alnum).strip()


def _core_and_versions(title: str) -> Tuple[str, set[str]]:
    head = _BRACKETED_RE.sub(" ", title or "").split(" - ", 1)[0]
    core = _norm_for_match(head) or _norm_for_match(title)
    extras = " ".join(m.group(0) for m in _BRACKETED_RE.finditer(title or ""))
    if " - " in (title or ""):
        extras += " " + (title or "").split(" - ", 1)[1]
    versions = {w for w in _norm_for_match(extras).split() if w in _VERSION_WORDS}
    return core, versions


def _artists_set(artist: str) -> set[str]:
    return {
        _norm_for_match(part)
        for part in _ARTIST_SEP_RE.split(artist or "")
        if _norm_for_match(part)
    }


def _looks_like_instrumental_notice(lines: Sequence[LyricLine]) -> bool:
    return 0 < len(lines) <= 2 and any(_INSTRUMENTAL_NOTICE_RE.search(l.text) for l in lines)


class LyricsOffsetStore:
    """Persists per-track lyrics timing offsets (milliseconds) on disk."""

    MIN_OFFSET_MS = -5000
    MAX_OFFSET_MS = 5000
    STEP_MS = 250

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else _DEFAULT_OFFSETS_PATH
        self._lock = threading.Lock()
        self._offsets: Dict[str, int] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._offsets = {
                        str(k): self._clamp(int(v))
                        for k, v in raw.items()
                        if isinstance(v, (int, float))
                    }
            except Exception:
                self._offsets = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._offsets, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    @classmethod
    def _clamp(cls, value: int) -> int:
        return max(cls.MIN_OFFSET_MS, min(cls.MAX_OFFSET_MS, int(value)))

    def get(self, track_id: str) -> int:
        if not track_id:
            return 0
        with self._lock:
            return self._offsets.get(track_id, 0)

    def set(self, track_id: str, offset_ms: int) -> int:
        if not track_id:
            return 0
        clamped = self._clamp(offset_ms)
        with self._lock:
            if clamped == 0:
                self._offsets.pop(track_id, None)
            else:
                self._offsets[track_id] = clamped
            self.save()
        return clamped

    def adjust(self, track_id: str, delta_ms: int) -> int:
        if not track_id:
            return 0
        return self.set(track_id, self.get(track_id) + delta_ms)


class LyricsManager:
    """Fetches, races, and caches word-synced, line-synced, or plain lyrics."""

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        session: Optional[requests.Session] = None,
        ytm_fetcher: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        providers: Optional[Sequence[str]] = None,
        offsets_path: Optional[Path] = None,
    ) -> None:
        self.cache_dir = cache_dir or _DEFAULT_LYRICS_DIR
        self._session = session or requests.Session()
        self._ytm_fetcher = ytm_fetcher
        # Preserve deterministic single-provider behavior when tests inject a custom session
        if providers is not None:
            self._providers: Tuple[str, ...] = tuple(providers)
        elif session is not None:
            self._providers = ("lrclib", "ytm")
        else:
            self._providers = DEFAULT_PROVIDERS

        self._lock = threading.Lock()
        self._mem: OrderedDict[str, LyricsResult] = OrderedDict()
        self._last_lyricsplus_mirror: Optional[str] = None
        self.offsets = LyricsOffsetStore(path=offsets_path)

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
                lines: List[LyricLine] = []
                for item in data.get("lines", []):
                    words = [
                        LyricWord(
                            start_ms=int(w["start_ms"]),
                            end_ms=int(w["end_ms"]),
                            text=str(w["text"]),
                        )
                        for w in item.get("words", [])
                        if isinstance(w, dict)
                    ]
                    lines.append(
                        LyricLine(
                            time_ms=item.get("time_ms"),
                            text=str(item.get("text", "")),
                            words=words,
                            sung_until_ms=item.get("sung_until_ms"),
                            is_gap=bool(item.get("is_gap", False)),
                        )
                    )
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
        prioritize_word_sync: bool = True,
    ) -> LyricsResult:
        """Fetch lyrics from cache or enabled providers (racing in parallel)."""
        cached = self.get_cached(track_id, title, artist)
        if cached is not None:
            return cached

        res = self._fetch_from_providers(
            track_id=track_id,
            title=title,
            artist=artist,
            album=album,
            duration_seconds=duration_seconds,
            prioritize_word_sync=prioritize_word_sync,
        )

        key = self._cache_key(track_id, title, artist)
        if res.lines:
            self._store_mem(key, res)
            self._store_disk(key, res)
        return res

    def _fetch_from_providers(
        self,
        track_id: str,
        title: str,
        artist: str,
        album: str,
        duration_seconds: int,
        prioritize_word_sync: bool,
    ) -> LyricsResult:
        providers = [p for p in self._providers if p != "ytm" or self._ytm_fetcher is not None]
        if not providers:
            return LyricsResult()

        # Fast path for single/simple provider chains without thread pool overhead
        if len(providers) <= 2 and set(providers).issubset({"lrclib", "ytm"}):
            res = LyricsResult()
            if "lrclib" in providers:
                res = self._fetch_lrclib(title, artist, album, duration_seconds)
            if (not res.lines or not res.synced) and "ytm" in providers:
                ytm_res = self._fetch_ytm(track_id)
                if ytm_res.lines and (ytm_res.synced or not res.lines):
                    res = ytm_res
            return res

        clean_title = _clean_search_title(title)
        clean_artist = _clean_search_artist(artist)

        def _call(name: str) -> LyricsResult:
            if name == "simpmusic":
                return self._fetch_simpmusic(track_id, duration_seconds)
            if name == "betterlyrics":
                return self._fetch_betterlyrics(clean_title, clean_artist, album, duration_seconds)
            if name == "lyricsplus":
                return self._fetch_lyricsplus(clean_title, clean_artist, album, duration_seconds)
            if name == "lrcred":
                return self._fetch_lrcred(clean_title, clean_artist, duration_seconds)
            if name == "lrclib":
                return self._fetch_lrclib(title, artist, album, duration_seconds)
            if name == "ytm":
                return self._fetch_ytm(track_id)
            return LyricsResult()

        results: Dict[str, LyricsResult] = {}
        with ThreadPoolExecutor(max_workers=min(6, len(providers))) as pool:
            futs = {name: pool.submit(_call, name) for name in providers}
            for name in providers:
                try:
                    results[name] = futs[name].result(timeout=6.5)
                except Exception:
                    results[name] = LyricsResult()

        # Pick in priority order: word-synced wins outright when prioritized, else first synced
        best_line_synced: Optional[LyricsResult] = None
        best_plain: Optional[LyricsResult] = None
        for name in providers:
            cand = results.get(name)
            if not cand or not cand.lines or _looks_like_instrumental_notice(cand.lines):
                continue
            if cand.word_synced:
                return cand
            if cand.synced:
                if not prioritize_word_sync:
                    return cand
                if best_line_synced is None:
                    best_line_synced = cand
            elif best_plain is None:
                best_plain = cand

        return best_line_synced or best_plain or LyricsResult()

    def _fetch_simpmusic(self, video_id: str, duration_seconds: int) -> LyricsResult:
        if not video_id:
            return LyricsResult()
        try:
            r = self._session.get(
                f"https://api-lyrics.simpmusic.org/v1/{video_id}",
                timeout=4.5,
                headers={"User-Agent": _USER_AGENT},
            )
            if r.status_code != 200:
                return LyricsResult()
            data = r.json()
            if not isinstance(data, dict) or not data.get("success"):
                return LyricsResult()
            items = data.get("data") or []
            if not isinstance(items, list):
                return LyricsResult()
            candidates = [
                it
                for it in items
                if isinstance(it, dict)
                and (
                    duration_seconds <= 0
                    or abs(int(it.get("duration") or 0) - duration_seconds) <= 10
                )
            ]
            if not candidates:
                return LyricsResult()
            candidates.sort(key=lambda it: abs(int(it.get("duration") or 0) - duration_seconds))
            chosen = candidates[0]
            rich = str(chosen.get("richSyncLyrics") or "").strip()
            if rich:
                lines = parse_enhanced_lrc(rich)
                if lines:
                    return LyricsResult(lines=lines, synced=True, source="SimpMusic")
            synced = str(chosen.get("syncedLyrics") or "").strip()
            if synced:
                lines = parse_lrc(synced)
                if lines:
                    return LyricsResult(lines=lines, synced=True, source="SimpMusic")
        except Exception:
            pass
        return LyricsResult()

    def _fetch_betterlyrics(
        self,
        title: str,
        artist: str,
        album: str,
        duration_seconds: int,
    ) -> LyricsResult:
        params: Dict[str, Any] = {"s": title, "a": artist}
        if duration_seconds > 0:
            params["d"] = str(duration_seconds)
        if album:
            params["al"] = album
        try:
            r = self._session.get(
                "https://lyrics-api.boidu.dev/getLyrics",
                params=params,
                timeout=5.0,
                headers={"User-Agent": _USER_AGENT},
            )
            if r.status_code != 200 or not r.text.strip():
                return LyricsResult()
            body = r.text.strip()
            if body.startswith("{"):
                obj = r.json()
                ttml = str(obj.get("ttml") or obj.get("lyrics") or "").strip()
            else:
                ttml = body
            lines = parse_ttml(ttml) if "<tt" in ttml.lower() else (parse_enhanced_lrc(ttml) or parse_lrc(ttml))
            if lines:
                return LyricsResult(
                    lines=lines,
                    synced=any(l.time_ms is not None for l in lines),
                    source="BetterLyrics",
                )
        except Exception:
            pass
        return LyricsResult()

    def _fetch_lyricsplus(
        self,
        title: str,
        artist: str,
        album: str,
        duration_seconds: int,
    ) -> LyricsResult:
        mirrors = list(_LYRICSPLUS_MIRRORS)
        if self._last_lyricsplus_mirror in mirrors:
            mirrors.remove(self._last_lyricsplus_mirror)
            mirrors.insert(0, self._last_lyricsplus_mirror)

        params: Dict[str, Any] = {"title": title, "artist": artist}
        if duration_seconds > 0:
            params["duration"] = str(duration_seconds)
        if album:
            params["album"] = album

        for host in mirrors[:4]:
            try:
                r = self._session.get(
                    f"{host}/v2/lyrics/get",
                    params=params,
                    timeout=4.0,
                    headers={"User-Agent": _USER_AGENT},
                )
                if r.status_code != 200:
                    continue
                payload = r.json()
                lines = self._parse_lyricsplus_payload(payload)
                if lines:
                    self._last_lyricsplus_mirror = host
                    return LyricsResult(
                        lines=lines,
                        synced=any(l.time_ms is not None for l in lines),
                        source="LyricsPlus",
                    )
            except Exception:
                continue
        return LyricsResult()

    @staticmethod
    def _parse_lyricsplus_payload(payload: Any) -> List[LyricLine]:
        if not isinstance(payload, dict):
            return []
        raw_lines = payload.get("lyrics")
        if not isinstance(raw_lines, list):
            return []
        parsed: List[LyricLine] = []
        for item in raw_lines:
            if not isinstance(item, dict):
                continue
            start = item.get("time")
            if start is None:
                continue
            start_ms = int(start)
            syllables = item.get("syllabus") or []
            runs: List[Tuple[int, int, str]] = []
            for syl in syllables:
                if not isinstance(syl, dict):
                    continue
                st = syl.get("time")
                txt = syl.get("text")
                if st is None or txt is None:
                    continue
                dur = int(syl.get("duration") or 0)
                runs.append((int(st), int(st) + max(0, dur), str(txt)))
            words = words_from_runs(runs, spacing_is_explicit=True)
            if words:
                parsed.append(
                    LyricLine(
                        time_ms=min(start_ms, words[0].start_ms),
                        text=" ".join(w.text for w in words),
                        words=words,
                        sung_until_ms=max(w.end_ms for w in words),
                    )
                )
            else:
                text = str(item.get("text") or "").strip()
                if text:
                    dur = int(item.get("duration") or 0)
                    parsed.append(
                        LyricLine(
                            time_ms=start_ms,
                            text=text,
                            sung_until_ms=(start_ms + dur) if dur > 0 else None,
                        )
                    )
        parsed.sort(key=lambda ln: ln.time_ms or 0)
        return add_instrumental_gaps(parsed)

    def _fetch_lrcred(
        self,
        title: str,
        artist: str,
        duration_seconds: int,
    ) -> LyricsResult:
        try:
            r = self._session.get(
                "https://lrc.red/search.json",
                params={"q": f"{title} {artist}".strip()},
                timeout=4.5,
                headers={"User-Agent": _USER_AGENT},
            )
            if r.status_code != 200:
                return LyricsResult()
            hits = (r.json() or {}).get("hits") or []
            if not isinstance(hits, list):
                return LyricsResult()

            want_core, want_ver = _core_and_versions(title)
            want_artists = _artists_set(artist)
            best_hit: Optional[Dict[str, Any]] = None
            best_dist = 1e9

            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                isrc = str(hit.get("isrc") or "").strip().upper()
                h_title = str(hit.get("title") or "").strip()
                if not re.match(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$", isrc) or not h_title:
                    continue
                h_core, h_ver = _core_and_versions(h_title)
                if h_core != want_core or h_ver != want_ver:
                    continue
                h_artists = _artists_set(str(hit.get("artist") or ""))
                if want_artists and not (h_artists & want_artists):
                    continue
                h_dur = hit.get("duration")
                dist = abs(float(h_dur) - duration_seconds) if (duration_seconds > 0 and h_dur is not None) else 0.0
                if dist <= 3.0 and dist < best_dist:
                    best_dist = dist
                    best_hit = hit

            if best_hit is None:
                return LyricsResult()

            isrc = str(best_hit["isrc"]).strip().upper()
            doc = self._session.get(
                f"https://lrc.red/s/{isrc}.ttml",
                timeout=4.5,
                headers={"User-Agent": _USER_AGENT},
            )
            if doc.status_code != 200:
                return LyricsResult()
            lines = parse_ttml(doc.text)
            if lines:
                return LyricsResult(
                    lines=lines,
                    synced=any(l.time_ms is not None for l in lines),
                    source="lrc.red",
                )
        except Exception:
            pass
        return LyricsResult()

    def _fetch_lrclib(
        self,
        title: str,
        artist: str,
        album: str,
        duration_seconds: int,
    ) -> LyricsResult:
        clean_title = _clean_search_title(title)
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
                headers={"User-Agent": _USER_AGENT},
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
                headers={"User-Agent": _USER_AGENT},
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
            lines = parse_enhanced_lrc(synced_raw) or parse_lrc(synced_raw)
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
                "version": 2,
                "source": res.source,
                "synced": res.synced,
                "lines": [
                    {
                        "time_ms": l.time_ms,
                        "text": l.text,
                        "sung_until_ms": l.sung_until_ms,
                        "is_gap": l.is_gap,
                        "words": [
                            {"start_ms": w.start_ms, "end_ms": w.end_ms, "text": w.text}
                            for w in l.words
                        ],
                    }
                    for l in res.lines
                ],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            files = sorted(self.cache_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
            excess = len(files) - _MAX_DISK_FILES
            for old in files[: max(0, excess)]:
                old.unlink(missing_ok=True)
        except Exception:
            pass
