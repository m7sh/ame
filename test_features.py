"""Tests for lyrics, stats, art palette, speed/sleep, queue reorder, & integrations."""

from __future__ import annotations

import asyncio
import json
import struct
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image as PILImage

from api import Album, Artist, Track
from artwork import extract_artwork_colors
from integrations import DiscordPresence
from lyrics import LyricLine, LyricsManager, LyricsResult, active_lyric_index, parse_lrc
from player import MPVPlayer
from stats import ListeningStatsStore
from theme import OmarchyThemeManager
from ui.views import OpenAlbumMsg, OpenArtistMsg, PlayTrackMsg, SearchView, StatsView
from ui.widgets import LyricLineItem, NowPlayingView, UpNextItem


# ------------------------------------------------------------------ #
# 1. Lyrics parser & fetcher
# ------------------------------------------------------------------ #

class TestLyrics:
    def test_parse_lrc_multi_timestamp_and_sorting(self):
        raw = "[00:12.50]First line\n[00:05.00][00:25.100]Chorus\n"
        lines = parse_lrc(raw)
        assert [(l.time_ms, l.text) for l in lines] == [
            (5000, "Chorus"),
            (12500, "First line"),
            (25100, "Chorus"),
        ]

    def test_active_lyric_index_tracks_playhead(self):
        lines = [
            LyricLine(5000, "line 1"),
            LyricLine(12000, "line 2"),
            LyricLine(20000, "line 3"),
        ]
        assert active_lyric_index(lines, 1.0) == -1
        assert active_lyric_index(lines, 5.0) == 0
        assert active_lyric_index(lines, 13.0) == 1
        assert active_lyric_index(lines, 25.0) == 2

    def test_fetch_prefers_lrclib_and_caches_to_disk(self, tmp_path: Path):
        session = MagicMock()
        resp = MagicMock(
            status_code=200,
            json=lambda: {"syncedLyrics": "[00:01.00]Hello world\n[00:04.00]Second line"},
        )
        session.get.return_value = resp

        mgr = LyricsManager(cache_dir=tmp_path, session=session)
        res = mgr.fetch("vid1", "Song", "Artist", duration_seconds=180)
        assert res.synced is True
        assert res.source == "LRCLIB"
        assert len(res.lines) == 2
        assert session.get.call_count == 1

        mgr2 = LyricsManager(cache_dir=tmp_path, session=session)
        cached = mgr2.get_cached("vid1", "Song", "Artist")
        assert cached is not None
        assert cached.lines[0].text == "Hello world"
        assert session.get.call_count == 1

    def test_fetch_falls_back_to_ytm_when_lrclib_misses(self, tmp_path: Path):
        session = MagicMock()
        session.get.return_value = MagicMock(status_code=404, json=lambda: {})
        ytm_fn = MagicMock(
            return_value={
                "hasTimestamps": True,
                "lyrics": [{"text": "YTM synced", "start_time": 2500}],
            }
        )

        mgr = LyricsManager(cache_dir=tmp_path, session=session, ytm_fetcher=ytm_fn)
        res = mgr.fetch("vid2", "Track", "Artist")
        assert res.source == "YouTube Music"
        assert res.synced is True
        assert res.lines[0] == LyricLine(2500, "YTM synced")


# ------------------------------------------------------------------ #
# 2. Dynamic artwork palette extraction
# ------------------------------------------------------------------ #

class TestArtworkPalette:
    def test_extracts_vibrant_hex_pair_and_tints_theme(self, tmp_path: Path):
        img = PILImage.new("RGB", (48, 48), (220, 40, 120))
        colors = extract_artwork_colors(img, dark_mode=True)
        assert colors is not None
        accent, secondary = colors
        assert accent.startswith("#") and len(accent) == 7
        assert secondary.startswith("#") and len(secondary) == 7

        tm = OmarchyThemeManager()
        tinted = tm.apply_artwork_tint(accent, secondary)
        assert tinted.accent == accent
        assert tinted.background == tm.base_theme.background
        restored = tm.apply_artwork_tint(None)
        assert restored == tm.base_theme


