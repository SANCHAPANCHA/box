"""Action log, debug clips on death and gameplay recording for future training."""
from __future__ import annotations

import collections
import json
import os
import time

import cv2


class Recorder:
    def __init__(self, cfg, fps: int, record: bool):
        self.cfg = cfg
        os.makedirs(cfg.log_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.actions = open(os.path.join(cfg.log_dir, f"actions-{stamp}.jsonl"), "a", encoding="utf-8")
        self.ring = collections.deque(maxlen=max(1, int(cfg.debug_seconds * fps)))
        self.fps = fps
        self.record = record
        if record:
            self.rec_dir = os.path.join(cfg.record_dir, stamp)
            os.makedirs(os.path.join(self.rec_dir, "frames"), exist_ok=True)
            self.rec = open(os.path.join(self.rec_dir, "samples.jsonl"), "a", encoding="utf-8")
            self.n = 0

    def log_action(self, action, st) -> None:
        self.actions.write(json.dumps({
            "t": round(time.time(), 3), "action": action.name, "conf": round(action.confidence, 2),
            "reason": action.reason, "lane": st.player.lane, "speed": round(st.speed, 1),
            "free": {str(k): round(v, 1) for k, v in st.lane_free.items()},
        }, ensure_ascii=False) + "\n")
        self.actions.flush()

    def push_debug(self, overlay_img) -> None:
        self.ring.append(overlay_img)

    def dump_death(self, reason: str = "death") -> str | None:
        if not self.cfg.save_debug_on_death or not self.ring:
            return None
        d = os.path.join(self.cfg.log_dir, "debug", time.strftime("%Y%m%d-%H%M%S") + f"-{reason}")
        os.makedirs(d, exist_ok=True)
        frames = list(self.ring)
        h, w = frames[-1].shape[:2]
        vw = cv2.VideoWriter(os.path.join(d, "last_seconds.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w, h))
        for f in frames:
            if f.shape[:2] == (h, w):
                vw.write(f)
        vw.release()
        cv2.imwrite(os.path.join(d, "last_frame.png"), frames[-1])
        self.ring.clear()
        return d

    def sample(self, frame_small, st, action_name: str | None, human_key: str | None) -> None:
        """One training sample: small frame + structured state + chosen/human action."""
        if not self.record:
            return
        name = f"{self.n:07d}.jpg"
        cv2.imwrite(os.path.join(self.rec_dir, "frames", name), frame_small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        self.rec.write(json.dumps({"frame": name, "state": st.to_dict(), "bot_action": action_name,
                                   "human_key": human_key}) + "\n")
        self.n += 1
