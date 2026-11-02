"""What the browser sees: the map once, then one small frame per second.

Everything here runs on the simulation thread (it reads the world while nothing moves) and returns
plain JSON-ready dicts. The page only watches; nothing here changes the game.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime

from engine import formulas as F
from engine.entities import Building, Person, State
from engine.units import count_types
from engine import development

NODE_KINDS = ["tree", "bush", "animal", "stone", "iron", "gold"]
STATES = {State.IDLE: "idle", State.WALK: "walk", State.GATHER: "gather", State.BUILD: "build", State.REST: "rest"}
BADGES = {"green": 0, "yellow": 1, "red": 2}
QUANT = 20                       # node amounts are sent in 1/20 steps (enough for the picture)


def _q(node) -> int:
    if node.max_amount <= 0:
        return 0
    return max(0, min(QUANT, round(QUANT * node.amount / node.max_amount)))


def map_payload(world) -> dict:
    """Sent once per page load: terrain, every resource node, the civilizations."""
    m = world.map
    return {
        "size": m.size,
        "terrain": base64.b64encode(bytes(m.terrain)).decode("ascii"),
        "kinds": NODE_KINDS,
        "nodes": [[n.id, NODE_KINDS.index(n.kind) if n.kind in NODE_KINDS else 0, n.x, n.y, n.size, _q(n)]
                  for n in m.nodes],
        "civs": [{"id": c.id, "name": c.name, "color": c.color} for c in world.civs],
        "labels": {k: s.label for k, s in world.rules.buildings.items()} | {"castle": world.rules.castle_spec.label},
        "units": {k: u.label for k, u in world.rules.units.items()} | {"spy": "Spy", "scout": "Scout"},
        "roads": base64.b64encode(bytes(m.road)).decode("ascii"),
        "camp": [world.bandits.x, world.bandits.y] if world.bandits is not None else None,
        "fog": {cid: base64.b64encode(bytes(b)).decode("ascii") for cid, b in world.fog.seen.items()}
        if world.fog.on else {},
        "techs": {k: {"label": t["label"], "effect": t["effect"]} for k, t in world.rules.tech.items()},
        "resources": list(world.civs[0].stock) if world.civs else [],
        "tz": str(world.clock.tz),
    }


class FrameMaker:
    """Builds one frame per call and remembers what it already sent (new events, thoughts, node changes)."""

    def __init__(self, world, journal_lines: int = 60, thoughts_lines: int = 30, own_effects: bool = True):
        self.w = world
        self.own_effects = own_effects            # the page's maker takes the hits; the timelapse recorder doesn't
        self.models: dict[str, str] = {}          # civ id → the model its king thinks with (set by the runner)
        self.pacing: dict = {}                   # current runtime speed and AI waiting status
        self.seq = 0
        self.journal_lines = journal_lines
        self.thoughts_lines = thoughts_lines
        self.node_q = [_q(n) for n in world.map.nodes]
        self.events_seen = world.journal.count
        self.thought_t = {c.id: -1.0 for c in world.civs}
        self.last_text: dict[str, str] = {}
        self.comments: list[dict] = []            # filled by the commentator, sent once
        self.msg_seen = max((m.id for m in world.diplomacy.messages), default=0)   # v5: messages already sent
        self.roads_seen = len(world.map.road_log)                                  # v7
        self.fog_sent: dict[str, int] = {}
        self.history_events: list[dict] = [event_row(e, world.clock.tz) for e in world.journal.events[-journal_lines:]]
        self.history_thoughts: list[dict] = []

    # ------------------------------------------------------------------ the frame
    def frame(self) -> dict:
        w = self.w
        tz = w.clock.tz
        self.seq += 1
        local = w.clock.local
        nodes = []
        for n in w.map.nodes:
            q = _q(n)
            if q != self.node_q[n.id]:
                self.node_q[n.id] = q
                nodes.append([n.id, q])
        events = self._new_events(tz)
        thoughts = self._new_thoughts(tz)
        comments, self.comments = self.comments, []
        return {
            "type": "frame",
            "seq": self.seq,
            "t": w.clock.t,
            "clock": {
                "iso": local.isoformat(timespec="seconds"),
                "hhmm": local.strftime("%H:%M"),
                "hour": local.hour + local.minute / 60 + local.second / 3600,
                "month": local.month,
                "date": local.strftime("%a %d %b %Y"),
                "day": w.day,
                "phase": w.clock.phase.name.lower(),
                "season": w.clock.season.name.title(),
                "working": w.clock.working,
                **self.pacing,
            },
            "era": {"n": getattr(w, "era", 1), "winner": getattr(w, "era_winner", None),
                    "new_in": round(w.new_era_at - w.clock.t) if getattr(w, "new_era_at", None) else None},
            "civs": [self._civ(c) for c in w.civs],
            "people": [self._person(i, p) for i, c in enumerate(w.civs) for p in c.people],
            "buildings": [self._building(i, c, b) for i, c in enumerate(w.civs) for b in c.buildings],
            "fires": sum(1 for c in w.civs for b in c.buildings if b.on_fire),
            "units": [self._unit(i, u) for i, c in enumerate(w.civs) for u in c.units]
            + ([self._unit(-1, u) for u in w.bandits.units] if w.bandits is not None else []),
            "roads": self._new_roads(),
            "fog": self._fog(),
            "carts": [[k.id, self._civ_index(k.src), round(k.x, 2), round(k.y, 2)] for k in w.trade.carts],
            "effects": self._effects(),
            "diplomacy": self._diplomacy(),
            "messages": self._new_messages(),
            "nodes": nodes,
            "events": events,
            "thoughts": thoughts,
            "comments": comments,
        }

    def hello(self) -> dict:
        """For a page that just connected: the recent journal and thoughts, so the panels aren't empty."""
        tz = self.w.clock.tz
        msgs = [m.as_dict() | {"hhmm": datetime.fromtimestamp(m.t, tz).strftime("%H:%M")}
                for m in self.w.diplomacy.messages[-60:]]
        return {"type": "hello", "events": self.history_events[-self.journal_lines:],
                "thoughts": self.history_thoughts[-self.thoughts_lines:], "messages": msgs}

    # ------------------------------------------------------------------ parts
    def _civ(self, c) -> dict:
        r = self.w.rules
        now = self.w.clock.t
        happy = [p.happiness for p in c.people]
        return {
            "id": c.id, "name": c.name, "color": c.color, "alive": c.alive, "mind": c.genome.id,
            "model": self.models.get(c.id) or None,
            "castle": c.castle_level, "age": r.age_name(c.castle_level),
            "pop": c.pop, "workers": len(c.workers(now)), "beds": c.beds(),
            "morale": round(c.morale, 2), "happiness": round(sum(happy) / len(happy)) if happy else 0,
            "stock": {k: round(v) for k, v in c.stock.items()},
            "cap": {k: round(c.cap(k)) for k in c.stock},
            "tools": dict(c.tools),
            "research": [{"craft": x["craft"], "level": x["level"], "progress": round(x.get("progress", 0), 3)}
                         for x in c.research],
            "recruiting": [round(x, 3) for x in c.recruiting],
            "buildings": sum(1 for b in c.buildings if b.built),
            "gathered": round(c.total_gathered()),
            "army": len(c.soldiers),
            "army_by_type": self._army(c),
            "spies": len(c.spies),
            "training": [t["type"] for t in c.training],
            "kills": c.kills, "losses": c.losses,
            "weapons": c.tool_level("weapons"), "armor": c.tool_level("armor"),
            "generation": c.generation,
            "rebirth_in": round(c.rebirth_at - self.w.clock.t) if c.rebirth_at and not c.alive else None,
            "attacking": any(u.order == "attack" for u in c.soldiers),
            "gathering": bool(getattr(c, "muster", None)),
            "techs": sorted(c.techs),
            "tech_job": {"tech": c.tech_job["tech"], "progress": round(c.tech_job["progress"], 3)} if c.tech_job else None,
            "carts": c.carts,
            "road": {"done": c.road_job["i"], "of": len(c.road_job["tiles"])} if c.road_job else None,
            "roads_built": c.roads_built,
            "known": round(self.w.fog.share_known(c), 3) if self.w.fog.on and c.id in self.w.fog.seen else 1.0,
            "scouts": len(c.scouts),
            "development": development.snapshot(c),
        }

    # ------------------------------------------------------------------ version 5
    def _civ_index(self, cid: str) -> int:
        for i, c in enumerate(self.w.civs):
            if c.id == cid:
                return i
        return 0

    @staticmethod
    def _army(c) -> dict:
        return count_types(c.soldiers)

    @staticmethod
    def _unit(civ_i: int, u) -> list:
        return [u.id, civ_i, round(u.x, 2), round(u.y, 2), u.type, u.state, round(u.hp / u.max_hp, 2), u.order]

    def _new_roads(self) -> list:
        log = self.w.map.road_log
        out = [list(t) for t in log[self.roads_seen:]]
        self.roads_seen = len(log)
        return out

    def _fog(self) -> dict:
        """Each kingdom's known land, sent again only when it grew."""
        fog = self.w.fog
        if not fog.on:
            return {}
        out = {}
        for cid, v in fog.version.items():
            if self.fog_sent.get(cid) != v and cid in fog.seen:
                self.fog_sent[cid] = v
                out[cid] = base64.b64encode(bytes(fog.seen[cid])).decode("ascii")
        return out

    def _effects(self) -> list:
        if not self.own_effects:
            return []
        fx = self.w.military.effects[-200:]
        self.w.military.effects.clear()
        return [[e["x"], e["y"], self._civ_index(e["civ"]), e["kind"]] for e in fx]

    def _diplomacy(self) -> dict:
        w = self.w
        dip = w.diplomacy
        if len(w.civs) < 2:
            return {}
        a, b = w.civs[0], w.civs[1]
        left = max(0.0, w.start_t + float(w.rules.war.get("peace_days", 14)) * 86400 - w.clock.t)
        pairs = [{"a": first.id, "b": second.id, "R": round(dip.rel(first.id, second.id), 1),
                  "state": dip.state(first.id, second.id), "war": dip.at_war(first.id, second.id),
                  "trust_a": round(dip.trust(first.id, second.id), 2), "trust_b": round(dip.trust(second.id, first.id), 2)}
                 for i, first in enumerate(w.civs) for second in w.civs[i + 1:]]
        return {"R": round(dip.rel(a.id, b.id), 1), "state": dip.state(a.id, b.id),
                "trust": {a.id: round(dip.trust(a.id, b.id), 2), b.id: round(dip.trust(b.id, a.id), 2)},
                "treaty_days": round(max(0.0, dip.treaty_until.get(dip.pair(a.id, b.id), 0) - w.clock.t) / 86400, 1),
                "war": dip.at_war(a.id, b.id), "peace_days_left": round(left / 86400, 1), "pairs": pairs}

    def _new_messages(self) -> list:
        msgs = self.w.diplomacy.messages
        out = [m.as_dict() for m in msgs if m.id > self.msg_seen]
        if msgs:
            self.msg_seen = max(self.msg_seen, max(m.id for m in msgs))
        tz = self.w.clock.tz
        for m in out:
            m["hhmm"] = datetime.fromtimestamp(m["t"], tz).strftime("%H:%M")
        return out

    def _person(self, civ_i: int, p: Person) -> list:
        home = p.home
        inside = p.state == State.REST and home is not None and \
            abs(p.x - home.center[0]) <= home.size and abs(p.y - home.center[1]) <= home.size
        return [p.id, civ_i, round(p.x, 2), round(p.y, 2), STATES.get(p.state, "idle"),
                p.carry_res if p.carry_amt > 0 else None, BADGES[p.badge()], p.job, 0 if inside else 1,
                p.sex, 1 if p.cart else 0]

    def _building(self, civ_i: int, c, b: Building) -> list:
        left = None
        if b.under_construction:
            left = self._seconds_left(c, b)
        hp = round(b.hp / b.max_hp, 2) if b.max_hp else 1.0
        return [b.id, civ_i, b.type, b.x, b.y, b.size, b.level, b.target_level, round(b.progress, 3),
                b.stage, bool(b.on_fire), hp, left, b.family_name]

    def _seconds_left(self, c, b: Building) -> float | None:
        r = self.w.rules
        if b.builders_now <= 0:
            return None
        rate = F.builder_power(b.builders_now, r.construction["builder_exponent"]) \
            * r.tool_mult(c.tool_level("builder")) * self.w.clock.season.build
        return round(self.w.remaining_hours(b) * 3600 / max(1e-9, rate))

    def _new_events(self, tz) -> list[dict]:
        j = self.w.journal
        n = min(j.count - self.events_seen, len(j.events))
        self.events_seen = j.count
        rows = [event_row(e, tz) for e in j.events[len(j.events) - n:]] if n > 0 else []
        self.history_events = (self.history_events + rows)[-self.journal_lines:]
        return rows

    def _new_thoughts(self, tz) -> list[dict]:
        out = []
        for c in self.w.civs:
            if c.ai is None:
                continue
            last = self.thought_t[c.id]
            for t, text in c.ai.thoughts:
                gist = re.sub(r"\d+", "#", text)                     # the same thought with new numbers adds nothing
                if t > last and gist != self.last_text.get(c.id):
                    self.last_text[c.id] = gist
                    out.append({"civ": c.id, "t": t, "hhmm": datetime.fromtimestamp(t, tz).strftime("%H:%M"),
                                "text": text})
            if c.ai.thoughts:
                self.thought_t[c.id] = c.ai.thoughts[-1][0]
        out = out[-self.thoughts_lines:]
        self.history_thoughts = (self.history_thoughts + out)[-self.thoughts_lines:]
        return out


