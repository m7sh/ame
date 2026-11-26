"""
spotify_import.py - Guest-mode public Spotify playlist importer & track matcher.

Modeled on BitChord's ``SpotifyImporter`` and ``TrackMatcher``:
- Parses ``https://open.spotify.com/playlist/<id>`` URLs and ``spotify:playlist:<id>`` URIs.
- Scrapes Spotify's public embed page (``__NEXT_DATA__``) with zero OAuth/API keys,
  and pages beyond the first 100 tracks via Spotify's anonymous embed token when present.
- Matches each imported track against YouTube Music search candidates using recording-level
  title/version-word/artist/duration scoring so remixes, live cuts, or karaoke edits do not
  shadow the original track.
"""

from __future__ import annotations

import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote, urlparse

import requests

from api import Playlist, Track

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
_BRACKETED_RE = re.compile(r"[(\[][^)\]]*[)\]]")
_COMBINING_RE = re.compile(r"\p{Mn}+" if False else r"[\u0300-\u036f]+")
_ARTIST_SEP_RE = re.compile(
    r"\s*(?:,|&|;|/|\s+and\s+|\s+x\s+|\s+with\s+|\s+feat\.?\s+|\s+ft\.?\s+)\s*",
    re.IGNORECASE,
)
_SPACES_RE = re.compile(r"\s+")

_EMBED_TRACK_LIMIT = 100
_PAGE_SIZE = 100
_PATHFINDER_URL = "https://api-partner.spotify.com/pathfinder/v1/query"
_FETCH_PLAYLIST_HASH = "19ff1327c29e99c208c86d7a9d8f1929cfdf3d3202a0ff4253c821f1901aa94d"

_VERSION_WORDS: Set[str] = {
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


@dataclass(frozen=True)
class SpotifyTrackStub:
    """Minimal track metadata extracted from a public Spotify playlist."""

    title: str
    artist: str
    duration_seconds: int = 0


def extract_spotify_playlist_id(raw: str) -> Optional[str]:
    """Extract a 22-char Spotify playlist ID from a URL or ``spotify:playlist:`` URI."""
    text = (raw or "").strip()
    if not text:
        return None

    if text.startswith("spotify:playlist:"):
        pid = text.removeprefix("spotify:playlist:").split("?", 1)[0].split("/", 1)[0]
        return pid if _PLAYLIST_ID_RE.match(pid) else None

    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host != "spotify.com" and not host.endswith(".spotify.com"):
        return None
    parts = [seg for seg in parsed.path.split("/") if seg]
    if "playlist" not in parts:
        return None
    idx = parts.index("playlist")
    if idx + 1 >= len(parts):
        return None
    pid = parts[idx + 1]
    return pid if _PLAYLIST_ID_RE.match(pid) else None


def _normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value or "")
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    lowered = stripped.lower()
    alnum = "".join(ch if ch.isalnum() else " " for ch in lowered)
    return _SPACES_RE.sub(" ", alnum).strip()


def _title_core_and_versions(title: str) -> Tuple[str, Set[str]]:
    head = _BRACKETED_RE.sub(" ", title or "").split(" - ", 1)[0]
    core = _normalize_text(head) or _normalize_text(title)
    extras = " ".join(m.group(0) for m in _BRACKETED_RE.finditer(title or ""))
    if " - " in (title or ""):
        extras += " " + (title or "").split(" - ", 1)[1]
    versions = {w for w in _normalize_text(extras).split() if w in _VERSION_WORDS}
    return core, versions


def _artist_tokens(artist: str) -> Set[str]:
    out: Set[str] = set()
    for part in _ARTIST_SEP_RE.split(artist or ""):
        norm = _normalize_text(part)
        if norm:
            out.add(norm)
    return out


