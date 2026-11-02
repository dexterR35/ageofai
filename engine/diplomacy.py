"""Version 5: relations between the kingdoms — R (−100…100), trust T (0…1), treaties, messages, deals.

This file holds the state and the doc's rules for changing it. Who decides what to say lives in
ai/diplomat.py (genes in fast mode, the local AI in the live world). Free text may lie; the concrete
promises (a deal's goods, a treaty's days) are tracked here, and breaking one costs trust.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .rules import RESOURCES

if TYPE_CHECKING:
    from .civ import Civilization
    from .world import World

MESSAGE_TYPES = ("trade", "gift", "tribute", "peace", "war", "free")


@dataclass
class Message:
    id: int
    t: float
    day: int
    src: str
    dst: str
    type: str                        # trade | gift | tribute | peace | war | free
    give: dict[str, float] = field(default_factory=dict)
    ask: dict[str, float] = field(default_factory=dict)
    days: int = 0                    # peace treaties
    text: str = ""
    reply_to: int | None = None
    status: str = "sent"             # sent | accepted | refused | done | broken
    by_ai: bool = False

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "t", "day", "src", "dst", "type", "give", "ask", "days", "text",
                                              "reply_to", "status", "by_ai")}


class Diplomacy:
    def __init__(self, world: "World"):
        self.w = world
        self.cfg = world.rules.diplomacy
        self.R: dict[frozenset, float] = {}
        self.T: dict[tuple[str, str], float] = {}          # (a, b) = how much a trusts b
        self.treaty_until: dict[frozenset, float] = {}
        self.war: set[frozenset] = set()                    # declared wars
        self.peace_bonus: dict[frozenset, float] = {}
        self.last_attack: dict[frozenset, float] = {}
        self.messages: list[Message] = []
        self.sent_today: dict[str, int] = {}
        self._ids = 0
        self.on_message = None                              # callback(Message): the page, the commentator

    # ------------------------------------------------------------------ state
    @staticmethod
    def pair(a: str, b: str) -> frozenset:
        return frozenset((a, b))

    def rel(self, a: str, b: str) -> float:
        return self.R.get(self.pair(a, b), float(self.cfg.get("R_start", 0)))

    def trust(self, a: str, b: str) -> float:
        return self.T.get((a, b), float(self.cfg.get("T_start", 0.5)))

    def state(self, a: str, b: str) -> str:
        return self.w.rules.diplomacy_state(self.rel(a, b))

    def change(self, a: str, b: str, key: str, times: float = 1.0, why: str = "") -> float:
        d = float(self.cfg.get("R_change", {}).get(key, 0)) * times
        p = self.pair(a, b)
        self.R[p] = max(-100.0, min(100.0, self.rel(a, b) + d))
        return d

    def kept(self, a: str, b: str) -> None:
        """b kept a promise to a: a trusts b more."""
        self.T[(a, b)] = min(1.0, self.trust(a, b) + float(self.cfg.get("T_kept", 0.1)))

    def broken(self, a: str, b: str) -> None:
        """b broke a promise to a."""
        self.T[(a, b)] = self.trust(a, b) * float(self.cfg.get("T_broken", 0.5))

    def treaty(self, a: str, b: str) -> bool:
        return self.treaty_until.get(self.pair(a, b), 0.0) > self.w.clock.t

    def at_war(self, a: str, b: str) -> bool:
        return self.pair(a, b) in self.war

    def peace_period(self) -> bool:
        """The first 14 days: nobody can attack."""
        return self.w.clock.t < self.w.start_t + float(self.w.rules.war.get("peace_days", 14)) * 86400

    def may_attack(self, a: str, b: str) -> bool:
        return not self.peace_period()

    # ------------------------------------------------------------------ what happens
    def attacked(self, attacker: "Civilization", victim: "Civilization") -> None:
        """Called when an army enters the other's land with an attack order."""
        p = self.pair(attacker.id, victim.id)
        self.change(attacker.id, victim.id, "attack")
        self.last_attack[p] = self.w.clock.t
        self.peace_bonus[p] = 0.0
        if self.treaty(attacker.id, victim.id):
            self.change(attacker.id, victim.id, "treaty_broken")
            self.broken(victim.id, attacker.id)
            self.treaty_until.pop(p, None)
            self.w.log(attacker, "treaty", f"{attacker.name} broke the peace treaty with {victim.name}!", 8)
        self.start_war(attacker.id, victim.id, by=attacker.id)

    # ------------------------------------------------------------------ wars (for the rivalry scoreboard)
    def start_war(self, a: str, b: str, by: str) -> None:
        """A war begins (a declaration, or an army marching in). `by` started it."""
        p = self.pair(a, b)
        if p in self.war:
            return
        self.war.add(p)
        ca, cb = self.w.civ_by_id(a), self.w.civ_by_id(b)
        self.__dict__.setdefault("war_since", {})[p] = {
            "t": self.w.clock.t, "by": by, "losses": {a: getattr(ca, "losses", 0), b: getattr(cb, "losses", 0)}}
        self.w.rival("war_started", by, other=b if by == a else a)

    def end_war(self, a: str, b: str, winner: str | None, how: str) -> None:
        """A war ends: a kingdom fell (`winner` is the other), or peace was signed. Without a winner the side that
        lost fewer soldiers during the war wins it; equal losses are a draw."""
        p = self.pair(a, b)
        if p not in self.war:
            return
        self.war.discard(p)
        info = self.__dict__.setdefault("war_since", {}).pop(p, None)
        if winner is None and info is not None:
            lost = {}
            for cid in (a, b):
                c = self.w.civ_by_id(cid)
                lost[cid] = getattr(c, "losses", 0) - info["losses"].get(cid, 0)
            if lost[a] != lost[b]:
                winner = a if lost[a] < lost[b] else b
        for x, y in ((a, b), (b, a)):
            value = 1.0 if winner == x else 0.0 if winner == y else 0.5
            self.w.rival("war_end", x, other=y, value=value, text=how)

    def daily(self) -> None:
        """Every day without an attack: R + 1 (at most +20 from peaceful days)."""
        civs = [c for c in self.w.civs if c.alive]
        cap = float(self.cfg.get("R_change", {}).get("peace_day_max", 20))
        for i, a in enumerate(civs):
            for b in civs[i + 1:]:
                p = self.pair(a.id, b.id)
                if self.w.clock.t - self.last_attack.get(p, -1e18) < 86400:
                    continue
                if self.peace_bonus.get(p, 0.0) < cap:
                    self.peace_bonus[p] = self.peace_bonus.get(p, 0.0) + self.change(a.id, b.id, "peace_day")
        self.sent_today.clear()

    # ------------------------------------------------------------------ messages
    def can_send(self, civ_id: str) -> bool:
        return self.sent_today.get(civ_id, 0) < int(self.cfg.get("messages_per_day", 3))

    def clean(self, msg: dict, src: "Civilization", dst: "Civilization") -> tuple[Message | None, str]:
        """Check a message (from the genes or the local AI). Returns (message, why it was refused)."""
        kind = str(msg.get("type", "")).lower().strip()
        if kind not in MESSAGE_TYPES:
            return None, f"unknown message type {kind!r}"
        cap = float(self.cfg.get("max_trade", 500))

        def goods(d) -> dict[str, float]:
            out = {}
            for k, v in (d or {}).items():
                k = str(k).lower().strip()
                try:
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                if k in RESOURCES and v > 0 and math.isfinite(v):
                    out[k] = round(min(v, cap))
            return out

        give, ask = goods(msg.get("give")), goods(msg.get("ask"))
        if kind in ("trade", "gift") and not give:
            return None, "a trade or gift must give something"
        if kind == "trade" and not ask:
            return None, "a trade must ask for something"
        if kind == "tribute" and not ask:
            return None, "a tribute demand must ask for something"
        for k, v in give.items():
            if src.stock.get(k, 0) < v:
                return None, f"{src.name} doesn't have {v:.0f} {k}"
        lo, hi = self.cfg.get("treaty_days", [3, 30])
        days = int(max(lo, min(hi, int(msg.get("days") or 0)))) if kind == "peace" else 0
        text = " ".join(str(msg.get("text", "")).split())[: int(self.cfg.get("max_chars", 300))]
        self._ids += 1
        return Message(self._ids, self.w.clock.t, self.w.day, src.id, dst.id, kind,
                       give if kind in ("trade", "gift") else {}, ask if kind in ("trade", "tribute") else {},
                       days, text, msg.get("reply_to"), by_ai=bool(msg.get("by_ai"))), ""

    def post(self, m: Message) -> None:
        self.messages.append(m)
        del self.messages[:-300]
        self.sent_today[m.src] = self.sent_today.get(m.src, 0) + 1
        src, dst = self.w.civ_by_id(m.src), self.w.civ_by_id(m.dst)
        what = {"trade": "offers a trade to", "gift": "sends a gift to", "tribute": "demands tribute from",
                "peace": f"proposes {m.days} days of peace to", "war": "declares war on", "free": "writes to"}[m.type]
        detail = ""
        if m.give:
            detail += " gives " + ", ".join(f"{v:.0f} {k}" for k, v in m.give.items())
        if m.ask:
            detail += (";" if detail else "") + " asks " + ", ".join(f"{v:.0f} {k}" for k, v in m.ask.items())
        quote = f' "{m.text}"' if m.text else ""
        self.w.log(src, "message", f"{src.name} {what} {dst.name}.{detail and ' (' + detail.strip() + ')'}{quote}", 5)
        if m.type == "war":
            self.start_war(m.src, m.dst, by=m.src)
        if m.type == "tribute":
            self.change(m.src, m.dst, "threat")
        if self.on_message:
            self.on_message(m)

    def answer(self, m: Message, accept: bool, text: str = "", by_ai: bool = False) -> None:
        """The receiver's answer to a trade, tribute or peace proposal."""
        src, dst = self.w.civ_by_id(m.src), self.w.civ_by_id(m.dst)
        m.status = "accepted" if accept else "refused"
        self._ids += 1
        reply = Message(self._ids, self.w.clock.t, self.w.day, m.dst, m.src, "free", text=text[:300],
                        reply_to=m.id, status=m.status, by_ai=by_ai)
        self.messages.append(reply)
        word = "accepts" if accept else "refuses"
        quote = f' "{reply.text}"' if reply.text else ""
        self.w.log(dst, "message", f"{dst.name} {word} the {m.type} offer from {src.name}.{quote}", 5)
        if self.on_message:
            self.on_message(reply)
        if not accept:
            return
        if m.type == "peace":
            self.treaty_until[self.pair(m.src, m.dst)] = self.w.clock.t + m.days * 86400
            self.end_war(m.src, m.dst, None, "peace")
            self.w.log(src, "treaty", f"{src.name} and {dst.name} signed {m.days} days of peace.", 7)
        elif m.type == "tribute":
            paid = {k: min(v, dst.stock.get(k, 0)) for k, v in m.ask.items()}
            if all(paid[k] >= m.ask[k] - 1e-6 for k in m.ask):
                dst.pay(paid)
                for k, v in paid.items():
                    src.store(k, v)
                self.change(m.src, m.dst, "tribute_paid")
                m.status = "done"
            else:
                m.status = "broken"
                self.broken(m.src, m.dst)
        elif m.type == "trade":
            self.w.trade.start_deal(m)

    def gift(self, m: Message) -> None:
        """A gift travels with a cart like a trade, but nothing comes back."""
        self.w.trade.start_deal(m)

    def recent(self, a: str, b: str, n: int = 10) -> list[Message]:
        return [m for m in self.messages if {m.src, m.dst} == {a, b}][-n:]
