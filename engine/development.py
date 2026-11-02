"""Civilization development: experiments, institutions and persistent knowledge.

Models propose names and research fields. Costs, trials and supported effects
belong to the engine; a proposal never becomes executable Python.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import formulas


FIELDS = ("food", "wood", "stone", "ore", "gold", "construction", "education", "preservation")
METHODS = ("process", "tools", "logistics")
OFFICES = {"economy": ("wood", "stone", "ore", "gold"), "research": ("build",), "government": ()}


@dataclass
class Development:
    era: str = "settlement"
    discoveries: list[dict] = field(default_factory=list)
    experiments: list[dict] = field(default_factory=list)
    project: dict | None = None
    materials: dict[str, float] = field(default_factory=dict)
    offices: dict[str, dict] = field(default_factory=dict)
    appointments: list[dict] = field(default_factory=list)
    next_review: float = 0.0
    next_experiment: float = 0.0
    education: float = 0.0
    spending: dict[str, float] = field(default_factory=lambda: {"research": 0.0, "industry": 0.0, "services": 0.0})
    culture: dict[str, str] = field(default_factory=dict)
    operating: set[int] = field(default_factory=set)


def enabled(civ) -> bool:
    return bool(civ.rules.raw.get("development", {}).get("enabled", False))


def config(civ) -> dict:
    return civ.rules.raw.get("development", {})


def building_rule(civ, btype: str) -> dict:
    return config(civ).get("buildings", {}).get(btype, {})


def era_index(civ, name: str | None = None) -> int:
    names = list(config(civ).get("eras", {"settlement": {}}))
    return names.index(name or civ.development.era) if (name or civ.development.era) in names else 0


def unlocked(civ, btype: str) -> bool:
    spec = building_rule(civ, btype)
    if not spec:
        return True
    if not enabled(civ):
        return False
    return (era_index(civ) >= era_index(civ, spec.get("era", "settlement"))
            and civ.pop >= spec.get("population", 0)
            and all(civ.count(k) >= v for k, v in spec.get("requires", {}).items()))


def bonus(civ, key: str) -> float:
    if not enabled(civ):
        return 0.0
    value = sum(d["gain"] for d in civ.development.discoveries if d["field"] == key)
    for b in civ.buildings:
        if b.built and b.closed_until <= civ.now:
            spec = building_rule(civ, b.type)
            if not spec.get("upkeep") or b.id in civ.development.operating:
                value += spec.get("effects", {}).get(key, 0.0) * b.level
    return min(10.0 if key == "happiness" else config(civ).get("bonus_cap", 1.0), value)


def balance(civ, resource: str) -> float:
    return civ.stock.get(resource, civ.development.materials.get(resource, 0.0))


def material_rate(civ, resource: str) -> float:
    """Potential units per work hour, for the tactical planner's waiting estimate."""
    return sum(b.level * building_rule(civ, b.type).get("recipe", {}).get("output", {}).get(resource, 0.0)
               for b in civ.buildings if b.built and b.closed_until <= civ.now)


def propose(world, civ, raw: object, source: str = "model") -> tuple[bool, str]:
    if not enabled(civ) or not civ.alive:
        return False, "research is unavailable"
    if civ.development.project is not None:
        return False, "an experiment is already running"
    if not isinstance(raw, dict):
        return False, "proposal must be an object"
    area = raw.get("field")
    if area not in FIELDS:
        return False, "unknown research field"
    method = raw.get("method", "process")
    if method not in METHODS:
        return False, "unknown experiment method"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        return False, "name must contain 1–80 characters"
    if any(d["name"].casefold() == name.strip().casefold() for d in civ.development.discoveries):
        return False, "this discovery already exists"
    cfg = config(civ)
    known = sum(d["field"] == area for d in civ.development.discoveries)
    if known >= cfg.get("discoveries_per_field", 4):
        return False, "this field needs a later research framework"
    facilities = sum(civ.count(t) for t in cfg.get("research_buildings", ["school", "library", "academy"]))
    if not facilities or civ.pop < cfg.get("research_min_population", 4):
        return False, "research needs an institution and enough people"
    tier = known + 1
    base = cfg.get("experiment_cost", {"coins": 30, "wood": 15})
    cost = {r: round(v * tier) for r, v in base.items()}
    reserve = {"food": civ.food_per_day(world.clock.t), "coins": cfg.get("coin_reserve", 40)}
    if not civ.can_afford(cost, reserve):
        return False, "the experiment would spend essential reserves"
    approach = raw.get("approach", "")
    if not isinstance(approach, str):
        return False, "approach must be text"
    civ.pay(cost)
    civ.development.spending["research"] += cost.get("coins", 0)
    civ.development.project = {"name": name.strip(), "field": area, "approach": approach[:400],
                               "method": method,
                               "tier": tier, "progress": 0.0, "source": source, "started": world.clock.t,
                               "hours": cfg.get("experiment_hours", 18) * tier, "cost": cost}
    civ.development.next_experiment = world.clock.t + cfg.get("experiment_interval_hours", 48) * 3600
    world.log(civ, "research_start", f"{civ.name} tests {name.strip()} ({area}); its outcome is not known yet.", 5)
    return True, "experiment started"


