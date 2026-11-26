"""
recommendations.py - Listening-history taste engine & category-matched queue builder.

Features:
- Track category, language/script, and vibe inference (``infer_track_categories``)
  for keeping playback queues locked to the current song's category/genre/language.
- Skip-aware, completion-weighted taste profiling (``build_taste_profile``) combining
  monthly + all-time listening stats, recent history, session history, and favourites.
- History-first recommendation ranking (``rank_recommendations``) and category-matched
  queue construction (``build_category_queue``).
- Recording-level deduplication (``is_same_recording``) & per-artist diversity caps.
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
CATEGORY_SEED_ARTIST_LIMIT = 3
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

_LANGUAGE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "hindi": (
        "bollywood", "hindi", "pritam", "arijit", "shreya ghoshal", "a.r. rahman",
        "ar rahman", "atif aslam", "sonu nigam", "kk", "jubin nautiyal", "vishal-shekhar",
        "amit trivedi", "t-series", "zee music", "tips official", "yrf", "aashiqui",
        "pyaar", "ishq", "dil ", "tere ", "mera ", "tum ", "hum ",
    ),
    "punjabi": (
        "punjabi", "diljit", "karan aujla", "sidhu moose", "ap dhillon", "shubh",
        "ammy virk", "gippy", "parmish", "jatt", "gallan", "pind", "bhangra",
    ),
    "tamil": ("tamil", "kollywood", "anirudh", "yuvan", "harris jayaraj", "ilaiyaraaja"),
    "telugu": ("telugu", "tollywood", "thaman", "devi sri prasad", "dsp", "sid sriram"),
    "malayalam": ("malayalam", "mollywood", "sushin shyam", "vineeth sreenivasan"),
    "japanese": (
        "j-pop", "jpop", "j-rock", "anime", "vocaloid", "yoasobi", "ado ", "kenshi yonezu",
        "radwimps", "lisa ", "aimyon", "king gnu", "opening", "ending", "op)", "ed)",
    ),
    "korean": (
        "k-pop", "kpop", "bts", "blackpink", "newjeans", "aespa", "twice", "stray kids",
        "seventeen", "exo", "iu ", "le sserafim", "ive ", "ost",
    ),
    "spanish": (
        "reggaeton", "latin", "corrido", "bachata", "salsa", "banda", "bad bunny",
        "feid", "karol g", "peso pluma", "maluma", "rauw",
    ),
}

_STYLE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "lofi": ("lofi", "lo-fi", "chillhop", "study beats", "chill beats"),
    "phonk": ("phonk", "drift", "montagem", "brazilian funk", "memphis"),
    "slowed": ("slowed", "reverb", "8d audio", "nightcore", "sped up"),
    "remix": ("remix", "bootleg", "mashup", "club mix", "dj mix"),
    "live": ("live", "unplugged", "tiny desk", "concert", "coke studio", "mtv"),
    "acoustic": ("acoustic", "stripped", "piano version"),
    "instrumental": ("instrumental", "karaoke", "type beat", "bgm", "orchestral", "symphony"),
    "soundtrack": ("ost", "soundtrack", "motion picture", "original score"),
    "hiphop": ("hip hop", "hip-hop", "rap", "trap", "drill", "freestyle", "cypher"),
    "rock": ("rock", "metal", "punk", "grunge", "metalcore", "post-punk", "hard rock"),
    "electronic": ("edm", "house", "techno", "trance", "dubstep", "dnb", "synthwave", "retrowave"),
    "classical": ("classical", "sonata", "concerto", "raga", "carnatic", "hindustani", "ghazal", "sufi", "qawwali"),
    "devotional": ("bhajan", "aarti", "chalisa", "mantra", "nasheed", "gospel", "worship", "hymn"),
    "ambient": ("ambient", "meditation", "rain sounds", "sleep music"),
}

_STYLE_CONFLICTS: Dict[str, Set[str]] = {
    "lofi": {"phonk", "rock", "remix", "live", "devotional"},
    "phonk": {"lofi", "acoustic", "classical", "devotional", "ambient"},
    "devotional": {"phonk", "hiphop", "remix", "rock", "electronic"},
    "classical": {"phonk", "hiphop", "remix", "electronic"},
    "ambient": {"phonk", "hiphop", "rock", "remix"},
    "acoustic": {"phonk", "electronic", "remix"},
}

_VIBE_NOVELTY_NOISE = {"slowed", "nightcore", "karaoke"}


@dataclass(frozen=True)
class TrackCategory:
    """Inferred language/region and style/vibe tags for a track."""

    language: str
    styles: frozenset[str]
    is_video: bool = False

    def summary_query_hint(self) -> str:
        parts: List[str] = []
        if self.language and self.language != "english":
            parts.append(self.language)
        for st in sorted(self.styles):
            if st != "studio":
                parts.append(st)
        return " ".join(parts[:2])


def _detect_script_language(text: str) -> str:
    for ch in text:
        cp = ord(ch)
        if 0x0900 <= cp <= 0x097F:
            return "hindi"
        if 0x0A00 <= cp <= 0x0A7F:
            return "punjabi"
        if 0x0B80 <= cp <= 0x0BFF:
            return "tamil"
        if 0x0C00 <= cp <= 0x0C7F:
            return "telugu"
        if 0x0D00 <= cp <= 0x0D7F:
            return "malayalam"
        if 0x3040 <= cp <= 0x30FF:
            return "japanese"
        if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            return "korean"
        if 0x0600 <= cp <= 0x06FF:
            return "arabic"
        if 0x0400 <= cp <= 0x04FF:
            return "russian"
    return ""


def infer_track_categories(track: Track, extra_context: str = "") -> TrackCategory:
    """Infer language/region and genre/vibe tags from track metadata."""
    combined = " ".join(
        p for p in (track.title, track.artist, track.album, extra_context) if p
    )
    low = f" {combined.lower()} "

    language = _detect_script_language(combined)
    if not language:
        for lang, cues in _LANGUAGE_KEYWORDS.items():
            if any(cue in low for cue in cues):
                language = lang
                break
    if not language and re.search(r'\(from\s+"[^"]+"\)', low):
        language = "hindi"
    if not language:
        language = "english"

    styles: Set[str] = set()
    for style, cues in _STYLE_KEYWORDS.items():
        if any(cue in low for cue in cues):
            styles.add(style)
    if not styles:
        styles.add("studio")

    return TrackCategory(
        language=language,
        styles=frozenset(styles),
        is_video=is_likely_video(track),
    )


def category_similarity(
    seed: Track,
    candidate: Track,
    seed_cat: Optional[TrackCategory] = None,
    cand_cat: Optional[TrackCategory] = None,
) -> float:
    """Compute category/language/vibe similarity between ``seed`` and ``candidate``."""
    sc = seed_cat or infer_track_categories(seed)
    cc = cand_cat or infer_track_categories(candidate)

    score = 0.0
    sa = artist_set(seed.artist)
    ca = artist_set(candidate.artist)
    same_artist = bool(sa and ca and (sa & ca))
    if same_artist:
        score += 3.0

    # Language/region coherence
    if sc.language == cc.language:
        score += 4.0 if sc.language != "english" else 2.0
    elif not same_artist:
        if sc.language != "english" and cc.language != "english":
            score -= 6.5
        elif sc.language != "english" or cc.language != "english":
            score -= 3.0

    # Style / vibe coherence
    overlap = sc.styles & cc.styles
    if overlap:
        score += 4.0 if any(s != "studio" for s in overlap) else 1.5
    else:
        for s in sc.styles:
            if cc.styles & _STYLE_CONFLICTS.get(s, set()):
                score -= 5.0
                break

    # Don't leak slowed/nightcore/karaoke into normal studio queues unless seed is one
    if not (sc.styles & _VIBE_NOVELTY_NOISE) and (cc.styles & _VIBE_NOVELTY_NOISE):
        score -= 4.5

    # Duration sanity check
    if seed.duration_seconds > 0 and candidate.duration_seconds > 0:
        diff = abs(seed.duration_seconds - candidate.duration_seconds)
        if diff <= 60:
            score += 1.0
        elif candidate.duration_seconds > seed.duration_seconds * 2.5 or candidate.duration_seconds < 45:
            score -= 2.5

    return score


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
    top_categories: Dict[str, float] = field(default_factory=dict)
    skip_penalized_ids: Set[str] = field(default_factory=set)

    @property
    def has_history(self) -> bool:
        return bool(self.seed_tracks or self.artists)

    @property
    def dominant_language(self) -> str:
        lang_scores = {
            k.removeprefix("lang:"): v
            for k, v in self.top_categories.items()
            if k.startswith("lang:")
        }
        if not lang_scores:
            return "english"
        return max(lang_scores.items(), key=lambda kv: kv[1])[0]

    @property
    def dominant_styles(self) -> List[str]:
        style_scores = [
            (k.removeprefix("style:"), v)
            for k, v in self.top_categories.items()
            if k.startswith("style:") and k != "style:studio"
        ]
        style_scores.sort(key=lambda kv: kv[1], reverse=True)
        return [name for name, _ in style_scores[:3]]


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
    stats_track_entries: Sequence[Any] = (),
    all_time_artists: Sequence[Any] = (),
) -> TasteProfile:
    """Rank listener artists, seed tracks, skip penalties & categories from full history."""
    scores: Dict[str, RankedArtist] = {}
    heard_ids: Set[str] = set()
    cat_scores: Dict[str, float] = {}
    skip_penalized: Set[str] = set()
    track_weights: Dict[str, Tuple[Track, float]] = {}

    def credit_artist(artist_id: str, name: str, weight: float) -> None:
        clean_name = (name or "").strip()
        if not clean_name or clean_name.lower() in ("various artists", "unknown artist"):
            return
        aid = (artist_id or "").strip()
        key = aid if aid.startswith("UC") else f"name:{clean_name.lower()}"
        prev = scores.get(key)
        if prev is None:
            scores[key] = RankedArtist(
                name=clean_name,
                artist_id=aid if aid.startswith("UC") else "",
                score=max(0.1, weight),
            )
        else:
            prev.score = max(0.1, prev.score + weight)
            if aid.startswith("UC") and not prev.artist_id:
                prev.artist_id = aid

    def credit_categories(tr: Track, weight: float) -> None:
        if weight <= 0:
            return
        cat = infer_track_categories(tr)
        l_key = f"lang:{cat.language}"
        cat_scores[l_key] = cat_scores.get(l_key, 0.0) + weight
        for st in cat.styles:
            s_key = f"style:{st}"
            cat_scores[s_key] = cat_scores.get(s_key, 0.0) + weight

    def add_seed_weight(tr: Track, weight: float) -> None:
        if not tr or not tr.id:
            return
        heard_ids.add(tr.id)
        prev = track_weights.get(tr.id)
        if prev is None:
            track_weights[tr.id] = (tr, weight)
        else:
            track_weights[tr.id] = (prev[0], prev[1] + weight)

    # 1. Analyze ListeningStatsStore entries with completion ratio (skips vs full listens)
    completion_by_id: Dict[str, float] = {}
    for entry in stats_track_entries:
        tr = getattr(entry, "track", None)
        if tr is None or not tr.id:
            continue
        plays = int(getattr(entry, "play_count", 0) or 0)
        secs = float(getattr(entry, "listen_seconds", 0.0) or 0.0)
        expected = float(tr.duration_seconds or 180)
        avg_ratio = (secs / max(1.0, plays * expected)) if plays > 0 else 0.0

        if plays >= 2 and avg_ratio < 0.20:
            # Repeatedly skipped early: downweight strongly
            completion_by_id[tr.id] = 0.25
            skip_penalized.add(tr.id)
            continue

        mult = 1.8 if avg_ratio >= 0.65 else (1.2 if avg_ratio >= 0.35 else 0.85)
        completion_by_id[tr.id] = mult
        w = min(6.0, (plays * 0.8 + secs / 180.0) * mult)
        add_seed_weight(tr, w)
        lead_name = tr.artist.split(",")[0].split("&")[0].strip() if tr.artist else ""
        credit_artist(tr.artist_id, lead_name, w * 0.7)
        credit_categories(tr, w)

    # 2. Recent + current session history (newest first, weighted by recency & completion)
    merged_history: List[Track] = []
    seen_hist: Set[str] = set()
    for tr in list(recent_history) + list(reversed(list(session_history))):
        if tr and tr.id:
            heard_ids.add(tr.id)
            if tr.id not in seen_hist:
                seen_hist.add(tr.id)
                merged_history.append(tr)

    hist_len = max(len(merged_history), 1)
    for idx, tr in enumerate(merged_history[:50]):
        recency = 1.0 - idx / hist_len
        comp = completion_by_id.get(tr.id, 1.0)
        w = (1.0 + recency * 0.6) * comp
        add_seed_weight(tr, w)
        lead_name = tr.artist.split(",")[0].split("&")[0].strip() if tr.artist else ""
        credit_artist(tr.artist_id, lead_name, w)
        credit_categories(tr, w)

    # 3. Favourites (explicit positive preference)
    for tr in favourites:
        if tr and tr.id:
            add_seed_weight(tr, 2.5)
            lead_name = tr.artist.split(",")[0].split("&")[0].strip() if tr.artist else ""
            credit_artist(tr.artist_id, lead_name, 2.2)
            credit_categories(tr, 2.2)

    # 4. Monthly + All-time stats top artists
    for scale, artist_list in ((0.65, stats_top_artists), (0.35, all_time_artists)):
        n_art = len(artist_list)
        for idx, entry in enumerate(artist_list[:20]):
            name = getattr(entry, "artist", None) or getattr(entry, "name", "")
            aid = getattr(entry, "artist_id", "") or ""
            secs = float(getattr(entry, "listen_seconds", 0.0) or 0.0)
            w = (float(n_art - idx) * 0.5 + min(4.0, secs / 600.0)) * scale
            credit_artist(aid, str(name), w)

    # 5. Explicitly passed stats_top_tracks (when stats_track_entries wasn't passed)
    for idx, tr in enumerate(stats_top_tracks[:15]):
        if tr and tr.id and tr.id not in skip_penalized:
            add_seed_weight(tr, max(0.5, (15 - idx) * 0.25))
            credit_categories(tr, 1.0)

    ranked_artists = sorted(scores.values(), key=lambda a: a.score, reverse=True)
    ranked_seeds = [
        tr
        for tr, _ in sorted(track_weights.values(), key=lambda item: item[1], reverse=True)
        if tr.id not in skip_penalized
    ]

    return TasteProfile(
        artists=ranked_artists,
        seed_tracks=ranked_seeds,
        heard_ids=heard_ids,
        top_categories=cat_scores,
        skip_penalized_ids=skip_penalized,
    )


def score_recommendation(track: Track, profile: TasteProfile) -> float:
    """Score a recommendation candidate against the listener's history taste profile."""
    score = 0.0

    # Artist affinity
    artist_scores = {a.name.lower(): a.score for a in profile.artists[:20]}
    max_art = max(artist_scores.values(), default=1.0)
    for name in artist_set(track.artist):
        if name in artist_scores:
            score += 3.5 * (artist_scores[name] / max_art)

    # Category / language / style alignment with listening history
    if profile.top_categories:
        total_cat = max(1.0, sum(v for k, v in profile.top_categories.items() if k.startswith("lang:")))
        cat = infer_track_categories(track)
        lang_share = profile.top_categories.get(f"lang:{cat.language}", 0.0) / total_cat
        score += lang_share * 5.0
        if lang_share == 0.0 and cat.language != "english":
            score -= 3.5
        for st in cat.styles:
            if profile.top_categories.get(f"style:{st}", 0.0) > 0:
                score += 1.2 if st != "studio" else 0.4
        if (cat.styles & _VIBE_NOVELTY_NOISE) and not any(
            profile.top_categories.get(f"style:{s}", 0.0) > 0 for s in _VIBE_NOVELTY_NOISE
        ):
            score -= 3.5

    # Novelty & skip avoidance
    if track.id in profile.skip_penalized_ids:
        score -= 8.0
    elif track.id not in profile.heard_ids:
        score += 2.2
    else:
        score -= 0.8

    if is_likely_video(track):
        score -= 1.2

    return score


