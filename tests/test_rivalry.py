"""Rivalry: two different minds that compete in the normal live world.

A model per kingdom, the king deciding war and peace (within the game's rules), diplomacy events in the
king's facts, a lineage of genomes per kingdom, and the scoreboard.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai.council import Council, check_plan
from ai.llm import FakeLLM
from engine.rules import default_rules
from engine.store import Store
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 9, 0, tzinfo=TZ)


def world(**kw):
    kw.setdefault("start", SPRING)
    return World(R, tz=TZ, **kw)


def scripted_king(name: str, war: str = "none", target: str | None = None):
    """A fake model: advisors answer briefly, the king answers with `name` in his thought."""
    def respond(system, user):
        if "You are the king" in system:
            return {"thought": f"{name} thinks", "priorities": {"castle": 1.5}, "build_next": None, "war": war,
                    "target": target, "war_reason": f"{name}'s reason", "reason": "because",
                    "listened_to": "general"}
        return {"proposal": "build", "argument": "it helps", "priorities": {"food": 1.2}, "urgency": 0.5}
    return respond


def pump(mind, rounds: int = 40) -> None:
    """Run the LiveMind's background jobs to the end (the world stands still meanwhile)."""
    for _ in range(rounds):
        mind.tick()
        if not mind.jobs:
            return
        for fut, _ in list(mind.jobs):
            fut.result(timeout=30)
    mind.tick()


def soldiers(w, civ, n, utype="militia"):
    for _ in range(n):
        w.military.spawn_unit(civ, utype)


# ---------------------------------------------------------------------------- 1. a model per kingdom
def test_each_kingdom_thinks_with_its_own_model(tmp_path, monkeypatch):
    from ai.live_mind import LiveMind
    from ai.llm import clients_from_rules, model_for

    w = world()
    north, south = w.civs
    qwen = FakeLLM(scripted_king("North"), model="qwen3:14b")
    gemma = FakeLLM(scripted_king("South"), model="gemma3:12b")
    mind = LiveMind(w, Store(tmp_path / "m.db"), FakeLLM(up=False), llms={"north": qwen, "south": gemma})
    try:
        pump(mind)
        assert north.ai.directive.thought == "North thinks"
        assert south.ai.directive.thought == "South thinks"
        assert qwen.prompts and gemma.prompts
        assert all("South thinks" not in json.dumps(p) for p in qwen.prompts)
        assert mind.model_name("north") == "qwen3:14b" and mind.model_name("south") == "gemma3:12b"
    finally:
        mind.close()
    # one shared model unless llm.models names one per kingdom (whatever rules.yaml says today)
    monkeypatch.setitem(R.llm, "models", {})
    assert model_for(R, "north") == model_for(R, "south") == R.llm["model"]
    clients = clients_from_rules(R)
    assert clients["north"] is clients["south"]


def test_a_kingdom_whose_model_is_missing_plays_on_its_genes(tmp_path):
    from ai.live_mind import LiveMind

    w = world()
    north, south = w.civs
    qwen = FakeLLM(scripted_king("North"), model="qwen3:14b")
    gone = FakeLLM(scripted_king("South"), model="gemma3:12b", up=False)        # not pulled, or Ollama busy
    mind = LiveMind(w, Store(tmp_path / "m.db"), FakeLLM(up=False), llms={"north": qwen, "south": gone})
    try:
        pump(mind)
        assert north.ai.directive is not None and south.ai.directive is None
        assert not gone.prompts
        w.run(3600)                                          # the world plays on, South on its genome
    finally:
        mind.close()


def test_per_kingdom_models_from_rules(monkeypatch):
    from ai.llm import clients_from_rules, model_for

    monkeypatch.setitem(R.llm, "models", {"north": "qwen3:14b", "south": "gemma3:12b"})
    assert model_for(R, "north") == "qwen3:14b" and model_for(R, "south") == "gemma3:12b"
    shared = FakeLLM(model="qwen3:14b")
    clients = clients_from_rules(R, shared)
    assert clients["north"] is shared and clients["south"].model == "gemma3:12b"


# ---------------------------------------------------------------------------- 2. the king decides war and peace
def test_the_kings_war_intent_attacks_only_when_the_rules_allow(tmp_path):
    w = world()
    north, south = w.civs
    north.genome.genes["theta_attack"] = 3.0                 # the genes alone would never attack
    north.genome.genes["honesty"] = 0.9
    raw = {"war": "attack", "target": "South", "war_reason": "They refused our tribute.", "priorities": {}}
    plan, dropped = check_plan(raw, north)
    assert plan.intent == "defend" and not plan.war and any("army" in d for d in dropped)   # no army: arm first
    assert plan.priorities["defense"] >= 1.5
    soldiers(w, north, 6)
    soldiers(w, south, 12, "spearman")                       # South looks much stronger
    plan, dropped = check_plan(raw, north)
    assert plan.war and plan.intent == "attack" and plan.target == "south"
    assert any("first 14 days" in d for d in dropped)        # the peace period: the army waits
    north.ai.set_directive(plan, w.clock.t + 3600)
    note = north.ai.army.war(north.ai.estimate())
    assert not north.muster and "nobody may attack" in (note or "")
    w.start_t -= 30 * 86400                                  # the peace period is over
    pair = w.diplomacy.pair("north", "south")
    w.diplomacy.treaty_until[pair] = w.clock.t + 5 * 86400   # a treaty: the king may break it, at a price
    note = north.ai.army.war(north.ai.estimate())
    assert north.muster and "king's war" in note and plan.war_done
    assert w.diplomacy.at_war("north", "south")              # an honest king declares it first
    north.muster["until"] = w.clock.t - 1                    # the army has gathered: it marches
    trust = w.diplomacy.trust("south", "north")
    w.military._muster(north)
    assert any(u.order == "attack" for u in north.soldiers)
    assert w.diplomacy.trust("south", "north") < trust       # the broken treaty costs trust…
    assert w.diplomacy.rel("north", "south") <= -60          # …and relation


