"""Screen capture of the game region with mss."""
from __future__ import annotations

import time

import mss
import numpy as np


class ScreenCapture:
    def __init__(self, region: dict):
        self.region = {k: int(region[k]) for k in ("left", "top", "width", "height")}
        self.sct = mss.mss()
        self.last_ms = 0.0

    def grab(self) -> np.ndarray:
        t0 = time.perf_counter()
        shot = self.sct.grab(self.region)
        frame = np.asarray(shot)[:, :, :3]  # BGRA -> BGR (view)
        self.last_ms = (time.perf_counter() - t0) * 1000
        return np.ascontiguousarray(frame)

    def full_screen(self, monitor: int = 0) -> tuple[np.ndarray, dict]:
        mon = self.sct.monitors[monitor]
        return np.ascontiguousarray(np.asarray(self.sct.grab(mon))[:, :, :3]), mon
