"""The army plan (docs/imbunatatiri.md, section 1): 300 per castle, more workers, 5 upgrade levels,
promotions, veterans, and the battle logic (counters, focus fire, gathering, formation, wounded,
early defense, counter-attack, siege)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine.entities import Building
from engine.rules import default_rules
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 7, 0, tzinfo=TZ)


def world(**kw):
    kw.setdefault("start", SPRING)
    return World(R, tz=TZ, **kw)


def give_building(w, civ, btype, level=1):
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


# ---------------------------------------------------------------------------- limits
def test_300_per_castle_villagers_and_soldiers_together():
    w = world(ai=False)
    civ = w.civs[0]
    assert R.people["pop_cap"] == 300
    assert civ.soldier_room(w.clock.t) == 300 - civ.pop
    for _ in range(10):
        w.military.spawn_unit(civ, "militia")
    assert civ.soldier_room(w.clock.t) == 300 - civ.pop - 10


def test_more_workers_at_high_castle_levels():
    assert R.workers_max(1) == 4 and R.workers_max(10) == 100
    levels = [R.workers_max(L) for L in range(1, 11)]
    assert levels == sorted(levels)
    for L in range(2, 11):                               # every castle requirement can still be met
        need = [req["workers"] for req in R.castle_requirements.get(L, []) if "workers" in req]
        assert all(n <= R.workers_max(L - 1) for n in need)


def test_upgrades_go_to_level_5():
    assert R.military_upgrades["max_level"] == 5
    assert R.research_cost("armor", 5)["wood"] > R.research_cost("armor", 3)["wood"]


# ---------------------------------------------------------------------------- veterans and promotions
def test_veterans_hit_harder():
    w = world(ai=False)
    north, south = w.civs
    u = w.military.spawn_unit(north, "militia")
    target = w.military.spawn_unit(south, "militia")
    north.rng.seed(1)
    base = w.military.unit_damage(u, north, target, south)
    u.kills = 15
    north.rng.seed(1)
    assert w.military.unit_damage(u, north, target, south) == pytest.approx(base * 1.2)


def test_a_militiaman_is_promoted_to_swordsman_and_keeps_his_kills():
    w = world(ai=False)
    civ = w.civs[0]
    civ.castle.level = 7
    give_building(w, civ, "barracks")
    civ.stock.update(food=900, ore=500, gold=500, coins=500)
    u = w.military.spawn_unit(civ, "militia")
    u.kills = 6
    cost = w.military.promotion_cost("militia", "swordsman")
    assert cost == {"food": 20, "ore": 20, "gold": 15, "coins": 10}
    assert w.military.promote(civ, "militia")
    assert u not in civ.units and civ.training[0]["promote"] == "militia"
    w.run(2 * 3600)                                       # half of 2 h, during work hours
    assert [x.type for x in civ.soldiers] == ["swordsman"]
    assert civ.soldiers[0].kills == 6


def test_no_promotion_without_the_building():
    w = world(ai=False)
    civ = w.civs[0]
    civ.castle.level = 7
    w.military.spawn_unit(civ, "spearman")
    assert w.military.promotion(civ, "spearman") is None   # knights need a stable


# ---------------------------------------------------------------------------- battle logic
def test_the_army_gathers_then_marches_together():
    w = world(ai=False)
    w.start_t -= 30 * 86400
    north, south = w.civs
    for _ in range(6):
        w.military.spawn_unit(north, "militia")
    assert w.military.order_attack(north, south) > 0
    assert north.muster and all(u.order == "muster" for u in north.soldiers[1:])
    assert not any(e.kind == "attack" for e in w.journal.events)
    w.run(3600)
    assert north.muster is None
    assert any(e.kind == "attack" for e in w.journal.events)
    assert north.attack_size0 >= 5


def test_soldiers_pick_what_they_beat_and_finish_the_weakest():
    w = world(ai=False)
    north, south = w.civs
    sp = w.military.spawn_unit(north, "spearman")
    knight = w.military.spawn_unit(south, "knight")
    militia = w.military.spawn_unit(south, "militia")
    for e in (knight, militia):
        e.x, e.y = sp.x + 0.5, sp.y
    spec = R.units["spearman"]
    tgt, _ = w.military._best_target(sp, spec, [(militia, south), (knight, south)])
    assert tgt is knight                                  # spearmen ×3 against cavalry
    m2 = w.military.spawn_unit(south, "militia")
    m2.x, m2.y = sp.x + 0.5, sp.y
    m2.hp = 5
    archer_spec = R.units["militia"]
    tgt, _ = w.military._best_target(sp, archer_spec, [(militia, south), (m2, south)])
    assert tgt is m2                                      # focus fire on the weakest in reach


def test_wounded_attackers_go_home():
    w = world(ai=False)
    north = w.civs[0]
    u = w.military.spawn_unit(north, "militia")
    u.order = "attack"
    u.hp = 0.1 * u.max_hp
    w.military._wounded(north)
    assert u.order == "return"


def test_defenders_go_out_to_meet_an_army_close_to_the_castle():
    w = world(ai=False)
    north, south = w.civs
    d = w.military.spawn_unit(north, "militia")
    e = w.military.spawn_unit(south, "militia")
    e.order = "attack"
    e.x, e.y = north.castle.center[0] + 20, north.castle.center[1] + 10   # out of reach (8), within 30
    tgt, _ = w.military._pick_target(north, d, R.units["militia"], [(e, south)], [south], 8)
    assert tgt is e


def test_archers_keep_behind_the_front():
    w = world(ai=False)
    north, south = w.civs
    front = w.military.spawn_unit(north, "militia")
    archer = w.military.spawn_unit(north, "archer")
    for u in (front, archer):
        u.order, u.goal = "attack", south.castle
    w.military._front[north.id] = w.military._front_line(north)
    gx, gy = w.military._formation_goal(north, archer)
    sx, sy = south.castle.center
    import math
    assert math.dist((gx, gy), (sx, sy)) > math.dist((front.x, front.y), (sx, sy))


def test_a_won_defense_leads_to_a_counter_attack():
    w = world()
    w.start_t -= 30 * 86400
    north, south = w.civs
    for _ in range(8):
        w.military.spawn_unit(north, "spearman")
    north.genome.genes["counter_attack"] = 1.0
    north.counter = {"enemy": "south", "until": w.clock.t + 3600}
    note = north.ai.army.war(north.ai.estimate())
    assert note and "counter-attack" in note and north.muster


def test_the_ai_mixes_archers_and_cavalry():
    w = world()
    civ = w.civs[0]
    civ.castle.level = 7
    for b in ("barracks", "archery_range", "stable"):
        give_building(w, civ, b)
    civ.stock.update(food=2000, wood=900, ore=900, gold=900, coins=900)
    for _ in range(10):
        w.military.spawn_unit(civ, "swordsman")
    assert civ.ai.army.unit_choice() == "archer"
    for _ in range(5):
        w.military.spawn_unit(civ, "archer")
    assert civ.ai.army.unit_choice() == "knight"
