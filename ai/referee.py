"""The referee (every Sunday 06:05): keeps the game from getting dull or one-sided.

Check 1 — something dominates: for each building type (units from version 5), share s of all
built this week. s > 60% → cost +5%; s < 5% (and seen before) → cost −5%.
Check 2 — all genomes look alike: diversity D < 0.05 → next night mutation 0.30 and 4 fresh
random genomes replace the worst 4.
Limits: ±5% per week, never beyond ±25% of the original cost. Only costs change, never formulas.
Changes are saved to data/balance.yaml (applied Monday 07:00, in fast mode and the live world)
and logged with their reason in data/rules_history.yaml.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from engine.rules import DATA_DIR

STEP = 0.05
BOUND = 0.25
HIGH, LOW = 0.60, 0.05
DIVERSITY_MIN = 0.05


@dataclass
class Verdict:
    changes: list[dict] = field(default_factory=list)       # {"what", "old", "new", "reason"}
    boost_mutation: bool = False
    effective: str = ""


def next_monday_7(today: date) -> datetime:
    days = (7 - today.weekday()) % 7 or 7
    d = today + timedelta(days=days)
    return datetime(d.year, d.month, d.day, 7, 0)


def adjust(mult: float, share: float) -> tuple[float, str | None]:
    """New cost multiplier for one building type and why (None = unchanged)."""
    if share > HIGH:
        new = min(1 + BOUND, mult * (1 + STEP))
        return new, f"{share:.0%} of everything built: more expensive" if new != mult else None
    if 0 < share < LOW or share == 0:
        new = max(1 - BOUND, mult * (1 - STEP))
        return new, f"only {share:.0%} of everything built: cheaper" if new != mult else None
    return mult, None


def review(build_counts: dict[str, int], seen_types: set[str], diversity: float,
           balance: dict, today: date) -> Verdict:
    v = Verdict(effective=next_monday_7(today).isoformat(timespec="minutes"))
    total = sum(build_counts.values())
    mults = balance.setdefault("building_cost", {})
    if total > 0:
        for btype in sorted(seen_types | set(build_counts)):
            share = build_counts.get(btype, 0) / total
            old = float(mults.get(btype, 1.0))
            new, why = adjust(old, share)
            if why:
                mults[btype] = round(new, 4)
                v.changes.append({"what": f"{btype} cost", "old": round(old, 4), "new": round(new, 4), "reason": why})
    if diversity < DIVERSITY_MIN:
        v.boost_mutation = True
        balance["next_night"] = {"mutation_rate": 0.30, "random_genomes": 4}
        v.changes.append({"what": "evolution", "old": "mutation 0.15", "new": "mutation 0.30 + 4 random genomes",
                          "reason": f"genomes too alike (D = {diversity:.3f})"})
    balance["effective_from"] = v.effective
    return v


def load_balance(path: Path | None = None) -> dict:
    path = path or DATA_DIR / "balance.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_balance(balance: dict, data_dir: Path | None = None) -> None:
    data_dir = data_dir or DATA_DIR
    with open(data_dir / "balance.yaml", "w", encoding="utf-8") as f:
        f.write("# Written by the referee. Cost multipliers, applied from 'effective_from'.\n")
        yaml.safe_dump(balance, f, sort_keys=True, allow_unicode=True)


def save(verdict: Verdict, balance: dict, explanation: str = "", data_dir: Path | None = None) -> None:
    data_dir = data_dir or DATA_DIR
    write_balance(balance, data_dir)
    hist_path = data_dir / "rules_history.yaml"
    history = []
    if hist_path.exists():
        with open(hist_path, encoding="utf-8") as f:
            history = yaml.safe_load(f) or []
    history.append({"date": datetime.now().isoformat(timespec="minutes"), "effective": verdict.effective,
                    "changes": verdict.changes, "explanation": explanation})
    with open(hist_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(history, f, sort_keys=False, allow_unicode=True)
