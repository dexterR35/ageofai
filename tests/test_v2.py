"""Version 2: happiness, families, events, Elo, the store and one tiny night of evolution."""
import copy
import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai.evolution import Evolution, base_fitness, match_score
from ai.genome import Genome
from engine import formulas as F
from engine.entities import State
from engine.rules import Rules, default_rules
from engine.store import Store
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 7, 0, tzinfo=TZ)


def tweak(**paths) -> Rules:
    """A copy of the rules with some values changed, e.g. tweak(**{"events.fire.p0": 1.0})."""
    raw = copy.deepcopy(R.raw)
    for path, value in paths.items():
        node = raw
        *head, last = path.split(".")
        for k in head:
            node = node[k]
        node[last] = value
    return Rules(raw)


def world(rules=R, **kw) -> World:
    kw.setdefault("start", SPRING)
    return World(rules, tz=TZ, **kw)


def house_for(w: World, civ, x: int, y: int):
    civ.stock.update(wood=1000, stone=1000)
    b = w.start_building(civ, "house", x, y)
    b.level = b.target_level = 1
    b.hp = b.max_hp = R.building_hp("house", 1)
    civ.invalidate_depots()
    return b


# ---------------------------------------------------------------------------- formulas from the doc

def test_happiness_target_examples():
    assert F.happiness_target(ate=True, bed=True, friends=2) == 78
    assert F.happiness_target(ate=False, bed=False, friends=0) == 0
    assert F.happiness_target(ate=True, bed=True, friends=9) == 82        # friends count up to 3
    assert F.happiness_target(True, True, 0, far_work=True, cold=True) == 50


def test_happiness_moves_two_thirds_in_ten_days():
    h = 0.0
    for _ in range(10):
        h = F.happiness_step(h, 100)
    assert 60 <= h <= 66                                     # "cam 63% din drum în 10 zile"
    assert F.happiness_step(50, 50, shock=-80) == 0          # clamped


def test_village_morale_from_happiness():
    assert F.morale_from_happiness(0) == 0.5
    assert F.morale_from_happiness(71.4) == pytest.approx(1.0, abs=0.001)
    assert F.morale_from_happiness(100) == pytest.approx(1.2)


def test_elo_example():
    a, b = F.elo_update(1000, 1200, 1.0)
    assert round(a) == 1024 and round(b) == 1176
    assert F.elo_expected(1000, 1200) == pytest.approx(0.24, abs=0.005)


def test_diversity():
    ranges = {"x": (0.0, 1.0), "y": (0.0, 2.0)}
    assert F.diversity([{"x": 0.5, "y": 1}] * 4, ranges) == 0
    assert F.diversity([{"x": 0, "y": 0}, {"x": 1, "y": 2}], ranges) == pytest.approx(1.0)


def test_match_score_draw_margin():
    assert match_score(0.30, 0.20, 0.02) == 1.0
    assert match_score(0.300, 0.297, 0.02) == 0.5
    assert match_score(0.2, 0.3, 0.02) == 0.0


def test_fitness_weights():
    side = {"castle": 10, "gathered": 1e9, "pop": R.people["pop_cap"], "alive": True}
    assert base_fitness(side, R) == pytest.approx(0.9)      # + 0.10 for wins = 1.0
    assert base_fitness({"castle": 0, "gathered": 0, "pop": 0, "alive": False}, R) == 0


# ---------------------------------------------------------------------------- society

def test_couple_in_a_house_becomes_a_family_and_has_a_child():
    rules = tweak(**{"families.birth_chance_per_day": 1.0, "events.fire.p0": 0.0})
    w = world(rules, ai=False)
    civ = w.civs[0]
    house = house_for(w, civ, 30, 30)
    for p in civ.people:
        p.home = house
    civ.stock["food"] = 2000
    w.society.form_families(civ)
    a, b = civ.people
    assert a.spouse == b.id and b.spouse == a.id
    assert a.last == b.last == house.family_name
    w.society.births(civ)
    child = civ.people[-1]
    assert civ.pop == 3 and child.parents == (a.id, b.id)
    assert child.last == house.family_name
    assert child.id in a.children
    assert civ.stage(child, w.clock.t)["name"] == "child"
    assert child not in civ.workers(w.clock.t)


def test_no_children_when_hungry():
    rules = tweak(**{"families.birth_chance_per_day": 1.0})
    w = world(rules, ai=False)
    civ = w.civs[0]
    house = house_for(w, civ, 30, 30)
    for p in civ.people:
        p.home = house
    w.society.form_families(civ)
    civ.stock["food"] = 10
    w.society.births(civ)
    assert civ.pop == 2


