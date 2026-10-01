"""Tests for MPVPlayer loop-mode state machine and track-end transitions."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from player import LoopMode, MPVPlayer


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _bare_player() -> MPVPlayer:
    """Build an MPVPlayer without spawning mpv."""
    with patch.object(MPVPlayer, "_start_mpv"):
        p = MPVPlayer(resolver=MagicMock())
    return p


# ------------------------------------------------------------------ #
# Seam 1: LoopMode enum cycling
# ------------------------------------------------------------------ #

class TestLoopMode:
    def test_values(self):
        assert LoopMode.OFF.value == "off"
        assert LoopMode.LOOP_ONE.value == "loop_one"
        assert LoopMode.STOP_AFTER_ONE.value == "stop_after_one"

    def test_cycle_off_to_loop_one(self):
        assert LoopMode.OFF.next_mode() == LoopMode.LOOP_ONE

    def test_cycle_loop_one_to_stop(self):
        assert LoopMode.LOOP_ONE.next_mode() == LoopMode.STOP_AFTER_ONE

    def test_cycle_stop_to_off(self):
        assert LoopMode.STOP_AFTER_ONE.next_mode() == LoopMode.OFF

    def test_full_cycle_returns_to_start(self):
        mode = LoopMode.OFF
        for _ in range(3):
            mode = mode.next_mode()
        assert mode == LoopMode.OFF


# ------------------------------------------------------------------ #
# Seam 2: cycle_loop_mode public API
# ------------------------------------------------------------------ #

class TestCycleLoopMode:
    def test_default_is_off(self):
        p = _bare_player()
        assert p.loop_mode == LoopMode.OFF

    def test_cycle_advances_mode(self):
        p = _bare_player()
        assert p.cycle_loop_mode() == LoopMode.LOOP_ONE
        assert p.cycle_loop_mode() == LoopMode.STOP_AFTER_ONE
        assert p.cycle_loop_mode() == LoopMode.OFF

    def test_cycle_updates_player_state(self):
        p = _bare_player()
        p.cycle_loop_mode()
        assert p.loop_mode == LoopMode.LOOP_ONE


# ------------------------------------------------------------------ #
# Seam 3: _handle_track_finished state transitions
# ------------------------------------------------------------------ #

class TestTrackEndOff:
    def test_advances_to_next_in_queue(self):
        p = _bare_player()
        p.loop_mode = LoopMode.OFF
        track_a = MagicMock(id="a", duration_seconds=180)
        track_b = MagicMock(id="b", duration_seconds=180)
        p.current_track = track_a
        p.queue = [track_b]
        played = []
        p._play_track_sync = lambda t: played.append(t)
        p.on_queue_change = MagicMock()

        p._handle_track_finished()

        assert played == [track_b]
        assert p.queue == []

    def test_empty_queue_triggers_autoplay(self):
        p = _bare_player()
        p.loop_mode = LoopMode.OFF
        track_a = MagicMock(id="a")
        p.current_track = track_a
        p.queue = []
        autoplay_cb = MagicMock()
        p.on_autoplay_trigger = autoplay_cb

        p._handle_track_finished()

        autoplay_cb.assert_called_once_with(track_a)
        assert p.is_playing is False

    def test_empty_queue_no_autoplay_stops(self):
        p = _bare_player()
        p.loop_mode = LoopMode.OFF
        p.autoplay = False
        p.current_track = MagicMock(id="a")
        p.queue = []
        played = []
        p._play_track_sync = lambda t: played.append(t)

        p._handle_track_finished()

        assert played == []
        assert p.is_playing is False
        assert p.current_track is None


class TestTrackEndLoopOne:
    def test_replays_current_track(self):
        p = _bare_player()
        p.loop_mode = LoopMode.LOOP_ONE
        track_a = MagicMock(id="a", duration_seconds=180)
        p.current_track = track_a
        p.queue = []
        played = []
        p._play_track_sync = lambda t: played.append(t)

        p._handle_track_finished()

        assert played == [track_a]
        assert p.current_track == track_a

    def test_does_not_advance_queue(self):
        p = _bare_player()
        p.loop_mode = LoopMode.LOOP_ONE
        track_a = MagicMock(id="a", duration_seconds=180)
        track_b = MagicMock(id="b", duration_seconds=180)
        p.current_track = track_a
        p.queue = [track_b]
        played = []
        p._play_track_sync = lambda t: played.append(t)

        p._handle_track_finished()

        assert played == [track_a]
        assert p.queue == [track_b]

    def test_no_current_track_falls_through_to_off(self):
        p = _bare_player()
        p.loop_mode = LoopMode.LOOP_ONE
        p.current_track = None
        p.queue = []
        p.autoplay = False

        p._handle_track_finished()

        assert p.is_playing is False


class TestTrackEndStopAfterOne:
    def test_stops_without_advancing(self):
        p = _bare_player()
        p.loop_mode = LoopMode.STOP_AFTER_ONE
        track_a = MagicMock(id="a", duration_seconds=180)
        track_b = MagicMock(id="b", duration_seconds=180)
        p.current_track = track_a
        p.queue = [track_b]
        played = []
        p._play_track_sync = lambda t: played.append(t)
        state_cb = MagicMock()
        p.on_state_change = state_cb

        p._handle_track_finished()

        assert played == []
        assert p.queue == [track_b]
        assert p.is_playing is False
        state_cb.assert_called_once_with(False, False, False)

    def test_preserves_current_track(self):
        p = _bare_player()
        p.loop_mode = LoopMode.STOP_AFTER_ONE
        track_a = MagicMock(id="a")
        p.current_track = track_a
        p.queue = []

        p._handle_track_finished()

        assert p.current_track == track_a

    def test_does_not_trigger_autoplay(self):
        p = _bare_player()
        p.loop_mode = LoopMode.STOP_AFTER_ONE
        p.current_track = MagicMock(id="a")
        p.queue = []
        autoplay_cb = MagicMock()
        p.on_autoplay_trigger = autoplay_cb

        p._handle_track_finished()

        autoplay_cb.assert_not_called()
