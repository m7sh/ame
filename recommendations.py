"""
recommendations.py - Personalized recommendation, taste-scoring, and station engine.

Features:
- Recording-level deduplication (`normalised_title` + `artist_set` + `is_same_recording`)
  so official audio & music video cuts of the same song never repeat back-to-back.
- Per-artist diversity caps (`PER_ARTIST_LIMIT = 2`, `SEED_ARTIST_LIMIT = 4`) so
  recommendations and radio queues never collapse into one artist's discography.
- Recency + play-count + favourites taste ranking with weighted sampling (`weighted_sample`).
- Time-of-day mood selection mapped to YouTube Music's mood category params.
"""

from __future__ import annotations

import datetime
import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from api import Playlist, Track

PER_ARTIST_LIMIT = 2
SEED_ARTIST_LIMIT = 4
MIN_VIDEO_STATION = 3
ARTIST_POOL = 10

_NOISE_RE = re.compile(
    r"\((?:official|lyric|lyrics|lyrical|audio|video|visuali[sz]er|full song|hd|4k)[^)]*\)"
    r"|\(from[^)]*\)"
    r"|\[[^\]]*\]"
    r"|\b(?:official (?:video|audio|music video)|lyrical video|full video|4k video)\b",
    re.IGNORECASE,
)
_TOPIC_RE = re.compile(r"\s*-\s*topic\b", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")
_CREDIT_SEP_RE = re.compile(r",|&|·|•|;| feat\.?| ft\.| ft | x | with ", re.IGNORECASE)
_VIDEO_HINT_RE = re.compile(
    r"\b(?:official (?:music )?video|music video|lyrical video|4k video|visuali[sz]er)\b",
    re.IGNORECASE,
)

# YouTube Music mood category browse params
MOOD_PARAMS: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "Energy": ("ggMPOg1uX2lRZUZiMnNrQnJW", ("energetic", "upbeat", "power", "motivation")),
    "Feel Good": ("ggMPOg1uXzZQbDB5eThLRTQ3", ("happy", "feel-good", "pop", "dance")),
    "Focus": ("ggMPOg1uX0NvNGNhWThMYWRh", ("focus", "flow", "instrumental", "reading")),
    "Relax": ("ggMPOg1uX1JOQWZFeDByc2Jm", ("unwind", "peaceful", "relaxing", "laidback", "chill")),
    "Romance": ("ggMPOg1uX0FzQ2FhZWtUY211", ("romantic", "love")),
    "Blue": ("ggMPOg1uX0JLQ0gySWZKZVY1", ("sad", "melanchol", "heartbreak", "lonely")),
    "Party": ("ggMPOg1uX0pmQ0s2V0JRclZs", ("party", "dance", "club")),
    "Workout": ("ggMPOg1uX09LWkhnTjRGRUJh", ("workout", "gym", "cardio", "running")),
    "Sleep": ("ggMPOg1uX1MxaFQ3Z0JMZkN4", ("sleep", "calm", "ambient")),
}


def normalised_title(raw: str) -> str:
    """Normalize a track title for recording matching (keeps remix/cover markers)."""
    head = (raw or "").lower().split(" | ", 1)[0]
    cleaned = _NOISE_RE.sub(" ", head)
    cleaned = _PUNCT_RE.sub(" ", cleaned)
    return _SPACES_RE.sub(" ", cleaned).strip()


def artist_set(raw: str) -> Set[str]:
    """Split collaborative artist credits so billing order stops mattering."""
    cleaned = _TOPIC_RE.sub(" ", (raw or "").lower())
    out: Set[str] = set()
    for part in _CREDIT_SEP_RE.split(cleaned):
        norm = _SPACES_RE.sub(" ", _PUNCT_RE.sub(" ", part)).strip()
        if norm:
            out.add(norm)
    return out


def is_likely_video(track: Track) -> bool:
    """Heuristic check for music video uploads vs catalogue audio cuts."""
    raw = track.raw or {}
    vtype = str(raw.get("videoType") or raw.get("resultType") or "").upper()
    if "OMV" in vtype or vtype == "VIDEO":
        return True
    if "ATV" in vtype:
        return False
    return bool(_VIDEO_HINT_RE.search(track.title or ""))


