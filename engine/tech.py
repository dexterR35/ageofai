"""Version 7: the tech tree, researched at the castle one at a time (work hours only).

| tech           | castle | effect                                             |
| Plow           | 3      | farms +15%                                         |
| Wheel          | 3      | unlocks carts                                      |
| Writing        | 4      | experience grows 20% faster; a richer Chronicle    |
| Irrigation     | 5      | farms +20% in summer                               |
| Crop Rotation  | 5      | winter farms ×0.7 instead of ×0.5                  |
| Water Mill     | 6      | all food +10%                                      |
| Medicine       | 7      | disease −50%, life +5 years                        |
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .civ import Civilization
    from .rules import Rules, Season


def food_mult(rules: "Rules", civ: "Civilization", node: str, resource: str, season: "Season") -> float:
    """The techs' bonus for a food job (1.0 for everything else)."""
    if resource != "food" or not civ.techs or not rules.feature("tech"):
        return 1.0
    fx = rules.tech_effects
    m = 1.0
    if node == "farm":
        if "plow" in civ.techs:
            m *= 1 + fx.get("plow_farm", 0.15)
        if "irrigation" in civ.techs and season.name == "summer":
            m *= 1 + fx.get("irrigation_summer_farm", 0.20)
        if "crop_rotation" in civ.techs and season.name == "winter" and season.farm > 0:
            m *= max(1.0, fx.get("rotation_winter_farm", 0.7) / season.farm)
    if "water_mill" in civ.techs:
        m *= 1 + fx.get("water_mill_food", 0.10)
    return m


def xp_speed(rules: "Rules", civ: "Civilization") -> float:
    return float(rules.tech_effects.get("writing_xp", 1.2)) if civ.has_tech("writing") else 1.0


def available(rules: "Rules", civ: "Civilization") -> list[str]:
    """Techs this kingdom could start now."""
    if not rules.feature("tech") or civ.tech_job is not None:
        return []
    return [k for k, t in rules.tech.items() if k not in civ.techs and civ.castle_level >= t["castle"]]
