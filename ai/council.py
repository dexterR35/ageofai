"""The king's council (Ollama): three advisors propose, the king decides.

Every 30 minutes, and on big events (attack, famine, new castle level):
  1. Economist, General and Diplomat get the same summary (+ their own extras) and each answer
     with a proposal, a short argument, priorities and an urgency (0–1).
  2. The king gets the three proposals, the 8 most relevant memories and how much he trusts
     each advisor, and answers with the plan + "reason" + "listened_to".
  3. Code checks the plan: impossible actions are dropped, numbers are clamped.

The plan nudges the tactical layer (gene multipliers 0.5–2 and a building to favour) and decides war
and peace ("war": attack | defend | seek_peace | none, with a target and a reason). The code honours a
war intent only within the game's rules: nobody attacks in the first 14 days, an attack needs a minimum
army, and breaking a treaty is allowed but costs trust and relation (engine/diplomacy.py). It never
bypasses the game's limits. If Ollama is down, the tactical layer simply runs on its genome.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .llm import LLM, LLMError
from engine import development

if TYPE_CHECKING:
    from engine.civ import Civilization
    from engine.world import World
    from .memory import MemoryBank

ROLES = ("economist", "general", "diplomat")
PRIORITY_KEYS = ("food", "wood", "stone", "ore", "gold", "houses", "castle", "tools", "defense", "safety")
FACTOR = (0.5, 2.0)
WAR_INTENTS = ("attack", "defend", "seek_peace", "none")
WAR_ALIASES = {"true": "attack", "war": "attack", "yes": "attack", "peace": "seek_peace", "seek peace": "seek_peace",
               "false": "none", "no": "none", "0": "none", "null": "none", "": "none", "hold": "defend"}
# journal kinds the king hears about (refused tributes, broken promises, caught spies, attacks, treaties…)
DIPLOMACY_KINDS = ("message", "promise", "spy_caught", "treaty", "attack", "battle", "battle_end", "loot", "fall",
                   "retreat", "muster", "wonder")

# Each advisor gets its own area of FACTS, never advice: the models learn what works from what happens
# (their memories, the night's lessons, the scoreboard), not from us.
ADVISOR_BRIEF = {
    "economist": "You are the Economist. Your facts: resources, storage, food eaten, buildings, the castle's "
                 "next level and what it still needs.",
    "general": "You are the General. Your facts: our army, towers, what spies or rumours say about the enemy "
               "(spies ±20%, rumours ±50%) and the strength ratio.",
    "diplomat": "You are the Diplomat. Your facts: relation R, trust T, messages, trades and treaties. "
                "Rules: a broken promise lowers the other side's trust; an attack lowers relation.",
}

ADVISOR_SYSTEM = """{brief}
You advise the king of a small medieval kingdom in a simulation. Answer ONLY with JSON:
{{"proposal": "<one short action>", "argument": "<at most 2 sentences>",
  "priorities": {{"<one of: {keys}>": <number 0.5-2>, ...}}, "urgency": <number 0-1>}}
Priorities above 1 mean "more of this", below 1 mean "less". Use only the listed keys. English only."""

KING_SYSTEM = """You are the king of a small medieval kingdom in a simulation. Your three advisors made proposals.
Decide. Nobody tells you how to play: your memories and lessons are what happened before.
Answer ONLY with JSON:
{{"thought": "<1-2 sentences, what you decided and why, in plain English>",
  "priorities": {{"<one of: {keys}>": <number 0.5-2>, ...}},
  "build_next": "<one building name from the allowed list, or null>",
  "war": "<attack|defend|seek_peace|none>",
  "target": "<the other kingdom's name, or null>",
  "war_reason": "<one sentence: why war or peace now, or empty>",
  "reason": "<one sentence>",
  "listened_to": "<economist|general|diplomat>"}}