def is_same_recording(a: Track, b: Track) -> bool:
    """True when two tracks are the same recording (catches audio vs official video pairs)."""
    if a.id and b.id and a.id == b.id:
        return True
    ta = normalised_title(a.title)
    tb = normalised_title(b.title)
    if not ta or ta != tb:
        return False
    la = artist_set(a.artist)
    lb = artist_set(b.artist)
    return (not la) or (not lb) or bool(la & lb)


def extend_station(
    existing: Sequence[Track],
    candidates: Sequence[Track],
    limit: int,
    prefer_videos: bool = False,
) -> List[Track]:
    """Deduplicate candidates by recording and cap tracks per artist."""
    if limit <= 0 or not candidates:
        return []

    taken: List[Track] = list(existing)
    per_artist: Dict[str, int] = {}
    seed_artists = artist_set(taken[-1].artist) if taken else set()
    out: List[Track] = []

    songs_only = [c for c in candidates if not is_likely_video(c)]
    videos_only = [c for c in candidates if is_likely_video(c)]
    if prefer_videos and len(videos_only) >= MIN_VIDEO_STATION:
        ordered = videos_only
    elif prefer_videos:
        ordered = list(candidates)
    else:
        ordered = songs_only if songs_only else list(candidates)

    for cand in ordered:
        if len(out) >= limit:
            break
        if any(is_same_recording(t, cand) for t in taken):
            continue

        artists = sorted(artist_set(cand.artist))
        if artists:
            key = artists[0]
            cap = SEED_ARTIST_LIMIT if any(a in seed_artists for a in artists) else PER_ARTIST_LIMIT
            played = per_artist.get(key, 0)
            if played >= cap:
                continue
            per_artist[key] = played + 1

        taken.append(cand)
        out.append(cand)

    return out


@dataclass
class RankedArtist:
    name: str
    artist_id: str = ""
    score: float = 0.0


@dataclass
class TasteProfile:
    artists: List[RankedArtist] = field(default_factory=list)
    seed_tracks: List[Track] = field(default_factory=list)
    heard_ids: Set[str] = field(default_factory=set)


def weighted_sample(
    ranked: Sequence[Any],
    count: int,
    rng: Optional[random.Random] = None,
) -> List[Any]:
    """Weighted random sample without replacement, skewed linearly toward top items."""
    if count <= 0 or not ranked:
        return []
    r = rng or random
    pool = list(enumerate(ranked))
    picked: List[Any] = []
    n = len(ranked)
    while len(picked) < count and pool:
        weights = [float(n - idx) for idx, _ in pool]
        total = sum(weights)
        roll = r.random() * total
        chosen_pos = 0
        for pos, w in enumerate(weights):
            roll -= w
            if roll <= 0:
                chosen_pos = pos
                break
        _, item = pool.pop(chosen_pos)
        picked.append(item)
    return picked


