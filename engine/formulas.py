"""Pure formulas from the design document. No game state, no I/O: easy to test.

Every function here matches a formula in "Age of AI — Document de design.md".
The numbers come from rules.yaml; these functions only do the math.
"""
from __future__ import annotations

import math
from typing import Mapping


def round_half_up(x: float) -> int:
    """Round like the design doc (153.6 → 154, 2.5 → 3), not banker's rounding."""
    return math.floor(x + 0.5)


def scaled_cost(base: Mapping[str, float], growth: float, level: int) -> dict[str, int]:
    """Cost_n = Cost_1 · growth^(n−1), rounded per resource."""
    f = growth ** (level - 1)
    return {r: round_half_up(v * f) for r, v in base.items()}


def scaled_hours(base_hours: float, growth: float, level: int) -> float:
    """t_n = t_1 · growth^(n−1)."""
    return base_hours * growth ** (level - 1)


def scaled_effect(effect_1: float, level: int, step: float = 0.25) -> float:
    """Effect_n = Effect_1 · (1 + step·(n−1))."""
    return effect_1 * (1 + step * (level - 1))


def collection_rate(carry: float, rate: float, mult: float, dist_tiles: float,
                    walk_tiles_per_hour: float) -> float:
    """Real gathering rate per hour, counting the round trip to the depot.

    R = C / (C/(r·m) + 2d/v)
    """
    work = rate * mult
    if work <= 0:
        return 0.0
    return carry / (carry / work + 2 * dist_tiles / walk_tiles_per_hour)


def builder_power(builders: float, exponent: float = 0.8) -> float:
    """More builders help, but not linearly: b^0.8."""
    return builders ** exponent if builders > 0 else 0.0


def build_hours(base_hours: float, builders: float, tool_mult: float = 1.0,
                exponent: float = 0.8) -> float:
    """t_real = t_n / (b^0.8 · m_T)."""
    power = builder_power(builders, exponent) * tool_mult
    return math.inf if power <= 0 else base_hours / power


def tool_mult(level: int, step: float = 0.2) -> float:
    """m_T = 1 + 0.2·T."""
    return 1 + step * level


def workers_max(castle_level: int, base: int = 2, per_level: int = 2, cap: int = 20) -> int:
    """W_max(L) = min(20, 2 + 2L)."""
    return min(cap, base + per_level * castle_level)


def beds(house_levels: list[int], castle_beds: int = 4, house_base: int = 3, cap: int = 200) -> int:
    """P_cap = 4 + Σ(3 + n_i), capped at 200."""
    return min(cap, castle_beds + sum(house_base + n for n in house_levels))


def death_chance_per_year(age: float, from_age: float = 55, base: float = 0.02,
                          growth: float = 0.1) -> float:
    """q(a) = 0.02 · e^(0.1(a−55)) for a ≥ 55, else 0."""
    if age < from_age:
        return 0.0
    return min(1.0, base * math.exp(growth * (age - from_age)))


def per_day_from_per_year(q_year: float, days_per_year: float) -> float:
    """Spread a yearly chance over the real days of a game-year: 1 − (1−q)^(1/days)."""
    if q_year >= 1:
        return 1.0
    return 1 - (1 - q_year) ** (1 / days_per_year)


def castle_cost(level: int, base: Mapping[str, float], growth: float,
                extra: Mapping[str, Mapping[str, float]]) -> dict[str, int]:
    """Cost to go from castle level L to L+1.

    (300 wood, 200 stone, 100 coins)·1.7^(L−1) + 100 ore·1.7^(L−3)|L≥3 + 80 gold·1.7^(L−6)|L≥6
    """
    cost = scaled_cost(base, growth, level)
    for res, spec in extra.items():
        start = int(spec["from_level"])
        if level >= start:
            cost[res] = round_half_up(spec["base"] * growth ** (level - start))
    return cost


def castle_hours(level: int, base_hours: float = 24, growth: float = 1.4) -> float:
    """T(L→L+1) = 24h · 1.4^(L−1), measured with the reference builder count."""
    return base_hours * growth ** (level - 1)


def winter_food_reserve(pop: int, winter_farmers: int, days: int = 90,
                        eat_per_day: float = 48, farmer_yield_per_day: float = 48) -> float:
    """R_food = 90 · (48·N_pop − 48·N_farmers). With the doc's numbers: 4320·(N_pop − N_farmers)."""
    return max(0.0, days * (eat_per_day * pop - farmer_yield_per_day * winter_farmers))


def winter_wood_reserve(houses: int, days: int = 90, per_house: float = 5) -> float:
    """R_wood = 90 · 5 · N_houses = 450 · N_houses."""
    return days * per_house * houses


def coins_per_day(pop: int, gold_melted: float = 0.0, mint_level: int = 0,
                  tax: float = 1.0, coins_per_gold: float = 2.5, level_bonus: float = 0.25) -> float:
    """M = N_pop·1 + 2.5·A_gold·(1 + 0.25(n_mint − 1))."""
    minted = coins_per_gold * gold_melted * (1 + level_bonus * (mint_level - 1)) if mint_level > 0 else 0.0
    return pop * tax + minted


def utility(benefits: Mapping[str, float], genes: Mapping[str, float],
            wait_hours: float, build_hours_: float) -> float:
    """U(a) = Σ g_j·benefit_j / (1 + (t_wait + t_build)/24h)."""
    score = sum(genes.get(k, 1.0) * v for k, v in benefits.items())
    return score / (1 + (wait_hours + build_hours_) / 24)


