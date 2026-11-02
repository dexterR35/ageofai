"""Train the kingdom's neural network with PPO (Proximal Policy Optimization), from reward only.

Run with the separate Python 3.12 + PyTorch environment, from the project folder:
    .venv-rl\\Scripts\\python -m rl.train_ppo                  # train until Ctrl+C (saves as it goes)
    .venv-rl\\Scripts\\python -m rl.train_ppo --updates 50     # a fixed number of updates
    .venv-rl\\Scripts\\python -m rl.train_ppo --fresh          # start again from zero

It plays `rl.envs` games at once (one process each). After every update it writes:
    data/rl/checkpoint.pt   the full training state (to continue later)
    data/rl/policy.json     the network for the game (plain numbers; the game runs it without PyTorch)
    data/rl/train_log.csv   one row per update: games finished, average reward, wins, losses, speed

The network: 49 inputs → 64 → 64 (tanh) → one choice per head (9 priorities × 3 levels, 1 war intent × 4),
plus a value estimate. PPO: clipped policy updates, GAE advantages, an entropy bonus so it keeps exploring.
"""
from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.rl import HEADS, OBS_SIZE  # noqa: E402

OUT = Path(os.environ.get("AGEOFAI_RL_OUT") or ROOT / "data" / "rl")   # the env var: trial runs elsewhere


# ---------------------------------------------------------------------------- games in parallel
def _worker(conn, seed: int, days: int | None) -> None:
    from rl.env import AgeEnv
    env = AgeEnv(seed=seed, days=days)
    obs = env.reset()
    while True:
        cmd, data = conn.recv()
        if cmd == "step":
            obs2, rew, done, info = env.step(data)
            if done:
                obs2 = env.reset()
            conn.send((obs2, rew, done, info))
        elif cmd == "obs":
            conn.send(obs)
        elif cmd == "close":
            conn.close()
            return


class Games:
    """N games, each in its own process (the game is plain Python: one core per game)."""

    def __init__(self, n: int, seed: int, days: int | None):
        ctx = mp.get_context("spawn")
        self.conns, self.procs = [], []
        for i in range(n):
            a, b = ctx.Pipe()
            p = ctx.Process(target=_worker, args=(b, seed * 1000 + i, days), daemon=True)
            p.start()
            self.conns.append(a)
            self.procs.append(p)

    def observe(self) -> np.ndarray:
        for c in self.conns:
            c.send(("obs", None))
        return np.array([c.recv() for c in self.conns], dtype=np.float32)

    def step(self, actions: np.ndarray):
        for c, a in zip(self.conns, actions):
            c.send(("step", [int(x) for x in a]))
        out = [c.recv() for c in self.conns]
        obs, rew, done, info = zip(*out)
        return np.array(obs, dtype=np.float32), np.array(rew, dtype=np.float32), np.array(done), list(info)

    def close(self) -> None:
        for c in self.conns:
            try:
                c.send(("close", None))
            except (BrokenPipeError, OSError):
                pass
        for p in self.procs:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()


# ---------------------------------------------------------------------------- the network
class Net(nn.Module):
    def __init__(self, hidden: int = 64):
        super().__init__()
        self.body = nn.Sequential(nn.Linear(OBS_SIZE, hidden), nn.Tanh(), nn.Linear(hidden, hidden), nn.Tanh())
        self.pi = nn.Linear(hidden, sum(HEADS))
        self.v_body = nn.Sequential(nn.Linear(OBS_SIZE, hidden), nn.Tanh(), nn.Linear(hidden, hidden), nn.Tanh())
        self.v = nn.Linear(hidden, 1)
        nn.init.orthogonal_(self.pi.weight, 0.01)
        nn.init.zeros_(self.pi.bias)

    def dists(self, x: torch.Tensor) -> list[torch.distributions.Categorical]:
        logits = self.pi(self.body(x))
        return [torch.distributions.Categorical(logits=part) for part in torch.split(logits, list(HEADS), dim=-1)]

    def value(self, x: torch.Tensor) -> torch.Tensor:
        return self.v(self.v_body(x)).squeeze(-1)