def test_a_king_without_enough_soldiers_does_not_attack():
    w = world()
    w.start_t -= 30 * 86400
    north, south = w.civs
    soldiers(w, north, 5)
    plan, _ = check_plan({"war": "attack", "target": "south", "priorities": {}}, north)
    assert plan.war
    north.ai.set_directive(plan, w.clock.t + 3600)
    for u in north.soldiers[2:]:
        north.units.remove(u)                                # the army melted before the march
    note = north.ai.army.war(north.ai.estimate())
    assert not north.muster and "arming" in note and not plan.war_done


def test_defend_and_seek_peace_keep_the_army_home():
    w = world()
    w.start_t -= 30 * 86400
    north, south = w.civs
    north.genome.genes["theta_attack"] = 1.0                 # the genes would attack…
    north.genome.genes["honesty"] = 0.9
    soldiers(w, north, 8, "spearman")
    for intent in ("defend", "seek_peace"):
        plan, _ = check_plan({"war": intent, "target": "south", "priorities": {}}, north)
        north.ai.set_directive(plan, w.clock.t + 3600)
        north.ai.army.war(north.ai.estimate())
        assert not north.muster, intent                      # …but the king keeps the army home
    north.ai.set_directive(None, 0)
    north.ai.army.war(north.ai.estimate())
    assert north.muster                                      # without the king, the genes decide as before


def test_seek_peace_sends_a_peace_offer(tmp_path):
    from ai.live_mind import LiveMind

    w = world()
    w.start_t -= 30 * 86400
    north, south = w.civs
    w.diplomacy.start_war("north", "south", by="south")
    mind = LiveMind(w, Store(tmp_path / "p.db"), FakeLLM(up=False),
                    llms={"north": FakeLLM(scripted_king("North", "seek_peace", "South")),
                          "south": FakeLLM(up=False)})
    try:
        pump(mind)
    finally:
        mind.close()
    offers = [m for m in w.diplomacy.messages if m.type == "peace" and m.src == "north"]
    assert offers and offers[0].text == "North's reason"
    assert offers[0].status in ("accepted", "refused")       # South's genes answered (its model is away)


# ---------------------------------------------------------------------------- 3. diplomacy in the king's facts
def test_a_refused_tribute_is_in_the_next_councils_facts():
    w = world()
    north, south = w.civs
    south.stock["gold"] = 0                                  # South can't pay: it refuses
    w.diplomat.send(north, south, {"type": "tribute", "ask": {"gold": 400}, "text": "Pay, or else."})
    assert any(m.type == "tribute" and m.status == "refused" for m in w.diplomacy.messages)
    king = FakeLLM(scripted_king("North"))
    council = Council(king)
    ctx = council.prepare(w, north)
    facts = ctx["summary"]["diplomacy"]
    assert any("refuses the tribute" in e for e in facts["events"])
    assert facts["relations"]["South"]["R"] < 0 and facts["peace_period_days_left"] > 13
    council.run(ctx)
    king_prompt = [u for s, u in king.prompts if "You are the king" in s][0]
    assert "refuses the tribute" in king_prompt and "seek_peace" in [s for s, _ in king.prompts][-1]


# ---------------------------------------------------------------------------- 4. a lineage per kingdom
@pytest.fixture
def tiny_bench(monkeypatch):
    small = {**R.evolution["benchmark"], "candidates": 1, "games_per_season": 1}
    monkeypatch.setitem(R.evolution, "benchmark", small)


