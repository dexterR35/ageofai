"""Version 3: the rule language, statistics, memory, council checks, chronicle, referee, rule lab."""
import math
import sqlite3
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from ai import chronicle as chron
from ai import referee
from ai.council import Council, check_plan, clamp_priorities, update_weights
from ai.evolution import Evolution
from ai.llm import FakeLLM, LLMError, parse_json
from ai.memory import MemoryBank, keep_days, memory_score
from ai.rule_lab import RuleLab
from ai.rulebook import RuleError, p_at_least, parse_rule, rule_state, split_rules, verdict
from engine.rules import default_rules
from engine.store import Store
from engine.world import World

TZ = ZoneInfo("Europe/Bucharest")
R = default_rules()
SPRING = datetime(2027, 4, 5, 7, 0, tzinfo=TZ)

DOC_RULE = '''RULE "Winter reserve"
WHEN month = november AND food < 5000
THEN move 2 workers to farms
PRIORITY 0.8'''


def world(**kw) -> World:
    kw.setdefault("start", SPRING)
    return World(R, tz=TZ, **kw)


# ---------------------------------------------------------------------------- the rule language

def test_doc_example_parses_and_round_trips():
    r = parse_rule(DOC_RULE, R)
    assert r.name == "Winter reserve" and r.priority == 0.8
    assert r.action.kind == "move" and r.action.target == "food" and r.action.amount == 2
    assert [c.var for c in r.conditions] == ["month", "food"]
    assert parse_rule(r.text(), R).signature() == r.signature()


def test_rule_logic_and_before_or():
    r = parse_rule('RULE "x"\nWHEN food < 100 OR wood < 50 AND stone < 50\nTHEN build farm\nPRIORITY 1', R)
    assert r.holds({"food": 50, "wood": 999, "stone": 999})
    assert r.holds({"food": 500, "wood": 10, "stone": 10})
    assert not r.holds({"food": 500, "wood": 10, "stone": 999})


@pytest.mark.parametrize("text, why", [
    ('RULE "x"\nWHEN food < 5\nTHEN import os\nPRIORITY 0.5', "unknown action"),
    ('RULE "x"\nWHEN gold_bars < 5\nTHEN build farm\nPRIORITY 0.5', "unknown variable"),
    ('RULE "x"\nWHEN food < 5\nTHEN move 9 workers to wood\nPRIORITY 0.5', "1–3"),
    ('RULE "x"\nWHEN food < 5\nTHEN build castle_of_doom\nPRIORITY 0.5', "unknown building"),
    ('RULE "x"\nWHEN food < 5\nTHEN multiply gene g_food by 5 for 24h\nPRIORITY 0.5', "between 0.5 and 2"),
    ('RULE "x"\nWHEN food < 5\nTHEN build farm\nPRIORITY 1.5', "between 0 and 1"),
    ('RULE "x"\nWHEN food < 5 AND wood < 5 AND stone < 5 AND ore < 5\nTHEN build farm\nPRIORITY 0.5', "at most 3"),
    ('RULE "x"\nWHEN season > winter\nTHEN build farm\nPRIORITY 0.5', "only be compared"),
    ('WHEN food < 5\nTHEN build farm\nPRIORITY 0.5', "4 lines"),
])
def test_bad_rules_are_refused(text, why):
    with pytest.raises(RuleError, match=why):
        parse_rule(text, R)


def test_all_action_kinds():
    acts = {
        "build granary": ("build", "granary"),
        "build a lumber camp": ("build", "lumber_camp"),
        "upgrade tools of lumberjacks": ("upgrade_tool", "lumberjack"),
        "multiply gene castle by 1.5 for 24h": ("gene", "g_castle"),
        "multiply gene winter_prep by 0.5 for 24 hours": ("gene", "winter_prep"),
        "train 3 archers": ("train", "archer"),
        "send message gift": ("message", "gift"),
    }
    for action, (kind, target) in acts.items():
        r = parse_rule(f'RULE "a"\nWHEN season = winter\nTHEN {action}\nPRIORITY 0.3', R)
        assert (r.action.kind, r.action.target) == (kind, target), action