# ------------------------------------------------------------------ #
# 3. Listening stats monthly store
# ------------------------------------------------------------------ #

class TestListeningStats:
    def test_records_plays_seconds_and_aggregates_periods(self, tmp_path: Path):
        store = ListeningStatsStore(directory=tmp_path)
        t1 = Track(id="t1", title="Song One", artist="Alpha", duration="03:00", duration_seconds=180)
        t2 = Track(id="t2", title="Song Two", artist="Beta", duration="02:30", duration_seconds=150)

        when_now = datetime(2026, 11, 25, 14, 30)
        when_prev = datetime(2026, 10, 12, 9, 15)

        store.record_play(t1, when=when_now)
        store.record_listening(t1, 120.0, when=when_now)
        store.record_play(t2, when=when_prev)
        store.record_listening(t2, 90.0, when=when_prev)

        month_sum = store.summary("month", today=when_now.date())
        assert month_sum.total_plays == 1
        assert month_sum.total_seconds == 120.0
        assert month_sum.top_tracks[0].track.id == "t1"
        assert month_sum.hourly_seconds[14] == 120.0

        all_sum = store.summary("all", today=when_now.date())
        assert all_sum.total_plays == 2
        assert all_sum.total_seconds == 210.0
        assert len(all_sum.top_tracks) == 2

        today_sum = store.summary("today", today=when_now.date())
        assert today_sum.total_plays == 1


# ------------------------------------------------------------------ #
# 4. Player speed & queue reordering
# ------------------------------------------------------------------ #

class TestPlayerSpeedAndReorder:
    def test_cycle_speed_sends_mpv_property(self):
        with patch.object(MPVPlayer, "_start_mpv"):
            p = MPVPlayer(resolver=MagicMock())
        p._active_deck = MagicMock()
        assert p.speed == 1.0
        assert p.cycle_speed() == 1.25
        p._active_deck.send_command.assert_called_with(["set_property", "speed", 1.25])
        assert p.cycle_speed() == 1.5
        assert p.set_speed(5.0) == 2.5

    def test_move_queue_item_reorders_and_disarms_head(self):
        with patch.object(MPVPlayer, "_start_mpv"):
            p = MPVPlayer(resolver=MagicMock())
        t1 = Track(id="1", title="One", artist="A")
        t2 = Track(id="2", title="Two", artist="B")
        t3 = Track(id="3", title="Three", artist="C")
        p.queue = [t1, t2, t3]
        p.on_queue_change = MagicMock()

        assert p.move_queue_item(0, 2) is True
        assert [t.id for t in p.queue] == ["2", "3", "1"]
        p.on_queue_change.assert_called_once()
        assert p.move_queue_item(0, 99) is False


# ------------------------------------------------------------------ #
# 5. Discord IPC packet encoding
# ------------------------------------------------------------------ #

class TestIntegrations:
    def test_discord_encode_packet_format(self):
        pkt = DiscordPresence.encode_packet(1, {"cmd": "SET_ACTIVITY"})
        op, length = struct.unpack("<II", pkt[:8])
        assert op == 1
        body = json.loads(pkt[8:].decode("utf-8"))
        assert length == len(pkt) - 8
        assert body["cmd"] == "SET_ACTIVITY"


class TestSettingsAndRecentHistory:
    def test_settings_persistence_and_corrupt_fallback(self, tmp_path: Path):
        from settings import AppSettingsStore

        path = tmp_path / "settings.json"
        store = AppSettingsStore(path=path)
        assert store.data.autoplay is True
        store.data.crossfade_seconds = 6
        store.data.show_sidebar = False
        store.save()

        reloaded = AppSettingsStore(path=path)
        assert reloaded.data.crossfade_seconds == 6
        assert reloaded.data.show_sidebar is False

        path.write_text("{corrupt", encoding="utf-8")
        fallback = AppSettingsStore(path=path)
        assert fallback.data.autoplay is True

    def test_recent_history_dedupe_order_and_cap(self, tmp_path: Path):
        from settings import RecentHistoryStore

        path = tmp_path / "recent.json"
        hist = RecentHistoryStore(path=path, max_items=3)
        t1 = Track(id="v1", title="One", artist="A")
        t2 = Track(id="v2", title="Two", artist="B")
        t3 = Track(id="v3", title="Three", artist="C")
        t4 = Track(id="v4", title="Four", artist="D")
        for tr in (t1, t2, t3, t1, t4):
            hist.record(tr)

        ids = [t.id for t in hist.all()]
        assert ids == ["v4", "v1", "v3"]

        reloaded = RecentHistoryStore(path=path, max_items=3)
        assert [t.id for t in reloaded.all()] == ["v4", "v1", "v3"]


