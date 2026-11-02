"""Version 7: tech tree, roads and carts, fog and scouts, bandits, notifications, the genome family tree."""
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine import tech as T
from engine.entities import Event
from engine.rules import default_rules
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 7, 0, tzinfo=TZ)


def world(rules=R, **kw):
    kw.setdefault("start", SPRING)
    return World(rules, tz=TZ, **kw)


# ---------------------------------------------------------------------------- tech tree
def test_a_tech_is_studied_at_the_castle_and_takes_effect():
    w = world(ai=False)
    civ = w.civs[0]
    civ.castle.level = 3
    civ.stock.update(wood=500, ore=500)
    assert "plow" in T.available(R, civ)
    w.start_tech(civ, "plow")
    assert civ.stock["wood"] == 400 and civ.tech_job
    w.run(2 * 86400)                                        # 12 work hours
    assert civ.has_tech("plow") and civ.tech_job is None
    spring = R.seasons["spring"]
    assert T.food_mult(R, civ, "farm", "food", spring) == pytest.approx(1.15)
    assert T.food_mult(R, civ, "tree", "wood", spring) == 1.0


def test_tech_effects():
    w = world(ai=False)
    civ = w.civs[0]
    civ.techs |= {"irrigation", "crop_rotation", "water_mill", "writing"}
    assert T.food_mult(R, civ, "farm", "food", R.seasons["summer"]) == pytest.approx(1.2 * 1.1)
    assert T.food_mult(R, civ, "farm", "food", R.seasons["winter"]) == pytest.approx(0.7 / 0.5 * 1.1)
    assert T.food_mult(R, civ, "animal", "food", R.seasons["spring"]) == pytest.approx(1.1)
    assert T.xp_speed(R, civ) == 1.2


def test_medicine_halves_disease():
    w = world(ai=False)
    civ = w.civs[0]
    before = w.events.disease_chance(civ)
    civ.techs.add("medicine")
    assert w.events.disease_chance(civ) == pytest.approx(before * 0.5)


# ---------------------------------------------------------------------------- roads and carts
def test_a_road_is_laid_tile_by_tile_and_speeds_people_up():
    from engine.roads import start_road
    w = world()
    civ = w.civs[0]
    civ.stock["stone"] = 500
    cx, cy = (int(v) for v in civ.castle.center)
    tiles = [(cx + 3 + i, cy + 3) for i in range(4) if w.map.walkable(cx + 3 + i, cy + 3)]
    assert start_road(w, civ, tiles, "test")
    assert civ.stock["stone"] == 500 - 5 * len(tiles)
    p = civ.people[0]
    civ.ai.set_job(p, "road", w.clock.t)
    w.run(4 * 3600)
    done = [t for t in tiles if w.map.road[w.map.idx(*t)]]
    assert done, "the road worker laid at least one tile"
    p.x, p.y = float(done[0][0]), float(done[0][1])
    on = w.behavior.speed(p, R.seasons["spring"])
    p.x, p.y = float(cx), float(cy - 6)
    off = w.behavior.speed(p, R.seasons["spring"])
    assert on == pytest.approx(off * 1.5)


def test_a_cart_carries_50_and_rolls_faster():
    w = world(ai=False)
    p = w.civs[0].people[0]
    p.trait = None
    base_speed = w.behavior.speed(p, R.seasons["spring"])
    p.cart = True
    assert w.behavior.carry_cap(p) == 50
    assert w.behavior.speed(p, R.seasons["spring"]) == pytest.approx(base_speed * 1.2)


def test_road_pays_only_with_enough_trips():
    from ai.roads import RoadPlanner
    w = world()
    civ = w.civs[0]
    est = civ.ai.estimate()
    rp = RoadPlanner(civ.ai)
    gain_many, cost = rp.value("wood", 6, 30, "wood", est)
    gain_few, _ = rp.value("wood", 1, 30, "wood", est)
    assert gain_many > gain_few


# ---------------------------------------------------------------------------- fog
def test_fog_hides_far_iron_until_seen():
    w = world(ai=False)
    civ = w.civs[0]
    fog = w.fog
    assert fog.knows(civ, *map(int, civ.castle.center))
    far = max(w.map.by_kind["iron"], key=lambda n: math.dist(n.center, civ.castle.center))
    assert not fog.usable(civ, far)
    fog.reveal(civ, *far.center, 3)
    assert fog.usable(civ, far)
    assert any(e.kind == "discovery" for e in w.journal.events) or w.clock.t <= civ.born_t + 60


def test_trees_are_never_hidden():
    w = world(ai=False)
    civ = w.civs[0]
    far_tree = max(w.map.by_kind["tree"], key=lambda n: math.dist(n.center, civ.castle.center))
    assert w.fog.usable(civ, far_tree)


def test_a_scout_explores():
    w = world(ai=False)
    civ = w.civs[0]
    before = w.fog.share_known(civ)
    w.military.spawn_unit(civ, "scout")
    w.run(4 * 3600)
    assert w.fog.share_known(civ) > before + 0.05


def test_fog_off_means_everything_is_known():
    w = world(R.with_features(fog=False), ai=False)
    civ = w.civs[0]
    far = max(w.map.by_kind["gold"], key=lambda n: math.dist(n.center, civ.castle.center))
    assert w.fog.usable(civ, far)


# ---------------------------------------------------------------------------- bandits
def test_the_camp_is_as_far_from_both_castles():
    w = world(ai=False)
    camp = w.bandits
    d0 = math.dist((camp.x, camp.y), w.civs[0].castle.center)
    d1 = math.dist((camp.x, camp.y), w.civs[1].castle.center)
    assert abs(d0 - d1) < 3 and w.map.walkable(int(camp.x), int(camp.y))


