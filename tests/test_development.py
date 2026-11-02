"""Civilization mode must preserve the classic world and account for real costs."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine import development, persist
from engine.entities import Building
from engine.rules import Rules, default_rules
from engine.world import World
from engine.worldmap import find_site


@pytest.fixture
def world():
    return World(Rules.profile("civilization"), seed=19,
                 start=datetime(2027, 4, 5, 9, tzinfo=ZoneInfo("Europe/Bucharest")), ai=False)


def building(w, civ, kind):
    spec = w.rules.buildings[kind]
    x, y = find_site(w.map, civ.castle.center, spec.size, radius=30, min_dist=4)
    b = Building(w.next_id(), civ.id, kind, x, y, spec.size, spec.walkable, w.clock.t)
    b.level = b.target_level = 1
    b.hp = b.max_hp = w.rules.building_hp(kind, 1)
    w.map.place_building(b.id, x, y, spec.size, spec.walkable)
    civ.buildings.append(b)
    return b


def prepare_research(w):
    civ = w.civs[0]
    building(w, civ, "school")
    for _ in range(4):
        w.spawn_person(civ, 24, "f", announce=False)
    civ.stock.update(coins=1000, wood=1000, food=2000)
    return civ


def test_profile_keeps_classic_rules_and_has_four_accessible_capitals(world):
    assert default_rules().map["size"] == 128
    assert len(default_rules().civs) == 2
    assert world.map.size == 256
    assert len(world.civs) == 4
    assert world.rules.raw["simulation"]["speed"] == 12
    castle = world.civs[0].castle
    origin = world.map.access_tiles(castle.x, castle.y, 3, False)
    for civ in world.civs[1:]:
        goals = world.map.access_tiles(civ.castle.x, civ.castle.y, 3, False)
        assert world.paths.find(origin[0], ("capital", civ.id), goals)
    counts = [0, 0, 0, 0]
    for node in world.map.by_kind["gold"]:
        index = (0 if node.x < 128 else 1) if node.y < 128 else (3 if node.x < 128 else 2)
        counts[index] += 1
    assert counts[0] > 0 and len(set(counts)) == 1


def test_experiment_pays_once_and_rejects_unknown_effects(world):
    civ = prepare_research(world)
    before = dict(civ.stock)
    assert not development.propose(world, civ, {"name": "Free resources", "field": "execute_python"})[0]
    assert civ.stock == before
    ok, _ = development.propose(world, civ, {"name": "Ore sorting", "field": "ore"})
    assert ok and civ.stock["coins"] == before["coins"] - 30
    assert not development.propose(world, civ, {"name": "Second", "field": "gold"})[0]
    assert civ.stock["coins"] == before["coins"] - 30


@pytest.mark.parametrize("passes", [True, False])
def test_trial_results_apply_effects_only_after_validation(world, monkeypatch, passes):
    civ = prepare_research(world)
    development.propose(world, civ, {"name": "Ore sorting", "field": "ore"})
    monkeypatch.setattr(civ.rng, "gauss", lambda mean, sigma: 0.08 if passes else -0.05)
    development.tick(world, civ, 24 * 3600, True)
    assert civ.development.project is None
    assert civ.development.experiments[-1]["passed"] is passes
    assert bool(civ.development.discoveries) is passes
    assert (0 < development.bonus(civ, "ore") <= 0.08) if passes else development.bonus(civ, "ore") == 0


def test_factories_consume_inputs_and_material_payments_are_atomic(world):
    civ = world.civs[0]
    building(world, civ, "foundry")
    civ.stock.update(ore=20, wood=20, coins=100, food=2000)
    development.tick(world, civ, 3600, True)
    assert 1 <= civ.development.materials["steel"] <= 1.2
    assert civ.stock["ore"] == 18 and civ.stock["wood"] == 18
    before = dict(civ.stock)
    with pytest.raises(ValueError):
        civ.pay({"wood": 2, "steel": 5})
    assert civ.stock == before
    civ.pay({"steel": 1})
    assert civ.development.materials["steel"] < 0.2


def test_officeholders_are_real_people_and_departed_people_are_replaced(world):
    civ = prepare_research(world)
    development.review_offices(world, civ)
    ids = [record["person"] for record in civ.development.offices.values()]
    assert len(ids) == 3 and len(set(ids)) == 3
    old = civ.development.offices["economy"]["person"]
    civ.people[:] = [p for p in civ.people if p.id != old]
    development.review_offices(world, civ)
    assert civ.development.offices["economy"]["person"] != old
    assert len({r["person"] for r in civ.development.offices.values()}) == 3


def test_new_adult_fills_vacant_office_without_waiting_a_week(world):
    civ = world.civs[0]
    development.review_offices(world, civ)
    assert len(civ.development.offices) == 2
    assert civ.development.next_review > world.clock.t
    newcomer = world.spawn_person(civ, 24, "f", announce=False)
    development.review_offices(world, civ)
    assert len(civ.development.offices) == 3
    assert newcomer.id in {r["person"] for r in civ.development.offices.values()}


@pytest.mark.parametrize("room", [0, 0.25])
def test_manufacturing_only_pays_for_output_that_fits(world, room):
    civ = world.civs[0]
    building(world, civ, "foundry")
    civ.stock.update(ore=20, wood=20, coins=100, food=2000)
    development.review_offices(world, civ)
    efficiency = 1 + development.office_skill(civ, "economy")
    civ.development.materials["steel"] = civ.cap("steel") - room
    before = dict(civ.stock)
    development.tick(world, civ, 3600, True)
    assert civ.development.materials["steel"] == pytest.approx(civ.cap("steel"))
    assert civ.stock["ore"] == pytest.approx(before["ore"] - 2 * room / efficiency)
    assert civ.stock["wood"] == pytest.approx(before["wood"] - 2 * room / efficiency)
    assert civ.stock["coins"] == pytest.approx(before["coins"] - 0.5 * room / efficiency)
    assert civ.development.spending["industry"] == pytest.approx(0.5 * room / efficiency)


def test_advancement_and_wonder_do_not_reset_the_city(world):
    civ = prepare_research(world)
    civ.castle.level = 4
    while civ.pop < 10:
        world.spawn_person(civ, 24, "m", announce=False)
    civ.development.education = 8
    civ.development.discoveries = [{"name": "A", "field": "ore", "gain": 0.1}, {"name": "B", "field": "food", "gain": 0.1}]
    civ.development.next_experiment = world.clock.t + 86400
    development.daily(world, civ)
    assert civ.development.era == "civic"
    world.live = True
    world._wonder_done(civ)
    assert world.new_era_at is None
    assert world.civs[0] is civ and civ.count("school") == 1


def test_save_roundtrip_keeps_institutions_and_research(world, tmp_path):
    civ = prepare_research(world)
    development.review_offices(world, civ)
    development.propose(world, civ, {"name": "Ore sorting", "field": "ore"})
    civ.development.materials["steel"] = 12
    persist.save_world(world, tmp_path)
    loaded, _ = persist.load_latest(tmp_path)
    assert loaded.civs[0].development.project["name"] == "Ore sorting"
    assert loaded.civs[0].development.materials["steel"] == 12
    assert loaded.civs[0].development.offices == civ.development.offices
    assert len(loaded.civs) == 4


def test_leader_appointments_validate_the_whole_slate(world):
    civ = prepare_research(world)
    development.review_offices(world, civ)
    original = dict(civ.development.offices)
    ids = [p.id for p in civ.people]
    assert not development.appoint(world, civ, {"economy": ids[0], "research": ids[0]})[0]
    assert civ.development.offices == original
    assert not development.appoint(world, civ, {"government": 999999})[0]
    assert development.appoint(world, civ, dict(zip(development.OFFICES, ids[-3:])))[0]
    assert {r["person"] for r in civ.development.offices.values()} == set(ids[-3:])


def test_a_capable_officeholder_can_remain_after_a_term(world):
    civ = prepare_research(world)
    development.review_offices(world, civ)
    office = civ.development.offices["economy"]
    person = next(p for p in civ.people if p.id == office["person"])
    person.job_seconds["ore"] = 10_000_000
    office["since"] -= 29 * 86400
    civ.development.next_review = 0
    development.review_offices(world, civ)
    assert civ.development.offices["economy"]["person"] == person.id


def test_unfunded_school_pauses_experiment_and_does_not_grant_service_bonus(world):
    civ = prepare_research(world)
    assert development.propose(world, civ, {"name": "Study", "field": "construction"})[0]
    civ.stock["coins"] = 0
    development.tick(world, civ, 3600, True)
    assert civ.development.project["progress"] == 0
    assert development.bonus(civ, "education") == 0


def test_culture_needs_a_place_and_is_kept_as_text(world):
    civ = world.civs[0]
    work = {"tradition": "Keepers of the River", "verse": "Water carries what the seasons leave."}
    assert not development.record_culture(world, civ, work)
    building(world, civ, "tavern")
    assert development.record_culture(world, civ, work)
    assert civ.development.culture["verse"] == work["verse"]
    assert not development.record_culture(world, civ, {"execute": "anything"})


def test_advanced_buildings_require_era_population_and_prerequisites(world):
    civ = world.civs[0]
    assert not development.unlocked(civ, "skyscraper")
    civ.development.era = "modern"
    while civ.pop < 60:
        world.spawn_person(civ, 24, "f", announce=False)
    assert not development.unlocked(civ, "skyscraper")
    building(world, civ, "factory")
    assert development.unlocked(civ, "skyscraper")
    assert "steel" in world.rules.building_cost("skyscraper", 2)


def test_accelerated_world_resumes_its_own_clock_and_save(tmp_path):
    from ai.llm import FakeLLM
    from server.runner import LiveRunner

    options = dict(profile="civilization", db=tmp_path / "civilization.db", save_dir=tmp_path / "save",
                   llm=FakeLLM(up=False), commentator=False, pictures=False, frames=False, evolve=False)
    first = LiveRunner(**options)
    try:
        assert first.speed == 12 and first.persistent and not first.realtime
        first.w.step(10)
        saved_t = first.w.clock.t
        persist.save_world(first.w, first.save_dir)
    finally:
        first.mind.close()
        first.store.close()
    second = LiveRunner(**options)
    try:
        assert second.recovered and second.w.clock.t == saved_t
        assert second.target_t() >= saved_t
        assert second.target_t() < saved_t + 60
        assert len(second.w.civs) == 4
    finally:
        second.mind.close()
        second.store.close()