def _finish(world, civ) -> None:
    state = civ.development
    p = state.project
    # Paired trials measure a candidate against a baseline. No bonus is applied
    # during testing; noisy or ineffective candidates can fail validation.
    cfg = config(civ)
    trials = int(cfg.get("trials", 8))
    expected = cfg.get("candidate_gain", 0.10) / math.sqrt(p["tier"])
    skill = state.education / 100
    samples = []
    logistics = p["method"] == "logistics"
    production = p["field"] in ("food", "wood", "stone", "ore", "gold")
    if production:
        # Geography and equipment are identical across paired trials. Scan
        # deposits once; each trial still draws its own quality and distance.
        job = world.rules.jobs["farm" if p["field"] == "food" else p["field"]]
        distance = min((math.dist(civ.castle.center, n.center) for n in world.map.by_kind[job.node]
                        if n.amount > 0 and world.fog.usable(civ, n)), default=10.0)
        carry = world.rules.gathering["carry"]
        speed = world.rules.gathering["walk_tiles_per_hour"]
        rate = job.rate * world.rules.tool_mult(civ.tool_level(job.tool))
    for _ in range(trials):
        quality = civ.rng.gauss(expected + skill * 0.03, cfg.get("trial_noise", 0.14))
        if production:
            trial_distance = distance * civ.rng.uniform(0.8, 1.2)
            baseline = formulas.collection_rate(carry, rate, 1.0, trial_distance, speed)
            candidate = formulas.collection_rate(
                carry * max(0.2, 1 + quality if logistics else 1),
                rate, max(0.2, 1 + quality if not logistics else 1), trial_distance, speed)
            samples.append(candidate / baseline - 1 if baseline else 0)
        else:
            samples.append(quality)
    measured = sum(samples) / trials
    wins = sum(v > 0 for v in samples)
    passed = wins >= math.ceil(trials * 0.75) and measured >= cfg.get("minimum_gain", 0.025)
    result = {k: p[k] for k in ("name", "field", "method", "approach", "tier", "source", "cost")}
    result.update(day=world.day, passed=passed, wins=wins, trials=trials, measured_gain=round(measured, 4))
    state.experiments.append(result)
    state.experiments = state.experiments[-100:]
    if passed:
        gain = min(cfg.get("discovery_gain_cap", 0.12), max(0.0, measured))
        state.discoveries.append({**result, "gain": gain})
        world.log(civ, "discovery", f"{civ.name} validates {p['name']}: {wins}/{trials} trials; "
                  f"{p['field']} improves by {gain:.1%}.", 8)
    else:
        world.log(civ, "experiment_failed", f"{p['name']} failed validation ({wins}/{trials} trials); "
                  "the cost and findings are recorded, with no production bonus.", 5)
    state.project = None
    civ.dirty = True


def competence(civ, person, office: str) -> float:
    years = sum(person.job_seconds.get(j, 0) for j in OFFICES[office]) / (3600 * 60)
    if office == "government":
        years = sum(person.job_seconds.values()) / (3600 * 90)
    return min(50.0, years) + person.happiness / 10 + civ.age(person, civ.now) / 20


