"""Projection of the game's 3D road onto the screen.

Mirrors the camera of BOBBLE RUN (config.js / game.js): the camera follows the
player's lateral position `ax` partially (posX .55, lookX .75). World units:
lane width 2.0, lanes at x = -2, 0, 2, road edges at x = +-3, player at z = 0.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

LANE_W = 2.0
LANES = (-1, 0, 1)
ROAD_HALF = 3.0


@dataclass
class CameraParams:
    pos_x_k: float = 0.55
    pos_y: float = 3.4
    pos_z: float = 7.0
    look_x_k: float = 0.75
    look_y: float = 1.3
    look_z: float = -9.0
    fov_landscape: float = 62.0
    fov_portrait: float = 74.0
    portrait_back: float = 1.4
    portrait_up: float = 1.5


class Camera:
    def __init__(self, width: int, height: int, ax: float = 0.0,
                 params: CameraParams | None = None, y_offset_px: float = 0.0, ay: float = 0.0):
        p = params or CameraParams()
        self.w, self.h = width, height
        aspect = width / max(1, height)
        fe = min(1.0, max(0.0, (1.3 - aspect) / 0.5))
        fov = p.fov_landscape + (p.fov_portrait - p.fov_landscape) * fe
        # the camera also rises with the player's jump height ay (posYk .35, lookYk .5)
        self.pos = np.array([p.pos_x_k * ax, p.pos_y + 0.35 * ay + p.portrait_up * fe,
                             p.pos_z + p.portrait_back * fe])
        look = np.array([p.look_x_k * ax, p.look_y + 0.5 * ay, p.look_z])
        f = look - self.pos
        f /= np.linalg.norm(f)
        r = np.cross(f, [0.0, 1.0, 0.0])
        r /= np.linalg.norm(r)
        self.f, self.r, self.u = f, r, np.cross(r, f)
        self.focal = (height / 2) / math.tan(math.radians(fov) / 2)
        self.y_off = y_offset_px

    def project(self, x: float, y: float, z: float) -> tuple[float, float]:
        d = np.array([x, y, z]) - self.pos
        cz = d @ self.f
        return (self.w / 2 + self.focal * (d @ self.r) / cz,
                self.h / 2 - self.focal * (d @ self.u) / cz + self.y_off)

    def ground_at(self, sx: float, sy: float) -> tuple[float, float] | None:
        """Inverse projection of a screen pixel onto the ground plane (y=0)."""
        nx = (sx - self.w / 2) / self.focal
        ny = -(sy - self.y_off - self.h / 2) / self.focal
        ray = self.f + nx * self.r + ny * self.u
        if ray[1] >= -1e-6:
            return None  # above the horizon
        t = -self.pos[1] / ray[1]
        hit = self.pos + t * ray
        return float(hit[0]), float(hit[2])

    def road_x_range(self, sy: float, x0: float = -ROAD_HALF, x1: float = ROAD_HALF):
        """Screen x span of world x in [x0, x1] at screen row sy (ground plane)."""
        g = self.ground_at(self.w / 2, sy)
        if g is None:
            return None
        z = g[1]
        return self.project(x0, 0, z)[0], self.project(x1, 0, z)[0]


SHIRT_Y = 1.05  # height of the shirt centre above the feet, world units


class PlayerXMapper:
    """Maps the player's shirt screen position back to world x / jump height,
    assuming a settled camera (it follows the player with a short lag)."""

    def __init__(self, width: int, height: int, params: CameraParams | None = None,
                 y_offset_px: float = 0.0):
        xs = np.linspace(-3.0, 3.0, 61)
        sx = [Camera(width, height, ax=x, params=params,
                     y_offset_px=y_offset_px).project(x, SHIRT_Y, 0.0)[0] for x in xs]
        self.xs, self.sx = xs, np.array(sx)
        ys = np.linspace(0.0, 3.0, 31)
        sy = [Camera(width, height, ay=y, params=params,
                     y_offset_px=y_offset_px).project(0, y + SHIRT_Y, 0.0)[1] for y in ys]
        self.ys, self.sy = ys, np.array(sy)  # sy decreases as the player rises

    def jump_height(self, shirt_screen_y: float) -> float:
        return float(np.interp(-shirt_screen_y, -self.sy, self.ys))

    def world_x(self, screen_x: float) -> float:
        return float(np.interp(screen_x, self.sx, self.xs))

    def screen_x(self, world_x: float) -> float:
        return float(np.interp(world_x, self.xs, self.sx))
