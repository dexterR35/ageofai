"""Loads data/rules.yaml and exposes typed, precomputed views of it.

The rest of the engine never hard-codes a game number: it asks `Rules`.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from . import formulas as F

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESOURCES: tuple[str, ...] = ("food", "wood", "stone", "ore", "gold", "coins")


def hhmm(s: str) -> int:
    """'07:30' → seconds after midnight."""
    h, m = s.split(":")
    return int(h) * 3600 + int(m) * 60


@dataclass(frozen=True, slots=True)
class Season:
    name: str
    months: tuple[int, ...]
    work: tuple[tuple[int, int], ...]   # (start_sec, end_sec) windows
    hours: float                        # h = work hours per day
    farm: float
    fruit: float
    hunt: float
    walk: float
    build: float
    firewood_per_house: float

    def mult(self, key: str | None) -> float:
        return 1.0 if key is None else getattr(self, key)


@dataclass(frozen=True, slots=True)
class JobSpec:
    name: str
    resource: str
    rate: float          # base units/hour
    tool: str            # craft whose tools apply
    node: str            # node kind or "farm"
    season: str | None   # seasonal multiplier key


@dataclass(frozen=True, slots=True)
class NodeSpec:
    kind: str
    amount: float
    regrow_days: float | None
    blocks: bool
    size: int
    max_workers: int


@dataclass(frozen=True, slots=True)
class BuildingSpec:
    type: str
    label: str
    castle: int                 # castle level that unlocks it
    cost: dict[str, int]
    hours: float
    max: int | None
    size: int
    material: str
    walkable: bool
    depot: tuple[str, ...]
    near: str | None            # must be placed next to this node kind
    workers: int                # job slots (farms)
    level_bonus: float
    economic: bool
    hp: float
    since_version: int


@dataclass(frozen=True, slots=True)
class UnitSpec:
    type: str
    label: str
    building: str               # trained here
    castle: int                 # castle level that unlocks it
    cost: dict[str, int]
    hours: float
    hp: float
    attack: float
    armor: float
    range: float
    speed: float                # tiles per hour
    bonus: dict[str, float]     # damage multiplier against these unit types
    buildings_only: bool        # rams hit buildings, nothing else


class Rules:
    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        t = raw["time"]
        self.timezone: str = t["timezone"]
        self.real_tick: float = float(t["real_tick_seconds"])
        self.fast_tick: float = float(t["fast_tick_seconds"])
        self.lunch: tuple[int, int] = (hhmm(t["lunch"][0]), hhmm(t["lunch"][1]))
        self.evening_end: int = hhmm(t["evening_end"])
        self.tactical_interval: float = t["tactical_interval_minutes"] * 60.0

        self.seasons: dict[str, Season] = {}
        self._month_season: dict[int, Season] = {}
        for name, s in raw["seasons"].items():
            work = tuple((hhmm(a), hhmm(b)) for a, b in s["work"])
            season = Season(
                name=name, months=tuple(s["months"]), work=work,
                hours=sum(b - a for a, b in work) / 3600,
                farm=s["farm"], fruit=s["fruit"], hunt=s["hunt"], walk=s["walk"], build=s["build"],
                firewood_per_house=s.get("firewood_per_house", 0),
            )
            self.seasons[name] = season
            for m in season.months:
                self._month_season[m] = season
        if sorted(self._month_season) != list(range(1, 13)):
            raise ValueError("seasons must cover all 12 months exactly once")

        self.jobs: dict[str, JobSpec] = {
            name: JobSpec(name, j["resource"], float(j["rate"]), j["tool"], j["node"], j.get("season"))
            for name, j in raw["jobs"].items()
        }
        self.nodes: dict[str, NodeSpec] = {
            kind: NodeSpec(kind, float(n["amount"]), n.get("regrow_days"), bool(n["blocks"]),
                           int(n.get("size", 1)), int(n["max_workers"]))
            for kind, n in raw["nodes"].items()
        }

        b = raw["buildings"]
        self.building_max_level: int = b["max_level"]
        self._b_cost_growth: float = b["cost_growth"]
        self._b_time_growth: float = b["time_growth"]
        self.effect_step: float = b["effect_step"]
        self.hp_step: float = b["hp_step"]
        self.buildings: dict[str, BuildingSpec] = {}
        for name, s in b["types"].items():
            self.buildings[name] = BuildingSpec(
                type=name, label=s["label"], castle=s["castle"], cost=dict(s["cost"]),
                hours=float(s["hours"]), max=s.get("max"), size=int(s.get("size", 2)),
                material=s.get("material", "wood"), walkable=bool(s.get("walkable", False)),
                depot=tuple(s.get("depot", ())), near=s.get("near"), workers=int(s.get("workers", 0)),
                level_bonus=float(s.get("level_bonus", 0.0)), economic=bool(s.get("economic", False)),
                hp=float(s.get("hp", b["default_hp"])), since_version=int(s.get("since_version", 1)),
            )
        c = raw["castle"]
        self.castle = c
        self.castle_spec = BuildingSpec(
            type="castle", label=c["label"], castle=1, cost={}, hours=0.0, max=1, size=int(c["size"]),
            material="stone", walkable=False, depot=tuple(c["depot"]), near=None, workers=0,
            level_bonus=0.0, economic=False, hp=float(c["hp"]), since_version=1,
        )
        self.castle_max_level: int = c["max_level"]
        self.castle_requirements: dict[int, list[dict]] = {int(k): v for k, v in c["requirements"].items()}

        self.depots_for: dict[str, list[str]] = {r: [] for r in RESOURCES}
        for spec in [self.castle_spec, *self.buildings.values()]:
            for r in spec.depot:
                self.depots_for[r].append(spec.type)

        p = raw["people"]
        self.people = p
        self.stages: list[dict] = p["stages"]
        self.tools = raw["tools"]
        self.crafts: list[str] = list(self.tools["crafts"])
        self.traits: dict[str, dict] = raw["traits"]["list"]
        self.trait_none_chance: float = raw["traits"]["none_chance"]
        self.morale = raw["morale"]
        self.storage = raw["storage"]
        self.gathering = raw["gathering"]
        self.construction = raw["construction"]
        self.winter = raw["winter"]
        self.sowing = raw["sowing"]
        self.economy = raw["economy"]
        self.start_stock: dict[str, float] = {r: float(raw["start_stock"].get(r, 0)) for r in RESOURCES}
        self.genome = raw["genome"]
        self.ai = raw["ai"]
        self.map = raw["map"]
        self.civs: list[dict] = raw["civs"]
        self.version: int = int(raw.get("version", 1))
        self.features: dict[str, bool] = dict(raw.get("features", {}))
        self.happiness = raw.get("happiness", {})
        self.friendship = raw.get("friendship", {})
        self.evening = raw.get("evening", {})
        self.strike = raw.get("strike", {})
        self.flight = raw.get("flight", {})
        self.families = raw.get("families", {})
        self.events = raw.get("events", {})
        self.evolution = raw.get("evolution", {})
        self.llm = raw.get("llm", {})
        self.council = raw.get("council", {})
        self.web = raw.get("web", {})                   # version 4
        self.commentator = raw.get("commentator", {})
        self.images = raw.get("images", {})
        self.units: dict[str, UnitSpec] = {                 # version 5
            k: UnitSpec(type=k, label=u["label"], building=u["building"], castle=int(u["castle"]),
                        cost=dict(u["cost"]), hours=float(u["hours"]), hp=float(u["hp"]), attack=float(u["attack"]),
                        armor=float(u["armor"]), range=float(u["range"]), speed=float(u["speed"]),
                        bonus={b: float(m) for b, m in (u.get("bonus") or {}).items()},
                        buildings_only=bool(u.get("buildings_only", False)))
            for k, u in raw.get("units", {}).items()}
        self.army = raw.get("army", {})
        self.military_upgrades = raw.get("military_upgrades", {})
        self.war = raw.get("war", {})
        self.diplomacy = raw.get("diplomacy", {})
        self.spies = raw.get("spies", {})
        self.market = raw.get("market", {})
        self.mint = raw.get("mint", {})
        self.persistence = raw.get("persistence", {})       # version 6
        self.doctor = raw.get("doctor", {})
        self.timelapse = raw.get("timelapse", {})
        self.tech = raw.get("tech", {})                     # version 7
        self.tech_effects = raw.get("tech_effects", {})
        self.roads = raw.get("roads", {})
        self.carts = raw.get("carts", {})
        self.fog = raw.get("fog", {})
        self.scout = raw.get("scout", {})
        self.bandits = raw.get("bandits", {})
        self.notify = raw.get("notify", {})
        self.evolution_watch = raw.get("evolution_watch", {})
        self.wonder = raw.get("wonder", {})                 # after castle 10: the Wonder wins the era
        self.cost_mult: dict[str, float] = {}          # the referee's weekly ±5% adjustments

    def apply_balance(self, balance: dict, now_iso: str | None = None) -> bool:
        """Use the referee's cost multipliers if they are already in effect. Returns True if applied."""
        from datetime import datetime
        eff = balance.get("effective_from")
        now_iso = now_iso or datetime.now().isoformat(timespec="minutes")
        if not eff or eff > now_iso:
            return False
        self.cost_mult = {k: float(v) for k, v in balance.get("building_cost", {}).items()}
        return True

    def feature(self, name: str) -> bool:
        return self.version >= 2 and bool(self.features.get(name, False))

    def with_features(self, **flags: bool) -> "Rules":
        """A copy of these rules with some version-2 systems switched on/off."""
        import copy
        raw = copy.deepcopy(self.raw)
        raw.setdefault("features", {}).update(flags)
        return Rules(raw)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: str | Path | None = None) -> "Rules":
        path = Path(path) if path else DATA_DIR / "rules.yaml"
        with open(path, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    @classmethod
    def profile(cls, name: str = "classic") -> "Rules":
        """Named local modes share the base rules without sharing saved worlds."""
        if name not in ("classic", "civilization"):
            raise ValueError(f"Unknown profile: {name}")
        if name == "classic":
            return cls.load()
        import copy

        def merge(base, changes):
            for key, value in changes.items():
                if isinstance(value, dict) and isinstance(base.get(key), dict):
                    merge(base[key], value)
                else:
                    base[key] = copy.deepcopy(value)

        raw = copy.deepcopy(cls.load().raw)
        with (DATA_DIR / f"{name}.yaml").open(encoding="utf-8") as handle:
            merge(raw, yaml.safe_load(handle))
        return cls(raw)

    # ------------------------------------------------------------------ time
    def season_of(self, month: int) -> Season:
        return self._month_season[month]

    # ------------------------------------------------------------------ buildings
    def building_cost(self, btype: str, level: int) -> dict[str, int]:
        base = self.buildings[btype].cost
        m = self.cost_mult.get(btype, 1.0)
        if m != 1.0:
            base = {r: v * m for r, v in base.items()}
        return F.scaled_cost(base, self._b_cost_growth, level)

    def building_hours(self, btype: str, level: int) -> float:
        """Hours of work for ONE builder with base tools (t_n)."""
        return F.scaled_hours(self.buildings[btype].hours, self._b_time_growth, level)

    def progressive(self, btype: str) -> bool:
        """Materials are delivered while building (nothing paid up front): the castle and the Wonder."""
        if btype == "castle":
            return bool(self.castle["progressive_materials"])
        return btype == self.wonder.get("type", "wonder") and btype in self.buildings

    def site_cost(self, site) -> dict[str, float]:
        """Everything a construction site needs in total."""
        if site.type == "castle":
            return self.castle_cost(site.level)
        return self.building_cost(site.type, site.target_level)

    def building_hp(self, btype: str, level: int) -> float:
        spec = self.castle_spec if btype == "castle" else self.buildings[btype]
        return spec.hp * (1 + self.hp_step * (max(level, 1) - 1))

    def max_building_level(self, castle_level: int) -> int:
        return min(self.building_max_level, castle_level)

    # ------------------------------------------------------------------ castle
    def castle_cost(self, level: int) -> dict[str, int]:
        """Cost to go from `level` to `level + 1`."""
        c = self.castle
        return F.castle_cost(level, c["base_cost"], c["cost_growth"], c["extra_cost"])

    def castle_hours(self, level: int) -> float:
        """Hours with the reference builder count (5)."""
        return F.castle_hours(level, self.castle["base_hours"], self.castle["time_growth"])

    def castle_one_builder_hours(self, level: int) -> float:
        """Same work expressed for one builder, so all sites share one progress formula."""
        exp = self.construction["builder_exponent"]
        return self.castle_hours(level) * F.builder_power(self.castle["reference_builders"], exp)

    def age_name(self, level: int) -> str:
        name = ""
        for lvl, label in sorted(self.castle["ages"].items()):
            if level >= int(lvl):
                name = label
        return name

    # ------------------------------------------------------------------ people
    def workers_max(self, castle_level: int) -> int:
        p = self.people
        table = p.get("workers_by_castle")
        if table:                                         # the army plan: more workers at high castle levels
            return int(table[max(0, min(len(table), castle_level) - 1)])
        return F.workers_max(castle_level, p["workers_base"], p["workers_per_castle_level"], p["max_workers"])

    def stage_of(self, age: float) -> dict:
        for s in self.stages:
            if age < s["until"]:
                return s
        return self.stages[-1]

    # ------------------------------------------------------------------ tools
    def tool_cost(self, level: int) -> dict[str, int]:
        t = self.tools
        return F.scaled_cost(t["base_cost"], t["cost_growth"], level)

    def tool_hours(self, level: int) -> float:
        t = self.tools
        return F.scaled_hours(t["base_hours"], t["time_growth"], level)

    def tool_mult(self, level: int) -> float:
        return F.tool_mult(level, self.tools["bonus_per_level"])

    def tool_requires(self, level: int) -> dict[str, int]:
        return self.tools["requires"][level]

    # ------------------------------------------------------------------ research: tools and (v5) military upgrades
    MILITARY = ("weapons", "armor")

    def research_cost(self, craft: str, level: int) -> dict[str, float]:
        cost = self.tool_cost(level)
        if craft in self.MILITARY:
            f = float(self.military_upgrades.get("cost_factor", 1.5))
            cost = {k: round(v * f) for k, v in cost.items()}
        return cost

    def research_hours(self, craft: str, level: int) -> float:
        return self.tool_hours(level)

    def research_label(self, craft: str) -> str:
        if craft in self.MILITARY:
            return self.military_upgrades.get("labels", {}).get(craft, craft.title())
        return self.tools["labels"][craft]

    def unit_cost(self, utype: str) -> dict[str, float]:
        return dict(self.units[utype].cost)

    def diplomacy_state(self, R: float) -> str:
        for row in self.diplomacy.get("states", []):
            if R >= row["from"]:
                return row["name"]
        return "hostility"

    # ------------------------------------------------------------------ genome
    @property
    def gene_ranges(self) -> dict[str, tuple[float, float]]:
        return {k: (float(v[0]), float(v[1])) for k, v in self.genome["genes"].items()}


@lru_cache(maxsize=1)
def default_rules() -> Rules:
    rules = Rules.load()
    balance = DATA_DIR / "balance.yaml"
    if balance.exists():
        with open(balance, encoding="utf-8") as f:
            rules.apply_balance(yaml.safe_load(f) or {})
    return rules


@lru_cache(maxsize=1)
def name_lists() -> dict[str, dict[str, list[str]]]:
    with open(DATA_DIR / "names.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)
