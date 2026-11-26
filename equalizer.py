"""
equalizer.py - 7-band audio equalizer presets & mpv lavfi filter chain builder.

Modeled on BitChord's EqualizerPreset & EqCurve:
- 7 fixed frequency centres (60 Hz low shelf, 150/400/1k/2.5k/6k Hz bells,
  14 kHz high shelf) with Q = 1.0.
- Half-max-boost preamp attenuation (-0.5 * max(0, max_boost)) so boosting
  a band preserves perceived loudness without clipping.
- Composes cleanly with YouTube loudnessDb normalization and Skip Silence
  (ffmpeg silenceremove) into a single mpv ``af`` string.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# 7 centres matching BitChord's EqLayout.MANUAL_BANDS_HZ
EQ_BANDS_HZ: Tuple[int, ...] = (60, 150, 400, 1000, 2500, 6000, 14000)
EQ_Q: float = 1.0

# Skip-silence tuning (ffmpeg silenceremove)
SILENCE_FILTER = "silenceremove=stop_periods=-1:stop_duration=0.35:stop_threshold=-42dB"


@dataclass(frozen=True)
class EqPreset:
    """Single 7-band equalizer preset."""

    id: str
    label: str
    gains_db: Tuple[float, float, float, float, float, float, float]


EQ_PRESETS: Tuple[EqPreset, ...] = (
    EqPreset("flat", "Flat", (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
    EqPreset("acoustic", "Acoustic", (3.0, 1.5, 0.0, 1.5, 2.5, 2.0, 1.0)),
    EqPreset("bass_boost", "Bass Boost", (6.0, 4.0, 1.5, 0.0, 0.0, 0.0, 0.0)),
    EqPreset("bass_cut", "Bass Cut", (-6.0, -4.0, -1.5, 0.0, 0.0, 0.0, 0.0)),
    EqPreset("vocal", "Vocal", (-3.0, -1.5, 1.0, 3.5, 3.0, 1.0, -1.0)),
    EqPreset("treble_boost", "Treble Boost", (0.0, 0.0, 0.0, 0.0, 1.5, 3.5, 5.0)),
    EqPreset("treble_cut", "Treble Cut", (0.0, 0.0, 0.0, 0.0, -1.5, -3.5, -5.0)),
    EqPreset("loudness", "Loudness", (6.0, 3.5, 0.0, -1.5, -1.0, 2.0, 5.0)),
    EqPreset("spoken_word", "Spoken Word", (-5.0, -2.5, 1.5, 4.0, 3.5, 1.5, -2.0)),
    EqPreset("electronic", "Electronic", (5.0, 3.0, -1.0, 0.0, 1.0, 3.0, 4.0)),
    EqPreset("rock", "Rock", (4.0, 2.5, -1.0, -1.5, 1.0, 3.0, 3.5)),
    EqPreset("hip_hop", "Hip-Hop", (6.0, 4.0, 0.5, -1.0, 0.5, 2.0, 2.5)),
    EqPreset("jazz", "Jazz", (3.0, 1.5, 0.0, 1.0, 1.5, 2.0, 2.5)),
    EqPreset("classical", "Classical", (3.0, 2.0, 0.0, 0.0, 1.0, 2.5, 3.0)),
    EqPreset("small_speakers", "Small Speakers", (5.0, 4.0, 2.0, 0.5, 0.0, -1.0, -2.0)),
    EqPreset("late_night", "Late Night", (3.0, 1.0, 0.0, 1.5, 1.0, -1.0, -3.0)),
)

EQ_PRESET_MAP: Dict[str, EqPreset] = {p.id: p for p in EQ_PRESETS}
EQ_PRESET_ORDER: Tuple[str, ...] = tuple(p.id for p in EQ_PRESETS)


def normalize_eq_preset_id(preset_id: Optional[str]) -> str:
    """Return a valid preset id, falling back to ``flat``."""
    key = (preset_id or "flat").strip().lower()
    return key if key in EQ_PRESET_MAP else "flat"


def get_eq_preset(preset_id: Optional[str]) -> EqPreset:
    return EQ_PRESET_MAP[normalize_eq_preset_id(preset_id)]


def next_eq_preset_id(current_id: Optional[str]) -> str:
    cur = normalize_eq_preset_id(current_id)
    idx = EQ_PRESET_ORDER.index(cur)
    return EQ_PRESET_ORDER[(idx + 1) % len(EQ_PRESET_ORDER)]


def compute_preamp_db(gains_db: Tuple[float, ...]) -> float:
    """Half-max-boost attenuation (<= 0 dB) matching BitChord's EqCurve."""
    peak_boost = max((g for g in gains_db if g > 0.0), default=0.0)
    if peak_boost <= 0.0:
        return 0.0
    return -0.5 * peak_boost


def build_eq_filters(preset_id: Optional[str]) -> List[str]:
    """Build ffmpeg filter nodes for active non-zero EQ bands."""
    preset = get_eq_preset(preset_id)
    filters: List[str] = []
    last_idx = len(EQ_BANDS_HZ) - 1
    for idx, (freq, gain) in enumerate(zip(EQ_BANDS_HZ, preset.gains_db)):
        if abs(gain) < 0.05:
            continue
        if idx == 0:
            filters.append(f"lowshelf=f={freq}:t=q:w={EQ_Q:.1f}:g={gain:.2f}")
        elif idx == last_idx:
            filters.append(f"highshelf=f={freq}:t=q:w={EQ_Q:.1f}:g={gain:.2f}")
        else:
            filters.append(f"equalizer=f={freq}:t=q:w={EQ_Q:.1f}:g={gain:.2f}")
    return filters


def build_mpv_af_chain(
    loudness_gain_db: Optional[float] = None,
    eq_preset: str = "flat",
    skip_silence: bool = False,
) -> str:
    """Compose an mpv ``af`` string combining loudness gain, EQ, and skip silence."""
    preset = get_eq_preset(eq_preset)
    preamp = compute_preamp_db(preset.gains_db)
    total_gain = float(loudness_gain_db or 0.0) + preamp
    eq_filters = build_eq_filters(preset.id)

    # Preserve simple single-node format when neither EQ nor skip silence is active
    if not eq_filters and not skip_silence:
        if abs(total_gain) >= 0.05:
            return f"volume=volume={total_gain:.2f}dB"
        return ""

    nodes: List[str] = []
    if abs(total_gain) >= 0.05:
        nodes.append(f"volume=volume={total_gain:.2f}dB")
    nodes.extend(eq_filters)
    if skip_silence:
        nodes.append(SILENCE_FILTER)
    return f"lavfi=[{','.join(nodes)}]"