def test_split_rules_from_a_model_answer():
    text = DOC_RULE + "\n\n" + DOC_RULE.replace("Winter reserve", "Second")
    assert len(split_rules(text)) == 2


# ---------------------------------------------------------------------------- statistics (doc numbers)

def test_sign_test_numbers_from_the_doc():
    assert p_at_least(6, 8) == pytest.approx(37 / 256)           # 14.5%
    assert p_at_least(12, 16) == pytest.approx(2517 / 65536)     # 3.8%


def test_verdicts():
    r = parse_rule(DOC_RULE, R)
    r.wins, r.games = 6, 8
    assert verdict(r) == "probation"
    r.status, r.wins, r.games = "probation", 12, 16
    assert verdict(r) == "accepted"
    r.status, r.wins = "probation", 11
    assert verdict(r) == "probation"                # improvements §6: 11/16 is promising — one more night
    r.status, r.wins, r.games = "probation", 15, 24
    assert verdict(r) == "probation"
    r.status, r.wins, r.games = "probation", 18, 24
    assert verdict(r) == "accepted"                 # as unlikely by luck as 12/16
    r.status, r.wins, r.games = "probation", 18, 32
    assert verdict(r) == "rejected"                 # 4 nights and still not convincing
    r.status, r.wins, r.games = "test", 5, 8
    assert verdict(r) == "test"                     # a second night of tests
    r.status, r.wins, r.games = "test", 3, 8
    assert verdict(r) == "rejected"
    r.status, r.wins, r.games = "test", 9, 16
    assert verdict(r) == "rejected"                 # two nights, still not 6/8-convincing


# ---------------------------------------------------------------------------- memory

def test_memory_score_and_half_life():
    half = math.log(0.5) / math.log(0.995)
    assert half == pytest.approx(138, abs=1)
    assert memory_score(0, 10, 1.0) == pytest.approx(1.0)
    assert memory_score(1e9, 9, 0) == pytest.approx(0.36)       # an old betrayal still scores on importance


def test_forgetting_rules():
    assert keep_days(5) == 60 and keep_days(6) == 60
    assert keep_days(7) == 365 and keep_days(8) == 365
    assert keep_days(9) is None and keep_days(10) is None


def test_memory_bank_recall_and_forget():
    db = sqlite3.connect(":memory:")
    bank = MemoryBank(db, FakeLLM(dim=64))
    day = 86400
    now = 400 * day
    bank.add("north", now - 300 * day, 100, "betrayal", "South broke the peace treaty and attacked our farm", 9)
    bank.add("north", now - 100 * day, 300, "fire", "A fire burned the mill", 5)
    bank.add("north", now - 1 * day, 399, "harvest", "Rich harvest on the farms", 6)
    got = bank.recall("north", "South sends a message about a peace treaty", now, k=2)
    # a betrayal from 300 days ago still comes back when the traitor writes; the old fire does not
    assert any("treaty" in m.text for m in got) and not any("fire" in m.text for m in got)
    assert bank.forget(now) == 1                                # the old I=5 fire is gone
    assert len(bank.all("north")) == 2


# ---------------------------------------------------------------------------- llm helpers

