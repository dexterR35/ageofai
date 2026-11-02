"""Night evolution (00:30–06:00): hundreds of fast games, keep the best strategies.

One night, per the design document:
  1. 32 genomes.
  2. Each plays 8 games of 60 days: 6 against genomes of the same night (96 games)
     and 2 against the Hall of Champions (64 games). Fast mode, no Ollama.
  3. F = 0.35·L/10 + 0.25·E/E_max + 0.15·P/200 + 0.15·survival + 0.10·wins
  4. The best 4 pass unchanged; the rest are children of two parents chosen by tournament (3).
  5. Each gene comes from a random parent; with probability 0.15 it moves ±10% of its range.
  7. In the morning the 2 best genomes go to the live world.
Plus the Elo league (every game updates ratings) and the Hall of Champions (max 20, top 3 stay forever).

Fairness: all games of the same round share the map, the start date and the event seed,
so genomes compared that night face the same luck.

Rivalry (evolution.lineages): each kingdom keeps its own lineage, half of the population each, seeded with
its first live mind (genome.live_start: North the builder, South the warlord). It is co-evolution: in the
peer rounds every North genome plays a South genome; in the champion rounds each plays the OTHER kingdom's
Hall of Champions. Selection and crossover stay inside a lineage, so the two styles can drift apart, and in
the morning each kingdom gets its own lineage's best. The game count is the same as with one population.
"""
from __future__ import annotations

import random
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from engine import formulas as F
from engine.rules import Rules, default_rules
from engine.store import Store

from .genome import Genome


@dataclass
class MatchSpec:
    night: int
    round: int
    kind: str                 # "peer" | "champion"
    a_id: str
    a_genes: dict
    b_id: str
    b_genes: dict
    map_seed: int
    event_seed: int
    start: float              # epoch
    days: int
    a_rules: list[str] = field(default_factory=list)   # AI-written rules in force for side A (v3)
    b_rules: list[str] = field(default_factory=list)
    scenario: str | None = None                          # improvements §2: start from this mid-game save


@dataclass
class NightReport:
    night: int
    best_id: str
    best_fitness: float
    mean_fitness: float
    diversity: float
    best_elo: float
    games: int
    seconds: float
    hall: list[tuple[str, float]] = field(default_factory=list)
    fitness: dict[str, float] = field(default_factory=dict)
    bench_id: str = ""
    bench_fitness: float = 0.0       # same 8 games every night: comparable across nights
    proposed: list[str] = field(default_factory=list)            # v3: new AI-written rules
    invalid: list[tuple[str, str]] = field(default_factory=list)
    rule_results: list[dict] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    lineage_best: dict[str, str] = field(default_factory=dict)      # rivalry: kingdom → its lineage's best


# ---------------------------------------------------------------------------- one game (runs in a worker process)

def play_match(spec: MatchSpec) -> dict:
    from engine.world import World

    from .rulebook import RuleError, parse_rule

    rules = default_rules()
    genomes = [Genome(spec.a_genes, id=spec.a_id), Genome(spec.b_genes, id=spec.b_id)]
    if spec.scenario:                                    # a mid-game start: the genomes take over both kingdoms
        from .scenarios import load_scenario
        w = load_scenario(spec.scenario)
        for civ, g in zip(w.civs, genomes):
            civ.genome = g
            if civ.ai is not None:
                civ.ai.book, civ.ai.activations, civ.ai.gene_boost = [], {}, {}
    else:
        w = World(rules, seed=spec.map_seed, start=spec.start, genomes=genomes, event_seed=spec.event_seed)
    base = [c.total_gathered() for c in w.civs]           # count only what each genome gathered itself
    for civ, texts in zip(w.civs, (spec.a_rules, spec.b_rules)):
        for text in texts:
            try:
                civ.ai.book.append(parse_rule(text, rules))
            except RuleError:
                pass
    w.run(spec.days * 86400)
    sides = []
    for civ in w.civs:
        built: dict[str, int] = {}
        for b in civ.buildings:
            if b.built and b.type != "castle":
                built[b.type] = built.get(b.type, 0) + 1
        sides.append({"castle": civ.castle_level, "gathered": civ.total_gathered() - base[len(sides)],
                      "days": spec.days, "midgame": bool(spec.scenario), "pop": civ.pop + len(civ.soldiers),
                      "soldiers": len(civ.soldiers), "kills": civ.kills, "losses": civ.losses,
                      "alive": civ.alive and civ.pop > 0, "departures": civ.departures + civ.fled,
                      "deaths": civ.deaths, "births": civ.births, "buildings": built,
                      "activations": dict(civ.ai.activations)})
    return {"sides": sides}


