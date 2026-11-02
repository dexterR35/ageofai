"""The genome: an AI's personality as numbers inside fixed ranges.

Version 1 plays with it; version 2 evolves it (crossover + mutation every night).
"""
from __future__ import annotations

import math
import random as _random
from dataclasses import dataclass, field

from engine.rules import Rules


@dataclass
class Genome:
    genes: dict[str, float]
    id: str = "default"
    parents: tuple[str, ...] = ()
    generation: int = 0
    meta: dict = field(default_factory=dict)

    def __getitem__(self, key: str) -> float:
        return self.genes[key]

    def get(self, key: str, default: float = 1.0) -> float:
        return self.genes.get(key, default)

    # ------------------------------------------------------------------ factories
    @classmethod
    def default(cls, rules: Rules) -> "Genome":
        return cls(dict(rules.genome["default"]), id="default").clamped(rules)

    @classmethod
    def live_start(cls, rules: Rules) -> list["Genome"]:
        """The live world's first minds, one per kingdom (rules.yaml → genome.live_start): the default genes
        with each kingdom's own character on top. Kingdoms without one play the default genome."""
        start = rules.genome.get("live_start") or {}
        out = []
        for spec in rules.civs:
            p = dict(start.get(spec["id"]) or {})
            name = p.pop("name", None)
            genes = {**rules.genome["default"], **{k: float(v) for k, v in p.items()}}
            out.append(cls(genes, id=f"start-{name}" if name else "default").clamped(rules))
        return out

    @classmethod
    def random(cls, rules: Rules, rng: _random.Random, id: str = "random") -> "Genome":
        return cls({k: rng.uniform(lo, hi) for k, (lo, hi) in rules.gene_ranges.items()}, id=id)

    def clamped(self, rules: Rules) -> "Genome":
        out = dict(self.genes)
        for k, (lo, hi) in rules.gene_ranges.items():
            out[k] = min(hi, max(lo, out.get(k, (lo + hi) / 2)))
        return Genome(out, self.id, self.parents, self.generation, dict(self.meta))

    # ------------------------------------------------------------------ evolution
    @staticmethod
    def crossover(a: "Genome", b: "Genome", rng: _random.Random, id: str = "child") -> "Genome":
        """Each gene comes from one parent at random."""
        genes = {k: (a.genes[k] if rng.random() < 0.5 else b.genes[k]) for k in a.genes}
        return Genome(genes, id=id, parents=(a.id, b.id), generation=max(a.generation, b.generation) + 1)

    def mutated(self, rules: Rules, rng: _random.Random, rate: float = 0.15, scale: float = 0.10) -> "Genome":
        """With probability `rate`, a gene moves by ±`scale` of its range."""
        out = dict(self.genes)
        for k, (lo, hi) in rules.gene_ranges.items():
            if rng.random() < rate:
                out[k] = min(hi, max(lo, out[k] + rng.choice((-1, 1)) * scale * (hi - lo)))
        return Genome(out, self.id, self.parents, self.generation, dict(self.meta))

    def distance(self, other: "Genome", rules: Rules) -> float:
        """Normalized RMS distance (0 = identical); used by the referee's diversity check."""
        ranges = rules.gene_ranges
        s = sum(((self.genes[k] - other.genes[k]) / (hi - lo)) ** 2 for k, (lo, hi) in ranges.items())
        return math.sqrt(s / len(ranges))
