"""CMA-ES for the night's training: a smarter way to tune the genes than crossover + mutation.

Every gene is scaled to 0…1 inside its range. A lineage keeps a search distribution: a mean (the genes it
believes in), a step size σ and a covariance C (which genes should move together). Each night:
  1. the genomes of the night are ranked by their fitness;
  2. the mean moves toward the best half (weighted by rank), C learns the directions that worked, and σ grows
     when progress is steady and shrinks when it wanders;
  3. the next night's genomes are drawn from N(mean, σ²·C), clipped to the ranges.
Standard (μ/μ_w, λ)-CMA-ES (Hansen), written in plain Python (no numpy here; 20 genes is small).
The state is JSON, kept in SQLite between nights (one per lineage).
"""
from __future__ import annotations

import math
import random


def _params(n: int, lam: int) -> dict:
    mu = max(1, lam // 2)
    raw = [math.log(mu + 0.5) - math.log(i + 1) for i in range(mu)]
    s = sum(raw)
    w = [x / s for x in raw]
    mueff = 1.0 / sum(x * x for x in w)
    cs = (mueff + 2) / (n + mueff + 5)
    ds = 1 + 2 * max(0.0, math.sqrt((mueff - 1) / (n + 1)) - 1) + cs
    cc = (4 + mueff / n) / (n + 4 + 2 * mueff / n)
    c1 = 2 / ((n + 1.3) ** 2 + mueff)
    cmu = min(1 - c1, 2 * (mueff - 2 + 1 / mueff) / ((n + 2) ** 2 + mueff))
    chin = math.sqrt(n) * (1 - 1 / (4 * n) + 1 / (21 * n * n))
    return {"mu": mu, "w": w, "mueff": mueff, "cs": cs, "ds": ds, "cc": cc, "c1": c1, "cmu": cmu, "chin": chin}


def cholesky(c: list[list[float]]) -> list[list[float]]:
    """Lower-triangular L with L·Lᵀ = C (a little jitter keeps it positive definite)."""
    n = len(c)
    for jitter in (0.0, 1e-10, 1e-8, 1e-6, 1e-4):
        L = [[0.0] * n for _ in range(n)]
        ok = True
        for i in range(n):
            for j in range(i + 1):
                s = c[i][j] + (jitter if i == j else 0.0) - sum(L[i][k] * L[j][k] for k in range(j))
                if i == j:
                    if s <= 0:
                        ok = False
                        break
                    L[i][i] = math.sqrt(s)
                else:
                    L[i][j] = s / L[j][j]
            if not ok:
                break
        if ok:
            return L
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _solve_lower(L: list[list[float]], y: list[float]) -> list[float]:
    """z with L·z = y (so ‖z‖² = yᵀ C⁻¹ y)."""
    z = []
    for i in range(len(y)):
        z.append((y[i] - sum(L[i][k] * z[k] for k in range(i))) / L[i][i])
    return z


def new_state(mean: list[float], sigma: float = 0.2) -> dict:
    n = len(mean)
    return {"m": list(mean), "sigma": sigma, "C": [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)],
            "pc": [0.0] * n, "ps": [0.0] * n, "gen": 0}


def update(state: dict, ranked: list[list[float]], lam: int) -> dict:
    """One CMA-ES step from the night's points (0…1 coordinates), best first."""
    m, sigma, C, pc, ps, gen = state["m"], state["sigma"], state["C"], state["pc"], state["ps"], state["gen"]
    n = len(m)
    p = _params(n, lam)
    top = ranked[: p["mu"]]
    ys = [[(x[i] - m[i]) / sigma for i in range(n)] for x in top]
    yw = [sum(p["w"][k] * ys[k][i] for k in range(len(ys))) for i in range(n)]
    m2 = [min(1.0, max(0.0, m[i] + sigma * yw[i])) for i in range(n)]
    z = _solve_lower(cholesky(C), yw)
    a = math.sqrt(p["cs"] * (2 - p["cs"]) * p["mueff"])
    ps = [(1 - p["cs"]) * ps[i] + a * z[i] for i in range(n)]
    norm_ps = math.sqrt(sum(v * v for v in ps))
    hs = 1.0 if norm_ps / math.sqrt(1 - (1 - p["cs"]) ** (2 * (gen + 1))) < (1.4 + 2 / (n + 1)) * p["chin"] else 0.0
    b = math.sqrt(p["cc"] * (2 - p["cc"]) * p["mueff"])
    pc = [(1 - p["cc"]) * pc[i] + hs * b * yw[i] for i in range(n)]
    c1, cmu = p["c1"], p["cmu"]
    keep = 1 - c1 - cmu + c1 * (1 - hs) * p["cc"] * (2 - p["cc"])
    C2 = [[keep * C[i][j] + c1 * pc[i] * pc[j] + cmu * sum(p["w"][k] * ys[k][i] * ys[k][j] for k in range(len(ys)))
           for j in range(n)] for i in range(n)]
    sigma2 = sigma * math.exp((p["cs"] / p["ds"]) * (norm_ps / p["chin"] - 1))
    return {"m": m2, "sigma": min(0.5, max(0.02, sigma2)), "C": C2, "pc": pc, "ps": ps, "gen": gen + 1}


def sample(state: dict, k: int, rng: random.Random) -> list[list[float]]:
    """k new points from N(m, σ²C), clipped to 0…1."""
    m, sigma = state["m"], state["sigma"]
    L = cholesky(state["C"])
    n = len(m)
    out = []
    for _ in range(k):
        z = [rng.gauss(0.0, 1.0) for _ in range(n)]
        out.append([min(1.0, max(0.0, m[i] + sigma * sum(L[i][j] * z[j] for j in range(i + 1)))) for i in range(n)])
    return out
