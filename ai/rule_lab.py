"""The night cycle of AI-written rules (design doc, version 3).

00:30  The local AI gets the champion genome, the current rules, the morning lessons and the
       champion's lost games, and proposes new rules so that 5 rules are under test.
then   Each rule plays 8 paired games: champion WITH the rule vs champion WITHOUT it, same map,
       same start, same events; sides swap at half time (4 + 4). 5 × 8 = 40 games, plus 4 shared
       control games (the same scenarios with no new rule).
       A game is won when the side with the rule scores higher than that same side did in the
       control game. (The two sides are never exactly equal, so comparing them directly would hand
       a do-nothing rule 4 wins out of 8.)
verdict 6/8 or better → probation (luck alone: 14.5%). The next night 8 more games:
       12/16 or better → accepted (luck alone: 3.8%). Otherwise rejected, and saved as rejected
       so it is never proposed again.
recheck Every 14 nights each accepted rule replays 8 games; under 4 wins → removed.
       A rule that never fired for 30 days is removed too. At most 30 rules are active.
"""
from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from engine.rules import Rules

from .genome import Genome
from .llm import LLM, LLMError
from .rulebook import (GENE_ALIASES, MAX_ACTIVE_RULES, Rule, RuleError,
                       parse_rule, split_rules, verdict)

UNDER_TEST = 5
GAMES = 8
RECHECK_EVERY = 14
RECHECK_MIN_WINS = 4
INACTIVE_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, text TEXT, signature TEXT UNIQUE, status TEXT,
    wins INTEGER DEFAULT 0, games INTEGER DEFAULT 0, activations INTEGER DEFAULT 0,
    created_night INTEGER, last_checked_night INTEGER, last_active TEXT, reason TEXT, history TEXT);
"""

PROPOSE_SYSTEM = """You design strategy rules for a medieval economy simulation. Rules nudge an AI that already
manages workers and buildings with formulas; a good rule fixes a weakness shown in the lessons or lost games.

Write rules ONLY in this exact language (4 lines each, keywords in capitals):
RULE "<short name>"
WHEN <condition> [AND|OR <condition>] [AND|OR <condition>]
THEN <one action>
PRIORITY <number between 0 and 1>

Condition: <variable> <operator> <value>. Operators: < > = <= >=
ONLY these variables exist (with the values they really take in a 60-day test game; measured):
{guide}
Values: numbers; month names (january … december) for month; spring/summer/autumn/winter for season (only "="); HH:MM for hour.
Genes are NOT variables: use them only in the "multiply gene" action, never in WHEN.
A rule only helps if its condition is true some of the time in a real game: choose thresholds inside the ranges above.
Test games last 60 days and start in a random month, so a rule about one specific month rarely fires.
Actions (exactly one):
  move N workers to <farms|wood|stone|ore|gold>     (N = 1, 2 or 3)
  build <building>          buildings: {buildings}
  upgrade tools of <craft>  crafts: farmer, lumberjack, stonecutter, iron miner, gold miner, builder
  multiply gene <gene> by <0.5 to 2> for 24h        genes: {genes}

Facts about the game: food is eaten all the time (48 per person per day); winter (Dec-Feb) halves farms and
needs 5 wood per house per day for fire; storage is 500 per resource plus granaries; the castle needs coins,
which come from taxes (2 per person per day) and the Mint (gold into coins).

