"""Look-ahead by simulation before the army marches, and CMA-ES for the night's training."""
import copy
import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai import cmaes, lookahead
from ai.evolution import Evolution
from ai.genome import Genome
from engine.rules import Rules, default_rules
from engine.store import Store
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
SPRING = datetime(2027, 4, 5, 9, 0, tzinfo=TZ)


def rules(**over) -> Rules:
    raw = copy.deepcopy(default_rules().raw)
    for k, v in over.items():
        raw.setdefault(k, {}).update(v)
    return Rules(raw)


def war_world(r: Rules) -> World:
    w = World(r, tz=TZ, start=SPRING)
    w.start_t -= 30 * 86400                              # past the 14 days of peace
    w.live = True
    north, south = w.civs
    for t in ["spearman"] * 6 + ["militia"] * 6:
        w.military.spawn_unit(north, t)
    w.military.spawn_unit(south, "militia")
    return w


# ---------------------------------------------------------------------------- look-ahead
def test_no_look_ahead_outside_the_live_world():
    w = war_world(rules(lookahead={"hours": 6}))
    w.live = False
    assert lookahead.gate(w, w.civs[0], w.civs[1], sync=True) is None


def test_the_futures_decide_and_a_wait_holds():
    w = war_world(rules(lookahead={"hours": 6, "samples": 1, "hold_hours": 12}))
    north, south = w.civs
    first = lookahead.gate(w, north, south, sync=True)
    assert first in ("attack", "wait")
    sim = lookahead.last_for(w, north)
    assert sim["choice"] == first and set(sim) >= {"attack", "wait", "against"}
    assert any(e.kind == "lookahead" for e in w.journal.events)
    if first == "wait":
        assert lookahead.gate(w, north, south, sync=True) == "wait"      # no new futures for 12 h
    from ai.council import Council
    facts = Council.diplomacy_facts(w, north)
    assert any(e.startswith("simulation") for e in facts["events"])     # the king sees the prediction


def test_a_future_never_looks_ahead_again_and_leaves_the_world_alone():
    w = war_world(rules(lookahead={"hours": 3}))
    north, south = w.civs
    t, army = w.clock.t, len(north.soldiers)
    blob = lookahead.snapshot(w)
    score = lookahead.play_future(blob, "north", "south", "attack", 3, 1)
    assert isinstance(score, float) and math.isfinite(score)
    assert w.clock.t == t and len(north.soldiers) == army and not any(u.order == "muster" for u in north.soldiers)


@pytest.mark.parametrize("seen, marches", [("wait", False), ("pending", False), ("attack", True)])
def test_the_army_marches_only_when_the_futures_say_so(monkeypatch, seen, marches):
    w = war_world(rules())
    north, south = w.civs
    north.genome = Genome({**north.genome.genes, "theta_attack": 1.0}, id="bold")
    monkeypatch.setattr(lookahead, "gate", lambda world, civ, enemy, sync=False: seen)
    est = north.ai.estimate()
    note = north.ai.army.war(est)
    assert bool(north.muster) == marches, note


# ---------------------------------------------------------------------------- CMA-ES
def test_cmaes_finds_a_target_in_20_genes():
    rng = random.Random(3)
    st = cmaes.new_state([0.2] * 20, 0.2)
    start = math.dist(st["m"], [0.7] * 20)
    for _ in range(40):
        xs = sorted(cmaes.sample(st, 16, rng), key=lambda x: math.dist(x, [0.7] * 20))
        st = cmaes.update(st, xs, 16)
    assert math.dist(st["m"], [0.7] * 20) < 0.25 * start
    assert all(0.0 <= v <= 1.0 for v in st["m"]) and 0.02 <= st["sigma"] <= 0.5


def test_the_night_uses_cmaes_and_keeps_its_state(tmp_path):
    r = rules(evolution={"method": "cmaes"})
    ev = Evolution(Store(tmp_path / "c.db"), r, seed=1, population=8)
    rng = random.Random(1)
    pop = [Genome.random(r, rng, id=f"g{i}") for i in range(8)]
    fit = {g.id: -abs(g["theta_attack"] - 2.5) for g in pop}            # tonight: a high threshold won
    nxt = ev.next_generation(1, pop, fit, rng)
    best = max(pop, key=lambda g: fit[g.id])
    assert len(nxt) == 8 and nxt[0] is best and all(g.id.startswith("n2g") for g in nxt[1:])
    for g in nxt:
        for k, (lo, hi) in r.gene_ranges.items():
            assert lo - 1e-9 <= g[k] <= hi + 1e-9
    state = ev.store.cma_state("all")
    assert state and state["gen"] == 1 and len(state["m"]) == len(r.gene_ranges)
    ev.next_generation(2, nxt, {g.id: 0.0 for g in nxt}, rng)
    assert ev.store.cma_state("all")["gen"] == 2                         # it carries on from last night