def rank_recommendations(
    candidates: Sequence[Track],
    profile: TasteProfile,
    limit: int = 50,
) -> List[Track]:
    """Score, deduplicate, and diversity-cap candidates for personalized Home picks."""
    if not candidates or limit <= 0:
        return []
    if not profile.has_history:
        return extend_station([], candidates, limit=limit)

    scored = sorted(
        candidates,
        key=lambda tr: score_recommendation(tr, profile),
        reverse=True,
    )
    fresh = [t for t in scored if t.id not in profile.heard_ids]
    picked = extend_station([], fresh, limit=limit)
    if len(picked) < limit:
        picked.extend(extend_station(picked, scored, limit=limit - len(picked)))
    return picked[:limit]


def build_category_queue(
    seed: Track,
    candidates: Sequence[Track],
    profile: Optional[TasteProfile] = None,
    limit: int = 30,
    prefer_videos: Optional[bool] = None,
) -> List[Track]:
    """Rank and filter candidates so queued tracks match ``seed``'s category & vibe."""
    if limit <= 0 or not candidates:
        return []

    seed_cat = infer_track_categories(seed)
    want_video = seed_cat.is_video if prefer_videos is None else prefer_videos
    artist_scores = (
        {a.name.lower(): a.score for a in profile.artists[:15]}
        if profile and profile.artists
        else {}
    )
    max_art = max(artist_scores.values(), default=1.0)
    skip_ids = profile.skip_penalized_ids if profile else set()

    scored_pairs: List[Tuple[float, Track]] = []
    for idx, cand in enumerate(candidates):
        if not cand or not cand.id or is_same_recording(seed, cand):
            continue
        if cand.id in skip_ids:
            continue

        sim = category_similarity(seed, cand, seed_cat=seed_cat)
        # Preserve mild collaborative-filtering position prior from YouTube Music radio/related
        rank_prior = max(0.0, 2.0 - idx * 0.05)
        taste_bonus = 0.0
        for a in artist_set(cand.artist):
            if a in artist_scores:
                taste_bonus += 1.2 * (artist_scores[a] / max_art)

        video_bias = 0.0
        if want_video and is_likely_video(cand):
            video_bias = 1.0
        elif not want_video and is_likely_video(cand):
            video_bias = -1.5

        total = sim + rank_prior + taste_bonus + video_bias
        scored_pairs.append((total, cand))

    scored_pairs.sort(key=lambda item: item[0], reverse=True)

    # First pass: take tracks with non-negative category similarity; relax only if sparse
    coherent = [tr for sc, tr in scored_pairs if sc >= 0.0]
    fallback = [tr for sc, tr in scored_pairs if sc < 0.0]

    taken: List[Track] = [seed]
    out: List[Track] = []
    per_artist: Dict[str, int] = {}
    seed_artists = artist_set(seed.artist)

    for cand in coherent + fallback:
        if len(out) >= limit:
            break
        if any(is_same_recording(t, cand) for t in taken):
            continue
        artists = sorted(artist_set(cand.artist))
        if artists:
            key = artists[0]
            cap = (
                CATEGORY_SEED_ARTIST_LIMIT
                if any(a in seed_artists for a in artists)
                else PER_ARTIST_LIMIT
            )
            played = per_artist.get(key, 0)
            if played >= cap:
                continue
            per_artist[key] = played + 1
        taken.append(cand)
        out.append(cand)

    return out


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
