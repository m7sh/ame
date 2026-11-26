"""
song_info.py - Song Info, YouTube Music Credits & Watch Telemetry (BitChord port).

Modeled on BitChord's ``SongInfoRepository`` & ``SongInfoParser``:
- Fetches YouTube Music track credits (``MPTC{video_id}``) for Performed by,
  Written by, Produced by, Label/Metadata provider, etc.
- Scrapes ``https://www.youtube.com/watch?v={video_id}`` (with ``Cookie: SOCS=CAI``)
  for exact upload view count, likes, comments, release date (``Released on: YYYY-MM-DD``),
  and description music credits fallback.
- Formats counts using YouTube-style truncated compact notation (``1.2K``, ``2.3M``).
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests

from api import Track
from stream_resolver import ResolvedStream

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_RELEASED_ON_RE = re.compile(r"^Released on:\s*(\d{4}-\d{2}-\d{2})$", re.IGNORECASE)
_NAME_SPLIT_RE = re.compile(r"\r?\n+|\s*[•·]\s*|\s*,\s+(?=[A-Z\"'])")


@dataclass(frozen=True)
class SongCredit:
    """Single credit role and credited contributors."""

    role: str
    names: Tuple[str, ...]

    @property
    def text(self) -> str:
        return ", ".join(self.names)


@dataclass
class SongInfo:
    """Detailed track metadata, statistics, and credits."""

    album: str = ""
    release_year: str = ""
    release_date: str = ""
    views: str = ""
    likes: str = ""
    comments: str = ""
    credits: List[SongCredit] = field(default_factory=list)
    codec_info: str = ""
    failed: bool = False


def compact_count(raw: Optional[str | int]) -> str:
    """YouTube-style truncated compact count: 950 -> '950', 1234 -> '1.2K', 2345678 -> '2.3M'."""
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    try:
        n = int(s)
    except ValueError:
        return s
    if n < 0:
        return s

    for unit, suffix in (
        (1_000_000_000, "B"),
        (1_000_000, "M"),
        (1_000, "K"),
    ):
        if n >= unit:
            whole = n // unit
            if whole >= 10:
                return f"{whole}{suffix}"
            tenth = (n % unit) * 10 // unit
            return f"{whole}{suffix}" if tenth == 0 else f"{whole}.{tenth}{suffix}"
    return str(n)


def _extract_balanced_json(text: str, var_name: str) -> Optional[Dict[str, Any]]:
    """Extract a balanced JSON object assigned to ``var_name`` inside HTML/JS."""
    m = re.search(rf"(?:var\s+)?{re.escape(var_name)}\s*=\s*", text)
    if not m:
        return None
    start = m.end()
    if start >= len(text) or text[start] != "{":
        return None

    depth = 0
    quoted = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        else:
            if ch == '"':
                quoted = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : idx + 1])
                        return parsed if isinstance(parsed, dict) else None
                    except Exception:
                        return None
    return None


def _runs_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return "".join(_runs_text(x) for x in obj)
    if isinstance(obj, dict):
        if isinstance(obj.get("simpleText"), str):
            return obj["simpleText"]
        if isinstance(obj.get("runs"), list):
            return "".join(
                str(r.get("text") or "")
                for r in obj["runs"]
                if isinstance(r, dict)
            )
        if "text" in obj:
            return _runs_text(obj["text"])
        if "content" in obj and isinstance(obj["content"], str):
            return obj["content"]
    return ""


def _find_objects(root: Any, key_name: str) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            val = node.get(key_name)
            if isinstance(val, dict):
                found.append(val)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(root)
    return found


def _with_digits(val: Optional[str]) -> str:
    if not val:
        return ""
    s = val.strip()
    return s if any(ch.isdigit() for ch in s) else ""


def _parse_release_date(description: str) -> str:
    for raw_line in (description or "").splitlines():
        m = _RELEASED_ON_RE.match(raw_line.strip())
        if m:
            return m.group(1)
    return ""


def parse_watch_html_song_info(html: str, video_id: str) -> SongInfo:
    """Parse view count, likes, comments, release date, and music credits from watch HTML."""
    player = _extract_balanced_json(html, "ytInitialPlayerResponse")
    if not player:
        return SongInfo(failed=True)
    details = player.get("videoDetails") or {}
    if details.get("videoId") and details.get("videoId") != video_id:
        return SongInfo(failed=True)

    views_raw = str(details.get("viewCount") or "").strip()
    views = views_raw if views_raw.isdigit() else ""
    rel_date = _parse_release_date(str(details.get("shortDescription") or ""))

    data = _extract_balanced_json(html, "ytInitialData")
    likes = ""
    comments = ""
    credits: List[SongCredit] = []

    if isinstance(data, dict):
        primaries = _find_objects(data, "videoPrimaryInfoRenderer")
        primary = primaries[0] if primaries else None
        if primary:
            for tb in _find_objects(primary, "toggleButtonRenderer"):
                icon = (tb.get("defaultIcon") or {}).get("iconType")
                if icon == "LIKE":
                    likes = _with_digits(_runs_text(tb.get("defaultText")))
                    if likes:
                        break
            if not likes:
                for lb in _find_objects(primary, "likeButtonViewModel"):
                    for bvm in _find_objects(lb, "buttonViewModel"):
                        title_val = _with_digits(str(bvm.get("title") or ""))
                        if title_val:
                            likes = title_val
                            break
                    if likes:
                        break

        for ch in _find_objects(data, "commentsHeaderRenderer"):
            c = _with_digits(_runs_text(ch.get("countText")))
            if c:
                comments = c
                break
        if not comments:
            for ce in _find_objects(data, "commentsEntryPointHeaderRenderer"):
                c = _with_digits(_runs_text(ce.get("commentCount")))
                if c:
                    comments = c
                    break

        for sec in _find_objects(data, "videoDescriptionMusicSectionRenderer"):
            rows = (
                _find_objects(sec, "infoRowRenderer")
                + _find_objects(sec, "metadataRowRenderer")
                + _find_objects(sec, "premiumMetadataRowRenderer")
            )
            for row in rows:
                role = _runs_text(row.get("title")).strip()
                if not role:
                    continue
                val_parts = [
                    _runs_text(row.get("defaultMetadata")),
                    _runs_text(row.get("expandedMetadata")),
                    _runs_text(row.get("contents")),
                    _runs_text(row.get("content")),
                ]
                joined = " ".join(p.strip() for p in val_parts if p.strip()).strip()
                if not joined:
                    continue
                names = tuple(
                    part.strip().strip(",•·")
                    for part in _NAME_SPLIT_RE.split(joined)
                    if part.strip().strip(",•·")
                ) or (joined,)
                credits.append(SongCredit(role=role, names=names))

    deduped: List[SongCredit] = []
    seen = set()
    for c in credits:
        key = (c.role.lower(), c.text.lower())
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    return SongInfo(
        release_date=rel_date,
        views=compact_count(views) if views else "",
        likes=likes,
        comments=comments,
        credits=deduped,
        failed=(data is None),
    )


def parse_ytm_credits_dict(raw_credits: Optional[Dict[str, Any]]) -> List[SongCredit]:
    """Normalize ``ytmusicapi.YTMusic.get_song_credits()`` output into ``SongCredit`` rows."""
    if not isinstance(raw_credits, dict):
        return []
    out: List[SongCredit] = []
    seen = set()
    for key in ("performed_by", "written_by", "produced_by", "music_metadata_provided_by"):
        sec = raw_credits.get(key)
        if isinstance(sec, dict):
            role = str(sec.get("localized_title") or key.replace("_", " ").title()).strip()
            names = tuple(str(x).strip() for x in (sec.get("data") or []) if str(x).strip())
            if role and names:
                dedupe_key = (role.lower(), ", ".join(names).lower())
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    out.append(SongCredit(role=role, names=names))

    for sec in raw_credits.get("other_sections") or []:
        if not isinstance(sec, dict):
            continue
        role = str(sec.get("localized_title") or "").strip()
        names = tuple(str(x).strip() for x in (sec.get("data") or []) if str(x).strip())
        if role and names:
            dedupe_key = (role.lower(), ", ".join(names).lower())
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                out.append(SongCredit(role=role, names=names))
    return out


def fetch_song_info(
    track: Track,
    api: Any = None,
    stream_info: Optional[ResolvedStream] = None,
    session: Optional[requests.Session] = None,
) -> SongInfo:
    """Fetch full Song Info (album, year/release date, counts, credits, audio stream details)."""
    codec_parts: List[str] = []
    if stream_info is not None:
        if stream_info.codec:
            codec_parts.append(stream_info.codec.upper())
        if stream_info.kbps:
            codec_parts.append(f"{stream_info.kbps} kbps")
        if stream_info.client_id:
            codec_parts.append(stream_info.client_id)
    codec_summary = " · ".join(codec_parts)

    if not track.id or not _VIDEO_ID_RE.match(track.id):
        return SongInfo(album=track.album, codec_info=codec_summary)

    sess = session or requests.Session()

    def _fetch_watch() -> Optional[SongInfo]:
        try:
            resp = sess.get(
                "https://www.youtube.com/watch",
                params={"v": track.id, "hl": "en"},
                headers={"User-Agent": _USER_AGENT, "Cookie": "SOCS=CAI"},
                timeout=6.0,
            )
            if resp.status_code != 200 or not resp.text:
                return None
            return parse_watch_html_song_info(resp.text, track.id)
        except Exception:
            return None

    def _fetch_credits() -> List[SongCredit]:
        if api is None or not hasattr(api, "get_song_credits"):
            return []
        try:
            raw = api.get_song_credits(track.id)
            return parse_ytm_credits_dict(raw)
        except Exception:
            return []

    def _fetch_release() -> Tuple[str, str]:
        album_name = track.album or ""
        year = ""
        if api is None:
            return album_name, year
        aid = track.album_id
        try:
            if aid and hasattr(api, "get_album"):
                alb, _ = api.get_album(aid)
                if alb:
                    album_name = album_name or getattr(alb, "title", "") or ""
                    y_match = _YEAR_RE.search(str(getattr(alb, "year", "") or ""))
                    if y_match:
                        year = y_match.group(1)
        except Exception:
            pass
        return album_name, year

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_watch = pool.submit(_fetch_watch)
        f_credits = pool.submit(_fetch_credits)
        f_release = pool.submit(_fetch_release)
        watch_info = f_watch.result()
        ytm_credits = f_credits.result()
        album_name, rel_year = f_release.result()

    base = watch_info or SongInfo(failed=True)
    if not rel_year and base.release_date:
        rel_year = base.release_date[:4]

    return SongInfo(
        album=album_name or track.album,
        release_year=rel_year,
        release_date=base.release_date,
        views=base.views,
        likes=base.likes,
        comments=base.comments,
        credits=ytm_credits or base.credits,
        codec_info=codec_summary,
        failed=base.failed and not ytm_credits,
    )