class RunningNorm:
    """Running mean/std of the inputs (saved with the network, so the game scales them the same way)."""

    def __init__(self):
        self.n, self.mean, self.m2 = 1e-4, np.zeros(OBS_SIZE), np.ones(OBS_SIZE)

    def update(self, x: np.ndarray) -> None:
        for row in x:
            self.n += 1
            d = row - self.mean
            self.mean += d / self.n
            self.m2 += d * (row - self.mean)

    @property
    def std(self) -> np.ndarray:
        return np.sqrt(np.maximum(self.m2 / self.n, 1e-8))

    def __call__(self, x: np.ndarray) -> np.ndarray:
        return np.clip((x - self.mean) / self.std, -5.0, 5.0).astype(np.float32)


def export(net: Net, norm: RunningNorm, updates: int, path: Path) -> None:
    """The policy for the game: plain lists of numbers (ai/rl.py `Policy` reads them, no PyTorch)."""
    lin = [m for m in net.body if isinstance(m, nn.Linear)]
    data = {"name": "ppo", "updates": updates, "obs_size": OBS_SIZE, "heads": list(HEADS),
            "obs_mean": norm.mean.tolist(), "obs_std": norm.std.tolist(),
            "layers": [{"W": m.weight.detach().tolist(), "b": m.bias.detach().tolist()} for m in lin],
            "head": {"W": net.pi.weight.detach().tolist(), "b": net.pi.bias.detach().tolist()}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)                                    # the game never reads half a file


