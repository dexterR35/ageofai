"""Integration tests: run the real world for a few days and check its invariants."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai.genome import Genome
from engine.entities import Building
from engine.rules import RESOURCES, default_rules
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
START = datetime(2026, 10, 30, 7, 0, tzinfo=TZ)


def world(seed=1, start=START, **kw) -> World:
    return World(R, seed=seed, start=start, tz=TZ, **kw)


def fingerprint(w: World) -> tuple:
    return tuple((c.castle_level, c.pop, tuple(round(v, 3) for v in c.stock.values()),
                  tuple(sorted((p.first, round(p.x, 3), round(p.y, 3)) for p in c.people))) for c in w.civs)


def test_start_matches_the_doc():
    w = world(ai=False)
    for civ in w.civs:
        assert civ.pop == 2
        ages = sorted(round(civ.age(p, w.clock.t)) for p in civ.people)
        assert ages == [20, 24]
        assert civ.castle_level == 1
        # born on 30 Oct → +1000 food before the first winter
        assert civ.stock == {"food": 1200, "wood": 200, "stone": 50, "ore": 0, "gold": 0, "coins": 100}


def test_no_winter_bonus_in_spring():
    w = world(start=datetime(2027, 4, 1, 7, 0, tzinfo=TZ), ai=False)
    assert w.civs[0].stock["food"] == 200


def test_people_eat_48_food_per_day():
    w = world(ai=False)
    food0 = w.civs[0].stock["food"]
    w.run(86400)
    eaten = food0 - w.civs[0].stock["food"]
    spoiled = (food0 - 96) * R.storage["food_spoil_per_day"]
    assert eaten == pytest.approx(2 * 48 + spoiled, rel=0.01)


def test_storage_cap_loses_overflow():
    w = world(ai=False)
    civ = w.civs[0]
    civ.stock["wood"] = 490
    assert civ.deposit("wood", 30) == 10
    assert civ.stock["wood"] == 500 and civ.lost["wood"] == 20


def test_same_seed_same_world():
    a, b = world(seed=4), world(seed=4)
    a.run(2 * 86400)
    b.run(2 * 86400)
    assert fingerprint(a) == fingerprint(b)


def test_invariants_over_ten_days():
    w = world(seed=2)
    seen_ids = set()

    def check(world: World) -> None:
        for civ in world.civs:
            for r in RESOURCES:
                v = civ.stock[r]
                assert v >= 0 and not math.isnan(v), (civ.id, r, v)
            assert 0.5 <= civ.morale <= 1.2
            assert len(civ.workers(world.clock.t)) <= R.workers_max(civ.castle_level)
            for b in civ.buildings:
                assert b.level <= max(1, civ.castle_level) or b.type == "castle"
                assert 0 <= b.progress <= 1
            for p in civ.people:
                assert world.map.inside(int(p.x + 0.5), int(p.y + 0.5))
                seen_ids.add(p.id)
        # node worker counters must match reality
        claimed = {}
        for civ in world.civs:
            for p in civ.people:
                t = p.target
                if t is not None and (not isinstance(t, Building) or t.type == "farm"):
                    claimed[id(t)] = claimed.get(id(t), 0) + 1
        for n in world.map.nodes:
            assert n.workers == claimed.get(id(n), 0), n

    w.run(10 * 86400, on_day=check)
    for civ in w.civs:
        assert civ.castle_level >= 2, "the AI should reach castle 2 within ten days"
        assert civ.departures == 0


def test_nights_are_quiet_and_skipped():
    w = world(seed=1)
    w.run(86400)
    ticks_day1 = w.ticks
    # A whole day at 10 s ticks would be 8,640 ticks; skipping quiet nights saves a lot of them.
    assert ticks_day1 < 8640


def test_people_go_home_at_night():
    w = world(seed=1)
    w.run(15 * 3600 + 30 * 60)          # 30 Oct 22:30
    assert w.clock.hhmm() == "22:30"
    for civ in w.civs:
        for p in civ.people:
            assert p.state.name == "REST"


def test_random_genomes_do_not_crash():
    import random
    rng = random.Random(0)
    genomes = [Genome.random(R, rng), Genome.random(R, rng)]
    w = world(seed=7, genomes=genomes)
    w.run(5 * 86400)
    assert all(c.alive for c in w.civs)


def test_genome_crossover_and_mutation_stay_in_range():
    import random
    rng = random.Random(1)
    a, b = Genome.random(R, rng, "a"), Genome.random(R, rng, "b")
    child = Genome.crossover(a, b, rng).mutated(R, rng, rate=1.0)
    for k, (lo, hi) in R.gene_ranges.items():
        assert lo <= child[k] <= hi
    assert child.parents == ("a", "b")
    assert a.distance(a, R) == 0 and 0 < a.distance(b, R) <= 1


def test_ai_does_not_recruit_into_a_hungry_winter():
    w = world(start=datetime(2026, 12, 20, 7, 0, tzinfo=TZ))
    civ = w.civs[0]
    civ.stock["food"] = 150                  # two people, one bad week from starving
    est = civ.ai.estimate()
    assert not est.can_feed
    civ.stock["food"] = 3000
    assert civ.ai.estimate().can_feed       # with a big reserve, one more mouth is fine


def test_firewood_comes_first_in_winter():
    w = world(start=datetime(2027, 1, 10, 7, 0, tzinfo=TZ))
    civ = w.civs[0]
    civ.stock.update(wood=500, stone=500)
    for x, y in [(30, 30), (33, 30), (36, 30)]:
        b = w.start_building(civ, "house", x, y)
        b.level = b.target_level = 1
    civ.stock["wood"] = 0
    est = civ.ai.estimate()
    assert est.firewood_workers >= 1
    assert civ.ai.allocate(est)["wood"] >= est.firewood_workers


@pytest.mark.slow
def test_winter_start_survives():
    """Born in mid-December: the hardest start. Nobody may starve or leave."""
    w = world(seed=2, start=datetime(2026, 12, 15, 7, 0, tzinfo=TZ))
    w.run(80 * 86400)
    for civ in w.civs:
        assert civ.alive and civ.departures == 0
        assert civ.castle_level >= 2


@pytest.mark.slow
def test_version_1_milestone():
    """Doc: version 1 is done when a civ survives its first winter and reaches castle 4 in fast mode."""
    w = world(seed=1)
    w.run(130 * 86400)                   # 30 Oct → 9 Mar: through the whole first winter
    best = max(w.civs, key=lambda c: c.castle_level)
    assert best.alive and best.departures == 0
    assert best.castle_level >= 4
