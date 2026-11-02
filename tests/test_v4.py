"""Version 4: what the browser sees, the radio commentator, the pictures, the web API.
Nothing here runs the live world in real time: worlds are stepped by hand."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ai.commentator import Commentator, clip
from ai.images import comfy_workflow, daily_pictures, person_seed, pick_scenes, portrait_prompt
from ai.llm import FakeLLM
from engine.entities import Event
from engine.rules import default_rules
from engine.world import World
from server.snapshot import FrameMaker, map_payload, person_card

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 9, 0, tzinfo=TZ)


def world(**kw):
    kw.setdefault("start", SPRING)
    return World(R, tz=TZ, **kw)


def local(h, m=0):
    return datetime(2027, 4, 5, h, m, tzinfo=TZ)


# ---------------------------------------------------------------------------- snapshot
def test_map_payload_has_every_node_and_the_terrain():
    w = world()
    m = map_payload(w)
    assert len(m["nodes"]) == len(w.map.nodes)
    assert len(m["terrain"]) > 0 and m["size"] == 128
    assert [c["id"] for c in m["civs"]] == ["north", "south"]
    json.dumps(m)                                              # plain JSON


def test_frames_send_only_whats_new():
    w = world()
    fm = FrameMaker(w)
    f1 = fm.frame()
    assert len(f1["people"]) == 4 and f1["events"] == []        # founding events come with "hello", not twice
    assert fm.hello()["events"]
    w.log(w.civs[0], "castle", "North reached castle 2.", 10)
    f2 = fm.frame()
    assert [e["text"] for e in f2["events"]] == ["North reached castle 2."]
    assert fm.frame()["events"] == []
    node = w.map.by_kind["tree"][0]
    node.amount = 0
    f4 = fm.frame()
    assert [node.id, 0] in f4["nodes"]
    json.dumps(f4)


def test_frames_survive_a_trimmed_journal():
    w = world()
    w.journal.max_events = 5
    fm = FrameMaker(w)
    for i in range(12):
        w.log(None, "test", f"event {i}", 1)
    texts = [e["text"] for e in fm.frame()["events"]]
    assert texts == [f"event {i}" for i in range(7, 12)]       # what is still in the journal, no repeats


def test_repeated_thoughts_with_new_numbers_are_skipped():
    w = world()
    fm = FrameMaker(w)
    ai = w.civs[0].ai
    ai.thoughts.clear()
    ai.thoughts.append((w.clock.t + 1, "Food: 140. Workers: 2."))
    ai.thoughts.append((w.clock.t + 2, "Food: 139. Workers: 2."))
    ai.thoughts.append((w.clock.t + 3, "Started: Farm."))
    texts = [t["text"] for t in fm.frame()["thoughts"] if t["civ"] == "north"]
    assert texts == ["Food: 140. Workers: 2.", "Started: Farm."]


def test_construction_site_shows_time_left():
    w = world()
    w.run(2 * 3600)
    fm = FrameMaker(w)
    sites = [b for b in fm.frame()["buildings"] if b[7] > b[6]]
    assert sites, "the AI starts a farm in the first hours"
    left = [b[12] for b in sites if b[12] is not None]
    assert left and all(x > 0 for x in left)


def test_person_card():
    w = world()
    w.run(3600)
    p = w.civs[0].people[0]
    card = person_card(w, p.id)
    assert card["name"] == p.name and card["civ"] == "north"
    assert card["home"] == "Castle" and card["history"]
    assert person_card(w, 99999) is None


# ---------------------------------------------------------------------------- commentator
def ev(kind, text, t=None, civ="north"):
    return Event(t or SPRING.timestamp(), 1, civ, kind, text, 5)


def test_commentator_picks_big_events_only():
    c = Commentator(R, FakeLLM(up=False))
    c.hear(ev("build_done", "North finished a House."))
    assert c.next_line(SPRING.timestamp(), local(10)) is None
    c.hear(ev("castle", "North reached castle 2."))
    line = c.next_line(SPRING.timestamp(), local(10))
    assert line is not None and line.kind == "castle"


def test_commentator_is_quiet_at_night_and_counts_per_hour():
    c = Commentator(R, FakeLLM(up=False))
    t = SPRING.timestamp()
    c.hear(ev("castle", "North reached castle 2.", t))
    assert c.next_line(t, local(23)) is None and not c.pending     # silent 22:00–07:00
    said = 0
    for i in range(25):                                            # 25 big events within the same hour
        c.hear(ev("death", f"Ana died, {i} days old.", t + i * 60))
        said += c.next_line(t + i * 60, local(12)) is not None
    assert said == 20                                              # at most 20 an hour


def test_commentator_line_from_ai_or_from_the_journal():
    p_ai = Commentator(R, FakeLLM(lambda s, u: {"line": "Big news from the North: the castle grows to level two!"}))
    p_ai.hear(ev("castle", "North reached castle 2."))
    c = p_ai.write(p_ai.next_line(SPRING.timestamp(), local(10)))
    assert c.by_ai and c.text.startswith("Big news") and c.audio is None     # no Piper here: the browser speaks
    plain = Commentator(R, FakeLLM(up=False))
    plain.hear(ev("castle", "North reached castle 2."))
    c2 = plain.write(plain.next_line(SPRING.timestamp(), local(10)))
    assert not c2.by_ai and c2.text == "North reached castle 2."


def test_clip_keeps_sentences_under_the_limit():
    text = "The North raised its castle to the second level today. " * 6
    out = clip(text, 200)
    assert len(out) <= 200 and out.endswith(".")


# ---------------------------------------------------------------------------- pictures
def test_same_person_same_seed():
    assert person_seed("north", "Ana Morar") == person_seed("north", "Ana Morar")
    assert person_seed("north", "Ana Morar") != person_seed("south", "Ana Morar")


def test_portrait_prompt_is_made_from_data():
    s = {"name": "Ana Morar", "age": 31, "job": "wood", "trait": "Strong", "kingdom": "North"}
    p = portrait_prompt(s, "Winter", "#3b82f6", "watercolor")
    for part in ("Ana Morar", "31", "woodcutter", "strong", "North", "blue", "winter", "watercolor"):
        assert part in p


def test_comfy_workflow_is_text_to_image():
    wf = comfy_workflow(R.images, "a castle", 42, "ageofai/x")
    kinds = {n["class_type"] for n in wf.values()}
    assert {"UNETLoader", "CLIPLoader", "VAELoader", "KSampler", "SaveImage"} <= kinds
    ks = next(n for n in wf.values() if n["class_type"] == "KSampler")["inputs"]
    assert ks["seed"] == 42 and ks["steps"] == R.images["steps"]


def test_scenes_are_the_days_biggest_moments():
    t = SPRING.timestamp()
    events = [ev("build_done", "North finished a House.", t), ev("castle", "North reached castle 3.", t),
              ev("castle", "North reached castle 2.", t - 100), ev("fall", "South has no people left.", t, "south")]
    picked = pick_scenes(events, t - 86400, 3, 7)
    assert [e.kind for e in picked] == ["castle", "fall"]          # one per kingdom and kind, nothing small


def test_no_comfyui_no_pictures():
    class Away:
        cfg = R.images

        def available(self):
            return False

    assert daily_pictures(Away(), FakeLLM(), "2027-04-05", None, [], "Spring", {}, {}) == []


# ---------------------------------------------------------------------------- the live mind's v4 schedule
def test_a_late_start_does_not_run_the_morning_jobs(tmp_path):
    from ai.live_mind import LiveMind
    from engine.store import Store

    w = world()                                                     # starts at 09:00
    mind = LiveMind(w, Store(tmp_path / "m.db"), FakeLLM(up=False))
    for key in ("chronicle", "pictures", "publish"):
        assert key in mind.done_today
    assert "reflection" not in mind.done_today                      # 22:00 is still ahead
    mind.close()


# ---------------------------------------------------------------------------- web API (no world thread)
@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from server.runner import LiveRunner

    r = LiveRunner(db=tmp_path / "web.db", start=SPRING, evolve=False, llm=FakeLLM(up=False), pictures=False)
    r.w.run(3600)
    with TestClient(create_app(r, run_world=False)) as c:
        yield c, r
    r.mind.close()


def test_web_endpoints(client):
    c, r = client
    assert c.get("/").status_code == 200
    m = c.get("/api/map").json()
    assert m["size"] == 128 and len(m["nodes"]) == len(r.w.map.nodes)
    pid = r.w.civs[1].people[0].id
    card = c.get(f"/api/person/{pid}").json()
    assert card["civ"] == "south" and card["portraits"] == []
    assert c.get("/api/person/123456").status_code == 404
    assert c.get("/api/stats").json()["civs"][0]["name"] == "North"
    evo = c.get("/api/evolution").json()
    assert evo["nights"] == [] and evo["best"] is None
    assert c.get("/api/chronicles").json() == []
    h = c.get("/api/health").json()
    assert h["ok"] and h["llm"] is False


def test_web_page_cannot_change_the_game(client):
    c, _ = client
    for path in ("/api/map", "/api/stats", "/api/person/1"):
        assert c.post(path).status_code == 405


def test_websocket_says_hello_then_sends_frames(client):
    c, r = client
    with c.websocket_connect("/ws") as ws:
        hello = json.loads(ws.receive_text())
        assert hello["type"] == "hello" and hello["events"]
        c.app.state.hub.publish(r.frames.frame())
        frame = json.loads(ws.receive_text())
        assert frame["type"] == "frame" and frame["clock"]["season"] == "Spring"
