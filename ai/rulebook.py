"""Rules written by the AI, in a small strict language (never Python).

    RULE "Winter reserve"
    WHEN month = november AND food < 5000
    THEN move 2 workers to farms
    PRIORITY 0.8

The parser accepts only the words below; anything else rejects the rule. Nothing the AI writes
is ever executed as code: a rule becomes data (conditions + one action) that the tactical layer
reads, so rules also work in fast mode without Ollama.

Testing (design doc): 8 paired games → probation at ≥ 6/8 wins; 8 more the next night → accepted
at ≥ 12/16; otherwise rejected. Every 14 nights accepted rules replay 8 games and leave below 4/8.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date

from engine.rules import Rules

MAX_CONDITIONS = 3
MAX_ACTIVE_RULES = 30
MAX_MOVE = 3
MAX_TRAIN = 5
GENE_FACTOR = (0.5, 2.0)

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]
SEASONS = {"spring", "summer", "autumn", "winter", "fall"}

VARIABLES = {
    "hour": "hour", "month": "month", "season": "season", "days_to_winter": "days_to_winter",
    "food": "food", "wood": "wood", "stone": "stone", "ore": "ore", "gold": "gold", "coins": "coins",
    "workers": "workers", "soldiers": "soldiers", "castle_level": "castle", "castle": "castle",
    "avg_happiness": "happiness", "happiness": "happiness",
    "my_power": "my_power", "enemy_power": "enemy_power",
    "relation": "relation", "relation r": "relation", "r": "relation",
    "trust": "trust", "trust t": "trust", "t": "trust",
}
VAR_TEXT = {"hour": "hour", "month": "month", "season": "season", "days_to_winter": "days_to_winter",
            "food": "food", "wood": "wood", "stone": "stone", "ore": "ore", "gold": "gold", "coins": "coins",
            "workers": "workers", "soldiers": "soldiers", "castle": "castle_level", "happiness": "avg_happiness",
            "my_power": "my_power", "enemy_power": "enemy_power", "relation": "relation", "trust": "trust"}
OPS = {"<": "<", ">": ">", "=": "=", "==": "=", "<=": "<=", ">=": ">=", "≤": "<=", "≥": ">="}

RESOURCES = {"farms": "food", "farm": "food", "food": "food", "fields": "food",
             "wood": "wood", "lumber": "wood", "forest": "wood", "stone": "stone", "quarry": "stone",
             "ore": "ore", "iron": "ore", "iron mine": "ore", "gold": "gold", "gold mine": "gold"}
RES_TEXT = {"food": "farms", "wood": "wood", "stone": "stone", "ore": "ore", "gold": "gold"}
UNITS = {"militia": "militia", "spearman": "spearman", "spearmen": "spearman", "archer": "archer",
         "archers": "archer", "swordsman": "swordsman", "swordsmen": "swordsman", "knight": "knight",
         "knights": "knight", "ram": "ram", "rams": "ram"}
CRAFTS = {"farmer": "farmer", "farmers": "farmer", "lumberjack": "lumberjack", "lumberjacks": "lumberjack",
          "stonecutter": "stonecutter", "stonecutters": "stonecutter", "iron miner": "iron_miner",
          "iron miners": "iron_miner", "gold miner": "gold_miner", "gold miners": "gold_miner",
          "builder": "builder", "builders": "builder"}
MESSAGES = {"trade_offer", "gift", "tribute_demand", "peace_treaty", "war_declaration", "free_message"}
GENE_ALIASES = {"food": "g_food", "wood": "g_wood", "stone": "g_stone", "ore": "g_ore", "gold": "g_gold",
                "houses": "g_houses", "castle": "g_castle", "tools": "g_tools", "defense": "g_defense"}


class RuleError(ValueError):
    pass


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def parse_number(s: str) -> float:
    """'0.8' → 0.8; '5000' → 5000; '5,000' → 5000."""
    s = s.strip()
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", s):
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        raise RuleError(f"not a number: '{s}'") from None


def fmt_number(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


@dataclass(frozen=True)
class Condition:
    var: str
    op: str
    value: float | str

    def holds(self, state: dict) -> bool:
        have = state.get(self.var)
        if have is None:
            return False
        if self.var == "season":
            return have == self.value
        a, b = float(have), float(self.value)
        return {"<": a < b, ">": a > b, "=": abs(a - b) < 1e-9, "<=": a <= b, ">=": a >= b}[self.op]

    def text(self) -> str:
        v = self.value
        if self.var == "month":
            v = MONTHS[int(v) - 1]
        elif self.var == "hour":
            h = float(v)
            v = f"{int(h):02d}:{round((h % 1) * 60):02d}"
        elif self.var != "season":
            v = fmt_number(float(v))
        return f"{VAR_TEXT[self.var]} {self.op} {v}"


@dataclass(frozen=True)
class RuleAction:
    kind: str            # move | build | upgrade_tool | train | gene | message
    target: str          # resource, building type, craft, unit, gene, message type
    amount: float = 0.0  # workers, units or gene factor

    def text(self) -> str:
        if self.kind == "move":
            return f"move {int(self.amount)} workers to {RES_TEXT[self.target]}"
        if self.kind == "build":
            return f"build {self.target.replace('_', ' ')}"
        if self.kind == "upgrade_tool":
            return f"upgrade tools of {self.target.replace('_', ' ')}"
        if self.kind == "train":
            return f"train {int(self.amount)} {self.target}"
        if self.kind == "gene":
            return f"multiply gene {self.target} by {fmt_number(self.amount)} for 24h"
        return f"send message {self.target}"


@dataclass
class Rule:
    name: str
    conditions: list[Condition]
    joins: list[str]          # "and" | "or" between consecutive conditions
    action: RuleAction
    priority: float
    # bookkeeping (stored in SQLite)
    id: int | None = None
    status: str = "test"      # test → probation → accepted | rejected | removed
    wins: int = 0
    games: int = 0
    activations: int = 0
    created_night: int = 0
    last_checked_night: int = 0
    last_active_day: str | None = None
    meta: dict = field(default_factory=dict)

    def holds(self, state: dict) -> bool:
        """AND binds tighter than OR."""
        groups, cur = [], [self.conditions[0].holds(state)]
        for join, cond in zip(self.joins, self.conditions[1:]):
            if join == "and":
                cur.append(cond.holds(state))
            else:
                groups.append(all(cur))
                cur = [cond.holds(state)]
        groups.append(all(cur))
        return any(groups)

    def text(self) -> str:
        cond = self.conditions[0].text()
        for join, c in zip(self.joins, self.conditions[1:]):
            cond += f" {join.upper()} {c.text()}"
        return (f'RULE "{self.name}"\nWHEN {cond}\nTHEN {self.action.text()}\n'
                f"PRIORITY {fmt_number(round(self.priority, 2))}")

    def signature(self) -> str:
        """Same logic = same rule, whatever its name (a rejected rule is never proposed twice)."""
        return norm(self.text().split("\n", 1)[1])


# ---------------------------------------------------------------------------- parser

def _parse_value(var: str, raw: str) -> float | str:
    v = norm(raw)
    if var == "month":
        if v in MONTHS:
            return float(MONTHS.index(v) + 1)
        n = parse_number(v)
        if not (1 <= n <= 12) or not n.is_integer():
            raise RuleError(f"bad month: '{raw}'")
        return n
    if var == "season":
        if v not in SEASONS:
            raise RuleError(f"unknown season: '{raw}'")
        return "autumn" if v == "fall" else v
    if var == "hour":
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", v)
        if not m or int(m.group(1)) > 23:
            raise RuleError(f"bad hour: '{raw}'")
        return int(m.group(1)) + int(m.group(2) or 0) / 60
    return parse_number(v)


def _parse_condition(text: str) -> Condition:
    m = re.fullmatch(r"(.+?)\s*(<=|>=|==|≤|≥|<|>|=)\s*(.+)", text.strip())
    if not m:
        raise RuleError(f"cannot read condition: '{text}'")
    var_raw, op, val = m.groups()
    key = norm(var_raw)
    var = VARIABLES.get(key) or VARIABLES.get(key.replace(" ", "_"))
    if var is None:
        raise RuleError(f"unknown variable: '{var_raw.strip()}'")
    if var == "season" and OPS[op] != "=":
        raise RuleError("season can only be compared with =")
    return Condition(var, OPS[op], _parse_value(var, val))


def _parse_conditions(text: str) -> tuple[list[Condition], list[str]]:
    parts = re.split(r"\s+(AND|OR)\s+", text.strip())
    conds = [_parse_condition(parts[0])]
    joins = []
    for i in range(1, len(parts), 2):
        joins.append(parts[i].lower())
        conds.append(_parse_condition(parts[i + 1]))
    if len(conds) > MAX_CONDITIONS:
        raise RuleError(f"at most {MAX_CONDITIONS} conditions")
    return conds, joins


def _lookup_building(name: str, rules: Rules) -> str:
    n = norm(name).removeprefix("a ").removeprefix("an ").replace(" ", "_")
    for cand in (n, n.removesuffix("s")):
        spec = rules.buildings.get(cand)
        if spec is not None and spec.since_version <= rules.version:
            return cand
    raise RuleError(f"unknown building: '{name}'")


def _lookup_craft(name: str) -> str:
    n = norm(name)
    n = re.sub(r"^(the )?(tools?)( of| for)?\s*", "", n)
    if n in CRAFTS:
        return CRAFTS[n]
    raise RuleError(f"unknown craft: '{name}'")


def _parse_action(text: str, rules: Rules) -> RuleAction:
    t = norm(text)
    if m := re.fullmatch(r"move (\d+) (?:workers?|people) to (.+)", t):
        n = int(m.group(1))
        res = RESOURCES.get(m.group(2))
        if res is None:
            raise RuleError(f"unknown resource: '{m.group(2)}'")
        if not 1 <= n <= MAX_MOVE:
            raise RuleError(f"can move 1–{MAX_MOVE} workers")
        return RuleAction("move", res, n)
    if m := re.fullmatch(r"build (.+)", t):
        return RuleAction("build", _lookup_building(m.group(1), rules))
    if m := re.fullmatch(r"upgrade (.+)", t):
        return RuleAction("upgrade_tool", _lookup_craft(m.group(1)))
    if m := re.fullmatch(r"train (\d+) (.+)", t):
        n = int(m.group(1))
        unit = UNITS.get(m.group(2))
        if unit is None:
            raise RuleError(f"unknown unit: '{m.group(2)}'")
        if not 1 <= n <= MAX_TRAIN:
            raise RuleError(f"can train 1–{MAX_TRAIN} units")
        return RuleAction("train", unit, n)
    if m := re.fullmatch(r"multiply gene (\w+) by ([\d.]+) for 24 ?(?:h|hours)", t):
        gene = GENE_ALIASES.get(m.group(1), m.group(1))
        if gene not in rules.gene_ranges:
            raise RuleError(f"unknown gene: '{m.group(1)}'")
        f = parse_number(m.group(2))
        if not GENE_FACTOR[0] <= f <= GENE_FACTOR[1]:
            raise RuleError("a gene factor is between 0.5 and 2")
        return RuleAction("gene", gene, f)
    if m := re.fullmatch(r"send message (\w+)", t):
        if m.group(1) not in MESSAGES:
            raise RuleError(f"unknown message type: '{m.group(1)}'")
        return RuleAction("message", m.group(1))
    raise RuleError(f"unknown action: '{text.strip()}'")


def parse_rule(text: str, rules: Rules) -> Rule:
    """Parse one rule. Raises RuleError with the reason (shown in the rulebook)."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) != 4:
        raise RuleError("a rule has exactly 4 lines: RULE, WHEN, THEN, PRIORITY")
    heads, bodies = [], []
    for ln in lines:
        m = re.match(r"(\S+)\s+(.*)$", ln)
        if not m:
            raise RuleError(f"empty line: '{ln}'")
        heads.append(m.group(1))
        bodies.append(m.group(2))
    if heads != ["RULE", "WHEN", "THEN", "PRIORITY"]:
        raise RuleError("lines must start with RULE, WHEN, THEN, PRIORITY (in capitals)")
    name = bodies[0].strip().strip('"“”\'').strip()
    if not name or len(name) > 60:
        raise RuleError("the rule name is missing or too long")
    conds, joins = _parse_conditions(bodies[1])
    action = _parse_action(bodies[2], rules)
    prio = parse_number(bodies[3])
    if not 0 <= prio <= 1:
        raise RuleError("priority is between 0 and 1")
    return Rule(name, conds, joins, action, prio)


