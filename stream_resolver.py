"""
stream_resolver.py - Direct YouTube audio stream resolver.

Resolves directly playable audio URLs with:
1. Session visitorData bootstrapped from youtube.com/sw.js_data.
2. Fast direct InnerTube /player walk (VISIONOS -> ANDROID_VR -> IOS) without
   adPlaybackContext or heavy JS extraction on the hot path.
3. Matched per-client HTTP headers (User-Agent, and Origin/Referer only for
   browser/TV clients — never sent for VISIONOS/mobile native clients).
4. Pre-playback 1 MiB byte-range probing so 403-after-1MiB or stalled CDNs are
   rejected before reaching mpv or the cache.
5. Coalesced in-flight resolution, 20-minute URL latency cache, 10-minute
   per-track client exclusion, and 10-minute permanent-unplayable verdict cache.
6. Serialised yt-dlp failsafe when all direct InnerTube clients are refused or
   require signature deciphering.
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp

# --------------------------------------------------------------------------- #
# Constants & Errors
# --------------------------------------------------------------------------- #

WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

_VISITOR_DATA_RE = re.compile(r"Cg[A-Za-z0-9_%-]{40,}")
_CPN_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"

_AGE_GATE_RE = re.compile(
    r"confirm your age|age-restricted|inappropriate for some users|sign in to confirm",
    re.IGNORECASE,
)

# StreamResolver constants
URL_TTL_SECONDS = 20 * 60.0
EXCLUDE_TTL_SECONDS = 10 * 60.0
UNPLAYABLE_TTL_SECONDS = 10 * 60.0
MAX_REMEMBERED = 64

AUTH_BOUNDARY_BYTES = 1024 * 1024  # 1 MiB (CDN 403 boundary)
PROBE_READ_BYTES = 16 * 1024       # 16 KiB body arrival check
PROBE_TIMEOUT_SECONDS = 6.0
PLAYER_TIMEOUT_SECONDS = 8.0

_REFUSAL_CODES = {401, 403, 404, 410}


class PermanentlyUnplayableError(RuntimeError):
    """Track cannot be played regardless of client retries (age gate, region, removed, etc.)."""

    def __init__(self, reason: str, age_gated: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.age_gated = age_gated


@dataclass(frozen=True)
class ClientProfile:
    """An InnerTube identity and the media headers googlevideo expects for it."""

    profile_id: str
    client_name: str
    client_version: str
    user_agent: str
    endpoint_host: str = "https://music.youtube.com"
    origin: Optional[str] = None
    os_name: Optional[str] = None
    os_version: Optional[str] = None
    device_make: Optional[str] = None
    device_model: Optional[str] = None
    android_sdk_version: Optional[int] = None
    platform: Optional[str] = None

    def media_headers(self) -> Dict[str, str]:
        """Headers required when fetching googlevideo media URLs minted by this client."""
        headers: Dict[str, str] = {"User-Agent": self.user_agent}
        if self.origin:
            headers["Origin"] = self.origin
            headers["Referer"] = f"{self.origin}/"
        return headers

    def player_headers(self, visitor_data: Optional[str]) -> Dict[str, str]:
        """Headers sent on the /youtubei/v1/player call."""
        headers: Dict[str, str] = {
            "User-Agent": self.user_agent,
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if self.origin:
            headers["Origin"] = self.origin
            headers["Referer"] = f"{self.origin}/"
            headers["X-Origin"] = self.origin
        if visitor_data:
            headers["X-Goog-Visitor-Id"] = visitor_data
        return headers

    def client_context(self, visitor_data: Optional[str]) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {
            "clientName": self.client_name,
            "clientVersion": self.client_version,
            "hl": "en",
            "gl": "US",
        }
        if visitor_data:
            ctx["visitorData"] = visitor_data
        if self.os_name:
            ctx["osName"] = self.os_name
        if self.os_version:
            ctx["osVersion"] = self.os_version
        if self.device_make:
            ctx["deviceMake"] = self.device_make
        if self.device_model:
            ctx["deviceModel"] = self.device_model
        if self.android_sdk_version is not None:
            ctx["androidSdkVersion"] = self.android_sdk_version
        if self.platform:
            ctx["platform"] = self.platform
        return ctx


# Ordered direct-stream InnerTube candidates:
# - VISIONOS_0_1 / VISIONOS_1_02 return fast direct Opus/AAC URLs without ciphers or PoTokens
# - ANDROID_VR_1_43 / ANDROID_VR_1_65 act as fallback direct clients (checked via 1 MiB probe)
# - IOS_21_26 provides native HLS/direct fallback
_CLIENT_PROFILES: Tuple[ClientProfile, ...] = (
    ClientProfile(
        profile_id="VISIONOS_0_1",
        client_name="VISIONOS",
        client_version="0.1",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
        ),
        endpoint_host="https://music.youtube.com",
        os_name="VISION_OS",
        os_version="1.3",
        device_make="Apple",
        device_model="RealityDevice14,1",
        platform="MOBILE",
    ),
    ClientProfile(
        profile_id="VISIONOS_1_02",
        client_name="VISIONOS",
        client_version="1.02",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_7_3) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"
        ),
        endpoint_host="https://music.youtube.com",
        os_name="visionOS",
        os_version="26.5.23O471",
        device_make="Apple",
        device_model="RealityDevice17,1",
    ),
    ClientProfile(
        profile_id="ANDROID_VR_1_43",
        client_name="ANDROID_VR",
        client_version="1.43.32",
        user_agent=(
            "com.google.android.apps.youtube.vr.oculus/1.43.32 "
            "(Linux; U; Android 12; en_US; Quest 3; Build/SQ3A.220605.009.A1; Cronet/107.0.5284.2)"
        ),
        endpoint_host="https://music.youtube.com",
        os_name="Android",
        os_version="12",
        device_make="Oculus",
        device_model="Quest 3",
        android_sdk_version=32,
    ),
    ClientProfile(
        profile_id="ANDROID_VR_1_65",
        client_name="ANDROID_VR",
        client_version="1.65.10",
        user_agent=(
            "com.google.android.apps.youtube.vr.oculus/1.65.10 "
            "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
        ),
        endpoint_host="https://music.youtube.com",
        os_name="Android",
        os_version="12L",
        device_make="Oculus",
        device_model="Quest 3",
        android_sdk_version=32,
    ),
    ClientProfile(
        profile_id="IOS_21_26",
        client_name="IOS",
        client_version="21.26.4",
        user_agent="com.google.ios.youtube/21.26.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)",
        endpoint_host="https://www.youtube.com",
        os_name="iPhone",
        os_version="18.3.2.22D82",
        device_make="Apple",
        device_model="iPhone16,2",
    ),
)

_MEDIA_FALLBACK_CLIENTS: Dict[str, Dict[str, str]] = {
    "IOS": {
        "User-Agent": "com.google.ios.youtube/21.26.4 (iPhone16,2; U; CPU iOS 18_3_2 like Mac OS X;)"
    },
    "ANDROID_VR": {
        "User-Agent": (
            "com.google.android.apps.youtube.vr.oculus/1.65.10 "
            "(Linux; U; Android 12L; eureka-user Build/SQ3A.220605.009.A1) gzip"
        )
    },
    "ANDROID_MUSIC": {
        "User-Agent": (
            "com.google.android.apps.youtube.music/8.39.42 "
            "(Linux; U; Android 15; en_US; Pixel 9 Pro; Build/AP4A.250205.002) gzip"
        )
    },
    "ANDROID": {
        "User-Agent": (
            "com.google.android.youtube/21.26.364 "
            "(Linux; U; Android 15; en_US; Pixel 9 Pro; Build/AP4A.250205.002; Cronet/132.0.6834.79) gzip"
        )
    },
    "TVHTML5": {
        "User-Agent": (
            "Mozilla/5.0(SMART-TV; Linux; Tizen 4.0.0.2) AppleWebkit/605.1.15 "
            "(KHTML, like Gecko) SamsungBrowser/9.2 TV Safari/605.1.15"
        ),
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
    },
    "WEB_REMIX": {
        "User-Agent": WEB_USER_AGENT,
        "Origin": "https://music.youtube.com",
        "Referer": "https://music.youtube.com/",
    },
    "WEB": {
        "User-Agent": WEB_USER_AGENT,
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
    },
    "MWEB": {
        "User-Agent": WEB_USER_AGENT,
        "Origin": "https://www.youtube.com",
        "Referer": "https://www.youtube.com/",
    },
    "VISIONOS": {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
        ),
    },
}


def media_headers_for_url(url: str) -> Dict[str, str]:
    """Infer matched googlevideo headers from a stream URL's `c=` query param."""
    try:
        parsed = urlparse(url)
        client = (parse_qs(parsed.query).get("c") or [""])[0].upper()
    except Exception:
        return dict(_MEDIA_FALLBACK_CLIENTS["WEB"])

    if client.startswith("VISIONOS"):
        return dict(_MEDIA_FALLBACK_CLIENTS["VISIONOS"])
    if client.startswith("IOS"):
        return dict(_MEDIA_FALLBACK_CLIENTS["IOS"])
    if client == "ANDROID_VR":
        return dict(_MEDIA_FALLBACK_CLIENTS["ANDROID_VR"])
    if client == "ANDROID_MUSIC":
        return dict(_MEDIA_FALLBACK_CLIENTS["ANDROID_MUSIC"])
    if client.startswith("ANDROID"):
        return dict(_MEDIA_FALLBACK_CLIENTS["ANDROID"])
    if client.startswith("TVHTML5"):
        return dict(_MEDIA_FALLBACK_CLIENTS["TVHTML5"])
    if client == "WEB_REMIX":
        return dict(_MEDIA_FALLBACK_CLIENTS["WEB_REMIX"])
    if client.startswith("WEB") or client == "MWEB":
        return dict(_MEDIA_FALLBACK_CLIENTS["WEB"])
    return dict(_MEDIA_FALLBACK_CLIENTS["IOS"])


