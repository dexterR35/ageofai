"""Version 6: the world on disk, recovery, the doctor, the timelapse."""
import gzip
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ai.llm import FakeLLM
from engine import persist
from engine.entities import State
from engine.rules import default_rules
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 7, 0, tzinfo=TZ)


def world(**kw):
    kw.setdefault("start", SPRING)
    return World(R, tz=TZ, **kw)


# ---------------------------------------------------------------------------- saves
def test_save_and_load_give_the_same_world(tmp_path):
    w = world()
    w.run(2 * 86400)
    w.journal.sink = lambda ev: None                    # a live hook must not end up in the file
    persist.save_world(w, tmp_path)
    assert w.journal.sink is not None                   # …and it is put back after saving
    back, which = persist.load_latest(tmp_path)
    assert which == persist.LATEST
    assert back.day == w.day and back.clock.t == w.clock.t
    assert [c.pop for c in back.civs] == [c.pop for c in w.civs]
    assert [round(c.stock["food"], 3) for c in back.civs] == [round(c.stock["food"], 3) for c in w.civs]
    assert back.journal.sink is None
    w.run(86400)
    back.run(86400)                                     # the loaded world carries on exactly the same
    assert [round(c.stock["wood"], 3) for c in back.civs] == [round(c.stock["wood"], 3) for c in w.civs]


def test_a_damaged_save_falls_back_to_the_one_before(tmp_path):
    w = world()
    persist.save_world(w, tmp_path)
    w.run(3600)
    persist.save_world(w, tmp_path)
    (tmp_path / persist.LATEST).write_bytes(b"not a save")
    back, which = persist.load_latest(tmp_path)
    assert which == persist.PREVIOUS and back is not None


def test_hourly_copies_and_pruning(tmp_path):
    now = datetime(2027, 6, 30, 12)
    for d in range(40):
        for h in (0, 6, 12, 18):
            stamp = (now - timedelta(days=d)).replace(hour=h).strftime("%Y-%m-%d_%H")
            (tmp_path / f"{stamp}.save").write_bytes(b"x")
    gone = persist.prune(tmp_path, now=now, hourly_days=7)
    left = persist.hourly_saves(tmp_path)
    old = [p for when, p in left if when < now - timedelta(days=8)]
    recent = [p for when, p in left if when >= now - timedelta(days=6)]
    assert gone > 0 and len(recent) >= 24
    days = {p.stem[:10] for p in old}
    assert len(old) == len(days)                         # one a day once they are old


def test_world_adopt_keeps_the_object(tmp_path):
    w = world()
    w.run(86400)
    persist.save_world(w, tmp_path)
    w.run(86400)
    loaded, _ = persist.load_latest(tmp_path)
    me = w
    w.adopt(loaded)
    assert w is me and w.day == 2
    assert w.civs[0].ai.w is w and w.behavior.w is w and w.military.w is w
    w.run(3600)                                          # still runs after adopting


# ---------------------------------------------------------------------------- recovery
def test_the_runner_resumes_and_replays_missing_hours(tmp_path):
    from server.runner import LiveRunner

    three_hours_ago = datetime.now(TZ) - timedelta(hours=3)
    w = World(R, tz=TZ, start=three_hours_ago)
    persist.save_world(w, tmp_path / "saves")
    r = LiveRunner(db=tmp_path / "r.db", llm=FakeLLM(up=False), pictures=False, save_dir=tmp_path / "saves")
    try:
        assert r.persistent and r.recovered == persist.LATEST
        assert r.caught_up == pytest.approx(3, abs=0.1)
        assert abs(r.w.clock.t - time.time()) < 120
        assert any(e.kind == "recovery" for e in r.w.journal.events)
        r.save()
        assert (tmp_path / "saves" / persist.LATEST).exists()
    finally:
        r.mind.close()


def test_a_test_run_is_not_saved(tmp_path):
    from server.runner import LiveRunner

    r = LiveRunner(db=tmp_path / "r.db", llm=FakeLLM(up=False), pictures=False, speed=60, start=SPRING,
                   save_dir=tmp_path / "saves")
    try:
        assert not r.persistent and not r.recovered
    finally:
        r.mind.close()


