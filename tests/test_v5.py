"""Version 5: army, combat, war rules, rebirth, diplomacy, trade carts, spies, market and mint."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine import formulas as F
from engine.entities import Building
from engine.rules import default_rules
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 7, 0, tzinfo=TZ)


def world(**kw):
    kw.setdefault("start", SPRING)
    return World(R, tz=TZ, **kw)


def after_peace(w):
    w.start_t -= 30 * 86400                        # the first 14 days of peace are long gone


def give_building(w, civ, btype, level=1):
    """A finished building next to the castle (for tests)."""
    from engine.worldmap import find_site
    spec = R.buildings[btype]
    x, y = find_site(w.map, civ.castle.center, spec.size, radius=20, min_dist=3, flip=civ.mirror)
    b = Building(w.next_id(), civ.id, btype, x, y, spec.size, spec.walkable, w.clock.t)
    b.level = b.target_level = level
    b.hp = b.max_hp = R.building_hp(btype, level)
    w.map.place_building(b.id, x, y, spec.size, spec.walkable)
    civ.buildings.append(b)
    civ.invalidate_depots()
    return b


# ---------------------------------------------------------------------------- the doc's numbers
def test_damage_example_spearman_against_knight():
    assert F.damage(3, 3, 0, 2, 0) == 7                        # 3 × 3 − 2 = 7
    assert F.damage(4, 1, 0, 9, 0) == 1                        # never below 1


def test_spy_catch_examples():
    assert F.spy_caught(F.spy_catch_per_hour(0)) == pytest.approx(0.059, abs=0.001)
    assert F.spy_caught(F.spy_catch_per_hour(2)) == pytest.approx(0.265, abs=0.001)
    assert F.spy_catch_per_hour(50) == 0.30


def test_lanchester_counts_squared():
    one = F.army_strength([(40, 4)])
    two = F.army_strength([(40, 4), (40, 4)])
    assert two == pytest.approx(4 * one)


def test_market_price_moves_and_recovers():
    p = F.market_price_after_sale(1.0, 100)
    assert p == pytest.approx(0.97)
    assert F.market_recover(p, 1.0, 1) == pytest.approx(0.9797)
    assert F.market_recover(0.5, 1.0, 1000) == 1.0


# ---------------------------------------------------------------------------- training
def test_barracks_trains_soldiers_who_eat_and_count():
    w = world(ai=False)
    civ = w.civs[0]
    civ.castle.level = 4
    give_building(w, civ, "barracks")
    civ.stock.update(food=500, wood=500, coins=500)
    before = civ.food_per_day(w.clock.t)
    assert w.military.start_training(civ, "militia")
    w.run(2 * 3600)                                            # 1 h of training during work hours
    assert len(civ.soldiers) == 1
    assert civ.food_per_day(w.clock.t) == before + 48
    assert civ.requirement_met({"soldiers": 1}, w.clock.t)


def test_no_soldiers_without_the_building_or_castle():
    w = world(ai=False)
    civ = w.civs[0]
    civ.stock.update(food=500, wood=500, coins=500)
    assert not w.military.start_training(civ, "militia")       # castle 1, no barracks
    civ.castle.level = 4
    assert not w.military.start_training(civ, "militia")       # still no barracks


def test_soldiers_are_paid_and_desert_when_not():
    w = world(ai=False)
    civ = w.civs[0]
    for _ in range(10):
        w.military.spawn_unit(civ, "militia")
    civ.stock["coins"] = 5
    w.military.daily(civ)
    assert civ.stock["coins"] == 0 and len(civ.soldiers) == 9  # 10% left unpaid


# ---------------------------------------------------------------------------- war
def test_no_attack_in_the_first_14_days():
    w = world(ai=False)
    north, south = w.civs
    for _ in range(5):
        w.military.spawn_unit(north, "militia")
    assert w.military.order_attack(north, south) == 0


def test_an_army_destroys_a_weak_kingdom_and_takes_loot():
    w = world(ai=False)
    after_peace(w)
    north, south = w.civs
    for t in ["spearman"] * 6 + ["militia"] * 6:
        w.military.spawn_unit(north, t)
    w.military.spawn_unit(south, "militia")
    wood_before = north.stock["wood"]
    assert w.military.order_attack(north, south) > 0
    assert north.muster and w.diplomacy.rel("north", "south") > -20   # first the army gathers…
    w.run(3600)
    assert north.muster is None and w.diplomacy.rel("north", "south") <= -20   # …then it marches: −20
    for _ in range(12):
        w.run(3600)
        if not south.alive:
            break
    assert not south.alive and south.castle is None and not south.people
    assert north.stock["wood"] > wood_before                     # 30% of what was destroyed
    kinds = {e.kind for e in w.journal.events}
    assert {"attack", "battle", "loot", "fall"} <= kinds
    assert north.kills >= 1


def test_towers_shoot_attackers():
    w = world(ai=False)
    north, south = w.civs
    tower = give_building(w, south, "tower")
    u = w.military.spawn_unit(north, "militia")
    u.x, u.y = tower.center[0] + 2, tower.center[1]
    w.diplomacy.war.add(w.diplomacy.pair("north", "south"))
    w.military._round()
    assert u.hp < u.max_hp


def test_soldiers_do_little_against_walls_but_rams_break_them():
    w = world(ai=False)
    north, south = w.civs
    wall = give_building(w, south, "wall")
    m = w.military.spawn_unit(north, "militia")
    ram = w.military.spawn_unit(north, "ram")
    assert w.military.unit_damage(m, north, wall, south) < 1.0
    assert w.military.unit_damage(ram, north, wall, south) >= 36


def test_a_fallen_kingdom_is_reborn_after_7_days_in_the_live_world():
    w = world(ai=True)
    w.live = True
    north, south = w.civs
    w.military.fall(south, north)
    assert not w.civs[1].alive and w.civs[1].rebirth_at is not None
    w.run(7 * 86400 + 3600)
    reborn = w.civs[1]
    assert reborn.alive and reborn.generation == 2 and reborn.pop == 2 and reborn.castle is not None
    assert w.diplomacy.treaty("north", "south")                 # a newborn kingdom gets its own peace


def test_no_rebirth_in_the_nights_games():
    w = world(ai=False)
    north, south = w.civs
    w.military.fall(south, north)
    assert w.civs[1].rebirth_at is None


# ---------------------------------------------------------------------------- diplomacy and trade
def test_a_trade_goes_by_cart_and_both_keep_their_word():
    w = world(ai=True)
    north, south = w.civs
    north.stock.update(wood=500)
    south.stock.update(stone=500)
    w.trade.keeps_promise = lambda civ, deal: True
    m, _ = w.diplomacy.clean({"type": "trade", "give": {"wood": 200}, "ask": {"stone": 100}}, north, south)
    w.diplomacy.post(m)
    w.diplomacy.answer(m, True)
    assert w.trade.carts                                         # the cart is on the road
    assert north.stock["wood"] == 300
    for _ in range(48):
        w.run(1800)
        if m.status == "done":
            break
    assert m.status == "done"
    assert south.stock["wood"] >= 200 and north.stock["stone"] >= 100
    assert w.diplomacy.rel("north", "south") >= 5
    assert w.diplomacy.trust("north", "south") > 0.5 and w.diplomacy.trust("south", "north") > 0.5


def test_a_broken_promise_halves_trust():
    w = world(ai=True)
    north, south = w.civs
    north.stock.update(wood=500)
    w.trade.keeps_promise = lambda civ, deal: False
    m, _ = w.diplomacy.clean({"type": "trade", "give": {"wood": 200}, "ask": {"stone": 100}}, north, south)
    w.diplomacy.post(m)
    w.diplomacy.answer(m, True)
    assert m.status == "broken" and not w.trade.carts
    assert w.diplomacy.trust("south", "north") == pytest.approx(0.25)


def test_messages_are_checked():
    w = world(ai=True)
    north, south = w.civs
    m, why = w.diplomacy.clean({"type": "trade", "give": {"gold": 99999}, "ask": {"wood": 1}}, north, south)
    assert m is None and "doesn't have" in why
    m, why = w.diplomacy.clean({"type": "invade"}, north, south)
    assert m is None
    m, _ = w.diplomacy.clean({"type": "peace", "days": 400, "text": "x" * 999}, north, south)
    assert m.days == 30 and len(m.text) == 300


def test_breaking_a_treaty_costs_60():
    w = world(ai=False)
    after_peace(w)
    north, south = w.civs
    p = w.diplomacy.pair("north", "south")
    w.diplomacy.treaty_until[p] = w.clock.t + 5 * 86400
    for _ in range(5):
        w.military.spawn_unit(north, "militia")
    w.military.order_attack(north, south)
    w.run(3600)                                                     # the army gathers, then marches
    assert w.diplomacy.rel("north", "south") == pytest.approx(-80)   # −20 attack, −60 treaty
    assert w.diplomacy.trust("south", "north") == pytest.approx(0.25)


def test_peaceful_days_raise_relation_up_to_20():
    w = world(ai=False)
    for _ in range(30):
        w.diplomacy.daily()
    assert w.diplomacy.rel("north", "south") == 20


def test_gene_diplomats_talk_twice_a_day():
    w = world(ai=True)
    w.civs[0].stock.update(food=2900)
    w.run(2 * 86400)
    assert all(w.diplomacy.sent_today.get(c.id, 0) <= 3 for c in w.civs)


# ---------------------------------------------------------------------------- spies
def test_spy_mission_brings_a_report():
    w = world(ai=False)
    north, south = w.civs
    spy = w.military.spawn_unit(north, "spy")
    for _ in range(3):
        w.military.spawn_unit(south, "militia")
    assert w.military.send_spy(north, south)
    for _ in range(40):
        w.run(1800)
        if north.intel.get("south", {}).get("source") == "spy":
            break
    rep = north.intel["south"]
    assert rep["source"] == "spy" and rep["castle"] == south.castle_level
    assert 2 <= rep["soldiers"] <= 4 or spy not in north.units      # ±20% (or he was caught)


def test_a_turned_spy_brings_a_false_report():
    w = world(ai=False)
    north, south = w.civs
    for _ in range(10):
        w.military.spawn_unit(south, "militia")
    south.genome.genes["deception"] = 2.0
    rep = w.military.report(south, True, north)
    assert rep["false"] and rep["soldiers"] >= 16                   # ×2, ±20%


def test_without_a_spy_the_enemy_is_a_rumour():
    w = world(ai=False)
    north, south = w.civs
    for _ in range(10):
        w.military.spawn_unit(south, "militia")
    w.military.daily(north)
    rep = north.intel["south"]
    assert rep["source"] == "rumour" and 5 <= rep["soldiers"] <= 15


# ---------------------------------------------------------------------------- market and mint
def test_market_exchange():
    w = world(ai=False)
    civ = w.civs[0]
    give_building(w, civ, "market")
    civ.stock.update(wood=900, stone=0)
    got = w.trade.exchange(civ, "wood", "stone", 300)
    assert got == pytest.approx(300 * 1.0 / 1.2 * 0.9)
    assert civ.market_price["wood"] < 1.0


def test_mint_turns_gold_into_coins():
    w = world(ai=False)
    civ = w.civs[0]
    give_building(w, civ, "mint")
    civ.stock.update(gold=900, coins=0)
    w.run(86400)
    taxes = civ.pop * R.economy["tax_per_person_per_day"]
    assert 0 < civ.stock["coins"] - taxes <= 40 * 2.5 + 1        # at most 40 gold a day at level 1
    assert civ.stock["gold"] < 900


def test_taxes_count_soldiers():
    w = world(ai=False)
    civ = w.civs[0]
    for _ in range(4):
        w.military.spawn_unit(civ, "militia")
    civ.stock["coins"] = 0
    w._daily(civ)
    assert civ.stock["coins"] == pytest.approx((civ.pop + 4) * R.economy["tax_per_person_per_day"])


# ---------------------------------------------------------------------------- the AI side
def test_the_ai_builds_an_army_once_it_has_barracks():
    w = world()
    civ = w.civs[0]
    civ.castle.level = 4
    give_building(w, civ, "barracks")
    civ.stock.update(food=2000, wood=900, stone=900, coins=900)
    w.run(86400)
    assert civ.soldiers or civ.training


def test_council_may_order_war_only_with_an_army():
    from ai.council import check_plan
    w = world()
    civ = w.civs[0]
    plan, dropped = check_plan({"war": True, "priorities": {}}, civ)
    assert not plan.war and dropped
    for _ in range(5):
        w.military.spawn_unit(civ, "militia")
    plan, _ = check_plan({"war": True, "priorities": {}}, civ)
    assert plan.war


def test_battles_are_called_day_and_night():
    from ai.commentator import Commentator
    from ai.llm import FakeLLM
    from engine.entities import Event
    c = Commentator(R, FakeLLM(up=False))
    t = SPRING.timestamp()
    c.hear(Event(t, 1, "north", "battle_update", "The battle goes on.", 2))
    assert c.next_line(t, datetime(2027, 4, 5, 23, 30, tzinfo=TZ)) is not None


def test_frames_carry_the_army_and_diplomacy():
    from server.snapshot import FrameMaker
    w = world()
    fm = FrameMaker(w)
    w.military.spawn_unit(w.civs[0], "knight")
    f = fm.frame()
    assert f["units"] and f["units"][0][4] == "knight"
    assert f["diplomacy"]["state"] == "neutral" and f["diplomacy"]["peace_days_left"] > 13
    assert f["civs"][0]["army"] == 1