def test_lineages_stay_separate_across_nights(tmp_path, tiny_bench):
    from ai.evolution import Evolution
    from server.runner import LiveRunner

    db = tmp_path / "l.db"
    s = Store(db)
    evo = Evolution(s, seed=5, processes=1, population=4, days=2, peer_games=1, champion_games=1)
    rep = evo.run_night()
    pop1 = s.population(1)
    assert sorted(g.meta["lineage"] for g in pop1) == ["north", "north", "south", "south"]
    assert set(rep.lineage_best) == {"north", "south"}
    by_id = {g.id: g.meta["lineage"] for g in pop1}
    hall = {g.id: g.meta.get("lineage") for g in s.hall()}
    assert hall.get("start-builder") == "north" and hall.get("start-warlord") == "south"
    for a, b, kind in s.db.execute("SELECT a, b, kind FROM matches WHERE night=1"):
        if kind == "peer":                                   # North's genomes play South's
            assert (by_id[a], by_id[b]) == ("north", "south")
        else:                                                # …and the other kingdom's champions
            assert hall.get(b) != by_id[a]
    rep2 = evo.run_night()
    for night in (2, 3):
        for g in s.population(night):
            assert g.meta["lineage"] in ("north", "south")
            for p in g.parents:                              # children only of their own line
                parent = s.genome(p)
                assert parent is None or parent.meta.get("lineage") in (g.meta["lineage"], None)
    north_best, south_best = s.morning_genomes()
    assert north_best.meta["lineage"] == "north" and south_best.meta["lineage"] == "south"
    assert north_best.id == rep2.lineage_best["north"] and south_best.id == rep2.lineage_best["south"]
    s.close()
    r = LiveRunner(db=db, llm=FakeLLM(up=False), pictures=False, speed=60, start=SPRING, evolve=False)
    try:
        assert [c.genome.id for c in r.w.civs] == [north_best.id, south_best.id]
        assert r._best_genome(r.w.civs[1]).id == south_best.id      # a reborn South keeps its own line
        for c in r.w.civs:
            c.genome = r.w.civs[0].genome if c is r.w.civs[1] else c.genome
        r._applied_night = 0                                        # the 07:00 handoff (it is 09:00 now)
        r.schedule()
        assert [c.genome.id for c in r.w.civs] == [north_best.id, south_best.id]
    finally:
        r.mind.close()


def test_a_population_from_before_the_lineages_is_split(tmp_path, tiny_bench):
    import random

    from ai.evolution import Evolution
    from ai.genome import Genome

    s = Store(tmp_path / "old.db")
    old = [Genome.random(R, random.Random(i), id=f"n1g{i}") for i in range(4)]
    s.save_population(1, old)
    evo = Evolution(s, seed=5, processes=1, population=4, days=2, peer_games=1, champion_games=1)
    evo.run_night()
    pop = s.population(1)
    assert sorted(g.meta["lineage"] for g in pop) == ["north", "north", "south", "south"]
    assert {"n1Nseed", "n1Sseed"} <= {g.id for g in pop}           # each line starts from its kingdom's mind


# ---------------------------------------------------------------------------- 5. the scoreboard
@pytest.fixture
def rival_runner(tmp_path):
    from server.runner import LiveRunner

    r = LiveRunner(db=tmp_path / "r.db", llm=FakeLLM(up=False), pictures=False, speed=60, start=SPRING,
                   evolve=False, llms={"north": FakeLLM(model="qwen3:14b"), "south": FakeLLM(model="gemma3:12b")})
    yield r
    r.mind.close()


def test_the_scoreboard_counts_a_won_war(rival_runner):
    from fastapi.testclient import TestClient

    from server.app import create_app

    r = rival_runner
    w = r.w
    north, south = w.civs
    w.start_t -= 30 * 86400
    north.castle.target_level = 2
    w._complete(north, north.castle)                         # castle 2 on day 1
    w.diplomacy.attacked(north, south)                       # North starts a war…
    w.military.fall(south, north)                            # …and destroys South's castle
    r.drain_rivalry()
    by_civ = r.store.rivalry("civ")
    assert by_civ["north"]["wars_started"] == 1 and by_civ["north"]["wars_won"] == 1
    assert by_civ["north"]["castles_destroyed"] == 1 and by_civ["north"]["best_castle"] == 2
    assert by_civ["south"]["wars_lost"] == 1 and by_civ["south"]["fallen"] == 1
    by_model = r.store.rivalry("model")
    assert by_model["qwen3:14b"]["wars_won"] == 1 and by_model["gemma3:12b"]["fallen"] == 1
    assert not w.diplomacy.at_war("north", "south")
    with TestClient(create_app(r, run_world=False)) as c:
        d = c.get("/api/rivalry").json()
    assert [k["model"] for k in d["kingdoms"]] == ["qwen3:14b", "gemma3:12b"]
    assert d["kingdoms"][0]["score"]["wars_won"] == 1
    assert {m["model"] for m in d["models"]} == {"qwen3:14b", "gemma3:12b"}
    assert any(e["kind"] == "war_end" for e in d["recent"])
    assert r.frames.frame()["civs"][0]["model"] == "qwen3:14b"     # the card shows the model next to the mind


def test_a_war_ended_by_peace_goes_to_the_side_that_lost_fewer_soldiers(rival_runner):
    r = rival_runner
    w = r.w
    north, south = w.civs
    w.diplomacy.start_war("north", "south", by="south")
    north.losses += 1
    south.losses += 4
    m = w.diplomat.send(south, north, {"type": "peace", "days": 7, "text": "Enough."})
    if m.status != "accepted":                               # North's model answers later; settle it now
        w.diplomacy.answer(m, True, "Agreed.")
    r.drain_rivalry()
    by_civ = r.store.rivalry("civ")
    assert by_civ["south"]["wars_started"] == 1
    assert by_civ["north"]["wars_won"] == 1 and by_civ["south"]["wars_lost"] == 1