def match_best_track(
    candidates: Sequence[Track],
    target_title: str,
    target_artist: str,
    target_duration_s: int = 0,
) -> Optional[Track]:
    """Score YouTube Music candidates and pick the closest recording match."""
    if not candidates:
        return None

    want_core, want_versions = _title_core_and_versions(target_title)
    want_artists = _artist_tokens(target_artist)

    best_track: Optional[Track] = None
    best_score = -10_000.0

    for idx, cand in enumerate(candidates):
        cand_core, cand_versions = _title_core_and_versions(cand.title)
        score = 0.0

        if want_core and cand_core == want_core:
            score += 100.0
        elif want_core and (want_core in cand_core or cand_core in want_core):
            score += 45.0
        else:
            want_words = set(want_core.split())
            cand_words = set(cand_core.split())
            if want_words and cand_words:
                overlap = len(want_words & cand_words) / max(len(want_words), len(cand_words))
                score += overlap * 35.0

        # Version parity prevents remixes/live/karaoke from beating the original cut
        if want_versions == cand_versions:
            score += 30.0
        else:
            score -= 40.0

        cand_artists = _artist_tokens(cand.artist)
        if want_artists and cand_artists:
            if want_artists & cand_artists:
                score += 40.0
            elif any(w in c or c in w for w in want_artists for c in cand_artists):
                score += 20.0
            else:
                score -= 25.0

        if target_duration_s > 0 and cand.duration_seconds > 0:
            diff = abs(cand.duration_seconds - target_duration_s)
            if diff <= 3:
                score += 20.0
            elif diff <= 10:
                score += 10.0
            elif diff > 35:
                score -= min(45.0, float(diff))

        # Slight tie-break in favour of earlier search rank
        score -= idx * 0.25

        if score > best_score:
            best_score = score
            best_track = cand

    return best_track