def base_fitness(side: dict, rules: Rules) -> float:
    """F without the wins term (wins are only known after comparing both sides)."""
    ev = rules.evolution
    wt = ev["fitness"]
    e_max = float(ev["e_max_per_day"]) * side.get("days", 60) if ev.get("e_max_per_day") else ev["e_max"]
    e = min(1.0, side["gathered"] / e_max)
    return (wt["castle"] * side["castle"] / rules.castle_max_level + wt["gathered"] * e
            + wt["pop"] * min(side["pop"], rules.people["pop_cap"]) / rules.people["pop_cap"]
            + wt["survival"] * (1.0 if side["alive"] else 0.0))


def match_score(fa: float, fb: float, margin: float) -> float:
    """1 = A wins, 0.5 = draw (F within 2%), 0 = B wins."""
    top = max(fa, fb)
    if top <= 0 or abs(fa - fb) < margin * top:
        return 0.5
    return 1.0 if fa > fb else 0.0


# ---------------------------------------------------------------------------- the night

def bench_key(rules: Rules, days: int) -> str:
    b = rules.evolution["benchmark"]
    return f"{b['games_per_season']}x4-{days}d-seed{b['seed']}"


def comparable_bench(nights: list[dict]) -> list[float]:
    """Benchmark scores of the latest nights that used the same benchmark settings (games, days)."""
    import json

    def key(n):
        cfg = n.get("config")
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except ValueError:
                cfg = {}
        return (cfg or {}).get("bench", "old")

    rows = [n for n in nights if n.get("bench_fitness") is not None]
    if not rows:
        return []
    last = key(rows[-1])
    tail = []
    for n in reversed(rows):
        if key(n) != last:
            break
        tail.append(n["bench_fitness"])
    return list(reversed(tail))


def stall_nights(nights: list[dict]) -> int:
    """Version 7: nights since the benchmark last rose (the doc: RL only if it stalls 21 nights)."""
    scores = comparable_bench(nights)
    if not scores:
        return 0
    best, since = scores[0], 0
    for s in scores[1:]:
        if s > best + 1e-9:
            best, since = s, 0
        else:
            since += 1
    return since