def test_parse_json_handles_fences_and_think_blocks():
    assert parse_json('<think>hmm</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(LLMError):
        parse_json("no json here")


# ---------------------------------------------------------------------------- council

def test_priorities_are_clamped_and_filtered():
    pri, dropped = clamp_priorities({"food": 8, "wood": 0.1, "dragons": 2, "stone": "x"})
    assert pri == {"food": 2.0, "wood": 0.5}
    assert len(dropped) == 2


def test_plan_check_drops_impossible_actions():
    w = world(ai=False)
    civ = w.civs[0]
    plan, dropped = check_plan({"thought": "Build!", "build_next": "Mint", "war": True,
                                "priorities": {"castle": 1.5}, "listened_to": "general"}, civ)
    assert plan.build_next is None and plan.war is False and plan.listened_to == "general"
    assert any("Mint" in d for d in dropped) and any("war" in d for d in dropped)
    plan, _ = check_plan({"build_next": "farm"}, civ)
    assert plan.build_next == "farm"


def test_advisor_weights_update():
    w = update_weights({"economist": 1 / 3, "general": 1 / 3, "diplomat": 1 / 3},
                       {"economist": 1.0, "general": 0.0, "diplomat": 0.0})
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["economist"] > w["general"] == w["diplomat"]


def test_council_with_a_scripted_model():
    def respond(system, user):
        if "You are the king" in system:
            return {"thought": "We need a granary before winter.", "priorities": {"food": 1.5, "castle": 9},
                    "build_next": "House", "war": False, "reason": "winter", "listened_to": "economist"}
        return {"proposal": "Gather food", "argument": "Winter is coming.", "priorities": {"food": 1.4},
                "urgency": 0.7}

    w = world()
    w.run(86400)
    civ = w.civs[0]
    d = Council(FakeLLM(respond)).decide(w, civ)
    assert set(d.proposals) == {"economist", "general", "diplomat"}
    assert d.plan.priorities == {"food": 1.5, "castle": 2.0}
    assert d.plan.build_next == "house"
    civ.ai.set_directive(d.plan, w.clock.t + 3600)
    assert civ.ai.gene("food") == pytest.approx(civ.genome["g_food"] * 1.5)


def test_council_is_silent_when_the_model_is_down():
    w = world()
    assert Council(FakeLLM(up=False)).decide(w, w.civs[0]) is None


# ---------------------------------------------------------------------------- rules change what the AI does

def test_a_move_rule_shifts_workers():
    w = world()
    w.run(5 * 86400)
    civ = w.civs[0]
    est = civ.ai.estimate()
    before = civ.ai.allocate(est)
    civ.ai.book = [parse_rule('RULE "stone!"\nWHEN castle >= 1\nTHEN move 2 workers to stone\nPRIORITY 1', R)]
    civ.ai.fire_rules()
    after = civ.ai.apply_moves(civ.ai.allocate(est), est)
    assert after["stone"] >= before["stone"] + 1
    assert sum(after.values()) == sum(before.values())
    for job, n in est.food_plan.items():
        assert after[job] >= n                           # the food safety rule still comes first


def test_a_gene_rule_lasts_24_hours():
    w = world()
    civ = w.civs[0]
    base = civ.ai.G("g_castle")
    civ.ai.book = [parse_rule('RULE "hurry"\nWHEN castle = 1\nTHEN multiply gene castle by 2 for 24h\nPRIORITY 1', R)]
    civ.ai.fire_rules()
    assert civ.ai.G("g_castle") == pytest.approx(2 * base)
    assert civ.ai.activations
    civ.ai.book = []                         # the rule stops firing; its effect fades after 24 h
    w.run(23 * 3600)
    assert civ.ai.G("g_castle") == pytest.approx(2 * base)
    w.run(2 * 3600)
    assert civ.ai.G("g_castle") == pytest.approx(base)


def test_rule_state_has_every_variable():
    w = world()
    s = rule_state(w, w.civs[0])
    assert s["season"] == "spring" and s["month"] == 4 and s["castle"] == 1
    assert s["days_to_winter"] == (date(2027, 12, 1) - date(2027, 4, 5)).days


# ---------------------------------------------------------------------------- chronicle

def test_event_importance_formula():
    assert chron.importance("castle", "The castle reached level 3.") == pytest.approx(10 * (1 + math.log10(4)))
    assert chron.importance("build_done", "Farm is finished.") == 3


def test_chronicle_without_ai_still_has_numbers():
    w = world()
    w.run(3 * 86400)
    ch = chron.write_chronicle(w, FakeLLM(up=False), w.clock.t - 3 * 86400)
    assert not ch.by_ai and len(ch.stories) == 3
    assert {row["kingdom"] for row in ch.table} == {"North", "South"}
    assert "THE CHRONICLE" in ch.text()


def test_chronicle_with_ai_uses_its_words_but_our_numbers():
    llm = FakeLLM(lambda s, u: {"headline": "North grows", "stories": ["a", "b", "c"], "portrait": "p"})
    w = world()
    w.run(86400)
    ch = chron.write_chronicle(w, llm, w.clock.t - 86400)
    assert ch.by_ai and ch.headline == "North grows"
    assert ch.table[0]["people"] == w.civs[0].pop


def test_lessons_with_invented_numbers_are_dropped():
    from ai.lessons import grounded
    facts = '{"winter": 0.342, "villages_that_died": 2}'
    assert grounded("Winter starts scored lowest (0.342).", facts)
    assert grounded("2 villages died: never let food run out.", facts)
    assert not grounded("Better winter preparation raises survival by 63%.", facts)


# ---------------------------------------------------------------------------- referee

def test_referee_bounds_and_diversity(tmp_path):
    balance = {}
    v = referee.review({"house": 70, "farm": 25, "well": 5}, {"house", "farm", "well", "mint"}, 0.5, balance,
                       date(2027, 5, 2))
    assert balance["building_cost"]["house"] == pytest.approx(1.05)
    assert balance["building_cost"]["mint"] == pytest.approx(0.95)
    assert "farm" not in balance["building_cost"]
    for _ in range(10):                                         # never beyond +25%
        referee.review({"house": 90, "farm": 10}, set(), 0.5, balance, date(2027, 5, 2))
    assert balance["building_cost"]["house"] == pytest.approx(1.25)
    v = referee.review({}, set(), 0.01, balance, date(2027, 5, 2))
    assert v.boost_mutation and balance["next_night"]["mutation_rate"] == 0.30
    assert v.effective.startswith("2027-05-03T07:00")           # Monday 07:00
    referee.save(v, balance, "test", data_dir=tmp_path)
    assert referee.load_balance(tmp_path / "balance.yaml")["building_cost"]["house"] == pytest.approx(1.25)


def test_balance_changes_building_costs():
    rules = type(R).load()
    assert rules.apply_balance({"building_cost": {"house": 1.2}, "effective_from": "2000-01-01T07:00"})
    assert rules.building_cost("house", 1) == {"wood": 72, "stone": 24}
    assert not rules.apply_balance({"building_cost": {"house": 2}, "effective_from": "2999-01-01T07:00"})


# ---------------------------------------------------------------------------- the rule lab, end to end

def test_rule_lab_night(tmp_path):
    proposals = {"rules": [
        'RULE "Granary first"\nWHEN castle >= 2\nTHEN build granary\nPRIORITY 0.7',
        'RULE "Bad"\nWHEN food < 5\nTHEN launch rockets\nPRIORITY 0.5',
        'RULE "Stone push"\nWHEN stone < 200\nTHEN move 1 workers to stone\nPRIORITY 0.4',
    ]}
    llm = FakeLLM(lambda s, u: proposals if "design strategy rules" in s else {"lessons": ["Keep food."]})
    store = Store(tmp_path / "lab.db")
    evo = Evolution(store, seed=2, processes=1, population=4, days=2, peer_games=1, champion_games=1, llm=llm)
    rep = evo.run_night()
    assert len(rep.proposed) == 2
    assert any("unknown action" in why for _, why in rep.invalid)        # refused, then asked again
    assert sum("design strategy rules" in s for s, _ in llm.prompts) == 3
    assert {r["games"] for r in rep.rule_results} == {8}
    never = next(r for r in rep.rule_results if r["name"] == "Granary first")  # castle 2 never comes in 2 days
    assert never["fired"] == 0 and never["wins"] == 0 and never["status"] == "rejected"
    assert all(r["status"] in ("probation", "rejected") for r in rep.rule_results)
    assert rep.lessons == ["Keep food."]
    lab = RuleLab(store.db, R)
    # a proposal is never accepted on the first night: it needs 12/16 over two nights
    assert not lab.accepted()
    rep2 = evo.run_night()
    statuses = {r["status"] for r in rep2.rule_results}
    assert statuses <= {"accepted", "rejected", "probation"}