def appoint(world, civ, choices: object) -> tuple[bool, str]:
    """Validate the whole slate before replacing any officeholder."""
    if not enabled(civ) or not isinstance(choices, dict) or not choices:
        return False, "appointments must name an office and a citizen"
    proposed = {role: record["person"] for role, record in civ.development.offices.items()}
    citizens = {p.id: p for p in civ.people if civ.age(p, world.clock.t) >= 18 and not p.away(world.clock.t)}
    for role, person_id in choices.items():
        if role not in OFFICES or type(person_id) is not int or person_id not in citizens:
            return False, "appointments require an existing office and an available adult citizen"
        proposed[role] = person_id
    if len(set(proposed.values())) != len(proposed):
        return False, "one citizen cannot hold two offices"
    for role, person_id in choices.items():
        previous = civ.development.offices.get(role)
        if previous and previous["person"] == person_id:
            continue
        person = citizens[person_id]
        civ.development.offices[role] = {"person": person_id, "name": person.name,
                                        "since": world.clock.t, "reason": "leader's appointment"}
        civ.development.appointments.append({"office": role, "person": person_id, "name": person.name,
                                             "day": world.day, "reason": "leader's appointment"})
        person.history.append(f"day {world.day}: the leader appointed them to the {role} office")
        world.log(civ, "appointment", f"The leader appoints {person.name} to the {role} office.", 5)
    civ.development.appointments = civ.development.appointments[-100:]
    return True, "appointments recorded"


def office_skill(civ, role: str) -> float:
    record = civ.development.offices.get(role)
    person = next((p for p in civ.people if record and p.id == record["person"] and not p.away(civ.now)), None)
    return min(0.20, competence(civ, person, role) / 100) if person is not None else 0.0


def review_offices(world, civ) -> None:
    state, cfg = civ.development, config(civ)
    now = world.clock.t
    eligible = [p for p in civ.people if civ.age(p, now) >= 18 and not p.away(now)]
    if now < state.next_review:
        # Fill vacancies as soon as a citizen becomes available, including
        # when a village grows beyond its original two adults.
        ids = {p.id for p in eligible}
        if (len(state.offices) == min(len(OFFICES), len(eligible))
                and all(record["person"] in ids for record in state.offices.values())):
            return
    state.next_review = now + cfg.get("review_days", 7) * 86400
    used = set()
    for office in OFFICES:
        current = state.offices.get(office)
        candidates = [p for p in eligible if p.id not in used]
        if not candidates:
            state.offices.pop(office, None)
            continue
        ranked = sorted(candidates, key=lambda p: (-competence(civ, p, office), p.id))
        best = ranked[0]
        incumbent = next((p for p in candidates if current and p.id == current["person"]), None)
        reason = "appointment"
        if incumbent is not None:
            tenure = (now - current["since"]) / 86400
            retiring = civ.age(incumbent, now) >= cfg.get("retirement_age", 65)
            term_over = tenure >= cfg.get("term_days", 28)
            stronger = competence(civ, best, office) > competence(civ, incumbent, office) + 3
            if not retiring and not stronger and (not term_over or len(ranked) == 1):
                best = incumbent
            elif retiring or (term_over and incumbent.happiness < 35):
                best = next((p for p in ranked if p is not incumbent), incumbent)
                reason = "retirement" if retiring else "term review"
        if current is None or best.id != current["person"]:
            state.offices[office] = {"person": best.id, "name": best.name, "since": now, "reason": reason}
            state.appointments.append({"office": office, "person": best.id, "name": best.name,
                                       "day": world.day, "reason": reason})
            state.appointments = state.appointments[-100:]
            best.history.append(f"day {world.day}: appointed to the {office} office")
            world.log(civ, "appointment", f"{best.name} takes the {office} office ({reason}).", 5)
        used.add(best.id)


