"""Debug overlay: draws the vision output on top of the processed frame."""
from __future__ import annotations

import cv2
import numpy as np

from .geometry import LANES
from .state import GameState, Screen

GREEN, RED, YELLOW, CYAN, WHITE = (60, 220, 60), (40, 40, 255), (0, 220, 255), (255, 220, 0), (255, 255, 255)


def _text(img, s, org, color=WHITE, scale=0.45, thick=1):
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


def draw(img: np.ndarray, st: GameState, vision, info: dict) -> np.ndarray:
    out = img.copy()
    h, w = out.shape[:2]
    if st.screen == Screen.PLAYING:
        an = vision.debug.get("anomaly")
        if an is not None:
            tint = out.copy()
            tint[an > 0] = (0, 0, 255)
            out = cv2.addWeighted(out, 0.7, tint, 0.3, 0)
        cam = vision._camera(w, h, st.player)
        for x in (-3, -1, 1, 3):  # lane boundaries on the ground
            pts = [tuple(int(v) for v in cam.project(x, 0, -z)) for z in (0, 45)]
            cv2.line(out, pts[0], pts[1], CYAN, 1, cv2.LINE_AA)
        for ln in LANES:  # free distance per lane
            z = st.lane_free.get(ln, 45)
            a = cam.project(ln * 2, 0, -0.6)
            b = cam.project(ln * 2, 0, -min(z, 45))
            col = GREEN if z > 20 else YELLOW if z > 9 else RED
            cv2.line(out, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), col, 3, cv2.LINE_AA)
        p = st.player
        if p.found or p.w:
            x0, y0 = int(p.x - p.w / 2), int(p.y - p.h)
            cv2.rectangle(out, (x0, y0), (int(x0 + p.w), int(p.y)), GREEN if p.found else (128, 128, 128), 2)
            _text(out, f"lane {p.lane:+d} {'ground' if p.on_ground else 'AIR'}", (x0, y0 - 5), GREEN)
            traj = info.get("trajectory")  # predicted world x over time
            if traj:
                pts = [tuple(int(v) for v in cam.project(x, 0, -z)) for x, z in traj]
                for a, b in zip(pts, pts[1:]):
                    cv2.line(out, a, b, (255, 0, 255), 2, cv2.LINE_AA)
        for o in st.obstacles:
            cv2.rectangle(out, (int(o.x), int(o.y)), (int(o.x + o.width), int(o.y + o.height)), RED, 2)
            _text(out, f"z{o.z:.0f} t{o.ttc:.1f}", (int(o.x), int(o.y) - 3), RED, 0.38)
        for o in st.targets:
            cv2.rectangle(out, (int(o.x), int(o.y)), (int(o.x + o.width), int(o.y + o.height)), YELLOW, 1)

    lines = [
        f"{'BOT ACTIVE' if info.get('active') else 'BOT PAUSED'}  [{st.screen.value}]",
        f"FPS {info.get('fps', 0):.0f}  latency {info.get('lat_ms', 0):.0f}ms",
        f"capture {info.get('cap_ms', 0):.0f}ms  vision {info.get('vis_ms', 0):.0f}ms",
        f"speed {st.speed:.1f} u/s  free L{st.lane_free.get(-1, 0):.0f} C{st.lane_free.get(0, 0):.0f} R{st.lane_free.get(1, 0):.0f}",
        f"action {info.get('action', '-')}  conf {info.get('confidence', 0):.2f}",
        f"{info.get('reason', '')}",
        "F8 stop  F9 pause  F10 test action",
    ]
    lines += ["", "state:"] + info.get("state_lines", [])
    panel = np.zeros((h, 300, 3), np.uint8)
    for i, s in enumerate(lines):
        col = (GREEN if info.get("active") else YELLOW) if i == 0 else WHITE
        _text(panel, s, (6, 18 + 16 * i), col, 0.5 if i == 0 else 0.38)
    return np.hstack([out, panel])