def parse_spotify_embed_html(html: str) -> Tuple[str, List[SpotifyTrackStub], Optional[str]]:
    """Parse title, initial track stubs, and anonymous token from Spotify embed HTML."""
    match = _NEXT_DATA_RE.search(html or "")
    if not match:
        raise ValueError("Could not parse Spotify playlist metadata. Make sure the playlist is public.")

    root = json.loads(match.group(1))
    props = root.get("props", {}).get("pageProps", {})
    state = props.get("state", {})
    entity = state.get("data", {}).get("entity") or props.get("entity")
    if not isinstance(entity, dict):
        raise ValueError("Spotify playlist entity missing or private.")

    title = str(entity.get("name") or entity.get("title") or "Imported Spotify Playlist").strip()
    raw_tracks = entity.get("trackList") or entity.get("tracks") or []

    stubs: List[SpotifyTrackStub] = []
    for item in raw_tracks:
        if not isinstance(item, dict):
            continue
        t_title = str(item.get("title") or item.get("name") or "").strip()
        if not t_title:
            continue
        subtitle = item.get("subtitle")
        if not subtitle and isinstance(item.get("artists"), list):
            subtitle = ", ".join(
                str(a.get("name") or "").strip()
                for a in item["artists"]
                if isinstance(a, dict) and a.get("name")
            )
        dur_ms = int(item.get("duration") or 0)
        stubs.append(
            SpotifyTrackStub(
                title=t_title,
                artist=str(subtitle or "").strip(),
                duration_seconds=max(0, dur_ms // 1000),
            )
        )

    token = (
        state.get("settings", {}).get("session", {}).get("accessToken")
        if isinstance(state.get("settings"), dict)
        else None
    )
    return title, stubs, (str(token) if token else None)


def _fetch_remaining_tracks(
    session: requests.Session,
    playlist_id: str,
    token: str,
    start: int,
) -> List[SpotifyTrackStub]:
    out: List[SpotifyTrackStub] = []
    offset = start
    while True:
        variables = json.dumps(
            {
                "uri": f"spotify:playlist:{playlist_id}",
                "offset": offset,
                "limit": _PAGE_SIZE,
                "enableWatchFeedEntrypoint": False,
            },
            separators=(",", ":"),
        )
        extensions = json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": _FETCH_PLAYLIST_HASH}},
            separators=(",", ":"),
        )
        url = (
            f"{_PATHFINDER_URL}?operationName=fetchPlaylist"
            f"&variables={quote(variables)}&extensions={quote(extensions)}"
        )
        resp = session.get(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Authorization": f"Bearer {token}",
                "App-platform": "WebPlayer",
                "Accept": "application/json",
            },
            timeout=8.0,
        )
        if resp.status_code != 200:
            break
        content = (
            resp.json()
            .get("data", {})
            .get("playlistV2", {})
            .get("content", {})
        )
        items = content.get("items") or []
        if not items:
            break
        for item in items:
            data = (item.get("itemV2") or {}).get("data") or {}
            t_title = str(data.get("name") or "").strip()
            if not t_title:
                continue
            artists_items = ((data.get("artists") or {}).get("items")) or []
            artist = ", ".join(
                str((a.get("profile") or {}).get("name") or "").strip()
                for a in artists_items
                if isinstance(a, dict)
            ).strip(", ")
            dur_ms = int(((data.get("trackDuration") or {}).get("totalMilliseconds")) or 0)
            out.append(
                SpotifyTrackStub(
                    title=t_title,
                    artist=artist,
                    duration_seconds=max(0, dur_ms // 1000),
                )
            )
        total = int(content.get("totalCount") or 0)
        offset += len(items)
        if offset >= total:
            break
    return out


def fetch_spotify_playlist(
    playlist_id: str,
    session: Optional[requests.Session] = None,
) -> Tuple[str, List[SpotifyTrackStub]]:
    """Fetch public Spotify playlist title and track stubs without auth."""
    sess = session or requests.Session()
    url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    resp = sess.get(url, headers={"User-Agent": _USER_AGENT}, timeout=8.0)
    if resp.status_code != 200 or not resp.text.strip():
        raise RuntimeError(f"Failed to load Spotify playlist (HTTP {resp.status_code})")

    title, stubs, token = parse_spotify_embed_html(resp.text)
    if not stubs:
        raise ValueError("No tracks found in public Spotify playlist.")

    if len(stubs) >= _EMBED_TRACK_LIMIT and token:
        try:
            extra = _fetch_remaining_tracks(sess, playlist_id, token, len(stubs))
            stubs.extend(extra)
        except Exception:
            pass

    return title, stubs


def import_spotify_playlist(
    raw_input: str,
    search_fn: Callable[[str, int], List[Track]],
    session: Optional[requests.Session] = None,
    max_workers: int = 4,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Tuple[Playlist, List[Track], int]:
    """Import a public Spotify playlist and resolve its tracks via YouTube Music search."""
    pid = extract_spotify_playlist_id(raw_input)
    if not pid:
        raise ValueError("Not a valid Spotify playlist URL.")

    title, stubs = fetch_spotify_playlist(pid, session=session)
    total = len(stubs)
    resolved: List[Optional[Track]] = [None] * total
    completed = 0

    def _resolve_one(idx: int, stub: SpotifyTrackStub) -> Tuple[int, Optional[Track]]:
        query = f"{stub.title} {stub.artist}".strip()
        try:
            candidates = search_fn(query, 8) or []
        except Exception:
            candidates = []
        matched = match_best_track(
            candidates,
            target_title=stub.title,
            target_artist=stub.artist,
            target_duration_s=stub.duration_seconds,
        )
        return idx, matched

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_resolve_one, i, s) for i, s in enumerate(stubs)]
        for fut in futures:
            idx, track = fut.result()
            resolved[idx] = track
            completed += 1
            if on_progress is not None:
                on_progress(completed, total)

    tracks = [t for t in resolved if t is not None]
    unmatched = total - len(tracks)
    playlist = Playlist(
        id=f"spotify:{pid}",
        title=title,
        author="Spotify Import",
        track_count=str(len(tracks)),
    )
    return playlist, tracks, unmatched
