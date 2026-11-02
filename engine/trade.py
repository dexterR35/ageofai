"""Version 5: the Market, the Mint and trade carts between the kingdoms.

* Market: swap one resource for another at prices that move (each 100 sold → −3%, +1%/hour back).
* Mint: turns gold into coins during work hours, so armies can be paid.
* Deals: an accepted trade or a gift travels on a cart from castle to castle. Payment happens when the
  cart arrives; whoever can't (or won't) pay breaks the promise and loses the other's trust.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from . import formulas as F
from .pathfinding import step_along
from .units import Cart

if TYPE_CHECKING:
    from .civ import Civilization
    from .diplomacy import Message
    from .world import World


class Trade:
    def __init__(self, world: "World"):
        self.w = world
        self.carts: list[Cart] = []
        self.deals: dict[int, dict] = {}
        self._hour_acc = 0.0
        # who keeps their word: by default the honesty gene decides (both modes)
        self.keeps_promise: Callable[["Civilization", dict], bool] = self._honest

    # ------------------------------------------------------------------ market
    def has_market(self, civ: "Civilization") -> bool:
        return any(b.type == "market" and b.built for b in civ.buildings)

    def quote(self, civ: "Civilization", give: str, get: str, amount: float) -> float:
        p = civ.market_price
        fee = float(self.w.rules.market.get("fee", 0.1))
        return amount * p.get(give, 1.0) / max(1e-9, p.get(get, 1.0)) * (1 - fee)

    def exchange(self, civ: "Civilization", give: str, get: str, amount: float) -> float:
        """Sell `amount` of `give` at the market for `get`. Returns what was received (0 if impossible)."""
        if give == get or not self.has_market(civ) or civ.stock.get(give, 0) < amount or amount <= 0:
            return 0.0
        got = self.quote(civ, give, get, amount)
        civ.stock[give] -= amount
        stored = civ.store(get, got)
        drop = float(self.w.rules.market.get("drop_per_100", 0.03))
        civ.market_price[give] = F.market_price_after_sale(civ.market_price[give], amount, drop)
        self.w.log(civ, "trade", f"Market: sold {amount:.0f} {give} for {got:.0f} {get}.", 2)
        return stored

    # ------------------------------------------------------------------ every tick
    def tick(self, dt: float, working: bool) -> None:
        self._hour_acc += dt
        if self._hour_acc >= 3600:
            hours, self._hour_acc = self._hour_acc / 3600, 0.0
            m = self.w.rules.market
            for civ in self.w.civs:
                for res, base in m.get("base_price", {}).items():
                    civ.market_price[res] = F.market_recover(civ.market_price.get(res, base), base, hours,
                                                             float(m.get("recover_per_hour", 0.01)))
        if working:
            self._mint(dt)
        self._move_carts(dt)

    def _mint(self, dt: float) -> None:
        cfg = self.w.rules.mint
        for civ in self.w.civs:
            if not civ.alive:
                continue
            mints = [b for b in civ.buildings if b.type == "mint" and b.built]
            if not mints:
                continue
            level = max(b.level for b in mints)
            rate = float(cfg.get("coins_per_gold", 2.5)) * (1 + float(cfg.get("level_bonus", 0.25)) * (level - 1))
            hours = max(1.0, self.w.clock.season.hours)       # 40 gold a day = over the day's work hours
            spare = civ.stock["gold"] - float(cfg.get("keep_gold", 100))
            room = (civ.cap("coins") - civ.stock["coins"]) / rate
            gold = min(spare, room, float(cfg.get("gold_per_day_per_level", 40)) * level * dt / (hours * 3600))
            if gold > 0:
                civ.stock["gold"] -= gold
                civ.stock["coins"] += gold * rate

    # ------------------------------------------------------------------ deals and carts
    def _honest(self, civ: "Civilization", deal: dict) -> bool:
        return civ.rng.random() < civ.genome.get("honesty", 0.7)

    def start_deal(self, m: "Message") -> None:
        """An accepted trade or a gift: the giver loads a cart (or breaks the promise right away)."""
        src, dst = self.w.civ_by_id(m.src), self.w.civ_by_id(m.dst)
        dip = self.w.diplomacy
        deal = {"id": m.id, "type": m.type, "src": m.src, "dst": m.dst, "give": dict(m.give), "ask": dict(m.ask),
                "msg": m}
        if not src.can_afford(m.give) or not self.keeps_promise(src, deal):
            m.status = "broken"
            dip.broken(dst.id, src.id)
            self.w.log(src, "promise", f"{src.name} did not send the goods it promised to {dst.name}.", 6)
            return
        src.pay(m.give)
        self.deals[m.id] = deal
        self._send_cart(deal, src, dst, m.give)

    def _send_cart(self, deal: dict, src: "Civilization", dst: "Civilization", load: dict) -> None:
        start = src.castle.center if src.castle else (0, 0)
        path = self._road(src, dst)
        cart = Cart(self.w.next_id(), deal["id"], src.id, dst.id, start[0], start[1], path or (), dict(load),
                    self.w.clock.t)
        if not path:                                   # no road between the castles: the goods arrive at once
            self._arrive(cart)
            return
        self.carts.append(cart)

    def _road(self, src: "Civilization", dst: "Civilization") -> tuple | None:
        if not src.castle or not dst.castle:
            return None
        m = self.w.map
        a = m.access_tiles(src.castle.x, src.castle.y, src.castle.size, False)
        b = m.access_tiles(dst.castle.x, dst.castle.y, dst.castle.size, False)
        if not a or not b:
            return None
        return self.w.paths.find(a[0], ("road", src.id, dst.id), b)

    def _move_carts(self, dt: float) -> None:
        speed = float(self.w.rules.diplomacy.get("cart_speed", 480)) / 3600
        for cart in list(self.carts):
            cart.x, cart.y, cart.path_i = step_along(cart.path, cart.path_i, cart.x, cart.y, speed * dt)
            if cart.path_i >= len(cart.path):
                self.carts.remove(cart)
                self._arrive(cart)

    def _arrive(self, cart: Cart) -> None:
        deal = self.deals.get(cart.deal_id)
        dst, src = self.w.civ_by_id(cart.dst), self.w.civ_by_id(cart.src)
        if dst is None or not dst.alive:                   # the kingdom fell while the cart was on the road
            if deal is not None:
                deal["msg"].status = "lost"
                self.deals.pop(cart.deal_id, None)
            return
        for k, v in cart.load.items():
            dst.store(k, v)
        if deal is None:
            return
        dip = self.w.diplomacy
        m = deal["msg"]
        if deal["type"] == "gift":
            dip.change(src.id, dst.id, "gift_per_50", sum(cart.load.values()) / 50)
            dip.kept(dst.id, src.id)
            m.status = "done"
            self.w.log(dst, "trade", f"The gift from {src.name} arrived.", 4)
            self.deals.pop(cart.deal_id, None)
            return
        if cart.src == deal["src"]:                    # the offer arrived: now the receiver pays
            price = float(self.w.rules.diplomacy.get("friendship_price", 0.9)) \
                if dip.state(src.id, dst.id) == "friendship" else 1.0
            ask = {k: round(v * price) for k, v in deal["ask"].items()}
            dip.kept(dst.id, src.id)
            if not dst.can_afford(ask) or not self.keeps_promise(dst, deal):
                m.status = "broken"
                dip.broken(src.id, dst.id)
                self.w.log(dst, "promise", f"{dst.name} took the goods from {src.name} and did not pay.", 7)
                self.deals.pop(cart.deal_id, None)
                return
            dst.pay(ask)
            self._send_cart(deal, dst, src, ask)
        else:                                          # the payment arrived: the deal is done
            dip.kept(dst.id, src.id)
            dip.change(src.id, dst.id, "trade")
            m.status = "done"
            self.w.log(dst, "trade", f"Trade done: {dst.name} and {src.name} kept their word.", 4)
            self.deals.pop(cart.deal_id, None)