class TestRecommendationsAndEQ:
    def test_queue_builder_dedupes_audio_video_and_caps_artists(self):
        from recommendations import extend_station, is_same_recording, normalised_title

        assert normalised_title("Kesariya (From \"Brahmastra\") | Official Video") == "kesariya"
        assert "remix" in normalised_title("Kesariya (Lo-Fi Remix)")

        t_audio = Track(id="vid_audio", title="Midnight Rain", artist="Charli xcx")
        t_video = Track(id="vid_mv", title="Midnight Rain (Official Video)", artist="Charli xcx")
        assert is_same_recording(t_audio, t_video) is True

        candidates = [t_video] + [
            Track(id=f"a_{i}", title=f"Track {i}", artist="Artist X") for i in range(6)
        ] + [
            Track(id=f"b_{i}", title=f"Song {i}", artist="Artist Y") for i in range(4)
        ]
        station = extend_station(existing=[t_audio], candidates=candidates, limit=10)
        # Music-video duplicate skipped; Artist X capped at PER_ARTIST_LIMIT=2, Artist Y capped at 2
        ids = [t.id for t in station]
        assert "vid_mv" not in ids
        assert sum(1 for t in station if t.artist == "Artist X") == 2
        assert sum(1 for t in station if t.artist == "Artist Y") == 2

    def test_taste_profile_ranks_recency_favourites_and_moods(self):
        from recommendations import build_taste_profile, moods_for_hour

        fav = Track(id="f1", title="Fav Track", artist="Charli xcx", artist_id="UC_CHARLI")
        rec = Track(id="r1", title="Recent Track", artist="Fred again..", artist_id="UC_FRED")
        profile = build_taste_profile(favourites=[fav], recent_history=[rec])
        assert profile.artists[0].name == "Charli xcx"
        assert "f1" in profile.heard_ids and "r1" in profile.heard_ids

        morning_moods = [m[0] for m in moods_for_hour(10)]
        night_moods = [m[0] for m in moods_for_hour(23)]
        assert "Focus" in morning_moods
        assert "Relax" in night_moods

    def test_spectrum_renders_graphic_eq_not_matrix(self):
        from ui.widgets import Spectrum

        spec = Spectrum()
        spec.styles.width = 40
        spec.styles.height = 1
        spec._bands = [0.2, 0.8, 0.4, 0.9]
        # Force a virtual render width via mock size check
        with patch.object(Spectrum, "size", new_callable=lambda: property(lambda _self: MagicMock(width=40, height=1))):
            rendered = spec.render().plain
        assert any(ch in rendered for ch in "▁▂▃▄▅▆▇█")
        assert "ｱ" not in rendered and "ｶ" not in rendered

    def test_api_recommendations_uses_home_taste_and_dedupes(self):
        from api import YTMusicAPI

        api = YTMusicAPI()
        mock_yt = MagicMock()
        mock_yt.get_home.return_value = [
            {"title": "Listen again", "contents": [{"videoId": "stale1", "title": "Stale", "artists": [{"name": "A"}]}]},
            {
                "title": "Quick picks",
                "contents": [
                    {"videoId": "qp1", "title": "Fresh Pick", "artists": [{"name": "Artist One"}]},
                    {"playlistId": "PL_HOME", "title": "Daily Mix"},
                ],
            },
        ]
        mock_yt.get_watch_playlist.return_value = {
            "tracks": [
                {"videoId": "rad1", "title": "Midnight City", "artists": [{"name": "M83"}]},
                {"videoId": "rad1_mv", "title": "Midnight City (Official Video)", "artists": [{"name": "M83"}]},
            ]
        }
        mock_yt.get_artist.return_value = {
            "name": "Charli xcx",
            "songs": {"results": [{"videoId": "art1", "title": "360", "artists": [{"name": "Charli xcx"}]}]},
            "related": {"results": []},
        }
        mock_yt.get_mood_playlists.return_value = []
        mock_yt.get_charts.return_value = {"videos": [{"playlistId": "PL_CHART", "title": "US Top"}], "genres": []}
        mock_yt.search.return_value = []
        api.yt = mock_yt

        fav = Track(id="fav1", title="Sympathy is a knife", artist="Charli xcx", artist_id="UC_CHARLI")
        recs = api.get_recommendations(favourites=[fav], limit=10, trending_seed=[])
        rec_ids = [t.id for t in recs]
        assert "stale1" not in rec_ids
        assert "qp1" in rec_ids
        assert "rad1" in rec_ids
        assert "rad1_mv" not in rec_ids
        assert "art1" in rec_ids

        mixes = api.get_recommended_playlists(favourites=[fav], limit=10)
        mix_ids = [p.id for p in mixes]
        assert mix_ids[0] == "PL_HOME"
        assert "PL_CHART" in mix_ids