Answer ONLY with JSON: {{"rules": ["RULE \\"...\\"\\nWHEN ...\\nTHEN ...\\nPRIORITY 0.6", ...]}}"""


# What each variable means and the values it really takes in a 60-day test game
# (measured: 4 games, one per season, sampled daily; p25–p75 is the typical range).
VARIABLE_GUIDE = [
    ("hour", "time of day, 0-23 (decisions are made every 5 minutes, work is 07:00-19:00)"),
    ("month", "1-12"),
    ("season", "spring, summer, autumn or winter"),
    ("days_to_winter", "days until 1 December, 0 during winter; typical 2-185"),
    ("food", "food in store; typical 450-6500, median 2000; can fall near 0 in winter; 48 eaten per person per day"),
    ("wood", "typical 185-980, median 490"),
    ("stone", "typical 35-520, median 77; often scarce"),
    ("ore", "0 until an iron mine exists (castle level 3); then 80-2400"),
    ("gold", "0 until a gold mine exists (castle level 6)"),
    ("coins", "from taxes (2 per person per day) and the mint; roughly 20-500; castles and soldiers need them"),
    ("workers", "people who can work; 2-14 early, up to 100 at castle 10"),
    ("castle_level", "1-6 in a 120-day game; armies need castle 4"),
    ("avg_happiness", "0-100; usually 70-75; below 50 means trouble"),
    ("soldiers", "0 until barracks (castle 4); then roughly 0-60"),
    ("my_power, enemy_power", "army strength (HP x damage, Lanchester); compare them with each other, e.g. my_power > enemy_power"),
    ("relation", "-100 to 100 with the other kingdom (0 at start; below -20 = tension)"),
    ("trust", "0-1, how much we trust the other kingdom (0.5 at start)"),
]


@dataclass
class LabReport:
    night: int
    proposed: list[str]
    invalid: list[tuple[str, str]]
    results: list[dict]           # {"name", "status", "wins", "games"}
    accepted: list[str]
    games: int


class RuleLab:
    def __init__(self, db: sqlite3.Connection, rules: Rules, llm: LLM | None = None):
        self.db = db
        self.rules = rules
        self.llm = llm
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ------------------------------------------------------------------ storage
    def _row_to_rule(self, row) -> Rule:
        r = parse_rule(row["text"], self.rules)
        r.id, r.status, r.wins, r.games = row["id"], row["status"], row["wins"], row["games"]
        r.activations, r.created_night = row["activations"], row["created_night"]
        r.last_checked_night, r.last_active_day = row["last_checked_night"] or 0, row["last_active"]
        return r

    def rules_with(self, *statuses: str) -> list[Rule]:
        self.db.row_factory = sqlite3.Row
        q = f"SELECT * FROM rules WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id"
        out = []
        for row in self.db.execute(q, statuses).fetchall():
            try:
                out.append(self._row_to_rule(row))
            except RuleError:
                continue
        return out

    def accepted(self) -> list[Rule]:
        return self.rules_with("accepted")[:MAX_ACTIVE_RULES]

    def known_signatures(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT signature FROM rules").fetchall()}

    def add(self, rule: Rule, night: int, status: str = "test", reason: str = "") -> int | None:
        try:
            cur = self.db.execute(
                "INSERT INTO rules(name, text, signature, status, created_night, reason, history) VALUES(?,?,?,?,?,?,?)",
                (rule.name, rule.text(), rule.signature(), status, night, reason, "[]"))
        except sqlite3.IntegrityError:
            return None
        self.db.commit()
        return cur.lastrowid

    def save(self, rule: Rule, note: str) -> None:
        hist = json.loads(self.db.execute("SELECT history FROM rules WHERE id=?", (rule.id,)).fetchone()[0] or "[]")
        hist.append(note)
        self.db.execute("UPDATE rules SET status=?, wins=?, games=?, activations=?, last_checked_night=?, "
                        "last_active=?, history=? WHERE id=?",
                        (rule.status, rule.wins, rule.games, rule.activations, rule.last_checked_night,
                         rule.last_active_day, json.dumps(hist), rule.id))
        self.db.commit()

    # ------------------------------------------------------------------ proposals
    def prompt(self) -> str:
        guide = "\n".join(f"  {name}: {meaning}" for name, meaning in VARIABLE_GUIDE)
        buildings = ", ".join(b.replace("_", " ") for b, s in self.rules.buildings.items()
                              if s.since_version <= self.rules.version)
        genes = ", ".join(sorted(set(GENE_ALIASES) | set(self.rules.gene_ranges)))
        return PROPOSE_SYSTEM.format(guide=guide, buildings=buildings, genes=genes)

    def propose(self, night: int, champion: Genome, lessons: list[str], lost_games: list[dict]) -> tuple[list[Rule], list[tuple[str, str]]]:
        """Ask the local AI for enough rules to have 5 under test. Returns (new rules, invalid ones with reasons)."""
        want = UNDER_TEST - len(self.rules_with("test", "probation"))
        if want <= 0 or self.llm is None or not self.llm.available():
            return [], []
        facts = {
            "champion_genes": {k: round(v, 2) for k, v in champion.genes.items()},
            "accepted_rules": [r.text() for r in self.accepted()],
            "rules_already_tested_do_not_repeat": [
                {"rule": text, "won": f"{wins}/{games} vs the same games without it",
                 "fired": "never (its condition was never true)" if fired == 0 else f"{fired} times"}
                for text, wins, games, fired in self.db.execute(
                    "SELECT text, wins, games, activations FROM rules WHERE status IN ('rejected','removed') "
                    "ORDER BY id DESC LIMIT 20")],
            "what_the_tests_show_so_far": "a rule must beat the champion's own formulas; rules that move workers "
                                          "away from their jobs or boost a gene all the time usually lose",
            "morning_lessons": lessons[:10],
            "champion_lost_games": lost_games[:10],
            "write_this_many_new_rules": want,
        }
        known = self.known_signatures()
        new, invalid = [], []
        for attempt in range(3):                  # refused rules go back with the reason, twice at most
            if attempt:
                facts["write_this_many_new_rules"] = want - len(new)
                facts["refused_last_time_fix_these"] = [{"rule": t, "why": why} for t, why in invalid[-6:]]
            try:
                raw = self.llm.chat_json(self.prompt(), json.dumps(facts, ensure_ascii=False), temperature=0.9)
            except LLMError:
                break
            texts = []
            for item in raw.get("rules", []):
                texts += split_rules(str(item)) or [str(item)]
            for text in texts:
                try:
                    rule = parse_rule(text, self.rules)
                except RuleError as e:
                    invalid.append((text, str(e)))
                    continue
                if rule.action.kind in ("train", "message"):
                    invalid.append((text, "armies and diplomacy arrive in version 5"))
                    continue
                if rule.signature() in known:
                    invalid.append((text, "already proposed before"))
                    continue
                rid = self.add(rule, night)
                if rid is None:
                    continue
                rule.id = rid
                known.add(rule.signature())
                new.append(rule)
                if len(new) >= want:
                    return new, invalid
        return new, invalid

    # ------------------------------------------------------------------ paired games
    def scenarios(self, night: int, seed: int, tz) -> list[tuple[int, int, float]]:
        """8 games for tonight: map seed, event seed, start (one per round, random season)."""
        rng = random.Random(seed * 7907 + night)
        out = []
        for _ in range(GAMES // 2):
            day = datetime(2027, 1, 1, 7, 0, tzinfo=tz) + timedelta(days=rng.randrange(365))
            out.append((rng.randrange(1 << 30), rng.randrange(1 << 30), day.timestamp()))
        return out

    def paired_specs(self, night: int, rule: Rule, champion: Genome, scenarios, days: int, shared: list[str]):
        from .evolution import MatchSpec

        specs = []
        for half in (0, 1):                               # sides swap at half time: 4 + 4
            for m, e, start in scenarios:
                with_rule = [*shared, rule.text()]
                a_rules, b_rules = (with_rule, shared) if half == 0 else (shared, with_rule)
                specs.append(MatchSpec(night, rule.id or 0, "rule", champion.id, champion.genes, champion.id,
                                       champion.genes, m, e, start, days, a_rules=a_rules, b_rules=b_rules))
        return specs

    def run_tests(self, night: int, champion: Genome, play_all: Callable, days: int, seed: int, tz,
                  progress: Callable[[int, int], None] | None = None) -> list[dict]:
        from .evolution import base_fitness

        testing = self.rules_with("test", "probation")
        due = [r for r in self.accepted() if night - (r.last_checked_night or r.created_night) >= RECHECK_EVERY]
        todo = testing + due
        if not todo:
            return []
        from .evolution import MatchSpec

        shared = [r.text() for r in self.accepted()]
        scen = self.scenarios(night, seed, tz)
        # Control games: the same scenarios with no extra rule. The two sides of a game are never
        # exactly equal (different people, different luck), so a rule is judged against ITS OWN SIDE
        # in the control game: a rule that changes nothing ties and earns no win.
        specs, owners, slots = [], [], []
        controls: dict[tuple, int] = {}
        for rule in todo:
            others = tuple(s for s in shared if s != rule.text())
            if others not in controls:
                controls[others] = len(specs)
                for m, e, start in scen:
                    specs.append(MatchSpec(night, 0, "control", champion.id, champion.genes, champion.id,
                                           champion.genes, m, e, start, days, a_rules=list(others),
                                           b_rules=list(others)))
                    owners.append(None)
                    slots.append(None)
            for i, spec in enumerate(self.paired_specs(night, rule, champion, scen, days, list(others))):
                specs.append(spec)
                owners.append(rule)
                slots.append(controls[others] + i % len(scen))
        results = play_all(specs, progress)
        tally: dict[int, list[int]] = {r.id: [0, 0] for r in todo}
        fired: dict[int, int] = {r.id: 0 for r in todo}
        for spec, rule, slot, res in zip(specs, owners, slots, results):
            if rule is None:
                continue
            rule_side_a = rule.text() in spec.a_rules
            k = 0 if rule_side_a else 1
            f_rule = base_fitness(res["sides"][k], self.rules)
            f_control = base_fitness(results[slot]["sides"][k], self.rules)
            tally[rule.id][0] += 1 if f_rule > f_control + 1e-9 else 0
            tally[rule.id][1] += 1
            fired[rule.id] += res["sides"][k].get("activations", {}).get(rule.text(), 0)
        out = []
        for rule in todo:
            wins, games = tally[rule.id]
            rule.activations += fired[rule.id]
            if fired[rule.id]:
                rule.last_active_day = datetime.now().date().isoformat()
            if rule.status == "accepted":
                rule.last_checked_night = night
                if wins < RECHECK_MIN_WINS:
                    rule.status = "removed"
                note = f"night {night}: recheck {wins}/{games}" + (" → removed" if rule.status == "removed" else "")
            else:
                rule.wins += wins
                rule.games += games
                rule.status = verdict(rule, GAMES)
                rule.last_checked_night = night
                note = f"night {night}: {wins}/{games} (total {rule.wins}/{rule.games}) → {rule.status}"
            self.save(rule, note)
            out.append({"id": rule.id, "name": rule.name, "status": rule.status, "wins": wins, "games": games,
                        "total_wins": rule.wins, "total_games": rule.games, "fired": fired[rule.id]})
        return out

    def retire_inactive(self, today: datetime) -> list[str]:
        """Accepted rules that never fired for 30 days leave the book."""
        gone = []
        for r in self.accepted():
            last = r.last_active_day
            if last and (today.date() - datetime.fromisoformat(last).date()).days > INACTIVE_DAYS:
                r.status = "removed"
                self.save(r, f"{today.date()}: inactive for {INACTIVE_DAYS} days → removed")
                gone.append(r.name)
        return gone