def test_unhappy_people_strike_then_flee_to_the_other_civ():
    rules = tweak(**{"flight.chance_per_day": 1.0})
    w = world(rules, ai=False)
    nord, sud = w.civs
    p = nord.people[0]
    p.happiness = 10
    for _ in range(3):
        w.society.strikes_and_flight(nord)
    assert p.away(w.clock.t) and p.away_reason == "on strike"
    for _ in range(2):
        w.society.strikes_and_flight(nord)
    assert p not in nord.people and p in sud.people
    assert p.civ_id == "south" and nord.fled == 1


def test_friends_who_work_together():
    w = world(ai=False)
    civ = w.civs[0]
    a, b = civ.people
    tree = w.map.by_kind["tree"][0]
    for p in (a, b):
        p.job, p.target = "wood", tree
    for _ in range(50):
        w.society.friendships(civ)
    assert a.friends[b.id] == 50 and a.friend_count(50) == 1
    a.job = "stone"
    w.society.friendships(civ)
    assert a.friends[b.id] == 49.5


def test_death_saddens_family():
    w = world(ai=False)
    civ = w.civs[0]
    a, b = civ.people
    a.spouse, b.spouse = b.id, a.id
    w.remove_person(civ, a, "test", "death")
    assert b.shock == -15 and b.spouse is None


def test_evening_choices_follow_traits():
    w = world(ai=False)
    civ = w.civs[0]
    hard, lazy = civ.people
    hard.trait, lazy.trait = "hardworking", "lazy"
    hard.job = lazy.job = "wood"
    counts = {hard.id: {"visit": 0, "rest": 0, "overtime": 0}, lazy.id: {"visit": 0, "rest": 0, "overtime": 0}}
    for _ in range(400):
        w.society.evening(civ)
        for p in (hard, lazy):
            counts[p.id][p.evening] += 1
    assert counts[hard.id]["overtime"] > 4 * counts[lazy.id]["overtime"]
    assert counts[lazy.id]["rest"] > counts[hard.id]["rest"]


def test_version_1_mode_still_works():
    rules = R.with_features(happiness=False, families=False, events=False)
    w = world(rules)
    w.run(5 * 86400)
    assert all(c.morale >= 1.0 for c in w.civs)


# ---------------------------------------------------------------------------- events

def test_fire_is_fought_and_put_out():
    rules = tweak(**{"events.fire.p0": 0.0})
    w = world(rules)
    w.run(3 * 86400)
    civ = w.civs[0]
    b = next(b for b in civ.buildings if b.built and b.type != "castle")
    b.on_fire = True
    civ.dirty = True
    w.run(4 * 3600)
    assert not b.on_fire and b in civ.buildings
    assert 0.5 * b.max_hp < b.hp < b.max_hp


def test_unfought_fire_burns_the_building_down():
    w = world(ai=False)
    civ = w.civs[0]
    b = house_for(w, civ, 30, 30)
    b.on_fire = True
    w.run(21 * 3600)                       # 5%/hour → gone after 20 hours
    assert b not in civ.buildings
    assert w.journal.of("destroyed")


def test_disease_sends_people_home():
    rules = tweak(**{"events.disease.p0": 1.0, "events.disease.death_share": 0.0})
    w = world(rules, ai=False)
    civ = w.civs[0]
    w.events.disease(civ, w.events.rng(civ))
    sick = [p for p in civ.people if p.away(w.clock.t)]
    assert len(sick) == 1 and sick[0].away_reason == "sick"


def test_storm_keeps_everyone_inside():
    w = world()
    civ = w.civs[0]
    civ.storm_from, civ.storm_until = w.clock.t, w.clock.t + 6 * 3600
    w.run(2 * 3600)
    assert all(p.state == State.REST for p in civ.people)


def test_same_event_seed_same_luck():
    rules = tweak(**{"events.fire.p0": 0.05, "events.storm.p0": 0.2})
    kinds = []
    for _ in range(2):
        w = world(rules, seed=3, event_seed=99)
        w.run(20 * 86400)
        kinds.append([(e.day, e.civ_id, e.kind) for e in w.journal.events if e.kind in ("fire", "storm")])
    assert kinds[0] == kinds[1] and kinds[0]