class Evolution:
    def __init__(self, store: Store, rules: Rules | None = None, *, seed: int = 1, processes: int | None = None,
                 population: int | None = None, days: int | None = None, peer_games: int | None = None,
                 champion_games: int | None = None, tz: str | None = None, llm=None):
        self.store = store
        self.llm = llm                     # version 3: proposes rules and writes lessons (None = skip)
        self.rules = rules = rules or default_rules()
        ev = rules.evolution
        self.seed = seed
        self.processes = processes or ev["processes"]
        self.size = population or ev["population"]
        if self.size % 2:
            raise ValueError("population must be even (genomes play in pairs)")
        self.days = days or ev["match_days"]
        from .scenarios import SCEN_DIR
        self.scenario_dir = SCEN_DIR
        self.midgame = self.days >= 30                    # mid-game starts only for real matches (not tiny tests)
        self.peer_games = ev["peer_games"] if peer_games is None else peer_games
        self.champion_games = ev["champion_games"] if champion_games is None else champion_games
        self.tz = ZoneInfo(tz or rules.timezone)

    # ------------------------------------------------------------------ lineages (rivalry)
    def lineages(self) -> list[str]:
        """The kingdoms that keep their own lineage ([] = one shared population, as before)."""
        ids = [c["id"] for c in self.rules.civs]
        if not self.rules.evolution.get("lineages", True) or len(ids) != 2 or self.size < 4:
            return []
        return ids

    def tag(self, lineage: str) -> str:
        """A short letter for genome ids: n3Ng5 is genome 5 of North's lineage, born on night 3."""
        ids = self.lineages()
        letters = [x[0].upper() for x in ids]
        return letters[ids.index(lineage)] if len(set(letters)) == len(ids) else f"L{ids.index(lineage)}"

    def seeds(self) -> dict[str, Genome]:
        """Each lineage's first genome: the kingdom's live-start mind (rules.yaml → genome.live_start)."""
        return {c["id"]: g for c, g in zip(self.rules.civs, Genome.live_start(self.rules))}

    def groups(self, pop: list[Genome]) -> dict[str, list[Genome]] | None:
        """The population split by lineage, or None when it is one shared population."""
        lines = self.lineages()
        if not lines:
            return None
        out = {x: [g for g in pop if g.meta.get("lineage") == x] for x in lines}
        if any(not v for v in out.values()) or sum(len(v) for v in out.values()) != len(pop):
            return None
        return out

    def split_population(self, pop: list[Genome], night: int, rng: random.Random) -> list[Genome]:
        """A population from before the lineages: its genomes are dealt to the kingdoms in turn, and each
        lineage's last place goes to a mutated copy of the kingdom's live-start mind, so the styles start apart."""
        lines = self.lineages()
        groups: dict[str, list[Genome]] = {x: [g for g in pop if g.meta.get("lineage") == x] for x in lines}
        for g in pop:
            if g.meta.get("lineage") not in lines:
                x = min(lines, key=lambda k: len(groups[k]))
                g.meta["lineage"] = x
                groups[x].append(g)
        seeds = self.seeds()
        for x in lines:
            if groups[x] and seeds[x].id != "default":
                s = seeds[x].mutated(self.rules, rng, rate=0.3, scale=0.1)
                s.id, s.parents = f"n{night}{self.tag(x)}seed", (seeds[x].id,)
                s.meta.update(elo=float(self.rules.evolution["elo_start"]), night=night, games=0, lineage=x)
                groups[x][-1] = s
        return [g for x in lines for g in groups[x]]

    # ------------------------------------------------------------------ setup
    def initial_population(self, rng: random.Random) -> list[Genome]:
        r = self.rules
        lines = self.lineages()
        if lines:                                        # rivalry: half each, around each kingdom's first mind
            out = []
            per = self.size // len(lines)
            for x, seed in self.seeds().items():
                t = self.tag(x)
                group = [Genome(dict(seed.genes), id=f"n1{t}g0", parents=(seed.id,))]
                n_mut = (per - 1) // 2
                for i in range(1, 1 + n_mut):
                    group.append(Genome(seed.mutated(r, rng, rate=0.5, scale=0.2).genes, id=f"n1{t}g{i}",
                                        parents=(seed.id,)))
                for i in range(1 + n_mut, per):
                    group.append(Genome.random(r, rng, id=f"n1{t}g{i}"))
                for g in group:
                    g.meta.update(lineage=x)
                out += group
            for g in out:
                g.meta.update(elo=float(r.evolution["elo_start"]), night=1)
            return out
        base = Genome.default(r)
        out = [Genome(dict(base.genes), id="n1g0")]
        n_mut = (self.size - 1) // 2
        for i in range(1, 1 + n_mut):
            out.append(Genome(base.mutated(r, rng, rate=0.5, scale=0.2).genes, id=f"n1g{i}"))
        for i in range(1 + n_mut, self.size):
            out.append(Genome.random(r, rng, id=f"n1g{i}"))
        for g in out:
            g.meta.update(elo=float(r.evolution["elo_start"]), night=1)
        return out

    def ensure_hall(self) -> list[Genome]:
        hall = self.store.hall()
        if not hall:
            g = Genome.default(self.rules)
            g.id = "default"
            g.meta.update(elo=float(self.rules.evolution["elo_start"]), night=0)
            self.store.hall_add(g, 0)
            self.store.db.commit()
            hall = self.store.hall()
        lines = self.lineages()
        if lines:                                        # each kingdom's first mind is its line's first champion
            for x, seed in self.seeds().items():
                if seed.id != "default" and not any(g.meta.get("lineage") == x for g in hall):
                    seed.meta.update(elo=float(self.rules.evolution["elo_start"]), night=0, lineage=x)
                    self.store.hall_add(seed, 0)
            self.store.db.commit()
            hall = self.store.hall()
        return hall

    def random_start(self, rng: random.Random) -> float:
        """Each round starts in a random season (so the AI also learns winter)."""
        day = datetime(2027, 1, 1, 7, 0, tzinfo=self.tz) + timedelta(days=rng.randrange(365))
        return day.replace(hour=7, minute=0).timestamp()

    def schedule(self, night: int, pop: list[Genome], hall: list[Genome], rng: random.Random) -> list[MatchSpec]:
        specs = []
        rounds = self.peer_games + self.champion_games
        mid = self.midgame_rounds()
        scenarios = self.ensure_scenarios() if mid else []
        for rnd in range(rounds):
            map_seed, event_seed = rng.randrange(1 << 30), rng.randrange(1 << 30)
            start = self.random_start(rng)
            scenario = rng.choice(scenarios) if scenarios and rnd in mid else None
            groups = self.groups(pop)
            if rnd < self.peer_games and groups:         # rivalry: North's genomes play South's (North's half)
                a_line, b_line = (groups[x][:] for x in self.lineages())
                rng.shuffle(a_line)
                rng.shuffle(b_line)
                pairs = [(a, b, "peer") for a, b in zip(a_line, b_line)]
            elif rnd < self.peer_games:
                order = pop[:]
                rng.shuffle(order)
                pairs = [(order[i], order[i + 1], "peer") for i in range(0, len(order), 2)]
            elif groups:                                 # rivalry: against the OTHER kingdom's champions
                pairs = []
                for g in pop:
                    rivals = [h for h in hall if h.meta.get("lineage") not in (g.meta.get("lineage"),)
                              and h.id != g.id]
                    pairs.append((g, rng.choice(rivals or [h for h in hall if h.id != g.id] or hall), "champion"))
            else:
                pairs = [(g, rng.choice([h for h in hall if h.id != g.id] or hall), "champion") for g in pop]
            days = int(self.rules.evolution.get("midgame", {}).get("days", 60)) if scenario else self.days
            for a, b, kind in pairs:
                specs.append(MatchSpec(night, rnd, kind, a.id, a.genes, b.id, b.genes, map_seed, event_seed,
                                       start, days, scenario=scenario))
        return specs

    # ------------------------------------------------------------------ improvements §2: mid-game starts
    def midgame_rounds(self) -> set[int]:
        """Which rounds start in mid-game: the last peer rounds (the champion rounds stay normal)."""
        n = int(self.rules.evolution.get("midgame", {}).get("rounds", 0)) if self.midgame else 0
        n = max(0, min(n, self.peer_games))
        return set(range(self.peer_games - n, self.peer_games))

    def ensure_scenarios(self) -> list[str]:
        """Make the missing mid-game scenarios (once; they are kept on disk)."""
        from pathlib import Path

        from .scenarios import make_scenario, scenario_specs
        specs = scenario_specs(self.rules, self.tz, self.scenario_dir)
        missing = [sp for sp in specs if not Path(sp.path).exists()]
        if missing:
            if self.processes <= 1:
                for sp in missing:
                    make_scenario(sp)
            else:
                with ProcessPoolExecutor(max_workers=min(self.processes, len(missing))) as ex:
                    list(ex.map(make_scenario, missing))
        return [sp.path for sp in specs]

    def benchmark_specs(self, night: int, genomes: list[Genome]) -> list[MatchSpec]:
        """The same games every night: 2 starts per season, fixed maps and luck, vs the default genome."""
        bench = self.rules.evolution["benchmark"]
        rng = random.Random(bench["seed"])
        base = Genome.default(self.rules)
        scenarios = []
        for month in (4, 7, 10, 1):                     # spring, summer, autumn, winter
            for _ in range(bench["games_per_season"]):
                start = datetime(2027, month, rng.randint(1, 25), 7, 0, tzinfo=self.tz).timestamp()
                scenarios.append((rng.randrange(1 << 30), rng.randrange(1 << 30), start))
        return [MatchSpec(night, -1, "benchmark", g.id, g.genes, "default", base.genes, m, e, s, self.days)
                for g in genomes for m, e, s in scenarios]

    def benchmark(self, night: int, genomes: list[Genome],
                  progress: Callable[[int, int], None] | None = None) -> dict[str, float]:
        ev = self.rules.evolution
        specs = self.benchmark_specs(night, genomes)
        results = self.play_all(specs, progress)
        total: dict[str, float] = {g.id: 0.0 for g in genomes}
        count: dict[str, int] = {g.id: 0 for g in genomes}
        for spec, res in zip(specs, results):
            fa, fb = (base_fitness(s, self.rules) for s in res["sides"])
            total[spec.a_id] += fa + ev["fitness"]["wins"] * match_score(fa, fb, ev["draw_margin"])
            count[spec.a_id] += 1
        return {gid: total[gid] / max(1, count[gid]) for gid in total}

    def play_all(self, specs: list[MatchSpec], progress: Callable[[int, int], None] | None = None) -> list[dict]:
        if self.processes <= 1:
            out = []
            for i, s in enumerate(specs):
                out.append(play_match(s))
                if progress:
                    progress(i + 1, len(specs))
            return out
        out = []
        with ProcessPoolExecutor(max_workers=self.processes) as ex:
            for i, res in enumerate(ex.map(play_match, specs, chunksize=1)):
                out.append(res)
                if progress:
                    progress(i + 1, len(specs))
        return out

    # ------------------------------------------------------------------ selection
    def tournament(self, ranked: list[Genome], fit: dict[str, float], rng: random.Random) -> Genome:
        pick = rng.sample(ranked, min(self.rules.evolution["tournament"], len(ranked)))
        return max(pick, key=lambda g: fit[g.id])

    def next_generation(self, night: int, pop: list[Genome], fit: dict[str, float], rng: random.Random,
                        mutation_rate: float | None = None, random_genomes: int = 0,
                        lineage: str | None = None) -> list[Genome]:
        """The next night's genomes. With `lineage`, only that kingdom's line (its share of the elite)."""
        ev = self.rules.evolution
        rate = ev["mutation_rate"] if mutation_rate is None else mutation_rate
        size = len(pop) if lineage else self.size
        elite = max(1, round(ev["elite"] * size / self.size)) if lineage else ev["elite"]
        prefix = f"n{night + 1}{self.tag(lineage)}g" if lineage else f"n{night + 1}g"
        born = {"lineage": lineage} if lineage else {}
        ranked = sorted(pop, key=lambda g: fit[g.id], reverse=True)
        if str(ev.get("method", "ga")).lower() == "cmaes":
            boost = mutation_rate is not None and mutation_rate > ev["mutation_rate"]   # the referee: too alike
            return self.cma_generation(night, ranked, size, prefix, born, rng, lineage or "all", boost)
        nxt = [g for g in ranked[:elite]]
        i = 0
        for _ in range(min(random_genomes, size - len(nxt))):     # referee: fresh blood
            g = Genome.random(self.rules, rng, id=f"{prefix}{i}")
            g.meta.update(elo=float(ev["elo_start"]), night=night + 1, games=0, **born)
            nxt.append(g)
            i += 1
        while len(nxt) < size:
            a, b = self.tournament(ranked, fit, rng), self.tournament(ranked, fit, rng)
            child = Genome.crossover(a, b, rng, id=f"{prefix}{i}")
            child = child.mutated(self.rules, rng, rate, ev["mutation_scale"])
            child.meta.update(elo=float(ev["elo_start"]), night=night + 1, games=0, **born)
            nxt.append(child)
            i += 1
        return nxt

    def cma_generation(self, night: int, ranked: list[Genome], size: int, prefix: str, born: dict,
                       rng: random.Random, key: str, boost: bool = False) -> list[Genome]:
        """CMA-ES (ai/cmaes.py): learn from tonight's ranking, then draw the next night's genomes.
        The night's best is kept as it is; the rest are new points from the search distribution."""
        from . import cmaes
        ev = self.rules.evolution
        names = list(self.rules.gene_ranges)
        lo_hi = [self.rules.gene_ranges[k] for k in names]

        def to_x(g: Genome) -> list[float]:
            return [min(1.0, max(0.0, (g.get(k, (lo + hi) / 2) - lo) / ((hi - lo) or 1.0)))
                    for k, (lo, hi) in zip(names, lo_hi)]

        state = self.store.cma_state(key)
        if state is None or len(state.get("m", [])) != len(names):        # first CMA night: start at tonight's mean
            xs = [to_x(g) for g in ranked]
            mean = [sum(x[i] for x in xs) / len(xs) for i in range(len(names))]
            state = cmaes.new_state(mean, float(ev.get("cma_sigma", 0.2)))
        state = cmaes.update(state, [to_x(g) for g in ranked], max(2, len(ranked)))
        if boost:                                                           # the referee asked for more variety
            state["sigma"] = min(0.5, state["sigma"] * 1.5)
        self.store.save_cma_state(key, night, state)
        best = ranked[0]
        nxt = [best]
        for i, x in enumerate(cmaes.sample(state, size - 1, rng)):
            genes = {k: lo + v * (hi - lo) for k, (lo, hi), v in zip(names, lo_hi, x)}
            g = Genome(genes, id=f"{prefix}{i}", parents=tuple(h.id for h in ranked[:2]),
                       generation=max(h.generation for h in ranked[:2]) + 1)
            g.meta.update(elo=float(ev["elo_start"]), night=night + 1, games=0, **born)
            nxt.append(g)
        return nxt

    def update_hall(self, night: int, best: Genome, ratings: dict[str, float], games: dict[str, int],
                    lineage: str | None = None) -> None:
        """With `lineage`, the kingdom's own champions (each line gets its share of the Hall's places)."""
        ev = self.rules.evolution
        hall = self.store.hall(lineage)
        if lineage:
            ev = {**ev, "hall_size": max(ev["hall_protected"] + 1, -(-ev["hall_size"] // len(self.lineages())))}
        for g in hall:
            g.meta["elo"] = ratings.get(g.id, g.meta["elo"])
            g.meta["games"] = games.get(g.id, g.meta.get("games", 0))
            self.store.save_genome(g, g.meta.get("night") or 0)
        if any(g.id == best.id for g in hall):
            return
        if len(hall) < ev["hall_size"]:
            self.store.hall_add(best, night)
            return
        by_elo = sorted(hall, key=lambda g: g.meta["elo"], reverse=True)
        removable = by_elo[ev["hall_protected"]:]
        weakest = min(removable, key=lambda g: g.meta["elo"])
        if best.meta["elo"] > weakest.meta["elo"]:
            self.store.hall_remove(weakest.id)
            self.store.hall_add(best, night)

    def benchmark_keeper(self) -> Genome | None:
        """The genome with the highest benchmark score of all past nights (re-measured each night)."""
        scored = [n for n in self.store.nights() if n.get("bench_id") and n.get("bench_fitness") is not None]
        if not scored:
            return None
        return self.store.genome(max(scored, key=lambda n: n["bench_fitness"])["bench_id"])

    # ------------------------------------------------------------------ one night
    def lost_games(self, champion_id: str, night: int) -> list[dict]:
        rows = self.store.db.execute(
            "SELECT a, b, start, score_a, la, lb, pa, pb FROM matches WHERE night=? AND (a=? OR b=?)",
            (night, champion_id, champion_id)).fetchall()
        out = []
        for a, b, start, sa, la, lb, pa, pb in rows:
            mine_a = a == champion_id
            lost = (sa < 0.5) if mine_a else (sa > 0.5)
            if lost:
                out.append({"start": start, "my_castle": la if mine_a else lb, "their_castle": lb if mine_a else la,
                            "my_people": pa if mine_a else pb, "their_people": pb if mine_a else pa})
        return out

    def run_rule_night(self, progress: Callable[[int, int], None] | None = None) -> NightReport:
        """Only the version-3 rule cycle (propose → paired games → verdicts), without evolution games.
        Same code and thresholds as a full night; useful to test rules quickly."""
        from .rule_lab import RuleLab

        t0 = time.perf_counter()
        lab = RuleLab(self.store.db, self.rules, self.llm)
        row = self.store.db.execute("SELECT MAX(MAX(COALESCE(created_night,0)), MAX(COALESCE(last_checked_night,0))) "
                                    "FROM rules").fetchone()
        night = max(self.store.last_night(), row[0] or 0) + 1
        from .lessons import write_lessons

        previous = self.store.morning_genomes()
        champion = previous[0] if previous else Genome.default(self.rules)
        last = self.store.last_night()
        lessons = self.store.lessons(last)
        if last and not lessons:                     # evolution ran without v3: write them now
            lessons, by_ai = write_lessons(self.store, self.rules, last, self.llm)
            self.store.save_lessons(last, lessons, by_ai)
        lost = []
        if last:
            for n in range(last, 0, -1):             # the champion's most recent lost games
                lost = self.lost_games(champion.id, n)
                if lost:
                    break
        proposed, invalid = lab.propose(night, champion, lessons, lost)
        results = lab.run_tests(night, champion, self.play_all, self.days, self.seed, self.tz, progress)
        return NightReport(night, champion.id, 0.0, 0.0, 0.0, champion.meta.get("elo", 1000.0),
                           sum(r["games"] for r in results), time.perf_counter() - t0,
                           proposed=[r.text() for r in proposed], invalid=invalid, rule_results=results)

    def run_night(self, progress: Callable[[int, int], None] | None = None) -> NightReport:
        from .lessons import write_lessons
        from .referee import load_balance, write_balance
        from .rule_lab import RuleLab

        ev = self.rules.evolution
        t0 = time.perf_counter()
        started = datetime.now().isoformat(timespec="seconds")
        night = self.store.last_night() + 1
        rng = random.Random(self.seed * 100003 + night)
        pop = self.store.population(night)
        if not pop:
            pop = self.initial_population(rng) if night == 1 else self.store.population(night - 1)
            self.store.save_population(night, pop)
        if self.lineages() and self.groups(pop) is None:      # a population from before the lineages
            pop = self.split_population(pop, night, rng)
            self.store.save_population(night, pop)
        hall = self.ensure_hall()

        # 00:30 — version 3: the local AI proposes rules (accepted rules play in every game)
        lab = RuleLab(self.store.db, self.rules, self.llm)
        previous = self.store.morning_genomes()
        champion = previous[0] if previous else Genome.default(self.rules)
        proposed, invalid = lab.propose(night, champion, self.store.lessons(night - 1),
                                        self.lost_games(champion.id, night - 1))
        shared = [r.text() for r in lab.accepted()]

        specs = self.schedule(night, pop, hall, rng)
        for s in specs:
            s.a_rules, s.b_rules = list(shared), list(shared)
        results = self.play_all(specs, progress)
        built: dict[str, int] = {}
        for res in results:
            for side in res["sides"]:
                for btype, n in side.get("buildings", {}).items():
                    built[btype] = built.get(btype, 0) + n
        self.store.save_build_stats(night, built)

        ratings = {g.id: g.meta.get("elo", ev["elo_start"]) for g in [*pop, *hall]}
        games = {g.id: g.meta.get("games", 0) for g in [*pop, *hall]}
        f_sum = {g.id: 0.0 for g in pop}
        s_sum = {g.id: 0.0 for g in pop}
        n_games = {g.id: 0 for g in pop}
        rows = []
        for spec, res in zip(specs, results):
            fa, fb = (base_fitness(s, self.rules) for s in res["sides"])
            sa = match_score(fa, fb, ev["draw_margin"])
            ratings[spec.a_id], ratings[spec.b_id] = F.elo_update(ratings[spec.a_id], ratings[spec.b_id], sa,
                                                                  ev["elo_k"])
            games[spec.a_id] += 1
            games[spec.b_id] += 1
            for gid, f, s in ((spec.a_id, fa, sa), (spec.b_id, fb, 1 - sa)):
                if gid in f_sum and not (spec.kind == "champion" and gid == spec.b_id):
                    f_sum[gid] += f
                    s_sum[gid] += s
                    n_games[gid] += 1
            a, b = res["sides"]
            rows.append({"night": night, "round": spec.round, "kind": spec.kind, "a": spec.a_id, "b": spec.b_id,
                         "map_seed": spec.map_seed, "event_seed": spec.event_seed,
                         "start": datetime.fromtimestamp(spec.start, self.tz).date().isoformat(), "days": spec.days,
                         "fa": fa, "fb": fb, "score_a": sa, "la": a["castle"], "lb": b["castle"],
                         "pa": a["pop"], "pb": b["pop"]})
        self.store.save_matches(rows)

        wins_w = ev["fitness"]["wins"]
        fit = {gid: f_sum[gid] / max(1, n_games[gid]) + wins_w * s_sum[gid] / max(1, n_games[gid]) for gid in f_sum}
        for g in pop:
            g.meta.update(fitness=fit[g.id], elo=ratings[g.id], games=games[g.id])
        self.store.save_population(night, pop)                   # this night's genomes with their scores
        best = max(pop, key=lambda g: fit[g.id])
        groups = self.groups(pop)
        lineage_best = {}
        if groups:                                               # rivalry: each line's best joins its own Hall
            for x, group in groups.items():
                b = max(group, key=lambda g: fit[g.id])
                lineage_best[x] = b.id
                self.update_hall(night, b, ratings, games, lineage=x)
        else:
            self.update_hall(night, best, ratings, games)

        # Hall updates write through the shared connection. Release SQLite's single
        # writer before benchmark games run, so the live world can keep saving.
        self.store.db.commit()

        # the benchmark: tonight's top genomes plus the best one measured so far, so it is never forgotten
        top = sorted(pop, key=lambda g: fit[g.id], reverse=True)[: ev["benchmark"]["candidates"]]
        keeper = self.benchmark_keeper()
        if keeper and all(g.id != keeper.id for g in top):
            top.append(keeper)
        bench = self.benchmark(night, top)
        bench_id = max(bench, key=bench.get)
        bench_best = next(g for g in top if g.id == bench_id)

        balance = load_balance()
        boost = balance.pop("next_night", None)          # the referee found the genomes too alike
        rate = boost["mutation_rate"] if boost else None
        fresh = boost["random_genomes"] if boost else 0
        if groups:                                       # rivalry: each line breeds only with itself
            lines = list(groups)
            nexts = {x: self.next_generation(night, groups[x], fit, rng, mutation_rate=rate,
                                             random_genomes=-(-fresh // len(lines)), lineage=x) for x in lines}
            home = bench_best.meta.get("lineage")
            if home in nexts and all(g.id != bench_id for g in nexts[home]):
                nexts[home][-1] = bench_best             # the benchmark winner keeps a place in its own line
            nxt = [g for x in lines for g in nexts[x]]
        else:
            nxt = self.next_generation(night, pop, fit, rng, mutation_rate=rate, random_genomes=fresh)
        if boost:
            write_balance(balance)                       # the boost is used once
        if not groups and all(g.id != bench_id for g in nxt):   # the benchmark winner keeps a place to breed from
            nxt[-1] = bench_best
        for g in nxt:
            if g.id in ratings:
                g.meta["elo"] = ratings[g.id]
        self.store.save_population(night + 1, nxt)

        # version 3: paired games for the rules under test, then the morning lessons
        lab_results = lab.run_tests(night, champion, self.play_all, self.days, self.seed, self.tz) if (
            proposed or lab.rules_with("test", "probation") or lab.accepted()) else []
        lessons, by_ai = write_lessons(self.store, self.rules, night, self.llm)
        self.store.save_lessons(night, lessons, by_ai)

        d = F.diversity([g.genes for g in pop], self.rules.gene_ranges)
        seconds = time.perf_counter() - t0
        self.store.save_night(night, started=started, finished=datetime.now().isoformat(timespec="seconds"),
                              bench_id=bench_id, bench_fitness=bench[bench_id],
                              best_id=best.id, best_fitness=fit[best.id],
                              mean_fitness=sum(fit.values()) / len(fit), diversity=d, games=len(specs),
                              seconds=seconds, config={"population": self.size, "days": self.days,
                                                       "bench": bench_key(self.rules, self.days),
                                                       "peer_games": self.peer_games,
                                                       "champion_games": self.champion_games, "seed": self.seed,
                                                       "lineages": lineage_best})
        hall_now = [(g.id, round(g.meta["elo"])) for g in self.store.hall()]
        return NightReport(night, best.id, fit[best.id], sum(fit.values()) / len(fit), d, ratings[best.id],
                           len(specs), seconds, hall_now, fit, bench_id, bench[bench_id],
                           proposed=[r.text() for r in proposed], invalid=invalid, rule_results=lab_results,
                           lessons=lessons, lineage_best=lineage_best)
