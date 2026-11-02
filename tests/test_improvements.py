"""Improvements (docs/imbunatatiri.md §2–7): taxes, longer games and mid-game starts, a 16-game benchmark,
fairer rule verdicts, rules that see the army, a fair bandit camp, the picture workflow check."""
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai.evolution import Evolution, base_fitness, comparable_bench, play_match, stall_nights
from ai.genome import Genome
from engine.rules import default_rules
from engine.store import Store
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 7, 0, tzinfo=TZ)


# ---------------------------------------------------------------------------- §3 coins
def test_taxes_are_two_coins_a_day():
    assert R.economy["tax_per_person_per_day"] == 2
    w = World(R, tz=TZ, start=SPRING, ai=False)
    civ = w.civs[0]
    civ.stock["coins"] = 0
    w._daily(civ)
    assert civ.stock["coins"] == pytest.approx(2 * civ.pop)


# ---------------------------------------------------------------------------- §2 longer games, mid-game starts
def test_gathering_counts_per_day_of_the_match():
    side = {"castle": 4, "gathered": 60000, "pop": 30, "alive": True}
    short = base_fitness({**side, "days": 60}, R)
    long = base_fitness({**side, "days": 120}, R)
    assert short > long                                   # the same haul is worth less in a longer game
    assert R.evolution["match_days"] == 120


@pytest.fixture
def tiny_midgame(tmp_path, monkeypatch):
    cfg = {**R.evolution["midgame"], "scenarios": 2, "until_castle": 2, "max_days": 12, "rounds": 1, "days": 2}
    monkeypatch.setitem(R.evolution, "midgame", cfg)
    return tmp_path / "scen"


def test_a_scenario_is_made_once_and_played_from(tiny_midgame):
    from ai.scenarios import make_scenario, scenario_specs
    specs = scenario_specs(R, TZ, tiny_midgame)
    assert len(specs) == 2 and all(s.until_castle == 2 for s in specs)
    path = make_scenario(specs[0])
    from ai.evolution import MatchSpec
    g = Genome.default(R)
    m = MatchSpec(1, 0, "peer", "a", g.genes, "b", g.genes, 0, 0, SPRING.timestamp(), 2, scenario=path)
    res = play_match(m)
    a, b = res["sides"]
    assert a["midgame"] and a["castle"] >= 2
    assert a["gathered"] < 20000                         # only what was gathered in this match


def test_the_night_has_mid_game_rounds(tmp_path, tiny_midgame):
    evo = Evolution(Store(tmp_path / "e.db"), seed=5, processes=1, population=4, days=2, peer_games=2,
                    champion_games=1)
    evo.midgame = True
    evo.scenario_dir = tiny_midgame
    import random
    pop = [Genome.random(R, random.Random(i), id=f"g{i}") for i in range(4)]
    specs = evo.schedule(1, pop, [Genome.default(R)], random.Random(1))
    rounds = {s.round: s.scenario for s in specs}
    assert rounds[1] is not None and rounds[0] is None and rounds[2] is None   # the last peer round
    assert all(s.days == 2 for s in specs)


def test_tiny_test_nights_skip_mid_game_starts(tmp_path):
    evo = Evolution(Store(tmp_path / "e.db"), population=4, days=2)
    assert not evo.midgame and evo.midgame_rounds() == set()


# ---------------------------------------------------------------------------- §6 benchmark and rules
def test_benchmark_is_16_games():
    evo = Evolution(Store.__new__(Store), population=4, days=2)
    assert len(evo.benchmark_specs(1, [Genome.default(R)])) == 16


def test_streaks_only_compare_the_same_benchmark():
    old = [{"bench_fitness": v, "config": json.dumps({"days": 60})} for v in (0.40, 0.41, 0.42)]
    new = [{"bench_fitness": v, "config": json.dumps({"bench": "4x4-120d-seed777"})} for v in (0.30, 0.31)]
    assert comparable_bench(old + new) == [0.30, 0.31]
    assert stall_nights(old + new) == 0


def test_rules_see_the_army():
    from ai.rulebook import rule_state
    w = World(R, tz=TZ, start=SPRING)
    north = w.civs[0]
    for _ in range(4):
        w.military.spawn_unit(north, "militia")
    s = rule_state(w, north)
    assert s["soldiers"] == 4 and s["my_power"] > 0
    assert s["trust"] == 0.5 and s["relation"] == 0


# ---------------------------------------------------------------------------- §7 a fair bandit camp
def test_the_bandit_camp_is_an_equal_walk_from_both_castles():
    w = World(R, tz=TZ, start=SPRING, ai=False, seed=3)
    c, m = w.bandits, w.map
    lengths = []
    for civ in w.civs:
        goals = m.access_tiles(civ.castle.x, civ.castle.y, civ.castle.size, False)
        path = w.paths.find((int(c.x), int(c.y)), ("t", civ.id), goals)
        lengths.append(sum(math.dist(a, b) for a, b in zip(((int(c.x), int(c.y)),) + tuple(path[:-1]), path)))
    assert abs(lengths[0] - lengths[1]) <= 3


# ---------------------------------------------------------------------------- §4 the picture workflow check
def test_picture_workflow_check_finds_missing_models():
    from ai.images import ComfyPainter, check_workflow

    class Fake(ComfyPainter):
        def _get(self, path, timeout=10):
            info = {
                "UNETLoader": {"input": {"required": {"unet_name": [["other.safetensors"]], "weight_dtype": [["default"]]}}},
                "CLIPLoader": {"input": {"required": {"clip_name": [[R.images["models"]["clip"]]], "type": [["flux3"]]}}},
                "VAELoader": {"input": {"required": {"vae_name": [[R.images["models"]["vae"]]]}}},
                "CLIPTextEncode": {"input": {"required": {"clip": ["CLIP"], "text": ["STRING"]}}},
                "ConditioningZeroOut": {"input": {"required": {"conditioning": ["CONDITIONING"]}}},
                "EmptyFlux2LatentImage": {"input": {"required": {"width": ["INT"], "height": ["INT"], "batch_size": ["INT"]}}},
                "KSampler": {"input": {"required": {"model": ["MODEL"], "seed": ["INT"]}}},
                "VAEDecode": {"input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}}},
            }
            return json.dumps(info).encode()

    problems = check_workflow(Fake(R.images))
    assert any("unet_name" in p for p in problems)            # the model file isn't there
    assert any("SaveImage" in p for p in problems)            # a node ComfyUI doesn't have
