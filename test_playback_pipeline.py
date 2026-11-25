"""Tests for StreamResolver, AudioCache, MPVPlayer fixes, and UI/API bugs."""

import json
import struct
import threading
import time
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api import Track, YTMusicAPI, _parse_track
from audio_cache import AudioCache
from player import LoopMode, MPVPlayer, _compute_loudness_gain_db, _format_mpv_options
from stream_resolver import (
    AUTH_BOUNDARY_BYTES,
    PermanentlyUnplayableError,
    ResolvedStream,
    StreamResolver,
    _pick_audio_format,
    media_headers_for_url,
)


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _bare_player(resolver=None, cache=False) -> MPVPlayer:
    with patch.object(MPVPlayer, "_start_mpv"):
        return MPVPlayer(resolver=resolver or MagicMock(), cache=cache)


def _write_wav(path: Path, seconds: float = 0.25) -> None:
    frames = int(8000 * seconds)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(struct.pack("<" + "h" * frames, *([150] * frames)))


class _FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, headers=None, chunks=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.reason = "OK" if self.ok else "Error"
        self.text = text
        self._json = json_data
        self.headers = headers or {}
        self._chunks = chunks or []

    def json(self):
        return self._json

    def iter_content(self, chunk_size=8192):
        for c in self._chunks:
            yield c

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


# ------------------------------------------------------------------ #
# StreamResolver (InnerTube walk, probing, coalescing, verdicts)
# ------------------------------------------------------------------ #

class TestStreamResolver:
    def test_format_picker_prefers_non_drc_opus_over_aac(self):
        fmts = [
            {"itag": 140, "mimeType": 'audio/mp4; codecs="mp4a.40.2"', "bitrate": 130000, "url": "https://a/140"},
            {"itag": 251, "mimeType": 'audio/webm; codecs="opus"', "bitrate": 140000, "url": "https://a/251-drc", "isDrc": True},
            {"itag": 251, "mimeType": 'audio/webm; codecs="opus"', "bitrate": 136000, "url": "https://a/251"},
        ]
        chosen = _pick_audio_format(fmts)
        assert chosen is not None
        assert chosen["url"] == "https://a/251"

    def test_media_headers_match_clients(self):
        vision_h = media_headers_for_url("https://rr1.googlevideo.com/videoplayback?c=VISIONOS&itag=251")
        assert "Origin" not in vision_h
        assert "Referer" not in vision_h
        assert "Safari" in vision_h["User-Agent"]

        web_h = media_headers_for_url("https://rr1.googlevideo.com/videoplayback?c=WEB_REMIX&itag=251")
        assert web_h["Origin"] == "https://music.youtube.com"
        assert web_h["Referer"] == "https://music.youtube.com/"

    def test_probe_detects_403_past_1mib_and_falls_back_to_next_client(self):
        session = MagicMock()
        vd_str = "Cg" + "A" * 44
        sw_resp = _FakeResponse(status_code=200, text=f')]}}\n[["{vd_str}"]]\n')

        bad_url = f"https://rr1.googlevideo.com/videoplayback?c=VISIONOS&clen={AUTH_BOUNDARY_BYTES + 65536}&id=1"
        good_url = f"https://rr2.googlevideo.com/videoplayback?c=VISIONOS&clen={AUTH_BOUNDARY_BYTES + 65536}&id=2"

        p1 = _FakeResponse(status_code=200, json_data={
            "playabilityStatus": {"status": "OK"},
            "streamingData": {"adaptiveFormats": [
                {"itag": 251, "mimeType": 'audio/webm; codecs="opus"', "bitrate": 137000, "url": bad_url}
            ]},
        })
        p2 = _FakeResponse(status_code=200, json_data={
            "playabilityStatus": {"status": "OK"},
            "playerConfig": {"audioConfig": {"loudnessDb": -2.5}},
            "streamingData": {"adaptiveFormats": [
                {"itag": 251, "mimeType": 'audio/webm; codecs="opus"', "bitrate": 137000, "url": good_url}
            ]},
        })

        post_calls = iter([p1, p2])
        seen_ranges = []

        def fake_get(url, headers=None, stream=False, timeout=None):
            if "sw.js_data" in url:
                return sw_resp
            seen_ranges.append(headers.get("Range"))
            if "id=1" in url:
                return _FakeResponse(status_code=403, headers={"Content-Type": "text/plain"})
            return _FakeResponse(
                status_code=206,
                headers={"Content-Type": "audio/webm"},
                chunks=[b"x" * 16384],
            )

        session.get.side_effect = fake_get
        session.post.side_effect = lambda *a, **kw: next(post_calls)

        resolver = StreamResolver(session=session)
        stream = resolver.resolve_stream("vid123")

        assert stream.url == good_url
        assert stream.client_id == "VISIONOS_1_02"
        assert stream.loudness_db == -2.5
        assert seen_ranges[0].startswith(f"bytes={AUTH_BOUNDARY_BYTES}-")

        # Client 1 (VISIONOS_0_1) should now be excluded for vid123
        assert "VISIONOS_0_1" in resolver._excluded_for("vid123")

    def test_coalescing_runs_single_resolution_for_concurrent_callers(self):
        resolver = StreamResolver(enable_direct_innertube=False)
        calls = 0
        barrier = threading.Barrier(2)

        def slow_ytdlp(vid):
            nonlocal calls
            calls += 1
            time.sleep(0.08)
            return ResolvedStream(video_id=vid, url="https://rr1.googlevideo.com/v?c=WEB", quality="OPUS", client_id="YTDLP")

        resolver._extract_ytdlp = slow_ytdlp
        resolver._probe = lambda *a, **kw: "OK"

        results = []

        def worker():
            barrier.wait()
            results.append(resolver.resolve_stream("same_vid"))

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert calls == 1
        assert len(results) == 2
        assert results[0].url == results[1].url

    def test_permanent_unplayable_verdict_is_cached(self):
        session = MagicMock()
        session.get.return_value = _FakeResponse(status_code=200, text="no_vd")
        session.post.return_value = _FakeResponse(status_code=200, json_data={
            "playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm your age"}
        })
        resolver = StreamResolver(session=session)
        resolver._extract_ytdlp = MagicMock(side_effect=PermanentlyUnplayableError("age-restricted", age_gated=True))

        with pytest.raises(PermanentlyUnplayableError):
            resolver.resolve_stream("age_vid")

        post_count = session.post.call_count
        # Second call within 10 min TTL must fail immediately from verdict cache
        with pytest.raises(PermanentlyUnplayableError):
            resolver.resolve_stream("age_vid")
        assert session.post.call_count == post_count

    def test_on_playback_refused_clears_cache_and_excludes_client(self):
        resolver = StreamResolver()
        stream = ResolvedStream(
            video_id="v1", url="https://rr/1", quality="OPUS", client_id="VISIONOS_0_1"
        )
        resolver._remember_resolved("v1", stream)
        assert resolver.resolve_stream("v1") is stream

        resolver.on_playback_refused("v1", url="https://rr/1", client_id="VISIONOS_0_1")
        assert "v1" not in resolver._recent
        assert "VISIONOS_0_1" in resolver._excluded_for("v1")


