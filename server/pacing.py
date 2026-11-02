"""A continuous game clock that gives delayed AI decisions more real time."""
from __future__ import annotations


class AdaptiveClock:
    def __init__(self, game_time: float, wall_time: float, speed: float, config: dict):
        self.base_speed = speed
        self.speed = speed
        self.config = config
        self.reason = "Normal pace"
        self._target = game_time
        self._wall = wall_time
        self._clear_since: float | None = None
        self._last_recovery = wall_time

    def target(self, now: float) -> float:
        self._target += max(0.0, now - self._wall) * self.speed
        self._wall = now
        return self._target

    def rebase(self, game_time: float, now: float) -> None:
        self._target = game_time
        self._wall = now

    def update(self, game_time: float, now: float, waits: list[float], crisis: bool) -> dict:
        # Integrate the old speed first. Changing speed never reinterprets time
        # already spent waiting and never produces a catch-up jump.
        self.target(now)
        lead = float(self.config.get("max_lead_game_seconds", 60))
        self._target = max(game_time, min(self._target, game_time + lead))
        oldest = max(waits, default=0.0)
        minimum = min(self.base_speed, float(self.config.get("minimum_speed", 1)))
        desired, reason = self.base_speed, "Normal pace"
        if waits and crisis and oldest >= float(self.config.get("crisis_grace_seconds", 30)):
            desired, reason = minimum, "Waiting for an AI decision during a crisis"
        elif waits and oldest >= float(self.config.get("slow_after_seconds", 600)):
            desired, reason = minimum, "AI decision is unusually slow; tactical AI continues"
        elif waits and oldest >= float(self.config.get("grace_seconds", 180)):
            desired = min(self.base_speed, float(self.config.get("waiting_speed", 3)))
            reason = "Giving a delayed AI decision more thinking time"

        if desired < self.speed:
            self.speed = desired
            self._clear_since = None
        elif desired > self.speed:
            if self._clear_since is None:
                self._clear_since = now
            if (now - self._clear_since >= float(self.config.get("recovery_delay_seconds", 15))
                    and now - self._last_recovery >= float(self.config.get("recovery_step_seconds", 5))):
                self.speed = min(desired, self.speed * 2)
                self._last_recovery = now
            if desired == self.base_speed and self.speed < desired:
                reason = "Gradually returning to normal pace"
        else:
            self._clear_since = None
        self.reason = reason
        return {"speed": round(self.speed, 3), "base_speed": self.base_speed,
                "pace_reason": reason, "pending_decisions": len(waits),
                "oldest_decision_seconds": round(oldest)}