War and peace are YOUR decision: "attack" sends the army against the target now; "defend" keeps it home and
builds defenses; "seek_peace" offers the target a peace treaty; "none" lets the army follow the kingdom's own
attack threshold (the strength ratio it attacks above).
Look at the "diplomacy" facts (relation, trust, treaties, refused demands, broken promises, caught spies,
attacks). Game rules: nobody may attack in the first {peace_days} days; an attack needs at least {min_army}
soldiers; you may break a treaty, but it costs trust and relation. A lost war can destroy your castle.
Allowed buildings right now: {buildings}."""


@dataclass
class Proposal:
    role: str
    proposal: str
    argument: str
    priorities: dict[str, float]
    urgency: float


@dataclass
class Plan:
    thought: str
    priorities: dict[str, float]
    build_next: str | None
    war: bool                        # True = the king orders an attack (intent "attack" on `target`)
    reason: str
    listened_to: str | None
    intent: str = "none"             # attack | defend | seek_peace | none
    target: str | None = None        # the other kingdom's id
    war_reason: str = ""
    war_done: bool = False           # the attack order was given (once per plan)
    invention: dict | None = None
    appointments: dict | None = None
    culture: dict | None = None


@dataclass
class Decision:
    t: float
    civ: str
    summary: dict
    proposals: dict[str, Proposal]
    plan: Plan
    seconds: float
    dropped: list[str] = field(default_factory=list)   # what the code refused, and why


# ---------------------------------------------------------------------------- validation

def clamp_priorities(raw: object) -> tuple[dict[str, float], list[str]]:
    out, dropped = {}, []
    if not isinstance(raw, dict):
        return out, ["priorities were not an object"]
    for k, v in raw.items():
        key = str(k).lower().strip()
        if key not in PRIORITY_KEYS:
            dropped.append(f"unknown priority '{k}'")
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            dropped.append(f"priority '{k}' is not a number")
            continue
        out[key] = min(FACTOR[1], max(FACTOR[0], f))
    return out, dropped


def allowed_buildings(civ: "Civilization") -> dict[str, str]:
    """name (lower-case) → building type, for buildings the civ may start now."""
    r = civ.rules
    out = {}
    for btype, spec in r.buildings.items():
        if spec.since_version > r.version or spec.castle > civ.castle_level:
            continue
        if not development.unlocked(civ, btype):
            continue
        if spec.max is not None and len(civ.of_type(btype, built_only=False)) >= spec.max:
            continue
        out[spec.label.lower()] = btype
        out[btype.replace("_", " ")] = btype
    return out


def parse_intent(raw: dict) -> str | None:
    """The king's "war" field: one of WAR_INTENTS (old answers said true/false). None = not understood."""
    v = raw.get("war")
    if isinstance(v, bool):
        return "attack" if v else "none"
    key = " ".join(str(v if v is not None else "").lower().replace("-", " ").replace("_", " ").split())
    key = WAR_ALIASES.get(key, key.replace(" ", "_"))
    return key if key in WAR_INTENTS else None


def resolve_target(raw_target: object, civ: "Civilization", world: "World | None") -> str | None:
    """The other kingdom the king means (by name or id); with one neighbour, that one."""
    if world is None:
        return None
    others = [c for c in world.civs if c is not civ and c.alive]
    want = str(raw_target or "").lower().strip()
    for o in others:
        if want and want in (o.id.lower(), o.name.lower()):
            return o.id
    return others[0].id if len(others) == 1 else None


def check_plan(raw: dict, civ: "Civilization", world: "World | None" = None) -> tuple[Plan, list[str]]:
    world = world if world is not None else getattr(civ.ai, "w", None)
    pri, dropped = clamp_priorities(raw.get("priorities", {}))
    build = raw.get("build_next")
    btype = None
    if isinstance(build, str) and build.strip() and build.strip().lower() not in ("null", "none"):
        btype = allowed_buildings(civ).get(build.strip().lower())
        if btype is None:
            dropped.append(f"cannot build '{build}' now")
    intent = parse_intent(raw)
    if intent is None:
        dropped.append(f"unknown war intent {raw.get('war')!r}")
        intent = "none"
    target = resolve_target(raw.get("target"), civ, world) if intent in ("attack", "seek_peace") else None
    if intent in ("attack", "seek_peace") and target is None and world is not None:
        dropped.append(f"no kingdom to {intent.replace('_', ' ')}: {raw.get('target')!r}")
        intent = "none"
    min_army = int(civ.rules.war.get("min_army", 5))
    if intent == "attack" and len(civ.soldiers) < min_army:
        # the king wants war but has no army: arm first (the next council may order the attack)
        dropped.append(f"war needs an army: we have {len(civ.soldiers)} soldiers, at least {min_army} are needed")
        intent = "defend"
    if intent == "attack" and world is not None and world.diplomacy.peace_period():
        left = (world.start_t + float(civ.rules.war.get("peace_days", 14)) * 86400 - world.clock.t) / 86400
        dropped.append(f"no attacks in the first {civ.rules.war.get('peace_days', 14)} days "
                       f"({left:.1f} days left): the army waits")
    if intent == "defend":
        pri["defense"] = max(pri.get("defense", 1.0), 1.5)
    listened = str(raw.get("listened_to", "")).lower().strip()
    return Plan(
        thought=str(raw.get("thought", "")).strip()[:300],
        priorities=pri,
        build_next=btype,
        war=intent == "attack",
        reason=str(raw.get("reason", "")).strip()[:300],
        listened_to=listened if listened in ROLES else None,
        intent=intent,
        target=target,
        war_reason=str(raw.get("war_reason", "")).strip()[:300],
        invention=raw.get("research") if isinstance(raw.get("research"), dict) else None,
        appointments=raw.get("appointments") if isinstance(raw.get("appointments"), dict) else None,
        culture=raw.get("culture") if isinstance(raw.get("culture"), dict) else None,
    ), dropped