# ---------------------------------------------------------------------------- the doctor
@pytest.fixture
def runner(tmp_path):
    from server.runner import LiveRunner

    r = LiveRunner(db=tmp_path / "d.db", llm=FakeLLM(up=False), pictures=False, speed=60, start=SPRING,
                   persistent=True, save_dir=tmp_path / "saves", evolve=False)
    yield r
    r.mind.close()


def test_doctor_restores_the_last_save_after_impossible_values(runner):
    r = runner
    r.w.run(3600)
    r.save()
    food = r.w.civs[0].stock["food"]
    r.w.civs[0].stock["food"] = float("nan")
    assert r.doctor.check() == "restore"
    how = r.restore()
    assert "went back" in how
    assert r.w.civs[0].stock["food"] == pytest.approx(food)
    assert not r.doctor.impossible_values()


def test_a_broken_world_is_never_saved(runner):
    r = runner
    r.w.civs[0].stock["wood"] = -50
    r.save()
    assert not (r.save_dir / persist.LATEST).exists()


def test_doctor_frees_a_stuck_person(runner):
    r = runner
    p = r.w.civs[0].people[0]
    p.state = State.WALK
    r.doctor.stuck_people()
    r.w.clock.t += 31 * 60
    p.state = State.WALK
    r.doctor.stuck_people()                              # first: a new path
    assert any(e.kind == "doctor" and "new path" in e.text for e in r.w.journal.events)
    r.w.clock.t += 31 * 60
    p.state = State.WALK
    r.doctor.stuck_people()                              # still stuck: moved to a free tile
    assert p.state == State.IDLE
    assert r.store.doctor_log(0)


def test_doctor_wakes_an_idle_kingdom(runner):
    r = runner
    civ = r.w.civs[0]
    r.w.clock.t += 20 * 86400                            # long after the last thing it started
    civ.born_t -= 20 * 86400
    r.doctor.idle_kingdoms()
    assert any("did nothing" in e.text for e in r.w.journal.events)
    assert r.mind.next_council[civ.id] <= r.w.clock.t


def test_doctor_pauses_a_silent_local_ai(runner):
    r = runner
    r.llm.failures_in_a_row = 3
    now = time.monotonic()
    r.llm.fail_times = [now - 20, now - 10, now]
    r.doctor.local_ai()
    assert r.llm.paused_until > now and not r.llm.available()


def test_doctor_turns_the_decor_off_when_slow(runner, monkeypatch):
    r = runner
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    r.doctor.tick_time(900)
    clock[0] += 6 * 60
    r.doctor.tick_time(900)
    assert r.doctor.decor_off and r.frame_seconds > 1


def test_doctor_prunes_when_the_disk_is_full(runner, monkeypatch):
    r = runner
    monkeypatch.setattr(persist, "disk_used_share", lambda folder=None: 0.95)
    r.doctor.disk()
    assert any("disk" in e.text for e in r.w.journal.events)


def test_weekly_report_marks_repeats_as_bugs(runner):
    r = runner
    for i in range(5):
        r.store.save_doctor(r.w.clock.t - i, "stuck", "north", "someone was stuck", "moved")
    facts = r.doctor.report_facts()
    assert facts["repairs"] == 5 and any("stuck" in b for b in facts["bugs"])
    text, by_ai = r.doctor.write_report(facts, None)
    assert not by_ai and "bugs" in text


# ---------------------------------------------------------------------------- timelapse
def test_timelapse_frames_are_recorded_and_served(runner):
    from fastapi.testclient import TestClient

    from server.app import create_app

    r = runner
    for _ in range(5):
        r.w.run(600)
        r.record()
    rows = r.store.frames_since(0)
    assert len(rows) == 5
    f = json.loads(gzip.decompress(rows[-1][1]))
    assert len(f["nodes_all"]) == len(r.w.map.nodes) and f["people"]
    with TestClient(create_app(r, run_world=False)) as c:
        data = c.get("/api/timelapse?hours=24").json()
        assert len(data["frames"]) == 5 and data["play_seconds"] == 120
        assert c.get("/api/doctor").json()["log"] == []