# ------------------------------------------------------------------ #
# 6. UI integration: split lyrics/up-next, sleep, speed, stats, search
# ------------------------------------------------------------------ #

def test_ui_features_end_to_end(tmp_path: Path):
    asyncio.run(_run_ui_features(tmp_path))


async def _run_ui_features(tmp_path: Path) -> None:
    import app as app_mod

    fake_player = MagicMock()
    fake_player.volume = 80
    fake_player.speed = 1.0
    fake_player.is_playing = True
    fake_player.is_paused = False
    fake_player.is_buffering = False
    fake_player.playback_pos = 6.0
    fake_player.duration = 180.0
    t1 = Track(
        id="t1",
        title="Neon Rain",
        artist="Artist A",
        album="Album A",
        album_id="MPRE1",
        artist_id="UC1",
        duration="03:00",
        duration_seconds=180,
        thumbnail="https://lh3.googleusercontent.com/x=w120-h120",
    )
    t2 = Track(id="t2", title="Second Song", artist="Artist B", duration="02:40", duration_seconds=160)
    fake_player.queue = [t2]
    fake_player.history = []
    fake_player.current_track = t1
    fake_player.current_stream_quality = "Opus 141kbps"
    fake_player.current_stream_info = None
    fake_player.cycle_speed.return_value = 1.25
    fake_player.move_queue_item.return_value = True

    fake_api = MagicMock()
    fake_api.resolver = MagicMock()
    fake_api.get_trending_songs.return_value = [t1, t2]
    fake_api.get_recommendations.return_value = [t1, t2]
    fake_api.get_recommended_playlists.return_value = []
    fake_api.get_charts.return_value = ([], [])
    fake_api.get_explore_sections.return_value = {
        "new_releases": [Album(id="MPRE1", title="Album A", artist="Artist A", year="2025")],
        "trending": [t1],
        "moods": [MagicMock(title="Focus", params="ggMPOg1u")],
    }
    fake_api.get_mood_playlists.return_value = []
    fake_api.get_album.return_value = (
        Album(id="MPRE1", title="Album A", artist="Artist A", year="2025"),
        [t1],
    )
    fake_api.get_artist.return_value = (
        Artist(id="UC1", name="Artist A", subscribers="1M"),
        [t1],
        [Album(id="MPRE1", title="Album A", artist="Artist A", year="2025")],
    )

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
        ame_app.lyrics_manager = LyricsManager(cache_dir=tmp_path / "lyrics")
        # Pre-seed synced lyrics in memory
        ame_app.lyrics_manager._store_mem(
            ame_app.lyrics_manager._cache_key(t1.id, t1.title, t1.artist),
            LyricsResult(
                lines=[
                    LyricLine(2000, "First lyric line"),
                    LyricLine(5500, "Active lyric line"),
                    LyricLine(12000, "Future lyric line"),
                ],
                synced=True,
                source="LRCLIB",
            ),
        )

        async with ame_app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()

            # Sleek 1-line Spectrum EQ sits below main_body (not at the top)
            from ui.widgets import Sidebar, Spectrum
            top_children = [c.id for c in ame_app.screen.children]
            assert top_children.index("spectrum") > top_children.index("main_body")
            spec = ame_app.query_one("#spectrum", Spectrum)
            spec.display = True
            await pilot.pause()
            spec.set_bands([0.3, 0.9, 0.5])
            assert any(ch in spec.render().plain for ch in "▁▂▃▄▅▆▇█")
            spec.display = False

            # Sidebar visible on wide browse view & toggles with 'b'
            sb = ame_app.query_one("#sidebar", Sidebar)
            assert sb.display is True
            await pilot.press("b")
            await pilot.pause()
            assert sb.display is False
            await pilot.press("b")
            await pilot.pause()
            assert sb.display is True

            # Explore ('e') & Settings (',') views
            await pilot.press("e")
            await pilot.pause()
            assert ame_app.active_view_id == "view_explore"
            await pilot.press("comma")
            await pilot.pause()
            assert ame_app.active_view_id == "view_settings"
            await pilot.press("h")
            await pilot.pause()
            assert ame_app.active_view_id == "view_trending"

            # Play track -> auto-opens NowPlayingView with Lyrics OPEN by default & sidebar hidden
            ame_app.post_message(PlayTrackMsg(t1, [t2]))
            ame_app._on_player_track_change(t1)
            await pilot.pause()
            assert ame_app.active_view_id == "view_now_playing"
            assert sb.display is False

            np = ame_app.query_one("#view_now_playing", NowPlayingView)

            # Lyrics panel is already open by default on wide screens
            assert np.side_panel_mode == "lyrics"
            lyric_items = list(np.query(LyricLineItem))
            assert len(lyric_items) == 3
            assert np._active_lyric_idx == 1

            # Pressing 'L' closes and re-opens the Lyrics panel
            await pilot.press("L")
            await pilot.pause()
            assert np.side_panel_mode is None
            await pilot.press("L")
            await pilot.pause()
            assert np.side_panel_mode == "lyrics"
            lyric_items = list(np.query(LyricLineItem))

            # Clicking a lyric line seeks to its timestamp
            await pilot.click(lyric_items[2])
            await pilot.pause()
            fake_player.seek_to.assert_called_with(12.0)

            # Toggle Up Next split panel ('O')
            await pilot.press("O")
            await pilot.pause()
            assert np.side_panel_mode == "queue"
            up_items = list(np.query(UpNextItem))
            assert len(up_items) == 1

            # Speed cycle ('C') & Sleep timer cycle ('Z') & Dynamic tint toggle ('D')
            await pilot.press("C")
            await pilot.press("Z")
            await pilot.press("D")
            await pilot.pause()
            fake_player.cycle_speed.assert_called_once()
            assert ame_app._sleep_minutes == 15
            assert ame_app._dynamic_tint is False

            # Open Artist page ('g') & Album page ('G')
            await pilot.press("g")
            await pilot.pause()
            assert ame_app.active_view_id == "view_artist_detail"

            await pilot.press("G")
            await pilot.pause()
            assert ame_app.active_view_id == "view_playlist_detail"

            # Open Stats view ('Y')
            await pilot.press("Y")
            await pilot.pause()
            assert ame_app.active_view_id == "view_stats"
            sv = ame_app.query_one("#view_stats", StatsView)
            assert sv.period == "month"

            # Open Queue and reorder ('J')
            await pilot.press("q")
            await pilot.pause()
            assert ame_app.active_view_id == "view_queue"
            await pilot.press("J")
            await pilot.pause()

            # SearchView supports songs, albums, artists, playlists
            search_view = ame_app.query_one("#view_search", SearchView)
            search_view.set_results(
                songs=[t1],
                playlists=[],
                albums=[Album(id="MPRE1", title="Album A", artist="Artist A", year="2025")],
                artists=[Artist(id="UC1", name="Artist A", subscribers="1M")],
            )
            assert len(search_view.album_results) == 1
            assert len(search_view.artist_results) == 1