def update_weights(weights: dict[str, float], success: dict[str, float]) -> dict[str, float]:
    """w_r ← (0.9·w_r + 0.1·success_r) / Σ (0.9·w_s + 0.1·success_s)."""
    raw = {r: 0.9 * weights.get(r, 1 / 3) + 0.1 * success.get(r, 0.0) for r in ROLES}
    total = sum(raw.values()) or 1.0
    return {r: v / total for r, v in raw.items()}


# ---------------------------------------------------------------------------- the council

def requirement_text(req: dict, civ: "Civilization") -> str:
    r = civ.rules
    if "building" in req:
        label = r.buildings[req["building"]].label
        lvl = f" (level {req['level']})" if req.get("level", 1) > 1 else ""
        return f"{req.get('count', 1)} × {label}{lvl}"
    if "workers" in req:
        return f"{req['workers']} workers"
    if "soldiers" in req:
        return f"{req['soldiers']} soldiers"
    if "tools" in req:
        return f"tools level {req['level']} for {req['tools']} crafts"
    if "tools_all" in req:
        return f"tools level {req['tools_all']} for every craft"
    if "economic_all" in req:
        return f"every economic building at level {req['economic_all']}"
    return json.dumps(req)


class Council:
    def __init__(self, llm: LLM, memory: "MemoryBank | None" = None, weights: dict[str, float] | None = None):
        self.llm = llm
        self.memory = memory
        self.weights = dict(weights or {r: 1 / 3 for r in ROLES})
        self.history: list[Decision] = []

    # ------------------------------------------------------------------ summary
    def summary(self, world: "World", civ: "Civilization", lessons: list[str] | None = None) -> dict:
        now = world.clock.t
        L = civ.castle_level
        missing = [requirement_text(q, civ) for q in civ.missing_requirements(L + 1, now)] \
            if L < civ.rules.castle_max_level else []
        others = [c for c in world.civs if c is not civ and c.alive]
        cx, cy = civ.castle.center
        return {
            "time": world.clock.hhmm(),
            "day": world.day,
            "season": world.clock.season.name,
            "castle": L,
            "workers": len(civ.workers(now)),
            "population": civ.pop,
            "soldiers": len(civ.soldiers),
            "morale": round(civ.morale, 2),
            "stock": {k: round(v) for k, v in civ.stock.items()},
            "storage_cap": {k: round(civ.cap(k)) for k in ("food", "wood", "stone")},
            f"missing_for_castle_{L + 1}": missing,
            "neighbors": [{"name": o.name, "castle": o.castle_level, "population": o.pop,
                           "distance": round(((o.castle.center[0] - cx) ** 2 + (o.castle.center[1] - cy) ** 2) ** 0.5)}
                          for o in others],
            "lessons": list(lessons or [])[:10],
            "diplomacy": self.diplomacy_facts(world, civ),
            **({"development": development.snapshot(civ)} if development.enabled(civ) else {}),
        }

    @staticmethod
    def diplomacy_facts(world: "World", civ: "Civilization", hours: float = 48, limit: int = 10) -> dict:
        """What the king knows about the neighbours: relation, trust, treaties, war, and what happened
        lately (refused tributes, broken promises, caught spies, attacks), so conflicts can lead to war."""
        dip, now, r = world.diplomacy, world.clock.t, civ.rules
        others = [o for o in world.civs if o is not civ and o.alive]
        ids = {civ.id, *(o.id for o in others)}
        peace_left = (world.start_t + float(r.war.get("peace_days", 14)) * 86400 - now) / 86400
        rel = {}
        for o in others:
            R = dip.rel(civ.id, o.id)
            state = dip.state(civ.id, o.id)
            until = dip.treaty_until.get(dip.pair(civ.id, o.id), 0.0)
            rel[o.name] = {"R": round(R), "state": state, "at_war": dip.at_war(civ.id, o.id),
                           "treaty_days_left": round(max(0.0, until - now) / 86400, 1),
                           "our_trust_in_them": round(dip.trust(civ.id, o.id), 2),
                           "their_trust_in_us": round(dip.trust(o.id, civ.id), 2),
                           "our_strength_vs_theirs": round(civ.ai.army.ratio(o), 2) if civ.ai else None}
            if R <= -20:
                rel[o.name]["warning"] = f"relations with {o.name} are {state} (R {R:.0f})"
        since = now - hours * 3600
        events = [f"day {e.day}: {e.text}" for e in world.journal.events
                  if e.t >= since and e.kind in DIPLOMACY_KINDS and (e.civ_id in ids or e.civ_id is None)][-limit:]
        from . import lookahead
        sim = lookahead.last_for(world, civ)
        if sim:                                             # the last simulated futures (scores: ours minus theirs)
            events.append(f"simulation {(now - sim['t']) / 3600:.0f} h ago against {sim['against']}: attacking scored "
                          f"{sim['attack']:+.3f}, waiting {sim['wait']:+.3f}")
        return {"peace_period_days_left": round(max(0.0, peace_left), 1),
                "our_soldiers": len(civ.soldiers), "min_army_to_attack": int(r.war.get("min_army", 5)),
                "relations": rel, "events": events}

    def extras(self, role: str, world: "World", civ: "Civilization") -> dict:
        if role == "economist":
            est = civ.ai.estimate() if civ.ai else None
            out = {"next_castle_cost": civ.rules.castle_cost(civ.castle_level)}
            if est:
                out.update(food_eaten_per_day=round(est.food_day),
                           winter_food_reserve_kept_by_the_tactical_layer=round(est.winter_food),
                           winter_wood_reserve_kept_by_the_tactical_layer=round(est.winter_wood))
            return out
        others = [o for o in world.civs if o is not civ and o.alive]
        if role == "general":
            mine: dict[str, int] = {}
            for u in civ.soldiers:
                mine[u.type] = mine.get(u.type, 0) + 1
            seen = {}
            for o in others:
                rep = civ.intel.get(o.id, {})
                age = (world.clock.t - rep["t"]) / 3600 if rep else None
                seen[o.name] = {"soldiers": rep.get("soldiers"), "units": rep.get("units"),
                                "source": rep.get("source", "nothing"),
                                "hours_old": round(age, 1) if age is not None else None,
                                "error": "±20%" if rep.get("source") == "spy" else "±50%"}
            return {"our_army": mine, "towers": len(civ.of_type("tower")), "enemy_seen": seen,
                    "peace_period": world.diplomacy.peace_period(),
                    "attack_threshold": round(civ.genome.get("theta_attack", 2.0), 2),
                    "our_strength_vs_enemy": {o.name: round(civ.ai.army.ratio(o), 2) for o in others} if civ.ai else {}}
        dip = world.diplomacy
        return {"relation": {o.name: {"R": round(dip.rel(civ.id, o.id)), "state": dip.state(civ.id, o.id),
                                      "our_trust_in_them": round(dip.trust(civ.id, o.id), 2),
                                      "treaty": dip.treaty(civ.id, o.id), "at_war": dip.at_war(civ.id, o.id)}
                             for o in others},
                "messages": [f"{m.src} → {m.dst} ({m.type}, {m.status}): {m.text}"
                             for o in others for m in dip.recent(civ.id, o.id, 6)]}

    # ------------------------------------------------------------------ calls
    def ask_advisor(self, role: str, summary: dict, extra: dict) -> Proposal | None:
        system = ADVISOR_SYSTEM.format(brief=ADVISOR_BRIEF[role], keys=", ".join(PRIORITY_KEYS))
        user = json.dumps({**summary, "your_extra_information": extra}, ensure_ascii=False)
        try:
            raw = self.llm.chat_json(system, user)
        except LLMError:
            return None
        pri, _ = clamp_priorities(raw.get("priorities", {}))
        try:
            urgency = min(1.0, max(0.0, float(raw.get("urgency", 0.5))))
        except (TypeError, ValueError):
            urgency = 0.5
        return Proposal(role, str(raw.get("proposal", ""))[:200], str(raw.get("argument", ""))[:300], pri, urgency)

    # Three steps so the live world never waits for the GPU:
    #   prepare (main thread, reads the world) → run (any thread, only AI calls) → finish (main thread).
    def prepare(self, world: "World", civ: "Civilization", lessons: list[str] | None = None) -> dict:
        summary = self.summary(world, civ, lessons)
        rows, situation = [], ""
        if self.memory is not None:
            # SQLite is read here; the situation's vector (a call to the local AI) is made in run()
            situation = " ".join(summary[f"missing_for_castle_{civ.castle_level + 1}"]) + f" {summary['season']}"
            rows = self.memory.all(civ.id)
        allowed = sorted({v for v in allowed_buildings(civ).values()})
        return {"t": world.clock.t, "civ": civ.id, "summary": summary,
                "extras": {role: self.extras(role, world, civ) for role in ROLES},
                "memories": [], "memory_rows": rows,
                "situation": situation, "buildings": [civ.rules.buildings[b].label for b in allowed],
                "peace_days": civ.rules.war.get("peace_days", 14), "min_army": int(civ.rules.war.get("min_army", 5))}

    def _recall(self, ctx: dict) -> list[str]:
        """The best memories for the situation (any thread: no SQLite)."""
        if self.memory is None or not ctx.get("memory_rows"):
            return list(ctx.get("memories", []))
        vec = self.memory.embed_many([ctx["situation"]])[0]
        return [m.text for m in self.memory.rank(ctx["memory_rows"], vec, ctx["t"])]

    def run(self, ctx: dict) -> dict | None:
        """Only AI calls, no game state: safe in a background thread."""
        t0 = time.perf_counter()
        ctx["memories"] = self._recall(ctx)
        proposals = {}
        for role in ROLES:
            p = self.ask_advisor(role, ctx["summary"], ctx["extras"][role])
            if p is not None:
                proposals[role] = p
        if not proposals:
            return None
        system = KING_SYSTEM.format(keys=", ".join(PRIORITY_KEYS),
                                    buildings=", ".join(ctx["buildings"]) or "none",
                                    peace_days=ctx.get("peace_days", 14), min_army=ctx.get("min_army", 5))
        if ctx["summary"].get("development"):
            system = system.replace("king of a small medieval kingdom", "leader of a developing civilization")
            system += ("\nYou may propose an experiment by adding a research object to your JSON: "
                       '{"name":"a short invention name","field":"one allowed field","method":"process|tools|logistics","approach":"what to test"}. '
                       "Allowed fields and past successes/failures appear in development. You may return research:null. "
                       "The engine sets the cost, time and bounded effects. Experiments can fail; their costs are spent. "
                       "Government, economy and research offices are held by named citizens, reviewed over time. "
                       'You may add "appointments":{"economy":citizen_id,"research":citizen_id,"government":citizen_id} '
                       "to choose different available adults from the listed candidates. Each person may hold one office. "
                       'Once a shrine or tavern exists you may add "culture":{"tradition":"name","principle":"belief","verse":"short poem"}. '
                       "Create fictional traditions for this world rather than adopting real-world religions. "
                       "Choose from the facts; no research field is prescribed.")
        user = json.dumps({"summary": ctx["summary"],
                           "proposals": {r: p.__dict__ for r, p in proposals.items()},
                           "trust_in_advisors": {r: round(w, 2) for r, w in self.weights.items()},
                           "memories": ctx["memories"]}, ensure_ascii=False)
        try:
            raw = self.llm.chat_json(system, user)
        except LLMError:
            return None
        return {"raw": raw, "proposals": proposals, "seconds": time.perf_counter() - t0}

    def finish(self, ctx: dict, result: dict, civ: "Civilization") -> Decision:
        """Check the king's answer against the game as it is now."""
        plan, dropped = check_plan(result["raw"], civ, getattr(civ.ai, "w", None))
        d = Decision(ctx["t"], civ.id, ctx["summary"], result["proposals"], plan, result["seconds"], dropped)
        self.history.append(d)
        return d

    def decide(self, world: "World", civ: "Civilization", lessons: list[str] | None = None) -> Decision | None:
        if not self.llm.available():
            return None
        ctx = self.prepare(world, civ, lessons)
        result = self.run(ctx)
        return self.finish(ctx, result, civ) if result else None