# ------------------------------------------------------------------ #
# AudioCache
# ------------------------------------------------------------------ #

class TestAudioCache:
    def test_chunked_download_and_lru_eviction_respects_protected(self, tmp_path):
        session = MagicMock()
        payload_a = b"A" * 5000
        payload_b = b"B" * 5000
        payload_c = b"C" * 5000

        def fake_get(url, headers=None, stream=False, timeout=None):
            data = payload_a if "va" in url else (payload_b if "vb" in url else payload_c)
            rng = (headers or {}).get("Range", "bytes=0-4999")
            s, e = [int(x) for x in rng.replace("bytes=", "").split("-")]
            return _FakeResponse(status_code=206, chunks=[data[s : e + 1]])

        session.get.side_effect = fake_get
        cache = AudioCache(cache_dir=tmp_path, max_bytes=11000, session=session)

        sa = ResolvedStream("va", "https://cdn/va", "OPUS 140kbps", content_length=5000)
        sb = ResolvedStream("vb", "https://cdn/vb", "OPUS 140kbps", content_length=5000)
        sc = ResolvedStream("vc", "https://cdn/vc", "OPUS 140kbps", content_length=5000)

        ca = cache.download(sa)
        time.sleep(0.01)
        cb = cache.download(sb)
        assert ca is not None and cb is not None

        # Download sc while protecting va -> vb (unprotected, newer than va) should be evicted, va kept!
        cc = cache.download(sc, protected_ids={"va"})
        assert cc is not None
        assert cache.get("va") is not None
        assert cache.get("vb") is None
        assert cache.get("vc") is not None


# ------------------------------------------------------------------ #
# MPVPlayer Concurrency, Crossfade, Loudness & Live MPV Integration
# ------------------------------------------------------------------ #