def event_row(e, tz) -> dict:
    return {"t": e.t, "hhmm": datetime.fromtimestamp(e.t, tz).strftime("%H:%M"), "day": e.day,
            "civ": e.civ_id, "kind": e.kind, "text": e.text, "importance": e.importance}


# ---------------------------------------------------------------------------- on request
def find_person(world, pid: int):
    for c in world.civs:
        for p in c.people:
            if p.id == pid:
                return c, p
    return None, None


def person_card(world, pid: int, family_names: dict[int, str] | None = None) -> dict | None:
    """The person card: name, age, job, trait, experience, parents, children, a short history."""
    civ, p = find_person(world, pid)
    if p is None:
        return None
    r = world.rules
    now = world.clock.t
    names = {q.id: q.name for c in world.civs for q in c.people}
    names.update(family_names or {})

    def who(i):
        return {"id": i, "name": names.get(i, "(gone)"), "alive": i in {q.id for c in world.civs for q in c.people}}

    stage = civ.stage(p, now)
    friends = sorted(((v, i) for i, v in p.friends.items() if v >= r.friendship.get("friend_from", 50)), reverse=True)
    home = None
    if p.home is not None:
        home = "Castle" if p.home.type == "castle" else (
            f"House of the {p.home.family_name} family" if p.home.family_name else "House")
    return {
        "id": p.id, "civ": civ.id, "civ_name": civ.name, "color": civ.color,
        "name": p.name, "sex": p.sex, "age": round(civ.age(p, now), 1), "stage": stage["name"],
        "job": p.job, "state": STATES.get(p.state, "idle"),
        "trait": r.traits[p.trait]["label"] if p.trait else None,
        "experience": {k: round(v / 3600, 1) for k, v in sorted(p.job_seconds.items(), key=lambda kv: -kv[1])},
        "happiness": round(p.happiness), "badge": p.badge(),
        "away": p.away_reason if p.away(now) else None,
        "home": home,
        "spouse": who(p.spouse) if p.spouse is not None else None,
        "parents": [who(i) for i in p.parents],
        "children": [who(i) for i in p.children],
        "friends": [who(i) for _, i in friends[:5]],
        "carrying": {"res": p.carry_res, "amount": round(p.carry_amt, 1)} if p.carry_amt > 0 else None,
        "history": list(p.history[-12:]),
        "portraits": [],
    }


def stats_payload(world) -> dict:
    """Daily rows for the Stats tab (one per civ per day)."""
    return {"civs": [{"id": c.id, "name": c.name, "color": c.color, "days": c.daily} for c in world.civs]}
