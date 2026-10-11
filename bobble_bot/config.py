"""Bot settings, stored in config.json next to the working directory."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

CONFIG_PATH = "config.json"
GAME_URL = "https://unboxbobble.com/run"


@dataclass
class BotConfig:
    # game canvas on screen, physical pixels: {"left", "top", "width", "height"}
    region: dict | None = None
    target_fps: int = 30
    # keys are sent only while the foreground window title contains this text
    window_title_contains: str = "BOBBLE"
    # vision
    y_offset_px: float = 0.0
    dev_threshold: float = 16.0
    # decision
    react_time: float = 0.45       # s of look-ahead before dodging
    jump_time: float = 0.28        # s before impact to jump when no lane is free
    min_action_gap: float = 0.12   # s between any two actions
    restart_delay: float = 1.5     # s after game over before pressing Enter
    chase_coins: bool = True
    # debug / logging
    show_overlay: bool = True
    overlay_pos: list = field(default_factory=lambda: [0, 0])
    save_debug_on_death: bool = True
    debug_seconds: float = 4.0
    log_dir: str = "logs"
    record_dir: str = "dataset"

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "BotConfig":
        if not os.path.exists(path):
            return cls()
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: str = CONFIG_PATH) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