@dataclass
class ResolvedStream:
    """Resolved audio stream ready for mpv or AudioCache."""

    video_id: str
    url: str
    quality: str
    codec: str = "opus"
    kbps: int = 0
    mime_type: str = "audio/webm"
    client_id: str = "UNKNOWN"
    headers: Dict[str, str] = field(default_factory=dict)
    loudness_db: Optional[float] = None
    content_length: Optional[int] = None
    is_live: bool = False
    expires_at: float = 0.0
    cached_path: Optional[str] = None

    @property
    def download_extension(self) -> str:
        low = self.mime_type.lower()
        if "mp4" in low or "m4a" in low or self.codec.lower() in ("aac", "m4a", "mp4a"):
            return "m4a"
        return "webm"


def _new_cpn() -> str:
    return "".join(random.choice(_CPN_ALPHABET) for _ in range(16))


def _is_live_url(url: str) -> bool:
    try:
        qs = parse_qs(urlparse(url).query)
        return qs.get("live", [""])[0] == "1" or qs.get("source", [""])[0] == "yt_live_broadcast"
    except Exception:
        return False


def _content_length_from_url(url: str) -> Optional[int]:
    try:
        qs = parse_qs(urlparse(url).query)
        raw = qs.get("clen", [None])[0]
        return int(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None


def _classify_permanent_reason(status: str, reason: str) -> Optional[Tuple[str, bool]]:
    """Map YouTube playability status/reason to (user_message, age_gated) when retrying cannot help."""
    text = (reason or "").strip()
    low = text.lower()
    if _AGE_GATE_RE.search(low) or status == "LOGIN_REQUIRED" and ("age" in low or "confirm" in low):
        return ("This track is age-restricted on YouTube.", True)
    if "country" in low or "region" in low or "not available in your" in low:
        return ("This track isn't available in your country.", False)
    if "private" in low:
        return ("This track is private.", False)
    if "terminated" in low:
        return ("The channel behind this track was terminated.", False)
    if "members-only" in low or "premium" in low or "paid" in low:
        return ("This track requires YouTube Premium or channel membership.", False)
    if status == "ERROR" and (
        "removed" in low or "no longer available" in low or "does not exist" in low or "unavailable" in low
    ):
        return ("This track is unavailable on YouTube.", False)
    return None


def _pick_audio_format(formats: List[Dict[str, Any]], max_kbps: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Pick best direct audio-only format:
    - Exclude ciphered formats without a direct `url`
    - Exclude DRC renditions and non-default dubs when standard tracks exist
    - Prefer Opus (itag 251/250/249) over AAC/MP4 (140/139), picking highest bitrate under max_kbps
    """
    audio = [
        f for f in formats
        if isinstance(f, dict)
        and isinstance(f.get("url"), str)
        and f.get("url")
        and str(f.get("mimeType", "")).lower().startswith("audio/")
    ]
    if not audio:
        return None

    # Prefer original/default audio track when multi-track dubbing exists
    default_tracks = [
        f for f in audio
        if not isinstance(f.get("audioTrack"), dict) or f["audioTrack"].get("audioIsDefault", True)
    ]
    pool = default_tracks or audio

    # Avoid DRC (dynamic range compressed) variants when non-DRC variants exist
    non_drc = [
        f for f in pool
        if "drc" not in str(f.get("audioQuality", "")).lower()
        and not str(f.get("isDrc", "")).lower() == "true"
        and "drc" not in str((f.get("audioTrack") or {}).get("id", "")).lower()
    ]
    pool = non_drc or pool

    def _kbps(fmt: Dict[str, Any]) -> int:
        br = fmt.get("bitrate") or fmt.get("averageBitrate") or 0
        return int(round(float(br) / 1000.0)) if br > 1000 else int(br or 0)

    def _choose(candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
        if max_kbps is not None:
            under = [c for c in candidates if _kbps(c) <= max_kbps]
            if under:
                return max(under, key=_kbps)
            return min(candidates, key=_kbps)
        return max(candidates, key=_kbps)

    opus = [f for f in pool if "opus" in str(f.get("mimeType", "")).lower()]
    if opus:
        return _choose(opus)
    aac = [
        f for f in pool
        if "mp4a" in str(f.get("mimeType", "")).lower() or "mp4" in str(f.get("mimeType", "")).lower()
    ]
    if aac:
        return _choose(aac)
    return _choose(pool)


def _extract_loudness_db(player_json: Dict[str, Any], selected_fmt: Dict[str, Any]) -> Optional[float]:
    """Extract loudnessDb from format or audioConfig (per-format on VISIONOS, audioConfig on others)."""
    fmt_loud = selected_fmt.get("loudnessDb")
    if isinstance(fmt_loud, (int, float)):
        return float(fmt_loud)
    audio_cfg = (player_json.get("playerConfig") or {}).get("audioConfig") or {}
    cfg_loud = audio_cfg.get("loudnessDb")
    if isinstance(cfg_loud, (int, float)):
        return float(cfg_loud)
    abs_lkfs = audio_cfg.get("trackAbsoluteLoudnessLkfs")
    target_lkfs = audio_cfg.get("loudnessTargetLkfs")
    if isinstance(abs_lkfs, (int, float)) and isinstance(target_lkfs, (int, float)):
        return float(abs_lkfs) - float(target_lkfs)
    return None


class StreamResolver:
    """
    Resolves, probes, and caches audio stream URLs using InnerTube client walk
    with yt-dlp as a serialised failsafe.
    """

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        max_kbps: Optional[int] = None,
        enable_direct_innertube: Optional[bool] = None,
    ) -> None:
        self._session = session or requests.Session()
        self.max_kbps = max_kbps
        if enable_direct_innertube is None:
            env = os.environ.get("AME_DISABLE_INNERTUBE", "").strip().lower()
            enable_direct_innertube = env not in ("1", "true", "yes")
        self.enable_direct_innertube = enable_direct_innertube

        self._lock = threading.RLock()
        self._visitor_data: Optional[str] = None
        self._visitor_fetched_at: float = 0.0

        # In-memory and persistent stream/failure caches
        self._recent: Dict[str, ResolvedStream] = {}
        self._excluded: Dict[str, Dict[str, float]] = {}     # video_id -> {profile_id: expiry_ts}
        self._unplayable: Dict[str, Tuple[str, bool, float]] = {}  # video_id -> (reason, age_gated, expiry_ts)
        self._loudness: Dict[str, float] = {}

        # Coalesced in-flight resolution per video_id
        self._in_flight: Dict[str, "_InFlightResolve"] = {}
        # Serialise yt-dlp JS player execution
        self._ytdlp_gate = threading.Lock()

        self._ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "extract_flat": False,
        }

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def resolve(self, video_id: str) -> Tuple[str, str]:
        """Backwards-compatible entry point returning ``(stream_url, quality_str)``."""
        stream = self.resolve_stream(video_id)
        return stream.url, stream.quality

    def resolve_stream(
        self,
        video_id: str,
        skip_clients: Optional[Iterable[str]] = None,
    ) -> ResolvedStream:
        """Resolve and probe a verified stream for ``video_id``."""
        extra_skip: Set[str] = set(skip_clients or ())
        now = time.monotonic()

        with self._lock:
            if not extra_skip:
                cached = self._recent.get(video_id)
                if cached is not None and now < cached.expires_at:
                    return cached

            verdict = self._unplayable.get(video_id)
            if verdict is not None:
                reason, age_gated, exp = verdict
                if now < exp:
                    raise PermanentlyUnplayableError(reason, age_gated=age_gated)
                del self._unplayable[video_id]

        # Coalesce concurrent resolves for the same video_id when not forcing extra skips
        if not extra_skip:
            return self._coalesced_resolve(video_id)
        return self._resolve_uncached(video_id, extra_skip)

    def loudness_db_for(self, video_id: str) -> Optional[float]:
        with self._lock:
            return self._loudness.get(video_id)

    def on_playback_refused(
        self,
        video_id: str,
        url: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> None:
        """
        Forget cached URL and bench the client that minted it for 10 minutes
        (invalidating caches and excluding the failed client).
        """
        now = time.monotonic()
        with self._lock:
            cached = self._recent.get(video_id)
            if cached is not None and (url is None or cached.url == url):
                if client_id is None:
                    client_id = cached.client_id
                del self._recent[video_id]
            if client_id:
                entries = self._excluded.setdefault(video_id, {})
                entries[client_id] = now + EXCLUDE_TTL_SECONDS
                if len(self._excluded) > MAX_REMEMBERED:
                    oldest_key = next(iter(self._excluded))
                    if oldest_key != video_id:
                        del self._excluded[oldest_key]

    def clear_caches(self) -> None:
        with self._lock:
            self._recent.clear()
            self._excluded.clear()
            self._unplayable.clear()

    # ------------------------------------------------------------------ #
    # Coalescing & Uncached Resolution
    # ------------------------------------------------------------------ #

    def _coalesced_resolve(self, video_id: str) -> ResolvedStream:
        while True:
            is_owner = False
            with self._lock:
                # Re-check cache after acquiring lock
                cached = self._recent.get(video_id)
                if cached is not None and time.monotonic() < cached.expires_at:
                    return cached

                flight = self._in_flight.get(video_id)
                if flight is None:
                    flight = _InFlightResolve()
                    self._in_flight[video_id] = flight
                    is_owner = True

            if not is_owner:
                flight.done.wait(timeout=30.0)
                if flight.error is not None:
                    raise flight.error
                if flight.result is not None:
                    return flight.result
                continue

            try:
                result = self._resolve_uncached(video_id, set())
                flight.result = result
                return result
            except BaseException as exc:
                flight.error = exc if isinstance(exc, Exception) else RuntimeError(str(exc))
                raise
            finally:
                with self._lock:
                    if self._in_flight.get(video_id) is flight:
                        del self._in_flight[video_id]
                flight.done.set()

    def _resolve_uncached(self, video_id: str, extra_skip: Set[str]) -> ResolvedStream:
        excluded = self._excluded_for(video_id) | extra_skip
        permanent_reasons: List[Tuple[str, bool]] = []
        last_error: Optional[str] = None

        # 1. Direct InnerTube client walk (hot path)
        if self.enable_direct_innertube:
            visitor_data = self._ensure_visitor_data()
            refreshed_visitor = False

            for profile in _CLIENT_PROFILES:
                if profile.profile_id in excluded:
                    continue

                try:
                    candidate, status_info = self._try_innertube_client(video_id, profile, visitor_data)
                except Exception as exc:
                    last_error = f"{profile.profile_id}: {exc}"
                    continue

                if status_info is not None:
                    st, reason = status_info
                    # Refresh visitorData once on bot/session error
                    if not refreshed_visitor and (
                        "not a bot" in reason.lower() or st == "LOGIN_REQUIRED"
                    ):
                        refreshed_visitor = True
                        new_vd = self._ensure_visitor_data(refresh=True)
                        if new_vd and new_vd != visitor_data:
                            visitor_data = new_vd
                            try:
                                candidate, status_info = self._try_innertube_client(
                                    video_id, profile, visitor_data
                                )
                            except Exception:
                                candidate = None

                    if candidate is None and status_info is not None:
                        st, reason = status_info
                        last_error = f"{profile.profile_id} ({st}: {reason})"
                        perm = _classify_permanent_reason(st, reason)
                        if perm is not None:
                            permanent_reasons.append(perm)
                        continue

                if candidate is None:
                    continue

                verdict = self._probe(candidate.url, candidate.headers, is_live=candidate.is_live)
                if verdict == "OK":
                    self._remember_resolved(video_id, candidate)
                    return candidate

                last_error = f"{profile.profile_id} probe={verdict}"
                self._exclude_client(video_id, profile.profile_id)

        # 2. Failsafe: yt-dlp extraction
        if "YTDLP" not in excluded:
            try:
                fallback = self._extract_ytdlp(video_id)
                if fallback is not None:
                    verdict = self._probe(fallback.url, fallback.headers, is_live=fallback.is_live)
                    if verdict == "OK":
                        self._remember_resolved(video_id, fallback)
                        return fallback
                    last_error = f"yt-dlp probe={verdict}"
                    self._exclude_client(video_id, "YTDLP")
            except PermanentlyUnplayableError as perm_err:
                self._remember_unplayable(video_id, perm_err.reason, perm_err.age_gated)
                raise
            except Exception as exc:
                last_error = f"yt-dlp: {exc}"

        # If every tried path reported a permanent refusal (e.g. age-restricted / geo-blocked)
        if permanent_reasons:
            reason_msg, age_gated = permanent_reasons[0]
            self._remember_unplayable(video_id, reason_msg, age_gated)
            raise PermanentlyUnplayableError(reason_msg, age_gated=age_gated)

        raise RuntimeError(
            f"Failed to resolve audio stream for {video_id}: {last_error or 'no playable stream'}"
        )

    # ------------------------------------------------------------------ #
    # InnerTube Direct Client Extraction
    # ------------------------------------------------------------------ #

    def _ensure_visitor_data(self, refresh: bool = False) -> Optional[str]:
        now = time.monotonic()
        with self._lock:
            if not refresh and self._visitor_data and (now - self._visitor_fetched_at) < 3600.0:
                return self._visitor_data
        try:
            resp = self._session.get(
                "https://www.youtube.com/sw.js_data",
                headers={"User-Agent": WEB_USER_AGENT},
                timeout=6.0,
            )
            if resp.ok:
                match = _VISITOR_DATA_RE.search(resp.text)
                if match:
                    vd = match.group(0)
                    with self._lock:
                        self._visitor_data = vd
                        self._visitor_fetched_at = time.monotonic()
                    return vd
        except Exception:
            pass
        with self._lock:
            return self._visitor_data

    def _try_innertube_client(
        self,
        video_id: str,
        profile: ClientProfile,
        visitor_data: Optional[str],
    ) -> Tuple[Optional[ResolvedStream], Optional[Tuple[str, str]]]:
        payload = {
            "context": {
                "client": profile.client_context(visitor_data),
                "user": {"lockedSafetyMode": False},
                "request": {"useSsl": True},
            },
            "videoId": video_id,
            "contentCheckOk": True,
            "racyCheckOk": True,
            "cpn": _new_cpn(),
        }
        resp = self._session.post(
            f"{profile.endpoint_host}/youtubei/v1/player?prettyPrint=false",
            headers=profile.player_headers(visitor_data),
            json=payload,
            timeout=PLAYER_TIMEOUT_SECONDS,
        )
        if not resp.ok:
            return None, (f"HTTP_{resp.status_code}", resp.reason or "request failed")

        data = resp.json()
        ps = data.get("playabilityStatus") or {}
        status = str(ps.get("status") or "")
        reason = str(ps.get("reason") or "")
        if status != "OK":
            return None, (status or "UNPLAYABLE", reason or "unavailable")

        sd = data.get("streamingData") or {}
        video_details = data.get("videoDetails") or {}
        is_live_video = bool(video_details.get("isLiveContent"))

        # Check adaptive formats first
        adaptive = sd.get("adaptiveFormats") or []
        fmt = _pick_audio_format(adaptive, max_kbps=self.max_kbps)

        hls_url = sd.get("hlsManifestUrl")
        if (is_live_video or (fmt and _is_live_url(fmt["url"]))) and isinstance(hls_url, str) and hls_url:
            loudness = _extract_loudness_db(data, fmt or {})
            return (
                ResolvedStream(
                    video_id=video_id,
                    url=hls_url,
                    quality=f"HLS LIVE · {profile.client_name}",
                    codec="hls",
                    kbps=0,
                    mime_type="application/x-mpegURL",
                    client_id=profile.profile_id,
                    headers=profile.media_headers(),
                    loudness_db=loudness,
                    is_live=True,
                ),
                None,
            )

        if fmt is None:
            # Some clients (e.g. IOS) may only return HLS or muxed formats
            if isinstance(hls_url, str) and hls_url:
                return (
                    ResolvedStream(
                        video_id=video_id,
                        url=hls_url,
                        quality=f"HLS · {profile.client_name}",
                        codec="hls",
                        kbps=0,
                        mime_type="application/x-mpegURL",
                        client_id=profile.profile_id,
                        headers=profile.media_headers(),
                        loudness_db=_extract_loudness_db(data, {}),
                        is_live=is_live_video,
                    ),
                    None,
                )
            return None, ("NO_AUDIO_STREAM", "no direct audio stream")

        url = fmt["url"]
        mime = str(fmt.get("mimeType") or "audio/webm")
        codec = "opus" if "opus" in mime.lower() else ("aac" if "mp4" in mime.lower() else "audio")
        raw_br = fmt.get("bitrate") or fmt.get("averageBitrate") or 0
        kbps = int(round(float(raw_br) / 1000.0)) if raw_br > 1000 else int(raw_br or 0)
        clen_raw = fmt.get("contentLength") or _content_length_from_url(url)
        clen = int(clen_raw) if clen_raw is not None else None
        loudness = _extract_loudness_db(data, fmt)

        quality_parts = [codec.upper()]
        if kbps > 0:
            quality_parts.append(f"{kbps}kbps")
        quality = f"{' '.join(quality_parts)} · {profile.client_name}"

        return (
            ResolvedStream(
                video_id=video_id,
                url=url,
                quality=quality,
                codec=codec,
                kbps=kbps,
                mime_type=mime,
                client_id=profile.profile_id,
                headers=profile.media_headers(),
                loudness_db=loudness,
                content_length=clen,
                is_live=_is_live_url(url) or is_live_video,
            ),
            None,
        )

    # ------------------------------------------------------------------ #
    # Failsafe: yt-dlp
    # ------------------------------------------------------------------ #

    def _extract_ytdlp(self, video_id: str) -> Optional[ResolvedStream]:
        with self._ytdlp_gate:
            try:
                with yt_dlp.YoutubeDL(self._ydl_opts) as ydl:
                    info = ydl.extract_info(
                        f"https://www.youtube.com/watch?v={video_id}", download=False
                    )
                if info and info.get("url"):
                    stream_url = info["url"]
                    acodec = (info.get("acodec") or "audio").replace("none", "").strip().upper()
                    abr = info.get("abr")
                    kbps = int(round(float(abr))) if abr else 0
                    ext = str(info.get("ext") or "")
                    mime = "audio/mp4" if ext in ("m4a", "mp4") else "audio/webm"
                    headers = dict(info.get("http_headers") or {}) or media_headers_for_url(stream_url)
                    # Prefer client-matched headers for googlevideo URLs when c= is known
                    if "googlevideo.com" in stream_url:
                        matched = media_headers_for_url(stream_url)
                        if "Origin" not in matched:
                            headers.pop("Origin", None)
                            headers.pop("Referer", None)
                        headers.update(matched)
                    clen = info.get("filesize") or info.get("filesize_approx") or _content_length_from_url(stream_url)
                    is_live = bool(info.get("is_live")) or _is_live_url(stream_url)
                    quality = f"{acodec} {kbps}kbps · yt-dlp" if kbps else f"{acodec or 'Audio'} · yt-dlp"
                    return ResolvedStream(
                        video_id=video_id,
                        url=stream_url,
                        quality=quality,
                        codec=acodec.lower() if acodec else "audio",
                        kbps=kbps,
                        mime_type=mime,
                        client_id="YTDLP",
                        headers=headers,
                        content_length=int(clen) if clen else None,
                        is_live=is_live,
                    )
            except Exception as exc:
                perm = _classify_permanent_reason("ERROR", str(exc))
                if perm is not None:
                    raise PermanentlyUnplayableError(perm[0], age_gated=perm[1]) from exc

                # CLI fallback when library raises
                cmd = ["yt-dlp", "--no-warnings", "-f", "bestaudio", "-g", video_id]
                res = subprocess.run(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15
                )
                if res.returncode == 0 and res.stdout.strip():
                    stream_url = res.stdout.strip().splitlines()[0]
                    return ResolvedStream(
                        video_id=video_id,
                        url=stream_url,
                        quality="Audio · yt-dlp",
                        codec="opus",
                        kbps=0,
                        mime_type="audio/webm",
                        client_id="YTDLP",
                        headers=media_headers_for_url(stream_url),
                        content_length=_content_length_from_url(stream_url),
                        is_live=_is_live_url(stream_url),
                    )
                cli_perm = _classify_permanent_reason("ERROR", res.stderr or str(exc))
                if cli_perm is not None:
                    raise PermanentlyUnplayableError(cli_perm[0], age_gated=cli_perm[1]) from exc
                raise RuntimeError(str(exc)) from exc
        return None

    # ------------------------------------------------------------------ #
    # Pre-playback 1 MiB Byte-Range Probe
    # ------------------------------------------------------------------ #

    def _probe(self, url: str, headers: Dict[str, str], is_live: bool = False) -> str:
        """
        Verify a resolved URL actually serves media bytes past the 1 MiB auth boundary.
        Returns "OK", "REFUSED", or "UNREACHABLE".
        """
        if is_live or ".m3u8" in url:
            try:
                resp = self._session.get(url, headers=headers, timeout=PROBE_TIMEOUT_SECONDS)
                if resp.status_code in _REFUSAL_CODES:
                    return "REFUSED"
                if not resp.ok:
                    return "UNREACHABLE"
                return "OK" if "#EXTM3U" in resp.text else "REFUSED"
            except Exception:
                return "UNREACHABLE"

        clen = _content_length_from_url(url)
        start = (
            AUTH_BOUNDARY_BYTES
            if clen is not None and clen > AUTH_BOUNDARY_BYTES + PROBE_READ_BYTES
            else 0
        )
        end = start + PROBE_READ_BYTES - 1
        if clen is not None and end >= clen:
            end = max(start, clen - 1)
        expected_min = min(PROBE_READ_BYTES, (end - start + 1))

        req_headers = dict(headers)
        req_headers["Range"] = f"bytes={start}-{end}"

        try:
            with self._session.get(
                url, headers=req_headers, stream=True, timeout=PROBE_TIMEOUT_SECONDS
            ) as resp:
                if resp.status_code in _REFUSAL_CODES:
                    return "REFUSED"
                if not (200 <= resp.status_code <= 299) and resp.status_code != 416:
                    return "UNREACHABLE"
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if ctype and not (ctype.startswith("audio/") or ctype.startswith("application/octet-stream")):
                    return "REFUSED"

                received = 0
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        received += len(chunk)
                        if received >= expected_min:
                            return "OK"
                return "OK" if received > 0 and (clen is not None and received >= min(expected_min, clen - start)) else "UNREACHABLE"
        except Exception:
            return "UNREACHABLE"

    # ------------------------------------------------------------------ #
    # Cache & Exclusion Bookkeeping
    # ------------------------------------------------------------------ #

    def _remember_resolved(self, video_id: str, stream: ResolvedStream) -> None:
        now = time.monotonic()
        stream.expires_at = now + URL_TTL_SECONDS
        with self._lock:
            if len(self._recent) >= MAX_REMEMBERED:
                expired = [k for k, v in self._recent.items() if now >= v.expires_at]
                for k in expired:
                    del self._recent[k]
                if len(self._recent) >= MAX_REMEMBERED:
                    self._recent.clear()
            self._recent[video_id] = stream
            if stream.loudness_db is not None:
                self._loudness[video_id] = stream.loudness_db

    def _exclude_client(self, video_id: str, profile_id: str) -> None:
        now = time.monotonic()
        with self._lock:
            entries = self._excluded.setdefault(video_id, {})
            entries[profile_id] = now + EXCLUDE_TTL_SECONDS
            if len(self._excluded) > MAX_REMEMBERED:
                oldest = next(iter(self._excluded))
                if oldest != video_id:
                    del self._excluded[oldest]

    def _excluded_for(self, video_id: str) -> Set[str]:
        now = time.monotonic()
        with self._lock:
            entries = self._excluded.get(video_id)
            if not entries:
                return set()
            expired = [cid for cid, exp in entries.items() if exp <= now]
            for cid in expired:
                del entries[cid]
            return set(entries.keys())

    def _remember_unplayable(self, video_id: str, reason: str, age_gated: bool) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._unplayable) >= MAX_REMEMBERED:
                self._unplayable.clear()
            self._unplayable[video_id] = (reason, age_gated, now + UNPLAYABLE_TTL_SECONDS)


class _InFlightResolve:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Optional[ResolvedStream] = None
        self.error: Optional[Exception] = None
