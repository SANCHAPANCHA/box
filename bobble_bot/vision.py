"""Computer vision: frame -> GameState.

Baseline without neural nets:
  * player  - the bright green shirt inside the expected body region;
  * road    - per-row colour model (median of the row, backed by a temporal
              reference so a wall covering all lanes is still visible);
  * objects - pixels deviating from the road model -> connected components ->
              ground contact point -> lane + distance via the camera model;
  * coins   - gold colour blobs on the road;
  * screen  - HUD pause button => playing; receipt / pause card otherwise.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import cv2
import numpy as np

from .geometry import LANE_W, LANES, ROAD_HALF, SHIRT_Y, Camera, CameraParams, PlayerXMapper
from .state import GameState, Obj, Player, Screen

PROC_W = 640          # frames are processed at this width
Z_NEAR = 0.6          # world units in front of the player feet
Z_FAR = 45.0


@dataclass
class VisionConfig:
    y_offset_px: float = 0.0       # vertical calibration of the road model (proc px)
    dev_threshold: float = 16.0    # minimal colour deviation from the road
    mad_k: float = 3.0             # adaptive threshold multiplier
    line_strip: float = 0.22       # half width of masked lane-line strips (world)
    pause_btn_threshold: float = 0.55


class Tracker:
    def __init__(self):
        self.tracks: dict[int, dict] = {}
        self.next_id = 1

    def update(self, objs: list[Obj], t: float) -> float:
        new: dict[int, dict] = {}
        speeds = []
        for o in sorted(objs, key=lambda o: o.z):
            best, best_d = None, 3.5
            for tid, tr in self.tracks.items():
                if tid in new or tr["type"] != o.type or not set(tr["lanes"]) & set(o.lanes):
                    continue
                dt = t - tr["t"]
                pred = tr["z"] - tr["vz"] * dt
                d = abs(pred - o.z)
                if d < best_d and o.z <= tr["z"] + 1.0:
                    best, best_d = tid, d
            if best is None:
                tid = self.next_id
                self.next_id += 1
                new[tid] = {"z": o.z, "vz": 0.0, "t": t, "age": 1, "type": o.type, "lanes": o.lanes}
            else:
                tr = self.tracks[best]
                dt = max(1e-3, t - tr["t"])
                inst = (tr["z"] - o.z) / dt
                vz = inst if tr["age"] == 1 else 0.6 * tr["vz"] + 0.4 * inst
                tid = best
                new[tid] = {"z": o.z, "vz": vz, "t": t, "age": tr["age"] + 1, "type": o.type, "lanes": o.lanes}
                if tr["age"] >= 2 and 2.0 < vz < 60.0:
                    speeds.append(vz)
            o.track_id, o.vz = tid, new[tid]["vz"]
        self.tracks = new
        return float(np.median(speeds)) if speeds else 0.0


class Vision:
    def __init__(self, cfg: VisionConfig | None = None, cam_params: CameraParams | None = None):
        self.cfg = cfg or VisionConfig()
        self.cam_params = cam_params or CameraParams()
        self.size = None
        self.prev_player: Player | None = None
        self.prev_t = None
        self.row_ref: np.ndarray | None = None
        self.tracker = Tracker()
        self.speed = 15.0
        self.jump_h = 0.0
        self.debug: dict = {}

    # ---------- setup ----------
    def _setup(self, w: int, h: int):
        self.size = (w, h)
        self.mapper = PlayerXMapper(w, h, self.cam_params, self.cfg.y_offset_px)
        c0 = Camera(w, h, 0, self.cam_params, self.cfg.y_offset_px)
        self.feet_y = c0.project(0, 0, 0)[1]
        self.ppu = c0.focal / np.linalg.norm(c0.pos - np.array([0, 1.0, 0]))  # px per world unit at player
        self.row_ref = None
        self.templates = self._pause_templates()

    def _pause_templates(self):
        out = []
        for d in range(18, 60, 4):
            t = np.full((d + 6, d + 6), 140, np.uint8)
            c = (d + 6) // 2
            cv2.circle(t, (c, c), d // 2, 20, -1)
            cv2.circle(t, (c, c), int(d * 0.42), 245, -1)
            bw, bh = max(2, d // 9), int(d * 0.36)
            for sx in (-1, 1):
                x = c + sx * int(d * 0.11)
                cv2.rectangle(t, (x - bw // 2, c - bh // 2), (x + bw // 2, c + bh // 2), 20, -1)
            out.append(t)
        return out

    # ---------- screen ----------
    def classify_screen(self, img: np.ndarray, gray: np.ndarray, hsv: np.ndarray) -> tuple[Screen, float]:
        h, w = gray.shape
        roi = gray[: int(h * 0.16), int(w * 0.80):]
        best = 0.0
        for t in self.templates:
            if t.shape[0] < roi.shape[0] and t.shape[1] < roi.shape[1]:
                best = max(best, float(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED).max()))
        self.debug["pause_btn"] = best
        if best >= self.cfg.pause_btn_threshold:
            return Screen.PLAYING, best
        cream = (hsv[..., 2] > 225) & (hsv[..., 1] < 45)
        receipt = cream[int(h * 0.3): int(h * 0.85), int(w * 0.16): int(w * 0.55)].mean()
        tan = cv2.inRange(hsv, (10, 40, 150), (30, 140, 255)) > 0
        card = tan[int(h * 0.3): int(h * 0.7), int(w * 0.38): int(w * 0.62)].mean()
        self.debug["receipt"], self.debug["card"] = float(receipt), float(card)
        if receipt > 0.35:
            return Screen.GAME_OVER, receipt
        if card > 0.3:
            return Screen.PAUSED, card
        return Screen.MENU, 1 - best

    # ---------- player ----------
    def detect_player(self, hsv: np.ndarray, t: float) -> Player:
        h, w = hsv.shape[:2]
        y0 = int(max(0, self.feet_y - 3.6 * self.ppu))
        y1 = int(min(h, self.feet_y + 0.1 * self.ppu))
        x0 = int(max(0, self.mapper.screen_x(-2.9)))
        x1 = int(min(w, self.mapper.screen_x(2.9)))
        mask = cv2.inRange(hsv[y0:y1, x0:x1], (38, 110, 90), (85, 255, 255))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        n, _, stats, cent = cv2.connectedComponentsWithStats(mask)
        exp_area = (0.62 * self.ppu) * (0.5 * self.ppu)
        dark = hsv[..., 2] < 70  # cap / hair above the shirt
        prev = self.prev_player if self.prev_player and self.prev_player.found else None
        best, best_score = None, -1e9
        for i in range(1, n):
            bx, by, bw, bh, area = stats[i]
            if not (0.15 * exp_area <= area <= 4.0 * exp_area) or bw > 1.6 * self.ppu:
                continue
            cx, cy = cent[i][0] + x0, cent[i][1] + y0
            hy0, hy1 = int(max(0, by + y0 - 0.9 * self.ppu)), int(by + y0)
            hx0, hx1 = int(max(0, cx - 0.35 * self.ppu)), int(cx + 0.35 * self.ppu)
            head = dark[hy0:hy1, hx0:hx1]
            head_dark = float(head.mean()) if head.size else 0.0
            if head_dark < 0.12:
                continue
            score = min(area / exp_area, 1.5) + head_dark
            if prev is not None:
                score -= abs(cx - prev.x) / self.ppu * 0.5
            if score > best_score:
                best, best_score = (cx, cy, bw, bh), score
        p = Player()
        if best is None:
            if prev is not None:  # keep last estimate, flag as not found
                p = Player(**{**prev.__dict__, "found": False, "vx": 0.0, "vy": 0.0})
            self.prev_player = p
            return p
        cx, cy, bw, bh = best
        jump_h = self.mapper.jump_height(cy)
        p.world_x = self.mapper.world_x(cx)
        self.jump_h = jump_h
        p.lane = int(np.clip(round(p.world_x / LANE_W), -1, 1))
        p.w, p.h = 1.0 * self.ppu, 1.9 * self.ppu
        p.x, p.y = cx, cy + SHIRT_Y * self.ppu
        p.on_ground = jump_h < 0.25
        p.found = True
        if prev is not None and self.prev_t is not None and t > self.prev_t:
            dt = t - self.prev_t
            p.vx, p.vy = (p.x - prev.x) / dt, (p.y - prev.y) / dt
        self.prev_player = p
        return p

    def _camera(self, w, h, player: Player) -> Camera:
        if not player.found:
            return Camera(w, h, 0.0, self.cam_params, self.cfg.y_offset_px)
        return Camera(w, h, player.world_x, self.cam_params, self.cfg.y_offset_px,
                      ay=0.0 if player.on_ground else self.jump_h)

    # ---------- road objects ----------
    def _road_rows(self, cam: Camera, h: int):
        y_near = cam.project(0, 0, -Z_NEAR)[1]
        y_far = cam.project(0, 0, -Z_FAR)[1]
        rows = np.arange(int(np.ceil(y_far)), int(min(h - 1, y_near)))
        spans = []
        for y in rows:
            g = cam.ground_at(cam.w / 2, y)
            z = g[1]
            xs = [cam.project(x, 0, z)[0] for x in (-ROAD_HALF, -1.0, 1.0, ROAD_HALF)]
            px_per_u = (xs[3] - xs[0]) / (2 * ROAD_HALF)
            spans.append((xs, px_per_u, -z))
        return rows, spans

    def detect_objects(self, img: np.ndarray, lab: np.ndarray, hsv: np.ndarray, player: Player):
        h, w = img.shape[:2]
        cam = self._camera(w, h, player)
        rows, spans = self._road_rows(cam, h)
        road = np.zeros((h, w), bool)
        valid = np.zeros((h, w), bool)
        strip = self.cfg.line_strip
        for y, (xs, ppu, _) in zip(rows, spans):
            a, b = int(max(0, xs[0] + strip * ppu)), int(min(w, xs[3] - strip * ppu))
            if b <= a:
                continue
            road[y, a:b] = True
            valid[y, a:b] = True
            for lx in xs[1:3]:
                valid[y, int(max(0, lx - strip * ppu)): int(min(w, lx + strip * ppu))] = False
        if player.found:  # the runner himself is not an obstacle
            x0, x1 = int(player.x - 0.62 * player.w), int(player.x + 0.62 * player.w)
            y0, y1 = int(player.y - player.h * 1.1), int(player.y + 0.1 * player.h)
            valid[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = False

        labf = lab.astype(np.float32)
        n_rows = len(rows)
        med = np.zeros((n_rows, 3), np.float32)
        thr = np.full(n_rows, self.cfg.dev_threshold, np.float32)
        dev = np.zeros((h, w), np.float32)
        wts = np.array([0.35, 1.0, 1.0], np.float32)
        for i, y in enumerate(rows):
            m = valid[y]
            if m.sum() < 8:
                continue
            px = labf[y, m]
            med[i] = np.median(px, axis=0)
        if self.row_ref is None or len(self.row_ref) != n_rows:
            self.row_ref = med.copy()
        ref_gap = np.sqrt((((med - self.row_ref) ** 2) * wts).sum(1))
        covered = ref_gap > 2 * self.cfg.dev_threshold      # row mostly hidden by a wide object
        base = np.where(covered[:, None], self.row_ref, med)
        for i, y in enumerate(rows):
            m = road[y]
            if not m.any():
                continue
            d = np.sqrt((((labf[y] - base[i]) ** 2) * wts).sum(1))
            vm = valid[y]
            if vm.sum() >= 8:
                mad = np.median(np.abs(d[vm] - np.median(d[vm])))
                thr[i] = max(self.cfg.dev_threshold, self.cfg.mad_k * 1.48 * mad + np.median(d[vm]))
            dev[y] = np.where(valid[y], d, 0)
        # slow temporal reference; fast enough to follow biome changes (~5 s)
        upd = ~covered
        self.row_ref[upd] = 0.9 * self.row_ref[upd] + 0.1 * med[upd]
        self.row_ref[~upd] = 0.98 * self.row_ref[~upd] + 0.02 * med[~upd]

        anomaly = np.zeros((h, w), np.uint8)
        for i, y in enumerate(rows):
            anomaly[y] = (dev[y] > thr[i]).astype(np.uint8) * 255
        coin = cv2.inRange(hsv, (12, 110, 140), (32, 255, 255))
        coin[~road] = 0
        anomaly[cv2.dilate(coin, np.ones((7, 7), np.uint8)) > 0] = 0
        if self._banner(hsv):  # level banner card floats over the road for a few seconds
            cream = (hsv[..., 2] > 200) & (hsv[..., 1] < 60)
            anomaly[cv2.dilate(cream.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0] = 0
        anomaly = cv2.morphologyEx(anomaly, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        anomaly = cv2.morphologyEx(anomaly, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
        self.debug["anomaly"] = anomaly

        row_index = {int(y): i for i, y in enumerate(rows)}
        obstacles = self._components(anomaly, cam, spans, row_index, "obstacle", min_obj=0.45)
        coin = cv2.morphologyEx(coin, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
        targets = self._components(coin, cam, spans, row_index, "coin", min_obj=0.25)
        return obstacles, targets

    def _banner(self, hsv) -> bool:
        h, w = hsv.shape[:2]
        reg = hsv[int(h * 0.12): int(h * 0.55), int(w * 0.3): int(w * 0.75)]
        frac = float(((reg[..., 2] > 215) & (reg[..., 1] < 45)).mean())
        self.debug["banner"] = frac
        return frac > 0.25

    def _components(self, mask, cam, spans, row_index, kind, min_obj):
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        out = []
        for i in range(1, n):
            bx, by, bw, bh, area = stats[i]
            bottom = by + bh - 1
            ri = row_index.get(int(bottom))
            if ri is None:
                continue
            xs, ppu, z = spans[ri]
            if area < (min_obj * ppu) ** 2 * 0.5:
                continue
            wx0 = (bx - xs[0]) / ppu - ROAD_HALF
            wx1 = (bx + bw - xs[0]) / ppu - ROAD_HALF
            lanes = [ln for ln in LANES
                     if min(wx1, ln * LANE_W + 1) - max(wx0, ln * LANE_W - 1) > 0.5]
            if not lanes:
                continue
            fill = area / float(bw * bh)
            out.append(Obj(x=float(bx), y=float(by), width=float(bw), height=float(bh), type=kind,
                           lanes=lanes, z=float(max(0.0, z - Z_NEAR * 0)),
                           confidence=float(min(1.0, 0.4 + fill * 0.6))))
        return out

    # ---------- main ----------
    def process(self, frame_bgr: np.ndarray, t: float | None = None) -> GameState:
        t = time.perf_counter() if t is None else t
        h0, w0 = frame_bgr.shape[:2]
        scale = PROC_W / w0
        img = cv2.resize(frame_bgr, (PROC_W, int(round(h0 * scale))), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
        if self.size != (w, h):
            self._setup(w, h)
        self.debug = {"scale": scale}
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        screen, _ = self.classify_screen(img, gray, hsv)
        player = Player(alive=screen != Screen.GAME_OVER)
        obstacles, targets = [], []
        if screen == Screen.PLAYING:
            player = self.detect_player(hsv, t)
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            obstacles, targets = self.detect_objects(img, lab, hsv, player)
            sp = self.tracker.update(obstacles + targets, t)
            if sp > 0:
                self.speed = 0.7 * self.speed + 0.3 * sp
            for o in obstacles + targets:
                o.ttc = o.z / max(self.speed, 5.0)
        else:
            self.prev_player = None
        self.prev_t = t
        lane_free = {ln: min([o.z for o in obstacles if ln in o.lanes], default=Z_FAR) for ln in LANES}
        st = GameState(t=t, screen=screen, width=w, height=h, player=player,
                       obstacles=obstacles, targets=targets, speed=self.speed, lane_free=lane_free)
        # everything in the state is in processing pixels; keep scale for drawing
        self.debug["img"] = img
        return st