class TestMPVPlayerFixes:
    def test_concurrent_queue_mutations_and_track_end_issue_7(self):
        """Regression test for GitHub issue #7: concurrent queue mutations and track finishes."""
        p = _bare_player()
        p.autoplay = False
        p._play_track_sync = MagicMock()
        errors = []

        def mutator():
            try:
                for i in range(100):
                    p.append_queue([Track(id=f"t{i}", title=f"T{i}", artist="A")])
                    p.shuffle_queue()
                    if i % 5 == 0:
                        p.remove_from_queue(0)
            except Exception as exc:
                errors.append(exc)

        def finisher():
            try:
                for _ in range(100):
                    p._handle_track_finished()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=mutator), threading.Thread(target=finisher)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_stale_play_generation_does_not_overwrite_newer_track(self):
        p = _bare_player()
        loaded = []

        def slow_resolve(vid, skip_clients=None):
            time.sleep(0.08 if vid == "slow" else 0.01)
            return ResolvedStream(video_id=vid, url=f"https://cdn/{vid}", quality="OPUS")

        p.resolver.resolve_stream = slow_resolve
        p._load_into_active_deck = lambda track, stream, gen, start_pos=0.0: loaded.append(track.id) or True

        t_slow = Track(id="slow", title="Slow", artist="A")
        t_fast = Track(id="fast", title="Fast", artist="B")

        p.play(t_slow)
        time.sleep(0.01)
        p.play(t_fast)
        time.sleep(0.15)

        assert loaded == ["fast"]
        assert p.current_track is not None and p.current_track.id == "fast"

    def test_loudness_and_mpv_options_formatting(self):
        assert _compute_loudness_gain_db(2.5) == -2.5
        assert _compute_loudness_gain_db(20.0) == -12.0
        assert _compute_loudness_gain_db(-10.0) == 6.0

        opts = _format_mpv_options(
            "Song · Artist",
            headers={"User-Agent": "CustomUA", "Origin": "https://music.youtube.com"},
            start_pos=12.5,
            loudness_db=3.0,
            normalize_loudness=True,
        )
        assert opts["force-media-title"] == "Song · Artist"
        assert opts["user-agent"] == "CustomUA"
        assert opts["http-header-fields"] == "Origin: https://music.youtube.com"
        assert opts["start"] == "12.50"
        assert opts["af"] == "volume=volume=-3.00dB"

    def test_cycle_crossfade_and_loudness_toggle(self):
        p = _bare_player()
        assert p.crossfade_seconds == 0
        assert p.cycle_crossfade() == 3
        assert p.cycle_crossfade() == 6
        assert p.cycle_crossfade() == 0
        assert p.loudness_normalization is True
        assert p.toggle_loudness_normalization() is False
        assert p.toggle_loudness_normalization() is True

    def test_live_mpv_gapless_playback_and_error_recovery(self, tmp_path, monkeypatch):
        """Real headless mpv IPC verification: playback, quality update, and 403 recovery."""
        monkeypatch.setenv("AME_MPV_AO", "null")
        wav1 = tmp_path / "one.wav"
        wav2 = tmp_path / "two.wav"
        _write_wav(wav1, 0.25)
        _write_wav(wav2, 0.25)

        resolver = StreamResolver(enable_direct_innertube=False)
        calls = {"n": 0}

        def fake_resolve_stream(vid, skip_clients=None):
            calls["n"] += 1
            if vid == "bad_then_good":
                if not skip_clients:
                    return ResolvedStream(video_id=vid, url=str(tmp_path / "missing.wav"), quality="OPUS · BAD", client_id="BAD_CLIENT")
                return ResolvedStream(video_id=vid, url=str(wav2), quality="OPUS 140kbps · GOOD", client_id="GOOD_CLIENT")
            return ResolvedStream(video_id=vid, url=str(wav1), quality="OPUS 137kbps · CACHE")

        resolver.resolve_stream = fake_resolve_stream

        player = MPVPlayer(resolver=resolver, cache=False)
        try:
            player.play(Track(id="bad_then_good", title="Recover [Test]", artist="Artist"))
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                if player.current_stream_quality == "OPUS 140kbps · GOOD" and not player.is_buffering:
                    break
                time.sleep(0.05)

            assert player.current_stream_quality == "OPUS 140kbps · GOOD"
            assert calls["n"] >= 2
        finally:
            player.cleanup()

    def test_live_mpv_crossfade_handoff(self, tmp_path, monkeypatch):
        """Real dual-mpv equal-power crossfade swaps decks and advances queue cleanly."""
        monkeypatch.setenv("AME_MPV_AO", "null")
        wav1 = tmp_path / "t1.wav"
        wav2 = tmp_path / "t2.wav"
        _write_wav(wav1, 3.0)
        _write_wav(wav2, 1.0)

        resolver = MagicMock()
        resolver.resolve_stream.side_effect = lambda vid, skip_clients=None: ResolvedStream(
            video_id=vid,
            url=str(wav1 if vid == "t1" else wav2),
            quality="OPUS 137kbps · TEST",
        )

        player = MPVPlayer(resolver=resolver, cache=False)
        try:
            player.crossfade_seconds = 1
            t1 = Track(id="t1", title="Track 1", artist="A", duration_seconds=3)
            t2 = Track(id="t2", title="Track 2", artist="B", duration_seconds=1)
            player.play_with_queue(t1, [t2])

            # Wait until crossfade triggers near t=2s and hands off to t2 by t=3.5s
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if player.current_track and player.current_track.id == "t2":
                    break
                time.sleep(0.05)

            assert player.current_track is not None
            assert player.current_track.id == "t2"
            assert player.history and player.history[-1].id == "t1"
            assert player.queue == []
        finally:
            player.cleanup()


