"""Decision engines: GameState -> Action.

RuleBasedEngine is the baseline. Any object with a `decide(state, now)` method
returning an Action (or None) can replace it, e.g. a learned policy:
    python -m bobble_bot --play --engine my_policy:Engine
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field

from .geometry import LANE_W
from .state import GameState, Screen

LATERAL_SPEED = 15.0   # world units / s (latSpeed 250 per tick at 60 Hz)


@dataclass
class Action:
    name: str                      # left | right | jump | slide | restart | noop
    key: str | None = None
    hold: float = 0.05             # key hold duration, s
    confidence: float = 1.0
    reason: str = ""
    trajectory: list = field(default_factory=list)  # [(world_x, z)] for the overlay


class DecisionEngine:
    def decide(self, st: GameState, now: float) -> Action | None:
        raise NotImplementedError


class RuleBasedEngine(DecisionEngine):
    def __init__(self, cfg):
        self.cfg = cfg
        self.last_action_t = -1e9
        self.last_jump_t = -1e9
        self.last_restart_t = -1e9
        self.not_playing_since: float | None = None
        self.target_lane: int | None = None
        self.target_until = 0.0

    def _traj(self, st: GameState, lane_to: int) -> list:
        p, v = st.player, max(st.speed, 5.0)
        out, x = [], p.world_x
        for i in range(12):
            t = i * 0.08
            goal = lane_to * LANE_W
            x = x + max(-LATERAL_SPEED * 0.08, min(LATERAL_SPEED * 0.08, goal - x)) if i else x
            out.append((x, v * t))
        return out

    def decide(self, st: GameState, now: float) -> Action | None:
        cfg = self.cfg
        if st.screen != Screen.PLAYING:
            self.not_playing_since = self.not_playing_since or now
            waited = now - self.not_playing_since
            if st.screen in (Screen.GAME_OVER, Screen.PAUSED, Screen.MENU) and waited > cfg.restart_delay \
                    and now - self.last_restart_t > 2.5:
                self.last_restart_t = now
                # Play / Resume / Run again are focused buttons, Enter activates them
                return Action("restart", "enter", 0.06, 0.9, f"{st.screen.value}: press Enter")
            return None
        self.not_playing_since = None
        p = st.player
        if not p.found and p.w == 0:
            return Action("noop", confidence=0.2, reason="player not found")
        # p.found False with a size = last known position (briefly occluded)
        if now - self.last_action_t < cfg.min_action_gap:
            return None

        v = max(st.speed, 5.0)
        danger = v * cfg.react_time + 2.5
        jump_z = v * cfg.jump_time + 1.0
        lane = p.lane
        if self.target_lane is not None and now < self.target_until:
            lane = self.target_lane  # a lane change is still in flight
        free = st.lane_free
        here = free.get(lane, 99)

        def reachable(target: int) -> float:
            # moving two lanes passes through the middle one
            path = range(min(lane, target), max(lane, target) + 1)
            return min(free.get(ln, 99) for ln in path if ln != lane)

        if here > danger:
            if cfg.chase_coins:
                coins = sorted((c for c in st.targets if lane not in c.lanes and c.z < danger * 1.6),
                               key=lambda c: c.z)
                min_z = v * 0.3 + 2.0  # closer coins can't be reached in time
                for c in coins:
                    tl = c.lanes[0]
                    if c.z > min_z and abs(tl - lane) == 1 and reachable(tl) > max(c.z + 6, danger * 1.5):
                        return self._move(st, now, lane, tl, 0.5, f"coin in lane {tl:+d} at z{c.z:.0f}")
            return Action("noop", confidence=min(1.0, here / (danger * 2)),
                          reason=f"lane clear ({here:.0f} > {danger:.0f})",
                          trajectory=self._traj(st, lane))

        options = [(reachable(t), t) for t in (lane - 1, lane + 1) if -1 <= t <= 1]
        options = [(z, t) for z, t in options if z > here + 2 and z > danger * 0.7]
        if options:
            z, t = max(options)
            conf = min(1.0, (z - here) / danger)
            return self._move(st, now, lane, t, conf, f"obstacle z{here:.0f} in lane {lane:+d}, lane {t:+d} free {z:.0f}")
        if here < jump_z and p.on_ground and now - self.last_jump_t > 0.35:
            self.last_jump_t = self.last_action_t = now
            return Action("jump", "jump", 0.08, 0.5, f"blocked everywhere, jump at z{here:.1f}",
                          self._traj(st, lane))
        return Action("noop", confidence=0.3, reason=f"waiting to jump (z{here:.0f})",
                      trajectory=self._traj(st, lane))

    def _move(self, st, now, lane, target, conf, reason) -> Action:
        self.last_action_t = now
        self.target_lane, self.target_until = target, now + 0.25
        name = "left" if target < lane else "right"
        return Action(name, name, 0.05, conf, reason, self._traj(st, target))


def load_engine(spec: str | None, cfg) -> DecisionEngine:
    if not spec:
        return RuleBasedEngine(cfg)
    mod, _, attr = spec.partition(":")
    cls = getattr(importlib.import_module(mod), attr or "Engine")
    return cls(cfg)
