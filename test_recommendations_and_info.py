"""Tests for Song Info/Credits, Audio<->Video Version Switch, History Recommendations & Category Queue."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from api import Album, Track, YTMusicAPI
from recommendations import (
    build_category_queue,
    build_taste_profile,
    category_similarity,
    infer_track_categories,
    rank_recommendations,
)
from song_info import (
    compact_count,
    fetch_song_info,
    parse_watch_html_song_info,
    parse_ytm_credits_dict,
)
from stream_resolver import ResolvedStream


# ------------------------------------------------------------------ #
# 1. Song Info & Credits
# ------------------------------------------------------------------ #


class TestSongInfo:
    def test_compact_count_truncates_like_youtube(self):
        assert compact_count("950") == "950"
        assert compact_count("1234") == "1.2K"
        assert compact_count("999999") == "999K"
        assert compact_count("2345678") == "2.3M"
        assert compact_count("1500000000") == "1.5B"

    def test_parse_watch_html_and_ytm_credits(self):
        player_json = {
            "videoDetails": {
                "videoId": "abcdefghijk",
                "viewCount": "2345678",
                "shortDescription": "Provided to YouTube\n\nReleased on: 2024-05-17\n\nAuto-generated.",
            }
        }
        data_json = {
            "contents": {
                "twoColumnWatchNextResults": {
                    "results": {
                        "results": {
                            "contents": [
                                {
                                    "videoPrimaryInfoRenderer": {
                                        "videoActions": {
                                            "menuRenderer": {
                                                "topLevelButtons": [
                                                    {
                                                        "likeButtonViewModel": {
                                                            "buttonViewModel": {"title": "42K"}
                                                        }
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                },
                                {"commentsEntryPointHeaderRenderer": {"commentCount": {"simpleText": "1.4K"}}},
                            ]
                        }
                    }
                }
            }
        }
        html_doc = (
            f"<script>var ytInitialPlayerResponse = {json.dumps(player_json)};</script>"
            f"<script>var ytInitialData = {json.dumps(data_json)};</script>"
        )
        parsed = parse_watch_html_song_info(html_doc, "abcdefghijk")
        assert parsed.views == "2.3M"
        assert parsed.likes == "42K"
        assert parsed.comments == "1.4K"
        assert parsed.release_date == "2024-05-17"

        credits_list = parse_ytm_credits_dict(
            {
                "performed_by": {"localized_title": "Performed by", "data": ["Artist X"]},
                "written_by": {"localized_title": "Written by", "data": ["Writer Y", "Writer Z"]},
                "other_sections": [{"localized_title": "Mastering", "data": ["Engineer A"]}],
            }
        )
        assert len(credits_list) == 3
        assert credits_list[1].role == "Written by"
        assert credits_list[1].text == "Writer Y, Writer Z"

    def test_fetch_song_info_combines_sources(self):
        tr = Track(
            id="abcdefghijk",
            title="Track",
            artist="Artist X",
            album="Album Z",
            album_id="MPRE1",
        )
        fake_api = MagicMock()
        fake_api.get_song_credits.return_value = {
            "produced_by": {"localized_title": "Produced by", "data": ["Producer Q"]},
            "other_sections": [],
        }
        fake_api.get_album.return_value = (Album(id="MPRE1", title="Album Z", year="2023"), [])
        session = MagicMock()
        session.get.return_value = MagicMock(
            status_code=200,
            text='<script>var ytInitialPlayerResponse = {"videoDetails":{"videoId":"abcdefghijk","viewCount":"15000","shortDescription":"Released on: 2023-11-01"}};</script><script>var ytInitialData = {};</script>',
        )
        stream = ResolvedStream(
            video_id="abcdefghijk",
            url="https://example.com/a",
            quality="OPUS",
            codec="opus",
            kbps=141,
            client_id="VISIONOS_0_1",
        )
        info = fetch_song_info(tr, api=fake_api, stream_info=stream, session=session)
        assert info.views == "15K"
        assert info.release_date == "2023-11-01"
        assert info.release_year == "2023"
        assert info.credits[0].role == "Produced by"
        assert "OPUS · 141 kbps" in info.codec_info


# ------------------------------------------------------------------ #
# 2. Listening-History Recommendations & Category-Matched Queue
# ------------------------------------------------------------------ #


class TestCategoryQueueAndHistoryRecs:
    def test_infer_track_categories_and_similarity(self):
        hindi = Track(id="h1", title='Tum Hi Ho (From "Aashiqui 2")', artist="Arijit Singh", duration_seconds=260)
        hindi2 = Track(id="h2", title="Channa Mereya", artist="Arijit Singh, Pritam", duration_seconds=290)
        phonk = Track(id="p1", title="METAMORPHOSIS - Phonk Drift", artist="INTERWORLD", duration_seconds=142)
        lofi = Track(id="l1", title="Midnight Study Beats (Lo-Fi Chillhop)", artist="Lofi Girl", duration_seconds=135)

        assert infer_track_categories(hindi).language == "hindi"
        assert "phonk" in infer_track_categories(phonk).styles
        assert "lofi" in infer_track_categories(lofi).styles

        assert category_similarity(hindi, hindi2) > category_similarity(hindi, phonk)
        assert category_similarity(lofi, phonk) < 0

    def test_skip_aware_taste_profile_and_history_ranked_recommendations(self):
        skipped = Track(id="skip1", title="Annoying Track", artist="Pop Star", duration_seconds=200)
        loved = Track(id="love1", title='Agar Tum Saath Ho (From "Tamasha")', artist="Arijit Singh", duration_seconds=340)
        stats_entries = [
            SimpleNamespace(track=skipped, play_count=4, listen_seconds=35.0),   # < 5% avg completion -> skipped
            SimpleNamespace(track=loved, play_count=5, listen_seconds=1500.0),   # ~88% avg completion -> loved
        ]
        profile = build_taste_profile(
            recent_history=[loved, skipped],
            stats_track_entries=stats_entries,
        )
        assert "skip1" in profile.skip_penalized_ids
        assert all(t.id != "skip1" for t in profile.seed_tracks)
        assert profile.dominant_language == "hindi"

        cands = [
            Track(id="c_phonk", title="Aggressive Drift Phonk", artist="Random", duration_seconds=140),
            Track(id="c_hindi", title="Raabta (Agent Vinod)", artist="Arijit Singh", duration_seconds=240),
            Track(id="c_bolly", title="Subhanallah", artist="Pritam", duration_seconds=250),
        ]
        ranked = rank_recommendations(cands, profile, limit=3)
        assert ranked[0].id in ("c_hindi", "c_bolly")
        assert ranked[-1].id == "c_phonk"

    def test_build_category_queue_locks_queue_to_seed_category(self):
        seed = Track(id="seed_lofi", title="Rainy Cafe (Lo-Fi Beats)", artist="Chillhop", duration_seconds=150)
        cands = [
            Track(id="bad_phonk", title="HARD DRIFT PHONK REMIX", artist="X", duration_seconds=130),
            Track(id="bad_nightcore", title="Song (Nightcore)", artist="Y", duration_seconds=140),
            Track(id="good_lofi1", title="Warm Tea Lo-Fi", artist="Nujabes", duration_seconds=165),
            Track(id="good_lofi2", title="Study Beats Chillhop", artist="Kupla", duration_seconds=155),
        ]
        q = build_category_queue(seed, cands, limit=2)
        q_ids = [t.id for t in q]
        assert q_ids == ["good_lofi1", "good_lofi2"]

    def test_api_get_category_queue_uses_related_and_watch(self):
        api = YTMusicAPI()
        mock_yt = MagicMock()
        mock_yt.get_watch_playlist.return_value = {
            "tracks": [
                {"videoId": "w1", "title": "Kesariya", "artists": [{"name": "Arijit Singh"}]},
                {"videoId": "w2", "title": "Drift Phonk", "artists": [{"name": "Phonk DJ"}]},
            ],
            "related": "MPRE_REL",
        }
        mock_yt.get_song_related.return_value = [
            {
                "title": "You might also like",
                "contents": [
                    {"videoId": "r1", "title": "Shayad (From \"Love Aaj Kal\")", "artists": [{"name": "Pritam"}]},
                ],
            }
        ]
        api.yt = mock_yt

        seed = Track(id="s1", title='Tum Hi Ho (From "Aashiqui 2")', artist="Arijit Singh")
        queue = api.get_category_queue(seed, limit=5)
        ids = [t.id for t in queue]
        assert "r1" in ids
        assert "w1" in ids
        # Hindi tracks ranked ahead of unrelated Phonk track
        assert ids.index("r1") < ids.index("w2")


# ------------------------------------------------------------------ #
# 3. UI Integration: I (Song Info) & Category Queue
# ------------------------------------------------------------------ #


def test_ui_song_info_and_category_queue(tmp_path: Path):
    asyncio.run(_run_ui_test(tmp_path))


async def _run_ui_test(tmp_path: Path) -> None:
    import app as app_mod
    from ui.widgets import SongInfoScreen

    t_audio = Track(
        id="aud_1111111",
        title="Midnight City",
        artist="M83",
        album="Hurry Up, We're Dreaming",
        duration="04:03",
        duration_seconds=243,
    )
    t_cat = Track(id="cat_3333333", title="Outro", artist="M83", duration_seconds=240)

    fake_player = MagicMock()
    fake_player.volume = 80
    fake_player.speed = 1.0
    fake_player.eq_preset = "flat"
    fake_player.skip_silence = False
    fake_player.is_playing = True
    fake_player.is_paused = False
    fake_player.is_buffering = False
    fake_player.playback_pos = 35.0
    fake_player.duration = 243.0
    fake_player.queue = [t_cat]
    fake_player.history = []
    fake_player.current_track = t_audio
    fake_player.current_stream_quality = "OPUS 141kbps"
    fake_player.current_stream_info = None

    fake_api = MagicMock()
    fake_api.resolver = MagicMock()
    fake_api.get_trending_songs.return_value = [t_audio]
    fake_api.get_recommendations.return_value = [t_audio]
    fake_api.get_recommended_playlists.return_value = []
    fake_api.get_charts.return_value = ([], [])
    fake_api.get_category_queue.return_value = [t_cat]
    fake_api.get_song_credits.return_value = {
        "written_by": {"localized_title": "Written by", "data": ["Anthony Gonzalez"]},
        "other_sections": [],
    }

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
        async with ame_app.run_test(size=(120, 34)) as pilot:
            await pilot.pause()

            # Press 'I' -> opens SongInfoScreen
            await pilot.press("I")
            await pilot.pause(0.1)
            assert isinstance(ame_app.screen, SongInfoScreen)
            await pilot.press("escape")
            await pilot.pause()