def split_rules(text: str) -> list[str]:
    """Cut a block of text into rule texts (each starts with RULE)."""
    chunks = re.split(r"(?=^\s*RULE\b)", text, flags=re.M)
    return [c.strip() for c in chunks if c.strip().startswith("RULE")]


# ---------------------------------------------------------------------------- statistics

def p_at_least(k: int, n: int) -> float:
    """Chance that a rule with no effect wins k or more of n games by luck: Σ C(n,i) / 2^n."""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n


P_PROBATION = 37 / 256        # 6 of 8 by luck alone (the doc's probation)
P_ACCEPT = 2517 / 65536       # 12 of 16 by luck alone (the doc's acceptance)
MAX_GAMES = 32                # improvements §6: a promising rule may be tested up to 4 nights


def verdict(rule: Rule, games_per_night: int = 8) -> str:
    """Next status after a night of paired games. The doc's thresholds, as odds of winning by luck:
    probation at 6/8 (14.5%), accepted at 12/16 (3.8%). Improvements §6: a rule that wins at least half
    gets a second night of tests, and one on probation that wins 60% may keep going up to 32 games."""
    n, k = rule.games, rule.wins
    if n <= 0:
        return rule.status
    if rule.status == "test":
        if n >= games_per_night and p_at_least(k, n) <= P_PROBATION + 1e-12:
            return "probation"
        if n < 2 * games_per_night and k >= n / 2:
            return "test"
        return "rejected"
    if rule.status == "probation":
        if p_at_least(k, n) <= P_ACCEPT + 1e-12:
            return "accepted"
        if n < MAX_GAMES and k / n >= 0.6:
            return "probation"
        return "rejected"
    return rule.status


# ---------------------------------------------------------------------------- what rules can see

def days_to_winter(d: date) -> int:
    if d.month in (12, 1, 2):
        return 0
    return (date(d.year, 12, 1) - d).days


def rule_state(world, civ) -> dict:
    """Everything a rule may test, as plain values."""
    local = world.clock.local
    others = [c for c in world.civs if c is not civ]
    people = civ.people
    return {
        "hour": local.hour + local.minute / 60,
        "month": float(local.month),
        "season": world.clock.season.name,
        "days_to_winter": float(days_to_winter(local.date())),
        **{r: float(v) for r, v in civ.stock.items()},
        "workers": float(len(civ.workers(world.clock.t))),
        "soldiers": float(len(civ.soldiers)),
        "castle": float(civ.castle_level),
        "happiness": sum(p.happiness for p in people) / len(people) if people else 0.0,
        "my_power": float(world.military.strength(civ.soldiers, civ)),
        "enemy_power": float(civ.ai.army.enemy_strength(others[0])) if others and civ.ai is not None else 0.0,
        "relation": float(world.diplomacy.rel(civ.id, others[0].id)) if others else 0.0,
        "trust": float(world.diplomacy.trust(civ.id, others[0].id)) if others else 0.5,
    }
