"""Tests for Now Playing album window, artwork cache, and minimize/maximize."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image as PILImage

from api import Track
from artwork import ArtworkCache, sized_artwork_url, square_cell_dimensions
from stream_resolver import ResolvedStream
from ui.widgets import NowPlayingView, PlayerBar, format_nerd_stats
from ui.views import PlayTrackMsg


# ------------------------------------------------------------------ #
# Artwork URL sizing & geometry
# ------------------------------------------------------------------ #

class TestArtworkHelpers:
    def test_sized_artwork_url_upgrades_google_params(self):
        url = "https://lh3.googleusercontent.com/abc=w120-h120-l90-rj"
        assert sized_artwork_url(url, 600) == "https://lh3.googleusercontent.com/abc=w600-h600-l90-rj"

    def test_sized_artwork_url_appends_size_when_missing(self):
        url = "https://lh3.googleusercontent.com/abcdef"
        assert sized_artwork_url(url, 600) == "https://lh3.googleusercontent.com/abcdef=w600-h600-l90-rj"

    def test_sized_artwork_url_upgrades_ytimg_low_res(self):
        url = "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
        assert sized_artwork_url(url, 600) == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"

    def test_square_cell_dimensions_respects_aspect_and_bounds(self):
        cols, rows = square_cell_dimensions(max_cols=40, max_rows=15, aspect_ratio=2.0)
        assert rows == 15
        assert cols == 30

        cols_clamped, rows_clamped = square_cell_dimensions(max_cols=20, max_rows=15, aspect_ratio=2.0)
        assert cols_clamped == 20
        assert rows_clamped == 10


class TestArtworkCache:
    def test_fetch_caches_to_memory_and_disk_and_crops_wide_thumb(self, tmp_path: Path):
        buf = io.BytesIO()
        PILImage.new("RGB", (320, 180), (40, 140, 220)).save(buf, format="JPEG")
        payload = buf.getvalue()

        session = MagicMock()
        resp = MagicMock(status_code=200, content=payload)
        session.get.return_value = resp

        cache = ArtworkCache(cache_dir=tmp_path, session=session)
        img = cache.fetch("vid1", "https://i.ytimg.com/vi/vid1/hqdefault.jpg")
        assert img is not None
        assert img.size == (180, 180)
        assert session.get.call_count == 1

        # Memory hit
        img2 = cache.fetch("vid1", "https://i.ytimg.com/vi/vid1/hqdefault.jpg")
        assert img2 is not None
        assert session.get.call_count == 1

        # Fresh cache instance hits disk without network
        cache2 = ArtworkCache(cache_dir=tmp_path, session=session)
        img3 = cache2.get_cached("vid1", "https://i.ytimg.com/vi/vid1/hqdefault.jpg")
        assert img3 is not None
        assert img3.size == (180, 180)
        assert session.get.call_count == 1


class TestNerdStats:
    def test_formats_stream_metadata(self):
        stream = ResolvedStream(
            video_id="abc",
            url="https://example.com/audio",
            quality="Opus 141kbps",
            codec="opus",
            kbps=141,
            client_id="VISIONOS_0_1",
            loudness_db=2.4,
        )
        out = format_nerd_stats(stream, "Opus 141kbps", loudness_norm=True)
        assert "OPUS" in out
        assert "141 kbps" in out
        assert "VISIONOS_0_1" in out
        assert "gain -2.4 dB" in out

    def test_formats_cached_and_norm_off(self):
        stream = ResolvedStream(
            video_id="abc",
            url="/tmp/abc.webm",
            quality="Opus 128kbps",
            codec="opus",
            kbps=128,
            client_id="CACHE",
            cached_path="/tmp/abc.webm",
        )
        out = format_nerd_stats(stream, "", loudness_norm=False)
        assert "CACHED" in out
        assert "norm off" in out


# ------------------------------------------------------------------ #
# UI integration tests (Now Playing auto-open, minimize/maximize, controls)
# ------------------------------------------------------------------ #

def test_now_playing_auto_opens_and_toggles_minimize_maximize(tmp_path: Path):
    asyncio.run(_run_now_playing_ui_test(tmp_path))


async def _run_now_playing_ui_test(tmp_path: Path) -> None:
    import app as app_mod

    fake_player = MagicMock()
    fake_player.volume = 80
    fake_player.is_playing = False
    fake_player.is_paused = False
    fake_player.is_buffering = False
    fake_player.playback_pos = 0.0
    fake_player.duration = 180.0
    fake_player.queue = []
    fake_player.history = []
    fake_player.current_track = None
    fake_player.current_stream_quality = "Opus 141kbps"
    fake_player.current_stream_info = None
    fake_player.cycle_loop_mode.return_value = MagicMock(value="loop_one")
    fake_player.cycle_crossfade.return_value = 3
    fake_player.toggle_loudness_normalization.return_value = False

    fake_api = MagicMock()
    fake_api.resolver = MagicMock()
    fake_api.get_trending_songs.return_value = []
    fake_api.get_recommendations.return_value = []
    fake_api.get_recommended_playlists.return_value = []
    fake_api.get_charts.return_value = ([], [])

    sample_img = PILImage.new("RGB", (200, 200), (90, 180, 240))
    track = Track(
        id="track_1",
        title="Midnight Rain",
        artist="Test Artist",
        artist_id="UC_TEST",
        album="Test Album",
        album_id="MPRE_TEST",
        duration="03:00",
        duration_seconds=180,
        thumbnail="https://lh3.googleusercontent.com/x=w120-h120",
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
        ame_app.artwork_cache = ArtworkCache(cache_dir=tmp_path / "art")
        # Seed artwork cache so no real HTTP call is made
        ame_app.artwork_cache._store_mem(
            ame_app.artwork_cache._key(track.id, sized_artwork_url(track.thumbnail, 600)),
            sample_img,
        )

        async with ame_app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            assert ame_app.active_view_id == "view_trending"
            assert ame_app.query_one("#player_bar", PlayerBar).display is True

            # Playing a song auto-opens the Now Playing album window and hides mini bar
            fake_player.current_track = track
            ame_app.post_message(PlayTrackMsg(track, []))
            ame_app._on_player_track_change(track)
            await pilot.pause()

            assert ame_app.active_view_id == "view_now_playing"
            assert ame_app.query_one("#player_bar", PlayerBar).display is False
            np = ame_app.query_one("#view_now_playing", NowPlayingView)
            assert np._artwork is not None
            assert np.side_panel_mode == "lyrics"

            # Verify no broken Rich bracket markup in chips and two-row layout exists
            for wid in (
                "#np_minimize_btn",
                "#np_artist_btn",
                "#np_album_btn",
                "#np_btn_loop",
                "#np_btn_lyrics",
                "#np_btn_queue",
                "#np_btn_tint",
            ):
                rendered = np.query_one(wid).render().plain
                assert "[[" not in rendered, f"Broken markup in {wid}: {rendered!r}"
            assert np.query_one("#np_controls") is not None
            assert np.query_one("#np_controls_secondary") is not None
            assert np.query_one("#np_links_row") is not None

            # Pressing 'm' minimizes back to trending and shows mini bar
            await pilot.press("m")
            await pilot.pause()
            assert ame_app.active_view_id == "view_trending"
            assert ame_app.query_one("#player_bar", PlayerBar).display is True

            # Pressing 'w' maximizes back to the album window
            await pilot.press("w")
            await pilot.pause()
            assert ame_app.active_view_id == "view_now_playing"
            assert ame_app.query_one("#player_bar", PlayerBar).display is False

            # Clicking playback controls inside NowPlayingView works
            await pilot.click("#np_btn_play")
            await pilot.click("#np_btn_next")
            await pilot.click("#np_btn_prev")
            await pilot.click("#np_btn_loop")
            await pilot.click("#np_btn_xfade")
            await pilot.click("#np_btn_norm")
            await pilot.click("#np_btn_fav")
            await pilot.pause()

            fake_player.toggle_pause.assert_called_once()
            fake_player.next_track.assert_called_once()
            fake_player.prev_track.assert_called_once()
            fake_player.cycle_loop_mode.assert_called_once()
            fake_player.cycle_crossfade.assert_called_once()
            fake_player.toggle_loudness_normalization.assert_called_once()
            assert ame_app.favourites.contains(track.id) is True

            # Clicking minimize button returns to browse view
            await pilot.click("#np_minimize_btn")
            await pilot.pause()
            assert ame_app.active_view_id == "view_trending"
            assert ame_app.query_one("#player_bar", PlayerBar).display is True

            # Clicking mini album avatar maximizes again
            await pilot.click("#mini_art_box")
            await pilot.pause()
            assert ame_app.active_view_id == "view_now_playing"
            assert ame_app.query_one("#player_bar", PlayerBar).display is False

            # Pressing escape minimizes back to browse view
            await pilot.press("escape")
            await pilot.pause()
            assert ame_app.active_view_id == "view_trending"
