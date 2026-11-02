"""Version 5: who says what to whom, twice a day (09:00 and 18:00), at most 3 messages a day.

In fast mode (the night's games) there is no local AI, so the genes decide (doc): diplomacy = how often
a kingdom proposes deals, honesty = the chance it keeps its promises (engine/trade.py). In the live
world the local AI writes the messages and answers (LiveMind sets `writer` and `answerer`); if it is
away, the genes take over.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from engine.rules import hhmm

if TYPE_CHECKING:
    from engine.civ import Civilization
    from engine.diplomacy import Message
    from engine.world import World

TRADE_RES = ("food", "wood", "stone", "ore", "gold")


class Diplomat:
    def __init__(self, world: "World"):
        self.w = world
        self.cfg = world.rules.diplomacy
        self.done: dict[str, object] = {}
        local = world.clock.local
        now_s = local.hour * 3600 + local.minute * 60
        for at in self.cfg.get("times", ["09:00", "18:00"]):
            if now_s >= hhmm(at):
                self.done[at] = local.date()
        # the live world plugs the local AI in here; each returns True when it takes the job (answers later)
        self.refused: dict[tuple, float] = {}          # (from, to, give, ask) → when it was refused
        self.writer: Callable[["Civilization", "Civilization"], bool] | None = None
        self.answerer: Callable[["Message"], bool] | None = None

    # ------------------------------------------------------------------ schedule
    def tick(self) -> None:
        local = self.w.clock.local
        now_s = local.hour * 3600 + local.minute * 60
        for at in self.cfg.get("times", ["09:00", "18:00"]):
            if now_s >= hhmm(at) and self.done.get(at) != local.date():
                self.done[at] = local.date()
                self.round()

    def round(self) -> None:
        civs = [c for c in self.w.civs if c.alive and c.ai is not None]
        for civ in civs:
            for other in civs:
                if other is civ or not self.w.diplomacy.can_send(civ.id):
                    continue
                if self.writer is not None and self.writer(civ, other):
                    continue
                msg = self.compose(civ, other)
                if msg:
                    self.send(civ, other, msg)

    # ------------------------------------------------------------------ sending and answering
    def send(self, civ: "Civilization", other: "Civilization", msg: dict) -> "Message | None":
        dip = self.w.diplomacy
        m, why = dip.clean(msg, civ, other)
        if m is None:
            return None
        dip.post(m)
        if m.type == "gift":
            dip.gift(m)
        elif m.type in ("trade", "tribute", "peace"):
            if self.answerer is None or not self.answerer(m):
                accept, text = self.decide(m)
                dip.answer(m, accept, text)
                if not accept:
                    self.refused[self._key(m.src, m.dst, m.give, m.ask)] = self.w.clock.t
        return m

    @staticmethod
    def _key(src: str, dst: str, give: dict, ask: dict) -> tuple:
        return (src, dst, tuple(sorted(give)), tuple(sorted(ask)))

    def offer_peace(self, civ: "Civilization", other: "Civilization", reason: str = "", days: int = 7) -> bool:
        """The king seeks peace: a treaty offer now (not if one is already in force or no message is left today)."""
        dip = self.w.diplomacy
        if dip.treaty(civ.id, other.id) or not dip.can_send(civ.id):
            return False
        text = reason or "Let there be peace between our kingdoms."
        return self.send(civ, other, {"type": "peace", "days": days, "text": text}) is not None

    def declare_war(self, civ: "Civilization", enemy: "Civilization", reason: str = "") -> None:
        """Honest kingdoms say it before they march."""
        self.send(civ, enemy, {"type": "war", "text": f"{civ.name} declares war on {enemy.name}. {reason}".strip()})

    # ------------------------------------------------------------------ the genes speak (fast mode, or no local AI)
    def compose(self, civ: "Civilization", other: "Civilization") -> dict | None:
        g, rng, dip = civ.genome, civ.rng, self.w.diplomacy
        planner = getattr(civ.ai, "army", None)
        ratio = planner.ratio(other) if planner else 1.0
        theta = g.get("theta_attack", 2.0)
        if dip.at_war(civ.id, other.id) and not dip.treaty(civ.id, other.id) and ratio < 0.8 and \
                rng.random() < 0.4 + 0.5 * g.get("prudence", 0.5):
            return {"type": "peace", "days": 7, "text": "Let us stop this war before both our villages burn."}
        if rng.random() > g.get("diplomacy", 0.5):
            return None
        if ratio > theta and g.get("honesty", 0.7) < 0.5 and not dip.peace_period() and rng.random() < 0.3:
            rep = civ.intel.get(other.id, {})
            stocks = rep.get("stocks") or {}
            res = max(TRADE_RES, key=lambda k: stocks.get(k, 0)) if stocks else "food"
            amount = max(50, min(self.cfg.get("max_trade", 500), 0.1 * stocks.get(res, 1000)))
            return {"type": "tribute", "ask": {res: round(amount)}, "text": "Pay, and our soldiers stay home."}
        surplus, need = self.surplus(civ), self.needs(civ)
        if surplus and need:
            give_res, spare = surplus[0]
            want_res = need[0]
            if give_res != want_res:
                base = self.w.rules.market.get("base_price", {})
                give = round(min(spare, self.cfg.get("max_trade", 500), 300))
                ask = round(give * base.get(give_res, 1) / base.get(want_res, 1) * 0.9)
                key = self._key(civ.id, other.id, {give_res: 1}, {want_res: 1})
                recently = self.w.clock.t - self.refused.get(key, -1e18) < 3 * 86400   # don't ask again for 3 days
                if give >= 50 and ask >= 20 and not recently:
                    return {"type": "trade", "give": {give_res: give}, "ask": {want_res: ask},
                            "text": f"We have {give_res} to spare and need {want_res}. A fair trade?"}
        if dip.rel(civ.id, other.id) < 0 and surplus and rng.random() < 0.3 * g.get("prudence", 0.5):
            res, spare = surplus[0]
            return {"type": "gift", "give": {res: round(min(100, spare))}, "text": "A gift between neighbours."}
        return None

    def decide(self, m: "Message") -> tuple[bool, str]:
        """The receiver's answer, by the genes."""
        dip = self.w.diplomacy
        me, them = self.w.civ_by_id(m.dst), self.w.civ_by_id(m.src)
        g = me.genome
        planner = getattr(me.ai, "army", None)
        ratio = planner.ratio(them) if planner else 1.0
        if m.type == "peace":
            plans_attack = ratio > g.get("theta_attack", 2.0) and len(me.soldiers) >= 5
            if plans_attack:
                return False, "We will not lay down our arms."
            return True, "Peace it is."
        if m.type == "tribute":
            weak = ratio < 1 / max(1.0, them.genome.get("theta_attack", 2.0)) or \
                (ratio < 1 and me.rng.random() < g.get("prudence", 0.5))
            if weak and me.can_afford(m.ask):
                return True, "We pay, this time."
            return False, "We pay nothing."
        if m.type == "trade":
            if dip.trust(me.id, them.id) < float(self.cfg.get("trust_to_accept", 0.35)) or dip.rel(me.id, them.id) <= -50:
                return False, "We don't trust you."
            if not me.can_afford(m.ask):
                return False, "We don't have that much."
            base = self.w.rules.market.get("base_price", {})
            value_in = sum(v * base.get(k, 1) for k, v in m.give.items())
            value_out = sum(v * base.get(k, 1) for k, v in m.ask.items())
            needs = self.needs(me)
            useful = any(k in needs for k in m.give)
            spare = all(me.stock.get(k, 0) - v > 0.3 * me.cap(k) for k, v in m.ask.items())
            if value_in >= 0.8 * value_out and (useful or spare):
                return True, "Agreed. Send the cart."
            return False, "Not at that price."
        return False, ""

    # ------------------------------------------------------------------ what we have and lack
    def surplus(self, civ: "Civilization") -> list[tuple[str, float]]:
        out = []
        for k in TRADE_RES:
            extra = civ.stock[k] - 0.6 * civ.cap(k)
            if extra > 50:
                out.append((k, extra))
        return sorted(out, key=lambda kv: -kv[1])

    def needs(self, civ: "Civilization") -> list[str]:
        need: dict[str, float] = {}
        for a in getattr(civ.ai, "targets", []):
            for k, v in a.cost.items():
                if k in TRADE_RES and civ.stock.get(k, 0) < v:
                    need[k] = need.get(k, 0) + v - civ.stock[k]
        return sorted(need, key=lambda k: -need[k])
