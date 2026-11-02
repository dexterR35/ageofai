"""The second review round: small fixes, shared helpers, the Wonder and new eras, two different minds."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai.genome import Genome
from ai.llm import FakeLLM
from engine.entities import Building
from engine.pathfinding import step_along
from engine.rules import default_rules
from engine.store import Store
from engine.units import count_types
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 9, 0, tzinfo=TZ)


def world(**kw):
    kw.setdefault("start", SPRING)
    return World(R, tz=TZ, **kw)


def give_building(w, civ, btype, level=1):
    from engine.worldmap import find_site
    spec = R.buildings[btype]
    x, y = find_site(w.map, civ.castle.center, spec.size, radius=24, min_dist=3, flip=civ.mirror)
    b = Building(w.next_id(), civ.id, btype, x, y, spec.size, spec.walkable, w.clock.t)
    b.level = b.target_level = level
    b.hp = b.max_hp = R.building_hp(btype, level)
    w.map.place_building(b.id, x, y, spec.size, spec.walkable)
    civ.buildings.append(b)
    civ.invalidate_depots()
    return b


# ---------------------------------------------------------------------------- small fixes
def test_portraits_belong_to_one_person_not_a_name(tmp_path):
    s = Store(tmp_path / "p.db")
    s.save_image({"date": "2027-04-05", "kind": "portrait", "person_id": 7, "person_name": "Ana Pop", "file": "a.png"})
    s.save_image({"date": "2027-04-06", "kind": "portrait", "person_id": 9, "person_name": "Ana Pop", "file": "b.png"})
    assert [r["file"] for r in s.images(person_id=7, person_name="Ana Pop")] == ["a.png"]
    assert s.images(person_id=7, person_name="Ion Pop") == []          # a new world reused the id


def test_bandits_who_robbed_the_castle_give_no_reward_but_the_loot_comes_back():
    w = world(ai=False)
    camp = w.bandits
    north = w.civs[0]
    north.castle.level = 2
    north.stock.update(food=300, gold=0)                 # under the storage cap (500), so the food fits back
    camp.next_raid = w.clock.t
    w.run(60)
    camp.plunder(north)                                  # they reached the castle…
    robbed = north.stock["food"]
    for u in list(camp.units):                           # …and all fell on the way home
        camp.units.remove(u)
    camp.tick()
    assert camp.raid is None
    assert north.stock["gold"] == 0                      # no 50-gold reward
    assert north.stock["food"] > robbed                  # the stolen food is back


def test_a_deal_for_a_fallen_kingdom_is_cleaned_up():
    w = world(ai=False)
    north, south = w.civs
    m, why = w.diplomacy.clean({"type": "gift", "give": {"wood": 50}}, north, south)
    assert m is not None, why
    w.diplomacy.post(m)
    w.trade.start_deal(m)
    assert m.id in w.trade.deals and w.trade.carts
    w.military.fall(south, None)
    w.run(3 * 3600)
    assert m.id not in w.trade.deals and m.status == "lost"


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.runner import LiveRunner

    r = LiveRunner(db=tmp_path / "web.db", start=SPRING, evolve=False, llm=FakeLLM(up=False), pictures=False)
    with TestClient(create_app(r, run_world=False)) as c:
        yield c, r
    r.mind.close()


def test_the_api_refuses_huge_requests(client):
    c, _ = client
    assert c.get("/api/comments?limit=100000").status_code == 422
    assert c.get("/api/timelapse?hours=100000").status_code == 422
    assert c.get("/api/chronicles?search=" + "x" * 500).status_code == 422
    assert c.get("/api/comments?limit=5").status_code == 200


def test_the_evolution_tab_shows_the_genomes_playing_now(client):
    c, r = client
    r.w.civs[0].genome = Genome(dict(r.w.civs[0].genome.genes), id="n3g7")
    assert c.get("/api/evolution").json()["in_use"][0] == "n3g7"


# ---------------------------------------------------------------------------- shared helpers
def test_shared_helpers():
    x, y, i = step_along(((1, 0), (2, 0)), 0, 0.0, 0.0, 1.5)
    assert (round(x, 2), y, i) == (1.5, 0.0, 1)
    assert step_along(((1, 0),), 0, 0.0, 0.0, 5.0) == (1.0, 0.0, 1)

    class U:
        def __init__(self, t):
            self.type = t
    assert count_types([U("archer"), U("militia"), U("archer")]) == {"archer": 2, "militia": 1}

    w = world(ai=False)
    m = w.map
    x, y = w.civs[0].castle.center
    ring = m.nearest_walkable(int(x), int(y), 5)
    assert ring and all(m.walkable(*t) for t in ring)
    info = w.military.unit_info("spy")
    assert info["building"] == "market" and info["cost"] == dict(R.spies["cost"])
    assert w.military.unit_info("archer")["hp"] == R.units["archer"].hp


# ---------------------------------------------------------------------------- the Wonder and new eras
def castle_10(w, civ):
    civ.castle.level = civ.castle.target_level = 10
    for res in civ.stock:
        civ.stock[res] = 1000.0


def test_the_wonder_is_paid_while_it_rises():
    w = world(ai=False)
    north = w.civs[0]
    castle_10(w, north)
    before = dict(north.stock)
    site = w.start_building(north, "wonder", *w.civs[0].ai.place("wonder")) if north.ai else None
    if site is None:
        from engine.worldmap import find_site
        x, y = find_site(w.map, north.castle.center, 4, radius=24, min_dist=3)
        site = w.start_building(north, "wonder", x, y)
    assert north.stock == before                          # nothing paid up front
    north.builders_at = {site.id: 5.0}
    delta = w._deliver_materials(north, site, 0.01)
    assert delta > 0 and sum(site.paid.values()) > 0 and north.stock["stone"] < before["stone"]


def test_a_wonder_site_is_as_strong_as_its_progress_and_loot_is_what_was_delivered():
    w = world(ai=False)
    w.start_t -= 30 * 86400
    north, south = w.civs
    castle_10(w, south)
    site = give_building(w, south, "wonder", level=0)
    site.target_level, site.progress, site.hp, site.max_hp = 1, 0.4, 0.0, 0.0   # a fresh site, as start_building makes it
    site.paid = {"stone": 1000.0}
    m = w.military.spawn_unit(north, "militia")
    w.military._hit(site, 10.0, north, south, m)
    assert site in south.buildings and site.hp > 0.39 * R.building_hp("wonder", 1)   # one blow is not enough
    stone = north.stock["stone"]
    site.hp = 1.0
    w.military._hit(site, 10.0, north, south, m)
    assert site not in south.buildings
    assert north.stock["stone"] == pytest.approx(stone + 300)                          # 30% of what was delivered


def test_the_first_wonder_wins_the_era_and_a_new_era_begins_in_the_live_world():
    w = world(ai=True)
    w.live = True
    north = w.civs[0]
    castle_10(w, north)
    site = give_building(w, north, "wonder", level=0)
    site.target_level = 1
    w._complete(north, site)
    assert w.era_winner == "north" and w.new_era_at == pytest.approx(w.clock.t + 7 * 86400)
    assert any(e.kind == "wonder" for e in w.journal.events)
    nxt = w.new_era_world()
    assert nxt.era == 2 and nxt.eras[0]["winner"] == "north" and nxt.era_winner is None
    assert all(c.castle_level == 1 and c.pop == 2 for c in nxt.civs)
    assert nxt.map is not w.map and nxt.live


def test_the_ai_starts_a_wonder_at_castle_10():
    w = world(ai=True)
    north = w.civs[0]
    castle_10(w, north)
    a = north.ai.wonder_action()
    assert a is not None and a.btype == "wonder"
    give_building(w, north, "wonder", level=0).target_level = 1
    assert north.ai.wonder_action() is None              # one Wonder at a time


def test_the_runner_moves_to_a_new_era(tmp_path):
    from server.runner import LiveRunner

    r = LiveRunner(db=tmp_path / "e.db", llm=FakeLLM(up=False), pictures=False, speed=60, start=SPRING,
                   evolve=False)
    try:
        me = r.w
        old_map = r.w.map
        r.w.era_winner, r.w.eras = "south", [{"era": 1, "winner": "south", "name": "South", "day": 3,
                                              "date": "2027-04-07"}]
        r.w.new_era_at = r.w.clock.t - 1
        r.schedule()
        assert r.w is me and r.w.era == 2 and r.w.map is not old_map and r.w.live
        assert r.w.military.w is r.w and r.w.bandits.w is r.w
        r.w.run(3600)                                      # the new era plays on
    finally:
        r.mind.close()


# ---------------------------------------------------------------------------- two different minds
def test_the_live_world_starts_with_two_different_minds(tmp_path):
    from server.runner import LiveRunner

    north, south = Genome.live_start(R)
    assert north.id == "start-builder" and south.id == "start-warlord"
    assert north["theta_attack"] > south["theta_attack"] and north["honesty"] > south["honesty"]
    r = LiveRunner(db=tmp_path / "m.db", llm=FakeLLM(up=False), pictures=False, speed=60, start=SPRING,
                   evolve=False)
    try:
        assert [c.genome.id for c in r.w.civs] == ["start-builder", "start-warlord"]
    finally:
        r.mind.close()