def tick(world, civ, dt: float, working: bool) -> None:
    """Run institutions during work hours; experiment state is part of the save."""
    if not enabled(civ):
        return
    state = civ.development
    review_offices(world, civ)
    if not working:
        return
    state.operating.clear()
    dt_hours = dt / 3600
    government_discount = 1 - office_skill(civ, "government")
    industry_yield = 1 + office_skill(civ, "economy")
    food_reserve = civ.food_per_day(world.clock.t)
    for b in civ.buildings:
        spec = building_rule(civ, b.type)
        if not b.built or not spec or b.closed_until > world.clock.t:
            continue
        upkeep = {r: value * b.level * dt / 86400 * government_discount
                  for r, value in spec.get("upkeep", {}).items()}
        if upkeep and not civ.can_afford(upkeep, {"food": food_reserve}):
            continue
        civ.pay(upkeep)
        state.operating.add(b.id)
        state.spending["services"] += upkeep.get("coins", 0)
        recipe = spec.get("recipe")
        if recipe:
            factor = b.level * dt_hours
            # Produce only what can be stored. At a nearly full warehouse,
            # charge inputs for a partial batch instead of discarding output.
            for resource, amount in recipe.get("output", {}).items():
                if amount > 0:
                    room = max(0.0, civ.cap(resource) - state.materials.get(resource, 0.0))
                    factor = min(factor, room / (amount * industry_yield))
            inputs = {r: amount * factor for r, amount in recipe.get("input", {}).items()}
            if factor > 0 and civ.can_afford(inputs, {"food": food_reserve, "coins": 30}):
                civ.pay(inputs)
                for resource, amount in recipe.get("output", {}).items():
                    state.materials[resource] = min(civ.cap(resource), state.materials.get(resource, 0)
                                                    + amount * factor * industry_yield)
                state.spending["industry"] += inputs.get("coins", 0)
        state.education = min(100.0, state.education + spec.get("education", 0) * b.level * dt_hours / max(1, civ.pop))
    p = state.project
    if p is not None:
        facilities = sum(b.built and b.type in config(civ).get("research_buildings", [])
                         and b.id in state.operating for b in civ.buildings)
        if facilities:
            p["progress"] = min(1.0, p["progress"] + dt_hours * (1 + bonus(civ, "education")
                                   + office_skill(civ, "research")) / p["hours"])
            if p["progress"] >= 1:
                _finish(world, civ)


def daily(world, civ) -> None:
    if not enabled(civ):
        return
    state, cfg = civ.development, config(civ)
    for name, req in cfg.get("eras", {}).items():
        if era_index(civ, name) <= era_index(civ):
            continue
        if (civ.castle_level >= req.get("castle", 1) and civ.pop >= req.get("population", 0)
                and len(state.discoveries) >= req.get("discoveries", 0)
                and state.education >= req.get("education", 0)):
            state.era = name
            world.log(civ, "development_era", f"{civ.name} enters the {name.replace('_', ' ')} era; "
                      "its cities and knowledge remain.", 9)
            civ.dirty = True
            break
    if cfg.get("fictional_culture", True) and civ.count("shrine") and not state.culture:
        state.culture = {"tradition": f"The {civ.name} Hearth Tradition", "principle": "Remember those who built before us"}
        world.log(civ, "culture", f"{civ.name} records its own fictional Hearth Tradition.", 5)
    if state.project is None and world.clock.t >= state.next_experiment:
        # The tactical fallback experiments too when no model has proposed work.
        # It gets the same resource costs and validation as every model.
        areas = sorted(FIELDS, key=lambda area: (sum(d["field"] == area for d in state.discoveries),
                       balance(civ, area) if area in civ.stock else 0, area))
        for area in areas:
            number = len(state.experiments) + 1
            ok, _ = propose(world, civ, {"name": f"{civ.name} {area} study {number}", "field": area,
                             "approach": "Compare a candidate technique against the current baseline."}, "tactical")
            if ok:
                break


def record_culture(world, civ, raw: object) -> bool:
    if not enabled(civ) or not isinstance(raw, dict) or not (civ.count("shrine") or civ.count("tavern")):
        return False
    limits = {"tradition": 80, "principle": 200, "verse": 400}
    if not raw or set(raw) - set(limits):
        return False
    if any(not isinstance(value, str) or not value.strip() or len(value) > limits[key] for key, value in raw.items()):
        return False
    civ.development.culture.update({key: value.strip() for key, value in raw.items()})
    world.log(civ, "culture", f"{civ.name} records a fictional cultural work: "
              + civ.development.culture.get("tradition", "a local verse") + ".", 5)
    return True


def snapshot(civ) -> dict | None:
    if not enabled(civ):
        return None
    s = civ.development
    return {"era": s.era, "education": round(s.education, 2), "offices": s.offices,
            "project": s.project, "discoveries": s.discoveries[-32:], "experiments": s.experiments[-12:],
            "materials": {k: round(v, 2) for k, v in s.materials.items()},
            "spending": {k: round(v, 2) for k, v in s.spending.items()}, "culture": s.culture,
            "fields": list(FIELDS), "methods": list(METHODS), "appointments": s.appointments[-12:],
            "candidates": [{"id": p.id, "name": p.name, "age": round(civ.age(p, civ.now), 1),
                            "experience": {role: round(competence(civ, p, role), 2) for role in OFFICES}}
                           for p in sorted(civ.people, key=lambda p: -sum(p.job_seconds.values()))[:12]
                           if civ.age(p, civ.now) >= 18]}
