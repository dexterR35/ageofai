"""Clock transitions must never reinterpret elapsed time or replay a backlog."""
import pytest

from server.pacing import AdaptiveClock


def test_speed_changes_integrate_elapsed_time_at_the_old_rate():
    clock = AdaptiveClock(1000, 0, 12, {})
    info = clock.update(1120, 10, [180], False)
    assert info["speed"] == 3
    assert clock.target(10) == 1120
    assert clock.target(20) == 1150


@pytest.mark.parametrize("wait,crisis,speed", [(179, False, 12), (180, False, 3),
                                              (600, False, 1), (29, True, 12), (30, True, 1)])
def test_decision_grace_and_crisis_thresholds(wait, crisis, speed):
    clock = AdaptiveClock(0, 0, 12, {})
    assert clock.update(0, 0, [wait], crisis)["speed"] == speed


def test_clock_recovers_gradually_after_pressure_clears():
    clock = AdaptiveClock(0, 0, 12, {})
    clock.update(0, 0, [600], False)
    assert clock.update(0, 1, [], False)["speed"] == 1
    assert clock.update(0, 15, [], False)["speed"] == 1
    assert clock.update(0, 16, [], False)["speed"] == 2
    assert clock.update(0, 20, [], False)["speed"] == 2
    assert clock.update(0, 21, [], False)["speed"] == 4
    assert clock.update(0, 26, [], False)["speed"] == 8
    assert clock.update(0, 31, [], False)["speed"] == 12


def test_backlog_is_capped_and_rebase_discards_old_debt():
    clock = AdaptiveClock(1000, 0, 12, {"max_lead_game_seconds": 60})
    clock.update(1000, 3600, [], False)
    assert clock.target(3600) == 1060
    clock.rebase(500, 4000)
    assert clock.target(4001) == 512


def test_slow_base_speed_never_accelerates_during_a_crisis():
    clock = AdaptiveClock(0, 0, 0.5, {})
    assert clock.update(0, 0, [600], True)["speed"] == 0.5
