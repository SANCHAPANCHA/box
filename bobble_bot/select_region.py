"""Pick the game area on screen and save it to config.json.

    python -m bobble_bot.select_region

Select ONLY the game canvas (the browser viewport, without tabs/address bar),
press Enter/Space to confirm, C to cancel.
"""
from __future__ import annotations

import cv2

from .capture import ScreenCapture
from .config import BotConfig
from .winutil import make_dpi_aware


def select_region(cfg: BotConfig) -> dict | None:
    make_dpi_aware()
    full, mon = ScreenCapture({"left": 0, "top": 0, "width": 1, "height": 1}).full_screen(0)
    scale = min(1.0, 1600 / full.shape[1], 900 / full.shape[0])
    small = cv2.resize(full, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    title = "Select the game area, then Enter"
    x, y, w, h = cv2.selectROI(title, small, showCrosshair=False)
    cv2.destroyWindow(title)
    if w < 50 or h < 50:
        print("Nothing selected")
        return None
    region = {"left": int(mon["left"] + x / scale), "top": int(mon["top"] + y / scale),
              "width": int(w / scale), "height": int(h / scale)}
    cfg.region = region
    # put the debug window right of the game, or below it if there is no room
    right = region["left"] + region["width"] + 10
    cfg.overlay_pos = [right, region["top"]] if right + 600 < mon["left"] + mon["width"] \
        else [region["left"], region["top"] + region["height"] + 10]
    cfg.save()
    print("Saved region", region)
    return region


if __name__ == "__main__":
    select_region(BotConfig.load())