def build_taste_profile(
    favourites: Sequence[Track] = (),
    recent_history: Sequence[Track] = (),
    session_history: Sequence[Track] = (),
    stats_top_artists: Sequence[Any] = (),
    stats_top_tracks: Sequence[Track] = (),
) -> TasteProfile:
    """Rank listener artists & seed tracks from persisted history, favourites, and stats."""
    scores: Dict[str, RankedArtist] = {}
    heard_ids: Set[str] = set()

    def credit_artist(artist_id: str, name: str, weight: float) -> None:
        clean_name = (name or "").strip()
        if not clean_name or clean_name.lower() in ("various artists", "unknown artist"):
            return
        aid = (artist_id or "").strip()
        key = aid if aid.startswith("UC") else f"name:{clean_name.lower()}"
        prev = scores.get(key)
        if prev is None:
            scores[key] = RankedArtist(name=clean_name, artist_id=aid if aid.startswith("UC") else "", score=weight)
        else:
            prev.score += weight
            if aid.startswith("UC") and not prev.artist_id:
                prev.artist_id = aid

    # Combine recent + session history (newest first, deduped by id while keeping recency)
    merged_history: List[Track] = []
    seen_hist: Set[str] = set()
    for tr in list(recent_history) + list(reversed(list(session_history))):
        if tr and tr.id:
            heard_ids.add(tr.id)
            if tr.id not in seen_hist:
                seen_hist.add(tr.id)
                merged_history.append(tr)

    hist_len = max(len(merged_history), 1)
    for idx, tr in enumerate(merged_history[:40]):
        recency = 1.0 - idx / hist_len
        lead_name = tr.artist.split(",")[0].split("&")[0].strip() if tr.artist else ""
        credit_artist(tr.artist_id, lead_name, 1.0 + recency * 0.5)

    for tr in favourites:
        if tr and tr.id:
            heard_ids.add(tr.id)
            lead_name = tr.artist.split(",")[0].split("&")[0].strip() if tr.artist else ""
            credit_artist(tr.artist_id, lead_name, 2.0)

    # Monthly Replay stats top artists
    n_art = len(stats_top_artists)
    for idx, entry in enumerate(stats_top_artists[:15]):
        name = getattr(entry, "artist", None) or getattr(entry, "name", "")
        aid = getattr(entry, "artist_id", "") or ""
        credit_artist(aid, str(name), float(n_art - idx) * 0.5)

    ranked_artists = sorted(scores.values(), key=lambda a: a.score, reverse=True)

    # Ordered unique seed tracks: recent -> monthly top tracks -> favourites
    seed_tracks: List[Track] = []
    seed_seen: Set[str] = set()
    for tr in list(merged_history[:12]) + list(stats_top_tracks[:10]) + list(favourites[:20]):
        if tr and tr.id and tr.id not in seed_seen:
            seed_seen.add(tr.id)
            seed_tracks.append(tr)
            heard_ids.add(tr.id)

    return TasteProfile(artists=ranked_artists, seed_tracks=seed_tracks, heard_ids=heard_ids)


def moods_for_hour(hour: Optional[int] = None) -> List[Tuple[str, str, Tuple[str, ...]]]:
    """Return hour-appropriate moods -> (name, params, words)."""
    h = datetime.datetime.now().hour if hour is None else int(hour) % 24
    if 5 <= h <= 8:
        names = ["Energy", "Feel Good", "Focus"]
    elif 9 <= h <= 11:
        names = ["Focus", "Feel Good", "Energy"]
    elif 12 <= h <= 16:
        names = ["Feel Good", "Focus", "Energy", "Relax"]
    elif 17 <= h <= 19:
        names = ["Energy", "Feel Good", "Relax"]
    elif 20 <= h <= 22:
        names = ["Romance", "Relax", "Feel Good"]
    else:
        names = ["Relax", "Blue", "Romance", "Sleep"]

    out: List[Tuple[str, str, Tuple[str, ...]]] = []
    for name in names:
        spec = MOOD_PARAMS.get(name)
        if spec:
            out.append((name, spec[0], spec[1]))
    return out


def interleave_buckets(buckets: Sequence[Sequence[Track]], limit: int) -> List[Track]:
    """Round-robin interleave recommendation sources so no single source dominates."""
    queues = [list(b) for b in buckets if b]
    merged: List[Track] = []
    idx = 0
    while queues and len(merged) < limit * 2:
        q = queues[idx % len(queues)]
        merged.append(q.pop(0))
        if not q:
            queues.pop(idx % len(queues))
            continue
        idx += 1
    return merged


def rank_mix_playlists(
    personalized: Sequence[Playlist],
    mood_playlists: Sequence[Playlist],
    charts: Sequence[Playlist],
    limit: int = 30,
) -> List[Playlist]:
    """Deduplicate and order mixes: personalized/artist -> mood -> chart playlists."""
    seen_ids: Set[str] = set()
    out: List[Playlist] = []
    for pl in list(personalized) + list(mood_playlists) + list(charts):
        if not pl or not pl.id:
            continue
        clean = pl.id[2:] if pl.id.startswith("VL") else pl.id
        if clean in seen_ids:
            continue
        seen_ids.add(clean)
        out.append(pl)
        if len(out) >= limit:
            break
    return out
