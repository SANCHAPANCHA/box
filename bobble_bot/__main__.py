"""BOBBLE RUN bot: screenshot -> vision -> decision -> input, with a debug overlay.

    python -m bobble_bot                # observe only: overlay, F10 = test action
    python -m bobble_bot --play         # rule-based bot controls the game
    python -m bobble_bot --play --open  # also opens the game in the browser first
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import webbrowser

import cv2

from .config import GAME_URL, BotConfig
from .decision import Action, load_engine
from .winutil import make_dpi_aware, make_overlay_passive
from .state import Screen
from .vision import Vision, VisionConfig

WINDOW = "BOBBLE BOT debug"
log = logging.getLogger("bot")


class Flags:
    def __init__(self, active: bool):
        self.stop = False
        self.active = active
        self.test_action = False
        self.human_key: str | None = None


def start_hotkeys(flags: Flags):
    from pynput import keyboard

    names = {keyboard.Key.left: "left", keyboard.Key.right: "right", keyboard.Key.up: "jump",
             keyboard.Key.space: "jump", keyboard.Key.down: "slide"}

    def on_press(key):
        if key == keyboard.Key.f8:
            log.warning("F8: emergency stop")
            flags.stop = True
            return False
        if key == keyboard.Key.f9:
            flags.active = not flags.active
            log.info("F9: bot %s", "ACTIVE" if flags.active else "PAUSED")
        elif key == keyboard.Key.f10:
            flags.test_action = True
        elif key in names and not flags.active:
            flags.human_key = names[key]  # labels for recorded gameplay

    listener = keyboard.Listener(on_press=on_press)
    listener.daemon = True
    listener.start()
    return listener


def state_lines(st) -> list[str]:
    p = st.player
    out = [f" player found={p.found} lane={p.lane:+d} x={p.world_x:+.2f}",
           f"   on_ground={p.on_ground} vx={p.vx:.0f} vy={p.vy:.0f}",
           f" obstacles: {len(st.obstacles)}  coins: {len(st.targets)}"]
    for o in sorted(st.obstacles, key=lambda o: o.z)[:6]:
        out.append(f"   #{o.track_id} lanes{o.lanes} z={o.z:.1f} ttc={o.ttc:.2f}s")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--play", action="store_true", help="start with the bot controlling the game")
    ap.add_argument("--open", action="store_true", help="open the game in the default browser first")
    ap.add_argument("--select", action="store_true", help="re-select the game area")
    ap.add_argument("--dry-run", action="store_true", help="never send real keys, only log them")
    ap.add_argument("--record", action="store_true", help="save frames + state + actions for training")
    ap.add_argument("--engine", help="custom decision engine 'module:Class'")
    ap.add_argument("--no-overlay", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    cfg = BotConfig.load()
    os.makedirs(cfg.log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(os.path.join(cfg.log_dir, "bot.log"), encoding="utf-8")])
    make_dpi_aware()

    if a.open:
        webbrowser.open(GAME_URL)
        log.info("Opened %s, waiting for it to load...", GAME_URL)
        time.sleep(8)
    if a.select or not cfg.region:
        from .select_region import select_region
        if not select_region(cfg):
            return 1

    from .capture import ScreenCapture
    from .controller import DryRunController, PynputController
    from .overlay import draw
    from .recorder import Recorder

    cap = ScreenCapture(cfg.region)
    vision = Vision(VisionConfig(y_offset_px=cfg.y_offset_px, dev_threshold=cfg.dev_threshold))
    engine = load_engine(a.engine, cfg)
    ctl = DryRunController(cfg.window_title_contains) if a.dry_run else PynputController(cfg.window_title_contains)
    rec = Recorder(cfg, cfg.target_fps, a.record)
    flags = Flags(active=a.play)
    start_hotkeys(flags)
    show = cfg.show_overlay and not a.no_overlay
    if show:
        cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)
        cv2.moveWindow(WINDOW, *cfg.overlay_pos)

    log.info("Region %s. Bot %s. F8 stop, F9 pause/resume, F10 test action.",
             cfg.region, "ACTIVE" if flags.active else "PAUSED (observe only)")
    log.info("Click into the game once so the browser has focus.")

    period = 1.0 / cfg.target_fps
    fps, last_screen, run_start, runs = 0.0, Screen.UNKNOWN, None, 0
    last_action: Action | None = None
    overlay_ready = False
    try:
        while not flags.stop:
            t0 = time.perf_counter()
            frame = cap.grab()
            t1 = time.perf_counter()
            st = vision.process(frame, t1)
            t2 = time.perf_counter()

            # run bookkeeping
            if st.screen == Screen.PLAYING and last_screen != Screen.PLAYING:
                run_start = run_start or time.time()
            if st.screen == Screen.GAME_OVER and last_screen == Screen.PLAYING:
                runs += 1
                dur = time.time() - (run_start or time.time())
                d = rec.dump_death()
                log.info("GAME OVER #%d after %.1fs%s", runs, dur, f", debug saved to {d}" if d else "")
                run_start = None
            last_screen = st.screen

            action = None
            if flags.test_action:
                flags.test_action = False
                action = Action("jump", "jump", 0.08, 1.0, "F10 test action")
            elif flags.active:
                action = engine.decide(st, t2)
            if action is not None and action.key:
                sent = ctl.tap(action.key, action.hold)
                if not sent:
                    action.reason += " [blocked: game window not focused]"
                rec.log_action(action, st)
                log.debug("%s conf=%.2f %s", action.name, action.confidence, action.reason)
            if action is not None:
                last_action = action
            t3 = time.perf_counter()

            fps = 0.9 * fps + 0.1 / max(1e-3, t3 - t0 + max(0.0, period - (t3 - t0)))
            info = {
                "active": flags.active, "fps": fps, "cap_ms": (t1 - t0) * 1000, "vis_ms": (t2 - t1) * 1000,
                "lat_ms": (t3 - t0) * 1000,
                "action": last_action.name if last_action else "-",
                "confidence": last_action.confidence if last_action else 0.0,
                "reason": (last_action.reason if last_action else "")[:60],
                "trajectory": last_action.trajectory if last_action else None,
                "state_lines": state_lines(st),
            }
            if show or cfg.save_debug_on_death:
                img = draw(vision.debug["img"], st, vision, info)
                rec.push_debug(img)
                if show:
                    cv2.imshow(WINDOW, img)
                    if not overlay_ready:
                        make_overlay_passive(WINDOW)
                        overlay_ready = True
                    if cv2.waitKey(1) & 0xFF == 27:  # Esc in the debug window
                        break
            if a.record and st.screen == Screen.PLAYING:
                small = cv2.resize(vision.debug["img"], (320, int(320 * st.height / st.width)))
                rec.sample(small, st, last_action.name if (last_action and flags.active) else None, flags.human_key)
                flags.human_key = None

            spare = period - (time.perf_counter() - t0)
            if spare > 0:
                time.sleep(spare)
    except KeyboardInterrupt:
        pass
    finally:
        if hasattr(ctl, "release_all"):
            ctl.release_all()
        if show:
            cv2.destroyAllWindows()
        log.info("Stopped. Runs finished: %d", runs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
