"""Every worked example from the design document, checked against the code."""
import math

import pytest

from engine import formulas as F
from engine.rules import default_rules

R = default_rules()


def test_collection_rate_examples():
    # Lumberjack, base tools, depot 8 tiles away: R = 10 / (1 + 0.033) = 9.7 wood/hour
    assert F.collection_rate(10, 10, 1.0, 8, 480) == pytest.approx(9.68, abs=0.01)
    # 60 tiles away: 8 wood/hour
    assert F.collection_rate(10, 10, 1.0, 60, 480) == pytest.approx(8.0, abs=0.01)


def test_farmer_feeds_about_two_and_a_half_people():
    # "Un fermier cu unelte de bază face Y ≈ 125 hrană/zi"
    y = F.collection_rate(10, 12, 1.0, 8, 480) * 11
    assert 120 <= y <= 132
    assert y / 48 == pytest.approx(2.6, abs=0.15)


def test_house_level_table():
    expected = {1: (60, 20, 3.0), 2: (96, 32, 4.5), 3: (154, 51, 6.75), 4: (246, 82, 10.125), 5: (393, 131, 15.1875)}
    for lvl, (wood, stone, hours) in expected.items():
        assert R.building_cost("house", lvl) == {"wood": wood, "stone": stone}
        assert R.building_hours("house", lvl) == pytest.approx(hours)


def test_construction_time_example():
    # Barracks 12 h, 4 builders with bronze tools (T=2): 12 / (3.03 × 1.4) = 2.8 h
    assert F.build_hours(12, 4, R.tool_mult(2)) == pytest.approx(2.83, abs=0.01)
    assert F.builder_power(4) == pytest.approx(3.03, abs=0.01)


def test_castle_cost_table():
    table = {
        2: (510, 340, 170, 0, 0), 3: (867, 578, 289, 100, 0), 4: (1474, 983, 491, 170, 0),
        5: (2506, 1670, 835, 289, 0), 6: (4260, 2840, 1420, 491, 80), 7: (7241, 4828, 2414, 835, 136),
        8: (12310, 8207, 4103, 1420, 231), 9: (20927, 13952, 6976, 2414, 393),
    }
    assert R.castle_cost(1) == {"wood": 300, "stone": 200, "coins": 100}
    for lvl, (wood, stone, coins, ore, gold) in table.items():
        c = R.castle_cost(lvl)
        assert (c["wood"], c["stone"], c["coins"], c.get("ore", 0), c.get("gold", 0)) == (wood, stone, coins, ore, gold)


def test_castle_time_and_total():
    assert R.castle_hours(1) == pytest.approx(24)
    total_days = sum(R.castle_hours(level) for level in range(1, 10)) / 24
    assert total_days == pytest.approx(49, abs=1)     # "cel puțin 49 de zile reale"


def test_tool_table():
    assert R.tool_cost(1) == {"wood": 40, "ore": 30, "coins": 20}
    assert R.tool_cost(2) == {"wood": 72, "ore": 54, "coins": 36}
    assert R.tool_cost(3) == {"wood": 130, "ore": 97, "coins": 65}
    assert R.tool_cost(4) == {"wood": 233, "ore": 175, "coins": 117}
    assert [round(R.tool_hours(t), 1) for t in (1, 2, 3, 4)] == [4.0, 6.4, 10.2, 16.4]
    total = [sum(R.tool_cost(t)[r] for t in (1, 2, 3, 4)) * 6 for r in ("wood", "ore", "coins")]
    assert total == [2850, 2136, 1428]


def test_worker_limits_and_beds():
    # the army plan: up to 100 workers at castle 10, 300 people per castle
    assert [R.workers_max(level) for level in (1, 2, 3, 5, 9, 10)] == [4, 6, 8, 22, 76, 100]
    assert F.beds([]) == 4
    assert F.beds([1]) == 8
    assert F.beds([5]) == 12
    assert F.beds([5] * 100) == 200                          # the formula's own default cap
    assert F.beds([5] * 100, cap=R.people["pop_cap"]) == 300


def test_death_chance_examples():
    assert F.death_chance_per_year(54) == 0
    for age, q in [(55, 0.02), (65, 0.054), (75, 0.148), (85, 0.40)]:
        assert F.death_chance_per_year(age) == pytest.approx(q, abs=0.002)


def test_winter_reserve_examples():
    # 10 people, 4 houses: 4 farmers → 25,920 food; 8 farmers → 8,640; wood 1,800
    assert F.winter_food_reserve(10, 4) == 25920
    assert F.winter_food_reserve(10, 8) == 8640
    assert F.winter_wood_reserve(4) == 1800
    granary_levels = math.ceil((8640 - 500) / 2000)
    assert granary_levels == 5
    # A winter farmer makes at most 12 × 0.5 × 8 = 48 food/day
    w = R.seasons["winter"]
    assert R.jobs["farm"].rate * w.farm * w.hours == 48


def test_full_multiplier_example():
    # Steel tools, morale 1.1, lumber camp level 5, max XP: 1.8 × 1.1 × 1.5 × 1.15 = 3.4
    m = R.tool_mult(4) * 1.1 * (1 + 0.1 * 5) * 1.15
    assert m == pytest.approx(3.4, abs=0.02)


def test_season_hours():
    assert {name: s.hours for name, s in R.seasons.items()} == {"spring": 11, "summer": 12, "autumn": 10, "winter": 8}


def test_utility_prefers_fast_actions():
    genes = {"castle": 1.0}
    assert F.utility({"castle": 1}, genes, 0, 0) == 1
    assert F.utility({"castle": 1}, genes, 24, 0) == 0.5


def test_allocate_proportional_sums_to_total():
    out = F.allocate_proportional(7, {"wood": 1.0, "stone": 1.0, "food": 1.0})
    assert sum(out.values()) == 7
    assert max(out.values()) - min(out.values()) <= 1
    assert F.allocate_proportional(3, {"wood": 0.0, "stone": 2.0}) == {"wood": 0, "stone": 3}


def test_coins_formula():
    assert F.coins_per_day(10) == 10
    assert F.coins_per_day(10, gold_melted=40, mint_level=1) == 10 + 100
    assert F.coins_per_day(0, gold_melted=40, mint_level=3) == pytest.approx(2.5 * 40 * 1.5)
