"""Tests for BitChord-inspired features: EQ, skip silence, word-synced lyrics, offsets, Spotify import & ListenBrainz."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from api import Track
from equalizer import (
    EQ_PRESETS,
    build_mpv_af_chain,
    compute_preamp_db,
    get_eq_preset,
    next_eq_preset_id,
)
from integrations import ListenBrainzScrobbler
from lyrics import (
    LyricLine,
    LyricsManager,
    LyricsOffsetStore,
    LyricsResult,
    LyricWord,
    active_lyric_index,
    active_word_index,
    add_instrumental_gaps,
    parse_enhanced_lrc,
    parse_ttml,
)
from player import MPVPlayer, _format_mpv_options
from spotify_import import (
    extract_spotify_playlist_id,
    import_spotify_playlist,
    match_best_track,
    parse_spotify_embed_html,
)


# ------------------------------------------------------------------ #
# 1. 7-band Equalizer & Skip Silence
# ------------------------------------------------------------------ #


class TestEqualizerAndSkipSilence:
    def test_preamp_uses_half_max_boost_and_preserves_flat_compat(self):
        assert len(EQ_PRESETS) == 16
        assert compute_preamp_db((0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)) == 0.0
        assert compute_preamp_db((6.0, 4.0, 1.5, 0.0, 0.0, 0.0, 0.0)) == -3.0
        assert compute_preamp_db((-6.0, -4.0, -1.5, 0.0, 0.0, 0.0, 0.0)) == 0.0

        # Flat + skip_silence=False matches simple volume= filter for backwards compat
        assert build_mpv_af_chain(-2.5, eq_preset="flat", skip_silence=False) == "volume=volume=-2.50dB"
        assert build_mpv_af_chain(None, eq_preset="flat", skip_silence=False) == ""

    def test_lavfi_chain_combines_loudness_eq_and_skip_silence(self):
        af = build_mpv_af_chain(-2.0, eq_preset="bass_boost", skip_silence=True)
        assert af.startswith("lavfi=[")
        # -2.0 dB loudness + (-3.0 dB preamp for +6 dB bass boost) = -5.00 dB
        assert "volume=volume=-5.00dB" in af
        assert "lowshelf=f=60:t=q:w=1.0:g=6.00" in af
        assert "equalizer=f=150:t=q:w=1.0:g=4.00" in af
        assert "silenceremove=" in af

        opts = _format_mpv_options(
            "Song",
            loudness_db=2.0,
            normalize_loudness=True,
            eq_preset="vocal",
            skip_silence=True,
        )
        assert "lavfi=[" in opts["af"]
        assert "highshelf=f=14000" in opts["af"]

    def test_player_cycles_eq_and_toggles_skip_silence(self):
        with patch.object(MPVPlayer, "_start_mpv"):
            p = MPVPlayer(resolver=MagicMock())
        p._active_deck = MagicMock()
        assert p.eq_preset == "flat"
        nxt = p.cycle_eq_preset()
        assert nxt == next_eq_preset_id("flat")
        assert get_eq_preset(nxt).label == "Acoustic"
        assert p._active_deck.send_command.call_args[0][0][0:2] == ["af", "set"]
        assert "lowshelf=f=60" in p._active_deck.send_command.call_args[0][0][2]

        assert p.toggle_skip_silence() is True
        assert "silenceremove=" in p._active_deck.send_command.call_args[0][0][2]


# ------------------------------------------------------------------ #
# 2. Word-synced Lyrics (Enhanced LRC, TTML, Gaps, Race, Offsets)
# ------------------------------------------------------------------ #


class TestWordSyncedLyrics:
    def test_parse_enhanced_lrc_glues_syllables_and_decodes_entities(self):
        raw = (
            "[00:05.00]<00:05.00>I <00:05.20>e<00:05.35>nough <00:05.80>don&#x27;t\n"
            "[00:12.00]<00:12.00>Wait\n"
        )
        lines = parse_enhanced_lrc(raw)
        # 5000ms intro >= 4000ms -> intro gap + 2 sung lines + gap between 5800+ and 12000
        assert lines[0].is_gap is True
        sung = [l for l in lines if not l.is_gap]
        assert len(sung) == 2
        assert [w.text for w in sung[0].words] == ["I", "enough", "don't"]
        assert active_word_index(sung[0], 5.25) == 1

    def test_parse_ttml_extracts_syllable_words_and_skips_translations(self):
        ttml = """<?xml version="1.0" encoding="UTF-8"?>
        <tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata">
          <body><div>
            <p begin="6.000s" end="8.500s">
              <span begin="6.000s" end="6.300s">Hel</span><span begin="6.300s" end="6.700s">lo</span>
              <span begin="6.800s" end="7.500s">world</span>
              <span ttm:role="x-translation">Hola mundo</span>
              <span ttm:role="x-bg" begin="7.600s" end="8.200s">ooh</span>
            </p>
            <p begin="14.000s" end="15.000s">Next verse</p>
          </div></body>
        </tt>"""
        lines = parse_ttml(ttml)
        sung = [l for l in lines if not l.is_gap]
        assert len(sung) == 2
        assert [w.text for w in sung[0].words] == ["Hello", "world"]
        assert "Hola" not in sung[0].text
        assert "ooh" not in sung[0].text
        # Intro gap (0 -> 6000) and interlude gap (8500 -> 14000) inserted
        gaps = [l for l in lines if l.is_gap]
        assert len(gaps) == 2

    def test_multi_provider_race_prefers_word_synced_and_caches_v2(self, tmp_path: Path):
        session = MagicMock()

        def fake_get(url, params=None, timeout=None, headers=None):
            if "simpmusic.org" in url:
                return MagicMock(status_code=404, json=lambda: {})
            if "boidu.dev" in url:
                return MagicMock(
                    status_code=200,
                    text='<tt><body><div><p begin="1.0s" end="2.5s"><span begin="1.0s" end="1.4s">Word</span> <span begin="1.5s" end="2.4s">sync</span></p></div></body></tt>',
                )
            if "lrclib.net" in url:
                return MagicMock(
                    status_code=200,
                    json=lambda: {"syncedLyrics": "[00:01.00]Line only"},
                )
            return MagicMock(status_code=404, text="", json=lambda: {})

        session.get.side_effect = fake_get
        mgr = LyricsManager(
            cache_dir=tmp_path,
            session=session,
            providers=("simpmusic", "betterlyrics", "lrclib"),
            offsets_path=tmp_path / "offsets.json",
        )
        res = mgr.fetch("vid_ws", "Song", "Artist", duration_seconds=180)
        assert res.source == "BetterLyrics"
        assert res.word_synced is True
        assert [w.text for w in res.lines[0].words] == ["Word", "sync"]

        # Reload from v2 disk cache without hitting network
        session.get.reset_mock()
        mgr2 = LyricsManager(
            cache_dir=tmp_path,
            session=session,
            providers=("simpmusic", "betterlyrics", "lrclib"),
            offsets_path=tmp_path / "offsets.json",
        )
        cached = mgr2.get_cached("vid_ws", "Song", "Artist")
        assert cached is not None
        assert cached.word_synced is True
        assert [w.text for w in cached.lines[0].words] == ["Word", "sync"]
        session.get.assert_not_called()

    def test_lyrics_offset_store_clamps_persists_and_shifts_index(self, tmp_path: Path):
        store = LyricsOffsetStore(path=tmp_path / "offsets.json")
        assert store.get("t1") == 0
        assert store.adjust("t1", 500) == 500
        assert store.adjust("t1", 10_000) == LyricsOffsetStore.MAX_OFFSET_MS

        reloaded = LyricsOffsetStore(path=tmp_path / "offsets.json")
        assert reloaded.get("t1") == LyricsOffsetStore.MAX_OFFSET_MS

        lines = [LyricLine(5000, "A"), LyricLine(8000, "B")]
        assert active_lyric_index(lines, 7.6, offset_ms=0) == 0
        assert active_lyric_index(lines, 7.6, offset_ms=500) == 1

    def test_add_instrumental_gaps_requires_known_end(self):
        plain_synced = [LyricLine(1000, "One"), LyricLine(9000, "Two")]
        # No sung_until_ms -> no interlude gap inserted between lines
        assert len(add_instrumental_gaps(plain_synced)) == 2


# ------------------------------------------------------------------ #
# 3. Spotify Public Playlist Import & TrackMatcher
# ------------------------------------------------------------------ #


class TestSpotifyImport:
    def test_extract_playlist_id_variants(self):
        pid = "37i9dQZF1DXcBWIGoYBM5M"
        assert extract_spotify_playlist_id(f"https://open.spotify.com/playlist/{pid}?si=abc") == pid
        assert extract_spotify_playlist_id(f"spotify:playlist:{pid}") == pid
        assert extract_spotify_playlist_id("https://example.com/playlist/123") is None
        assert extract_spotify_playlist_id("regular search query") is None

    def test_match_best_track_respects_version_words_and_artist(self):
        cands = [
            Track(id="remix", title="Blinding Lights (Slowed + Reverb)", artist="LoFi Guy", duration_seconds=220),
            Track(id="live", title="Blinding Lights (Live)", artist="The Weeknd", duration_seconds=210),
            Track(id="orig", title="Blinding Lights", artist="The Weeknd", duration_seconds=200),
        ]
        chosen = match_best_track(cands, "Blinding Lights", "The Weeknd", target_duration_s=200)
        assert chosen is not None and chosen.id == "orig"

    def test_import_spotify_playlist_parses_embed_and_resolves(self):
        pid = "37i9dQZF1DXcBWIGoYBM5M"
        next_data = {
            "props": {
                "pageProps": {
                    "state": {
                        "data": {
                            "entity": {
                                "name": "Night Drive",
                                "trackList": [
                                    {"title": "Midnight City", "subtitle": "M83", "duration": 243000},
                                    {"title": "Missing Track", "subtitle": "Nobody", "duration": 100000},
                                ],
                            }
                        }
                    }
                }
            }
        }
        html_doc = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></html>'
        title, stubs, _ = parse_spotify_embed_html(html_doc)
        assert title == "Night Drive"
        assert len(stubs) == 2

        session = MagicMock()
        session.get.return_value = MagicMock(status_code=200, text=html_doc)

        def fake_search(query: str, limit: int):
            if "Midnight City" in query:
                return [Track(id="yt_m83", title="Midnight City", artist="M83", duration_seconds=243)]
            return []

        pl, tracks, unmatched = import_spotify_playlist(
            f"https://open.spotify.com/playlist/{pid}",
            search_fn=fake_search,
            session=session,
        )
        assert pl.title == "Night Drive"
        assert [t.id for t in tracks] == ["yt_m83"]
        assert unmatched == 1


# ------------------------------------------------------------------ #
# 4. ListenBrainz Scrobbler
# ------------------------------------------------------------------ #


class TestListenBrainz:
    def test_listenbrainz_submits_playing_now_and_single_scrobble(self, monkeypatch):
        monkeypatch.delenv("AME_DISABLE_SCROBBLE", raising=False)
        session = MagicMock()
        scrobbler = ListenBrainzScrobbler(token="lb-secret", enabled=True, session=session)
        tr = Track(id="t1", title="Resonance", artist="HOME, Guest", album="Odyssey", duration_seconds=212)

        payload = scrobbler.build_payload("playing_now", tr, pos_seconds=12.0)
        assert payload["listen_type"] == "playing_now"
        meta = payload["payload"][0]["track_metadata"]
        assert meta["artist_name"] == "HOME"
        assert meta["release_name"] == "Odyssey"
        assert meta["additional_info"]["duration_ms"] == 212000

        assert scrobbler.submit_playing_now(tr, 0.0) is True
        # Below threshold (30s / 50%) -> does not scrobble
        assert scrobbler.maybe_scrobble(tr, 20.0, 212.0) is False
        # Past threshold -> scrobbles once per track
        assert scrobbler.maybe_scrobble(tr, 110.0, 212.0) is True
        assert scrobbler.maybe_scrobble(tr, 150.0, 212.0) is False


# ------------------------------------------------------------------ #
# 5. UI integration: word-sync highlighting, EQ, Skip Silence, Offset
# ------------------------------------------------------------------ #


def test_ui_bitchord_features_end_to_end(tmp_path: Path):
    asyncio.run(_run_ui_bitchord_test(tmp_path))


async def _run_ui_bitchord_test(tmp_path: Path) -> None:
    import app as app_mod
    from ui.widgets import LyricLineItem, NowPlayingView

    fake_player = MagicMock()
    fake_player.volume = 80
    fake_player.speed = 1.0
    fake_player.eq_preset = "flat"
    fake_player.skip_silence = False
    fake_player.is_playing = True
    fake_player.is_paused = False
    fake_player.is_buffering = False
    fake_player.playback_pos = 1.6
    fake_player.duration = 180.0
    fake_player.queue = []
    fake_player.history = []
    fake_player.current_stream_quality = "Opus 141kbps"
    fake_player.current_stream_info = None
    fake_player.cycle_eq_preset.return_value = "acoustic"
    fake_player.toggle_skip_silence.return_value = True

    tr = Track(id="ws1", title="Word Track", artist="Artist", duration="03:00", duration_seconds=180)
    fake_player.current_track = tr

    fake_api = MagicMock()
    fake_api.resolver = MagicMock()
    fake_api.get_trending_songs.return_value = [tr]
    fake_api.get_recommendations.return_value = [tr]
    fake_api.get_recommended_playlists.return_value = []
    fake_api.get_charts.return_value = ([], [])

    with (
        patch.object(app_mod, "YTMusicAPI", return_value=fake_api),
        patch.object(app_mod, "MPVPlayer", return_value=fake_player),
        patch.object(app_mod.CavaSpectrum, "available", return_value=False),
        patch.object(app_mod, "MPRISController", return_value=MagicMock()),
        patch.object(app_mod, "DiscordPresence", return_value=MagicMock()),
        patch("favourites._default_path", return_value=tmp_path / "favs.json"),
        patch("stats._default_stats_dir", return_value=tmp_path / "stats"),
        patch("settings._default_settings_path", return_value=tmp_path / "settings.json"),
        patch("settings._default_recent_path", return_value=tmp_path / "recent.json"),
    ):
        ame_app = app_mod.AmeApp()
        ame_app.lyrics_manager = LyricsManager(
            cache_dir=tmp_path / "lyrics",
            offsets_path=tmp_path / "offsets.json",
        )
        ame_app.lyrics_manager._store_mem(
            ame_app.lyrics_manager._cache_key(tr.id, tr.title, tr.artist),
            LyricsResult(
                lines=[
                    LyricLine(
                        time_ms=1000,
                        text="Hello bright world",
                        words=[
                            LyricWord(1000, 1400, "Hello"),
                            LyricWord(1400, 2000, "bright"),
                            LyricWord(2000, 2800, "world"),
                        ],
                        sung_until_ms=2800,
                    ),
                ],
                synced=True,
                source="BetterLyrics",
            ),
        )

        async with ame_app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()
            ame_app._on_player_track_change(tr)
            ame_app._open_now_playing()
            await pilot.pause(0.3)

            np = ame_app.query_one("#view_now_playing", NowPlayingView)
            assert np._active_lyric_idx == 0
            assert np._active_word_idx == 1
            item = list(np.query(LyricLineItem))[0]
            assert "Hello bright world" in item.render().plain

            # Cycle EQ ('E'), Toggle Skip Silence ('M'), Adjust lyrics offset ('>')
            await pilot.press("E")
            await pilot.press("M")
            await pilot.press("greater_than_sign")
            await pilot.pause()

            fake_player.cycle_eq_preset.assert_called_once()
            fake_player.toggle_skip_silence.assert_called_once()
            assert ame_app.settings.data.eq_preset == "acoustic"
            assert ame_app.settings.data.skip_silence is True
            assert ame_app.lyrics_manager.offsets.get(tr.id) == 250
