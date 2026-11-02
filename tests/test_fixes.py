"""Bugs found in the code review (after version 7), one test each."""
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ai.llm import FakeLLM
from engine import persist
from engine.rules import default_rules
from engine.store import Store
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 7, 0, tzinfo=TZ)


def world(**kw):
    kw.setdefault("start", SPRING)
    return World(R, tz=TZ, **kw)


# ---------------------------------------------------------------------------- 1. restoring a save
def test_restore_points_every_part_at_the_live_world(tmp_path):
    w = world()
    persist.save_world(w, tmp_path)
    loaded, _ = persist.load_latest(tmp_path)
    w.adopt(loaded)
    assert w.fog.w is w and w.bandits.w is w
    assert all(c.ai.roads.w is w and c.ai.army.w is w for c in w.civs)
    ids = {w.next_id(), w.bandits.w.next_id(), w.fog.w.next_id()}
    assert len(ids) == 3                                   # no two things get the same id after a restore


# ---------------------------------------------------------------------------- 2. the local AI off the game thread
class ThreadSpyLLM(FakeLLM):
    def __init__(self):
        super().__init__(dim=16)
        self.embed_threads: list[str] = []

    def embed(self, texts):
        self.embed_threads.append(threading.current_thread().name)
        return super().embed(texts)


def test_memories_are_embedded_on_the_ai_thread(tmp_path):
    from ai.live_mind import LiveMind

    w = world()
    llm = ThreadSpyLLM()
    store = Store(tmp_path / "m.db")
    mind = LiveMind(w, store, llm)
    try:
        w.log(w.civs[0], "battle", "Battle: South attacks North!", 9)
        assert llm.embed_threads == [] and len(mind.memory_queue) == 1   # the game thread only queues it
        for _ in range(3):
            mind.tick()
            for fut, _ in list(mind.jobs):
                fut.result(timeout=30)
        mind.tick()
        rows = store.db.execute("SELECT text, vec FROM memories WHERE civ='north'").fetchall()
        assert any("South attacks North" in r[0] and r[1] for r in rows)    # saved, with its vector
        assert llm.embed_threads and threading.main_thread().name not in llm.embed_threads
    finally:
        mind.close()


def test_council_recall_embeds_outside_prepare(tmp_path):
    from ai.council import Council
    from ai.memory import MemoryBank

    w = world()
    llm = ThreadSpyLLM()
    bank = MemoryBank(Store(tmp_path / "c.db").db, llm)
    bank.add("north", w.clock.t - 3600, 1, "fire", "Day 1: a fire burned the granary", 7, embed=False)
    council = Council(llm, bank)
    ctx = council.prepare(w, w.civs[0])
    assert llm.embed_threads == []                       # prepare (game thread) makes no AI call
    assert council._recall(ctx) == ["Day 1: a fire burned the granary"]


# ---------------------------------------------------------------------------- 3. treaties
def test_honesty_is_rolled_once_per_treaty():
    w = world()
    w.start_t -= 30 * 86400
    north, south = w.civs
    p = w.diplomacy.pair("north", "south")
    w.diplomacy.treaty_until[p] = w.clock.t + 10 * 86400
    army = north.ai.army
    answers = {army.keeps_treaty(south) for _ in range(300)}
    assert len(answers) == 1                             # the same answer at every decision
    w.diplomacy.treaty_until[p] = w.clock.t + 20 * 86400  # a new treaty: a new roll, the old one forgotten
    army.keeps_treaty(south)
    assert len(army.word) == 1


# ---------------------------------------------------------------------------- 4. a kingdom that dies out
def _empty(w, civ):
    for p in list(civ.people):
        w.remove_person(civ, p, f"{p.name} left.", "leave")


def test_a_kingdom_with_no_people_is_reborn_in_the_live_world():
    w = world()
    w.live = True
    south = w.civs[1]
    _empty(w, south)
    w.run(86400)
    assert not w.civs[1].alive and w.civs[1].rebirth_at is not None and w.civs[1].castle is None
    w.run(7 * 86400 + 3600)
    reborn = w.civs[1]
    assert reborn.alive and reborn.generation == 2 and reborn.pop == 2 and reborn.castle is not None


def test_a_kingdom_with_no_people_just_loses_in_the_nights_games():
    w = world()
    south = w.civs[1]
    _empty(w, south)
    w.run(86400)
    assert not south.alive and south.rebirth_at is None and south.castle is not None


# ---------------------------------------------------------------------------- 5. a full disk
def _hourly(folder, now, days):
    for h in range(days * 24):
        stamp = (now - timedelta(hours=h)).strftime("%Y-%m-%d_%H")
        (folder / f"{stamp}.save").write_bytes(b"x")


def test_a_full_disk_frees_more_than_the_daily_pruning(tmp_path):
    now = datetime(2027, 6, 30, 12)
    _hourly(tmp_path, now, 60)
    persist.prune(tmp_path, now=now, hourly_days=7)                 # the normal rule ran already
    gone = persist.prune(tmp_path, now=now, hourly_days=7, disk_full=True, keep_all_days=30)
    assert gone > 0
    left = persist.hourly_saves(tmp_path)
    older_than_a_day = [when for when, _ in left if when < now - timedelta(days=1)]
    assert len(older_than_a_day) == len({w.date() for w in older_than_a_day})       # one a day
    very_old = [when for when, _ in left if when < now - timedelta(days=30)]
    assert len(very_old) == len({w.isocalendar()[:2] for w in very_old})           # one a week


def test_the_doctor_does_not_repeat_a_full_disk_every_10_minutes(tmp_path, monkeypatch):
    from server.runner import LiveRunner

    r = LiveRunner(db=tmp_path / "d.db", llm=FakeLLM(up=False), pictures=False, speed=60, start=SPRING,
                   persistent=True, save_dir=tmp_path / "saves", evolve=False)
    try:
        monkeypatch.setattr(persist, "disk_used_share", lambda folder=None: 0.95)
        for _ in range(3):
            r.doctor.disk()
        assert sum(1 for e in r.w.journal.events if e.kind == "doctor" and "disk" in e.text) == 1
    finally:
        r.mind.close()


# ---------------------------------------------------------------------------- 6. who goes to war
def test_archers_stay_home_when_part_of_the_army_marches():
    w = world(ai=False)
    w.start_t -= 30 * 86400
    north, south = w.civs
    for t in ["archer"] * 4 + ["militia"] * 6:
        w.military.spawn_unit(north, t)
    assert w.military.order_attack(north, south, share=0.6) == 6
    assert all(u.order == "defend" for u in north.soldiers if u.type == "archer")
    assert all(u.order == "muster" for u in north.soldiers if u.type == "militia")


# ---------------------------------------------------------------------------- 7. the map's time left
def test_a_site_without_builders_shows_no_old_estimate():
    from engine.entities import Building

    w = world(ai=False, start=datetime(2027, 4, 5, 9, 0, tzinfo=TZ))
    civ = w.civs[0]
    assert w.clock.working
    site = Building(w.next_id(), civ.id, "farm", 0, 0, 3, True, w.clock.t)
    site.builders_now = 2.0                               # yesterday's builders went to hunt
    civ.buildings.append(site)
    w.step(10)
    assert site.builders_now == 0.0                       # the page says "waiting for builders", not a stale time
