"""Structured game state produced by vision and consumed by decision engines."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class Screen(str, Enum):
    PLAYING = "playing"
    GAME_OVER = "game_over"
    PAUSED = "paused"
    MENU = "menu"        # title / loading / anything else
    UNKNOWN = "unknown"


@dataclass
class Player:
    x: float = 0.0           # screen px (centre of the detected body)
    y: float = 0.0           # screen px (bottom of the body)
    w: float = 0.0
    h: float = 0.0
    vx: float = 0.0          # screen px / s
    vy: float = 0.0
    world_x: float = 0.0     # world units, lanes at -2 / 0 / 2
    lane: int = 0            # -1 left, 0 centre, 1 right
    on_ground: bool = True
    alive: bool = True
    found: bool = False


@dataclass
class Obj:
    x: float                 # bbox in screen px
    y: float
    width: float
    height: float
    type: str                # "obstacle" | "coin"
    lanes: list[int] = field(default_factory=list)
    z: float = 0.0           # distance ahead of the player, world units (>0)
    track_id: int = -1
    vz: float = 0.0          # approach speed, world units / s (>0 = coming closer)
    ttc: float = 99.0        # time to collision, s
    confidence: float = 0.0


@dataclass
class GameState:
    t: float
    screen: Screen
    width: int
    height: int
    player: Player
    obstacles: list[Obj] = field(default_factory=list)
    targets: list[Obj] = field(default_factory=list)
    speed: float = 0.0       # estimated forward speed, world units / s
    lane_free: dict[int, float] = field(default_factory=dict)  # lane -> nearest obstacle z

    def to_dict(self) -> dict:
        d = asdict(self)
        d["screen"] = self.screen.value
        d["lane_free"] = {str(k): round(v, 2) for k, v in self.lane_free.items()}
        return d