def allocate_proportional(total: int, weights: Mapping[str, float]) -> dict[str, int]:
    """Split `total` workers by weight with the largest-remainder method (integers that sum to total)."""
    keys = [k for k, w in weights.items() if w > 0]
    if total <= 0 or not keys:
        return {k: 0 for k in weights}
    s = sum(weights[k] for k in keys)
    raw = {k: total * weights[k] / s for k in keys}
    out = {k: int(raw[k]) for k in keys}
    left = total - sum(out.values())
    for k in sorted(keys, key=lambda k: raw[k] - out[k], reverse=True)[:left]:
        out[k] += 1
    for k in weights:
        out.setdefault(k, 0)
    return out


def happiness_target(ate: bool, bed: bool, friends: int, far_work: bool = False, cold: bool = False,
                     cfg: Mapping[str, float] | None = None) -> float:
    """H_target = 50 + 10[ate] − 30[hungry] + 10[bed] − 20[no bed] + 4·min(3, friends) − 10[far] − 10[cold]."""
    c = cfg or {"base": 50, "ate": 10, "hungry": -30, "bed": 10, "no_bed": -20, "per_friend": 4,
                "max_friends_counted": 3, "far_work": -10, "no_fire": -10}
    t = c["base"] + (c["ate"] if ate else c["hungry"]) + (c["bed"] if bed else c["no_bed"])
    t += c["per_friend"] * min(c["max_friends_counted"], friends)
    if far_work:
        t += c["far_work"]
    if cold:
        t += c["no_fire"]
    return t


def happiness_step(h: float, target: float, pull: float = 0.1, shock: float = 0.0) -> float:
    """H ← H + 0.1·(H_target − H) + shocks, kept in 0..100."""
    return min(100.0, max(0.0, h + pull * (target - h) + shock))


def morale_from_happiness(mean_h: float, base: float = 0.5, per_point: float = 0.007,
                          lo: float = 0.5, hi: float = 1.2) -> float:
    """m_M = 0.5 + 0.007·H̄ (0 → 0.5, 71.4 → 1.0, 100 → 1.2)."""
    return min(hi, max(lo, base + per_point * mean_h))


def elo_expected(ra: float, rb: float) -> float:
    """E_A = 1 / (1 + 10^((R_B − R_A)/400))."""
    return 1 / (1 + 10 ** ((rb - ra) / 400))


def elo_update(ra: float, rb: float, score_a: float, k: float = 32) -> tuple[float, float]:
    """R_A' = R_A + K(S_A − E_A); B gets the mirror change."""
    ea = elo_expected(ra, rb)
    delta = k * (score_a - ea)
    return ra + delta, rb - delta


def diversity(genomes: list[Mapping[str, float]], ranges: Mapping[str, tuple[float, float]]) -> float:
    """D = mean pairwise RMS distance, each gene divided by its range (0 = all identical)."""
    n = len(genomes)
    if n < 2:
        return 0.0
    keys = list(ranges)
    total = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            s = sum(((genomes[i][k] - genomes[j][k]) / (ranges[k][1] - ranges[k][0])) ** 2 for k in keys)
            total += math.sqrt(s / len(keys))
    return 2 * total / (n * (n - 1))


def fitness(castle_level: int, gathered: float, gathered_max: float, pop: int,
            survived: bool, win_rate: float) -> float:
    """F = 0.35·L/10 + 0.25·E/E_max + 0.15·P/200 + 0.15·survival + 0.10·wins."""
    e = min(1.0, gathered / gathered_max) if gathered_max > 0 else 0.0
    return (0.35 * castle_level / 10 + 0.25 * e + 0.15 * min(pop, 200) / 200
            + 0.15 * (1.0 if survived else 0.0) + 0.10 * win_rate)


# ---------------------------------------------------------------------------- version 5: war, spies, market
def damage(attack: float, bonus: float, weapons: int, armor: float, armor_upgrades: int, luck: float = 1.0) -> float:
    """D = max(1, A·k + U_weapons − (Ar + U_armor)) · ε"""
    return max(1.0, attack * bonus + weapons - (armor + armor_upgrades)) * luck


def army_strength(units: list[tuple[float, float]], seconds: float = 2.0) -> float:
    """Lanchester: S = (Σ HP_i) · (Σ D_i / 2 s). `units` = [(hp, damage per blow)]."""
    if not units:
        return 0.0
    return sum(h for h, _ in units) * sum(d / seconds for _, d in units)


def spy_catch_per_hour(towers: int, base: float = 0.01, per_tower: float = 0.02, cap: float = 0.30) -> float:
    """p_h = min(0.30, 0.01 + 0.02 · N_towers)"""
    return min(cap, base + per_tower * towers)


def spy_caught(p_hour: float, hours: float = 6) -> float:
    """P = 1 − (1 − p_h)^6"""
    return 1 - (1 - p_hour) ** hours


def market_price_after_sale(price: float, sold: float, drop_per_100: float = 0.03) -> float:
    """Every 100 units sold lower the price 3% (compounding)."""
    return price * (1 - drop_per_100) ** (sold / 100.0)


def market_recover(price: float, base: float, hours: float, per_hour: float = 0.01) -> float:
    """The price creeps back to normal, +1% an hour, never past it."""
    if price >= base:
        return base
    return min(base, price * (1 + per_hour) ** hours)