# ------------------------------------------------------------------ #
# API Parsing & Chart Deduplication
# ------------------------------------------------------------------ #

class TestAPIHelpers:
    def test_get_charts_skip_tracks_avoids_duplicate_trending(self):
        api = YTMusicAPI()
        api.yt = MagicMock()
        api.yt.get_charts.return_value = {"videos": [{"playlistId": "PL1", "title": "Top"}], "genres": []}
        api.get_trending_songs = MagicMock()

        tracks, playlists = api.get_charts(include_tracks=False)
        assert tracks == []
        assert len(playlists) == 1
        api.get_trending_songs.assert_not_called()

    def test_parse_track_handles_dict_and_list_thumbnails(self):
        tr = _parse_track({
            "videoId": "abc",
            "title": "Song [Official]",
            "artists": [{"name": "Artist"}],
            "thumbnail": {"thumbnails": [{"url": "https://img/1.jpg"}]},
            "length": "3:05",
        })
        assert tr is not None
        assert tr.thumbnail == "https://img/1.jpg"
        assert tr.duration_seconds == 185


class TestUIFixes:
    def test_app_renders_bracketed_titles_tabs_and_clears_status(self, tmp_path: Path):
        import asyncio
        from unittest.mock import patch as _patch
        from app import AmeApp
        from api import Playlist
        from ui.views import SearchView, TrendingView
        from ui.widgets import HelpScreen, PlayerBar, TopBar

        tr = Track(id="x1", title="Track [Official Video]", artist="Artist [Feat. X]", duration_seconds=120)
        pl = Playlist(id="PL1", title="Chart [Top]")

        async def _run():
            with _patch.object(MPVPlayer, "_start_mpv"), \
                 _patch.object(YTMusicAPI, "get_trending_songs", return_value=[tr]), \
                 _patch.object(YTMusicAPI, "get_recommendations", return_value=[tr]), \
                 _patch.object(YTMusicAPI, "get_recommended_playlists", return_value=[pl]), \
                 _patch.object(YTMusicAPI, "get_charts", return_value=([], [pl])), \
                 _patch("settings._default_settings_path", return_value=tmp_path / "settings.json"), \
                 _patch("settings._default_recent_path", return_value=tmp_path / "recent.json"):
                app = AmeApp()
                async with app.run_test(size=(90, 30)) as pilot:
                    await pilot.pause(0.15)
                    top = app.query_one("#top_bar", TopBar)
                    assert top.status == ""
                    rendered = top.render().plain
                    assert "VOL" in rendered

                    bar = app.query_one("#player_bar", PlayerBar)
                    bar.set_track(tr, "OPUS 137kbps · VISIONOS")
                    bar.set_crossfade(3)
                    await pilot.pause(0.05)

                    # TrendingView tab-aware selection: playlist must be None when recommended tab is active
                    tv = app.query_one("#view_trending", TrendingView)
                    assert tv.get_selected_track() is not None
                    assert tv.get_selected_playlist() is None

                    # SearchView tab-aware selection
                    sv = app.query_one("#view_search", SearchView)
                    sv.set_results([tr], [pl])
                    assert sv.get_selected_track() is not None
                    assert sv.get_selected_playlist() is None

                    # Crossfade, loop, loudness shortcuts
                    await pilot.press("x")
                    assert app.player.crossfade_seconds == 3
                    await pilot.press("l")
                    assert app.player.loop_mode == LoopMode.LOOP_ONE
                    await pilot.press("N")
                    assert app.player.loudness_normalization is False

                    # HelpScreen composes with escaped markup and new bindings
                    app.action_show_help()
                    await pilot.pause(0.05)
                    assert isinstance(app.screen, HelpScreen)
                    await pilot.press("escape")
                    app.player.cleanup()

        asyncio.run(_run())