# ---------------------------------------------------------------------------- PPO
def train(args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    torch.set_num_threads(1)                                 # the cores are for the games
    net = Net()
    opt = torch.optim.Adam(net.parameters(), lr=args.lr, eps=1e-5)
    norm = RunningNorm()
    updates, episodes = 0, 0
    ckpt = OUT / "checkpoint.pt"
    if ckpt.exists() and not args.fresh:
        st = torch.load(ckpt, weights_only=False)
        net.load_state_dict(st["net"])
        opt.load_state_dict(st["opt"])
        norm.n, norm.mean, norm.m2 = st["norm"]
        updates, episodes = st["updates"], st["episodes"]
        print(f"Continuing from update {updates} ({episodes} games played).", flush=True)
    log = OUT / "train_log.csv"
    new_log = not log.exists() or args.fresh
    logf = open(log, "w" if new_log else "a", newline="", encoding="utf-8")
    writer = csv.writer(logf)
    if new_log:
        writer.writerow(["update", "games", "avg_reward", "wins", "draws", "losses", "fallen", "avg_castle",
                         "steps_per_s", "time"])

    games = Games(args.envs, args.seed + updates, args.days)
    obs = games.observe()
    n, T = args.envs, args.steps
    ep_ret = np.zeros(n)
    try:
        while args.updates <= 0 or updates < args.updates:
            t0 = time.time()
            buf_o = np.zeros((T, n, OBS_SIZE), np.float32)
            buf_a = np.zeros((T, n, len(HEADS)), np.int64)
            buf_lp = np.zeros((T, n), np.float32)
            buf_v = np.zeros((T, n), np.float32)
            buf_r = np.zeros((T, n), np.float32)
            buf_d = np.zeros((T, n), np.float32)
            finished = []
            for t in range(T):
                norm.update(obs)
                x = torch.from_numpy(norm(obs))
                with torch.no_grad():
                    ds = net.dists(x)
                    acts = torch.stack([d.sample() for d in ds], dim=-1)
                    lp = sum(d.log_prob(acts[:, i]) for i, d in enumerate(ds))
                    v = net.value(x)
                obs2, rew, done, info = games.step(acts.numpy())
                buf_o[t], buf_a[t], buf_lp[t], buf_v[t] = x.numpy(), acts.numpy(), lp.numpy(), v.numpy()
                buf_r[t], buf_d[t] = rew, done.astype(np.float32)
                ep_ret += rew
                for i in range(n):
                    if done[i]:
                        finished.append((ep_ret[i], info[i]))
                        ep_ret[i] = 0.0
                obs = obs2
            with torch.no_grad():
                last_v = net.value(torch.from_numpy(norm(obs))).numpy()
            adv = np.zeros((T, n), np.float32)                # GAE
            gae = np.zeros(n, np.float32)
            for t in reversed(range(T)):
                nxt_v = last_v if t == T - 1 else buf_v[t + 1]
                nonterm = 1.0 - buf_d[t]
                delta = buf_r[t] + args.gamma * nxt_v * nonterm - buf_v[t]
                gae = delta + args.gamma * args.lam * nonterm * gae
                adv[t] = gae
            ret = adv + buf_v
            b_o = torch.from_numpy(buf_o.reshape(T * n, -1))
            b_a = torch.from_numpy(buf_a.reshape(T * n, -1))
            b_lp = torch.from_numpy(buf_lp.reshape(-1))
            b_adv = torch.from_numpy(adv.reshape(-1))
            b_ret = torch.from_numpy(ret.reshape(-1))
            b_adv = (b_adv - b_adv.mean()) / (b_adv.std() + 1e-8)
            idx = np.arange(T * n)
            for _ in range(args.epochs):
                np.random.shuffle(idx)
                for s in range(0, T * n, args.batch):
                    mb = torch.from_numpy(idx[s:s + args.batch])
                    ds = net.dists(b_o[mb])
                    lp = sum(d.log_prob(b_a[mb, i]) for i, d in enumerate(ds))
                    ent = sum(d.entropy() for d in ds).mean()
                    ratio = torch.exp(lp - b_lp[mb])
                    pg = -torch.min(ratio * b_adv[mb],
                                    torch.clamp(ratio, 1 - args.clip, 1 + args.clip) * b_adv[mb]).mean()
                    vl = ((net.value(b_o[mb]) - b_ret[mb]) ** 2).mean()
                    loss = pg + 0.5 * vl - args.ent * ent
                    opt.zero_grad()
                    loss.backward()
                    nn.utils.clip_grad_norm_(net.parameters(), 0.5)
                    opt.step()
            updates += 1
            episodes += len(finished)
            res = [f[1].get("result", 0.5) for f in finished]
            wins, draws, losses = sum(r == 1.0 for r in res), sum(r == 0.5 for r in res), sum(r == 0.0 for r in res)
            fallen = sum(not f[1].get("alive", True) for f in finished)
            avg = float(np.mean([f[0] for f in finished])) if finished else float("nan")
            castle = float(np.mean([f[1].get("castle", 0) for f in finished])) if finished else float("nan")
            sps = T * n / max(1e-6, time.time() - t0)
            writer.writerow([updates, episodes, round(avg, 4), wins, draws, losses, fallen, round(castle, 2),
                             round(sps, 1), time.strftime("%Y-%m-%d %H:%M:%S")])
            logf.flush()
            torch.save({"net": net.state_dict(), "opt": opt.state_dict(), "norm": (norm.n, norm.mean, norm.m2),
                        "updates": updates, "episodes": episodes}, ckpt)
            export(net, norm, updates, OUT / "policy.json")
            print(f"update {updates}: {len(finished)} games done (W{wins} D{draws} L{losses}, fell {fallen}), "
                  f"avg reward {avg:+.3f}, castle {castle:.1f}, {sps:.0f} steps/s", flush=True)
    except KeyboardInterrupt:
        print("Stopped; the last update is saved.", flush=True)
    finally:
        games.close()
        logf.close()


def main(argv=None) -> None:
    sys.path.insert(0, str(ROOT))
    from engine.rules import default_rules
    cfg = default_rules().raw.get("rl", {}) or {}
    p = argparse.ArgumentParser(description="Train the kingdom's network with PPO (reward only).")
    p.add_argument("--envs", type=int, default=int(cfg.get("envs", 6)), help="games at once (one process each)")
    p.add_argument("--days", type=int, default=int(cfg.get("episode_days", 60)), help="game days per episode")
    p.add_argument("--steps", type=int, default=128, help="decisions per game per update")
    p.add_argument("--updates", type=int, default=0, help="stop after this many updates (0 = until Ctrl+C)")
    p.add_argument("--epochs", type=int, default=4)
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--gamma", type=float, default=0.99)
    p.add_argument("--lam", type=float, default=0.95)
    p.add_argument("--clip", type=float, default=0.2)
    p.add_argument("--ent", type=float, default=0.01)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--fresh", action="store_true", help="start again from zero (ignore the checkpoint)")
    train(p.parse_args(argv))


if __name__ == "__main__":
    main()