def test_bandits_raid_take_20_percent_and_go_home():
    w = world(ai=False)
    camp = w.bandits
    north, south = w.civs
    north.castle.level = south.castle.level = 2          # castle-1 villages are too poor to rob
    north.stock.update(food=5000, wood=900)
    camp.next_raid = w.clock.t
    w.run(60)
    assert camp.raid is not None and camp.raid["target"] == "north"     # the richest
    assert len(camp.units) == 3 + w.day // 15
    food = north.stock["food"]
    for _ in range(24):
        w.run(1800)
        if camp.raid is None:
            break
    assert north.stock["food"] < food * 0.85
    assert any("took" in e.text for e in w.journal.events if e.kind == "bandits")
    assert all(p.alive for p in north.people)                           # loot, not murder


def test_beaten_bandits_leave_gold():
    w = world(ai=False)
    camp = w.bandits
    north = w.civs[0]
    north.castle.level = 2
    for _ in range(8):
        w.military.spawn_unit(north, "spearman")
    north.stock.update(food=5000)
    camp.next_raid = w.clock.t
    w.run(60)
    for u in camp.units:
        u.x, u.y = north.castle.center[0] + 6, north.castle.center[1] + 6   # they arrive at once
    gold = north.stock["gold"]
    for _ in range(24):
        w.run(600)
        if camp.raid is None:
            break
    assert camp.raid is None and north.stock["gold"] >= gold + 50


def test_friends_help_against_bandits():
    w = world(ai=False)
    camp = w.bandits
    north, south = w.civs
    for _ in range(3):
        w.military.spawn_unit(south, "militia")
    w.diplomacy.R[w.diplomacy.pair("north", "south")] = 70
    north.castle.level = 2
    north.stock.update(food=9000)
    camp.next_raid = w.clock.t
    w.run(60)
    assert all(u.order == "help" for u in south.soldiers)


# ---------------------------------------------------------------------------- notifications
def test_notifications_are_off_by_default():
    from ai.notify import Notifier
    sent = []
    n = Notifier(R.notify, sender=lambda t, x: sent.append(x))
    assert not n.enabled
    assert not n.event(Event(0, 1, "north", "castle", "North reached castle 5.", 10), "2027-04-05")
    assert not sent


def test_notifications_send_big_events_up_to_10_a_day():
    from ai.notify import Notifier
    sent = []
    cfg = {**R.notify, "enabled": True, "ntfy_topic": "test-topic"}
    n = Notifier(cfg, sender=lambda t, x: sent.append(x))
    assert not n.event(Event(0, 1, "north", "build_done", "A House is finished.", 3), "2027-04-05")
    for i in range(12):
        n.event(Event(0, 1, "north", "attack", f"North attacks South {i}!", 9), "2027-04-05")
    assert len(sent) == 10
    assert n.chronicle("A new day", "2027-04-06") and sent[-1] == "A new day"


# ---------------------------------------------------------------------------- evolution: family tree, stall, new gene
def test_family_tree_shows_changed_genes(tmp_path):
    from ai.genome import Genome
    from engine.store import Store
    s = Store(tmp_path / "t.db")
    a = Genome.default(R)
    a.id = "a"
    b = Genome({**a.genes, "g_food": 2.5}, id="b")
    child = Genome({**a.genes, "g_food": 2.5, "theta_attack": 2.9}, id="c", parents=("a", "b"))
    for g in (a, b, child):
        s.save_genome(g, 1)
    tree = s.family_tree("c")
    me = tree[0]
    assert me["id"] == "c" and set(me["parents"]) == {"a", "b"}
    assert "theta_attack" in me["changed"] and "g_food" not in me["changed"]
    assert {n["id"] for n in tree} == {"a", "b", "c"}


def test_stall_watch():
    from ai.evolution import stall_nights
    nights = [{"bench_fitness": v} for v in (0.40, 0.45, 0.45, 0.44, 0.45)]
    assert stall_nights(nights) == 3
    assert stall_nights([{"bench_fitness": 0.4}, {"bench_fitness": 0.5}]) == 0


def test_old_genomes_get_the_new_gene(tmp_path):
    from engine.store import Store
    s = Store(tmp_path / "o.db")
    genes = {k: v for k, v in R.genome["default"].items() if k != "exploration"}
    s.db.execute("INSERT INTO genomes(id, night, parents, generation, genes) VALUES(?,?,?,?,?)",
                 ("old", 1, "[]", 0, json.dumps(genes)))
    g = s.genome("old")
    assert g.genes["exploration"] == 0.5


# ---------------------------------------------------------------------------- the page
def test_frames_carry_roads_fog_and_bandits():
    from server.snapshot import FrameMaker, map_payload
    w = world()
    m = map_payload(w)
    assert m["camp"] and m["roads"] and set(m["fog"]) == {"north", "south"}
    fm = FrameMaker(w)
    f1 = fm.frame()
    assert set(f1["fog"]) == {"north", "south"}
    w.map.road[w.map.idx(10, 10)] = 1
    w.map.road_log.append((10, 10, 1))
    w.civs[0].castle.level = 2
    w.bandits.next_raid = w.clock.t
    w.run(60)
    f2 = fm.frame()
    assert [10, 10, 1] in f2["roads"]
    assert any(u[1] == -1 for u in f2["units"])
    assert "techs" in f2["civs"][0] and "known" in f2["civs"][0]
