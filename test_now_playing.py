"""Tests for Now Playing album window, artwork cache, and minimize/maximize."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image as PILImage

from api import Track, _extract_thumbnail_url
from artwork import (
    ArtworkCache,
    HEADER_ART_PX,
    NOTIFICATION_ART_PX,
    PLAYER_ART_PX,
    ROW_ART_PX,
    sized_artwork_url,
    snap_artwork_size,
    square_cell_dimensions,
    youtube_thumbnail_candidates,
    youtube_thumbnail_fallback,
)
from stream_resolver import ResolvedStream
from ui.widgets import NowPlayingView, PlayerBar, format_nerd_stats
from ui.views import PlayTrackMsg


# ------------------------------------------------------------------ #
# Artwork URL sizing, YouTube fallback, letterbox crop & geometry
# ------------------------------------------------------------------ #

class TestArtworkHelpers:
    def test_sized_artwork_url_upgrades_google_params_and_preserves_flags(self):
        url = "https://lh3.googleusercontent.com/abc=w120-h120-l90-rj"
        assert (
            sized_artwork_url(url, PLAYER_ART_PX)
            == "https://lh3.googleusercontent.com/abc=w1200-h1200-l90-rj"
        )
        avatar = "https://yt3.ggpht.com/ytc/AIdro_x123=s88-c-k-c0x00ffffff-no-rj"
        assert (
            sized_artwork_url(avatar, 512)
            == "https://yt3.ggpht.com/ytc/AIdro_x123=s512-c-k-c0x00ffffff-no-rj"
        )
        # Non-Google hosts must not rewrite =sN query/path params
        assert (
            sized_artwork_url("https://notggpht.com/avatar=s88.jpg", 512)
            == "https://notggpht.com/avatar=s88.jpg"
        )

    def test_sized_artwork_url_appends_size_when_missing(self):
        url = "https://lh3.googleusercontent.com/abcdef"
        assert (
            sized_artwork_url(url, PLAYER_ART_PX)
            == "https://lh3.googleusercontent.com/abcdef=w1200-h1200-l90-rj"
        )

    def test_sized_artwork_url_upgrades_ytimg_and_drops_signature_query(self):
        mq = "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg?sqp=-oaymwEj&rs=AOn4CL"
        assert (
            sized_artwork_url(mq, PLAYER_ART_PX)
            == "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
        )
        assert (
            sized_artwork_url(mq, 320)
            == "https://i.ytimg.com/vi/dQw4w9WgXcQ/sddefault.jpg"
        )
        # An already-upgraded cover scales back down to hqdefault for small rows
        maxres = "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
        assert (
            sized_artwork_url(maxres, ROW_ART_PX)
            == "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"
        )
        # Small original rendition stays intact (including query) when below 240px
        hq = "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg?sqp=-oaymwEj&rs=AOn4CL"
        assert sized_artwork_url(hq, ROW_ART_PX) == hq

    def test_youtube_thumbnail_fallback_and_candidates(self):
        maxres = "https://i.ytimg.com/vi/jNQXAC9IVRw/maxresdefault.jpg"
        assert (
            youtube_thumbnail_fallback(maxres)
            == "https://i.ytimg.com/vi/jNQXAC9IVRw/hqdefault.jpg"
        )
        assert youtube_thumbnail_candidates(maxres) == [
            "https://i.ytimg.com/vi/jNQXAC9IVRw/maxresdefault.jpg",
            "https://i.ytimg.com/vi/jNQXAC9IVRw/sddefault.jpg",
            "https://i.ytimg.com/vi/jNQXAC9IVRw/hqdefault.jpg",
        ]
        assert youtube_thumbnail_fallback("https://i.ytimg.com/vi/jNQXAC9IVRw/hqdefault.jpg") is None
        assert youtube_thumbnail_fallback("https://lh3.googleusercontent.com/x=w544-h544") is None

    def test_snap_artwork_size_uses_cached_ladder(self):
        assert snap_artwork_size(100) == 120
        assert snap_artwork_size(500) == 544
        assert snap_artwork_size(900) == 1200
        assert NOTIFICATION_ART_PX == 544
        assert HEADER_ART_PX == 720
        assert PLAYER_ART_PX == 1200

    def test_extract_thumbnail_url_prefers_largest_area(self):
        item = {
            "thumbnails": [
                {"url": "https://lh3.googleusercontent.com/a=w60-h60", "width": 60, "height": 60},
                {"url": "https://lh3.googleusercontent.com/a=w544-h544", "width": 544, "height": 544},
                {"url": "https://lh3.googleusercontent.com/a=w120-h120", "width": 120, "height": 120},
            ]
        }
        assert _extract_thumbnail_url(item) == "https://lh3.googleusercontent.com/a=w544-h544"

    def test_square_cell_dimensions_respects_aspect_and_bounds(self):
        cols, rows = square_cell_dimensions(max_cols=40, max_rows=15, aspect_ratio=2.0)
        assert rows == 15
        assert cols == 30

        cols_clamped, rows_clamped = square_cell_dimensions(max_cols=20, max_rows=15, aspect_ratio=2.0)
        assert cols_clamped == 20
        assert rows_clamped == 10


class TestArtworkCache:
    def test_fetch_falls_back_on_404_and_trims_letterbox_bars(self, tmp_path: Path):
        # 480x360 hqdefault with 45px black letterbox bars top & bottom around colorful middle
        letterboxed = PILImage.new("RGB", (480, 360), (0, 0, 0))
        mid = PILImage.new("RGB", (480, 270), (40, 140, 220))
        letterboxed.paste(mid, (0, 45))
        buf = io.BytesIO()
        letterboxed.save(buf, format="JPEG", quality=95)
        payload = buf.getvalue()

        session = MagicMock()

        def fake_get(url, timeout=None, headers=None):
            if "maxresdefault" in url or "sddefault" in url:
                return MagicMock(status_code=404, content=b"")
            return MagicMock(status_code=200, content=payload)

        session.get.side_effect = fake_get

        cache = ArtworkCache(cache_dir=tmp_path, session=session)
        img = cache.fetch("vid1", "https://i.ytimg.com/vi/vid1/hqdefault.jpg")
        assert img is not None
        # Letterbox bars trimmed (360 -> 270) then center-cropped to 270x270 square
        assert img.size[0] == img.size[1]
        assert img.size[0] <= 272
        assert img.getpixel((10, 10))[2] > 150  # blue art, not black bar
        assert session.get.call_count == 3  # maxresdefault -> sddefault -> hqdefault

        # Memory hit (uses remembered fallback chain without re-hitting 404s)
        img2 = cache.fetch("vid1", "https://i.ytimg.com/vi/vid1/hqdefault.jpg")
        assert img2 is not None
        assert session.get.call_count == 3

        # Fresh cache instance hits disk without network when passed hqdefault directly
        cache2 = ArtworkCache(cache_dir=tmp_path, session=session)
        img3 = cache2.get_cached("vid1", "https://i.ytimg.com/vi/vid1/hqdefault.jpg", size=ROW_ART_PX)
        assert img3 is not None
        assert img3.size == img.size
        assert session.get.call_count == 3

        # A second track with same cache remembers 404 URLs per-URL and skips directly if already known
        session.get.reset_mock()
        img4 = cache.get_cached("vid1", "https://i.ytimg.com/vi/vid1/hqdefault.jpg")
        assert img4 is not None
        session.get.assert_not_called()

    def test_fetch_upgrades_to_maxres_when_available_and_does_not_blacklist_transient_errors(self, tmp_path: Path):
        buf = io.BytesIO()
        PILImage.new("RGB", (1280, 720), (80, 160, 220)).save(buf, format="JPEG")
        maxres_bytes = buf.getvalue()

        attempts = {"count": 0}

        def fake_get(url, timeout=None, headers=None):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise ConnectionError("transient drop")
            if "sddefault" in url:
                return MagicMock(status_code=503, content=b"")
            return MagicMock(status_code=200, content=maxres_bytes)

        session = MagicMock()
        session.get.side_effect = fake_get

        cache = ArtworkCache(cache_dir=tmp_path, session=session)
        # First call hits transient error on maxresdefault, 503 on sddefault, succeeds on hqdefault
        img1 = cache.fetch("vid2", "https://i.ytimg.com/vi/vid2/hqdefault.jpg")
        assert img1 is not None
        # maxresdefault was NOT blacklisted because it wasn't a 404
        assert "https://i.ytimg.com/vi/vid2/maxresdefault.jpg" not in cache._missing_yt_urls

        # A fresh track on the same video / uncached key still tries maxresdefault and gets 720x720
        img2 = cache.fetch("vid2_fresh", "https://i.ytimg.com/vi/vid2/hqdefault.jpg")
        assert img2 is not None
        assert img2.size == (720, 720)

    def test_crisp_tgp_preserves_hidpi_source_resolution(self):
        import artwork as art_mod

        if not art_mod._HAS_TEXTUAL_IMAGE or art_mod._CrispTGPRenderable is None:
            pytest.skip("textual_image unavailable")

        src = PILImage.new("RGB", (1200, 1200), (90, 140, 200))
        ren = art_mod._CrispTGPRenderable(src, width=40, height=20)
        sent_sizes = []

        def fake_send(self, w, h):
            sent_sizes.append((w, h))

        with patch.object(art_mod._TGPRenderable, "_send_image_to_terminal", fake_send):
            ren._send_image_to_terminal(400, 400)

        assert sent_sizes == [(960, 960)]


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
            ame_app.artwork_cache._key(track.id, sized_artwork_url(track.thumbnail, PLAYER_ART_PX)),
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