def test_wells_cut_fire_risk_in_their_radius():
    w = world(ai=False)
    civ = w.civs[0]
    b = house_for(w, civ, 30, 30)
    civ.stock["stone"] = 100
    well = w.start_building(civ, "well", 34, 30)
    well.level = well.target_level = 1
    assert w.events._wells_near(civ, *b.center, R.events["fire"]["well_radius"])


# ---------------------------------------------------------------------------- store and evolution

def test_store_roundtrip(tmp_path):
    s = Store(tmp_path / "t.db")
    g = Genome.random(R, random.Random(1), id="x1")
    g.meta.update(elo=1111, fitness=0.3, games=8)
    s.save_population(1, [g])
    back = s.population(1)[0]
    assert back.id == "x1" and back.genes == pytest.approx(g.genes) and back.meta["elo"] == 1111
    s.hall_add(back, 1)
    assert [h.id for h in s.hall()] == ["x1"]


def ga_rules() -> Rules:
    """The old genetic algorithm (crossover + mutation, top genomes kept); CMA-ES has its own tests."""
    raw = copy.deepcopy(default_rules().raw)
    raw["evolution"]["method"] = "ga"
    return Rules(raw)


def test_one_tiny_night(tmp_path):
    s = Store(tmp_path / "evo.db")
    evo = Evolution(s, ga_rules(), seed=5, processes=1, population=4, days=2, peer_games=1, champion_games=1)
    rep = evo.run_night()
    assert rep.night == 1 and rep.games == 4 * 1 // 2 + 4
    assert len(s.population(2)) == 4                        # next night's genomes are ready
    assert {g.id for g in s.population(2)} >= set(sorted(rep.fitness, key=rep.fitness.get)[-4:])
    hall = {g.id for g in s.hall()}
    assert "default" in hall and rep.best_id in hall
    assert s.morning_genomes() is not None
    rows = s.db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    assert rows == rep.games
    rep2 = evo.run_night()
    assert rep2.night == 2


def test_benchmark_never_forgets_its_best(tmp_path, monkeypatch):
    s = Store(tmp_path / "keep.db")
    evo = Evolution(s, seed=5, processes=1, population=4, days=2, peer_games=1, champion_games=1)
    small = {**evo.rules.evolution["benchmark"], "candidates": 1, "games_per_season": 1}
    monkeypatch.setitem(evo.rules.evolution, "benchmark", small)     # the rules object is shared
    rep1 = evo.run_night()
    assert rep1.bench_id in {g.id for g in s.population(2)}     # kept to breed from
    rep2 = evo.run_night()
    assert rep2.bench_fitness >= rep1.bench_fitness - 1e-9        # last night's best was measured again
    assert evo.benchmark_keeper().id in {rep1.bench_id, rep2.bench_id}


def test_identical_genomes_get_identical_luck():
    """Both sides draw the same people and the same luck, so the better strategy wins, not the luckier."""
    from ai.evolution import MatchSpec, play_match
    g = Genome.default(R)
    res = play_match(MatchSpec(1, 0, "control", "a", g.genes, "b", g.genes, 101, 201, SPRING.timestamp(), 20))
    a, b = res["sides"]
    assert [p for p in (a["pop"], a["castle"])] == [p for p in (b["pop"], b["castle"])]
    fa, fb = base_fitness(a, R), base_fitness(b, R)
    assert abs(fa - fb) / max(fa, fb) < 0.02


def test_mirrored_civ_builds_mirrored():
    w = world()
    w.run(2 * 86400)
    n = w.map.size
    north, south = w.civs
    spots_n = sorted((b.type, b.x, b.y) for b in north.buildings)
    spots_s = sorted((b.type, n - b.x - b.size, n - b.y - b.size) for b in south.buildings)
    assert spots_n == spots_s


def test_next_generation_keeps_elites_and_size(tmp_path):
    evo = Evolution(Store(tmp_path / "g.db"), ga_rules(), population=8, processes=1)
    rng = random.Random(0)
    pop = [Genome.random(R, rng, id=f"g{i}") for i in range(8)]
    fit = {g.id: i / 10 for i, g in enumerate(pop)}
    nxt = evo.next_generation(1, pop, fit, rng)
    assert len(nxt) == 8
    assert [g.id for g in nxt[:4]] == ["g7", "g6", "g5", "g4"]
    for g in nxt[4:]:
        assert g.id.startswith("n2g") and len(g.parents) == 2
        for k, (lo, hi) in R.gene_ranges.items():
            assert lo <= g[k] <= hi
