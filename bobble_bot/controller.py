"""Input layer. Swap PynputController for another backend without touching the bot."""
from __future__ import annotations

import logging
import sys
import threading
import time

log = logging.getLogger("bot.input")

GAME_KEYS = ("left", "right", "jump", "slide", "enter")


def foreground_title() -> str | None:
    """Title of the active window (Windows); None when unknown on this OS."""
    if sys.platform != "win32":
        return None
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


class InputController:
    """Base class: sends abstract game keys, guarded by a safety check."""

    EXCLUDE = ("bot debug",)  # our own overlay window

    def __init__(self, title_contains: str = ""):
        self.title_contains = title_contains.lower()
        self.enabled = True
        self.last_title: str | None = None

    def allowed(self) -> bool:
        if not self.enabled:
            return False
        if not self.title_contains:
            return True
        title = foreground_title()
        self.last_title = title
        if title is None:
            return True
        t = title.lower()
        return self.title_contains in t and not any(e in t for e in self.EXCLUDE)

    def tap(self, key: str, hold: float = 0.05) -> bool:
        if key not in GAME_KEYS:
            raise ValueError(key)
        if not self.allowed():
            log.debug("blocked %s: game window is not focused", key)
            return False
        self._tap(key, hold)
        return True

    def _tap(self, key: str, hold: float) -> None:
        raise NotImplementedError


class DryRunController(InputController):
    def _tap(self, key: str, hold: float) -> None:
        log.info("[dry-run] %s", key)


class PynputController(InputController):
    """Real OS keyboard events. Releases happen on a timer so the loop never blocks."""

    def __init__(self, title_contains: str = ""):
        super().__init__(title_contains)
        from pynput.keyboard import Controller, Key

        self.kb = Controller()
        self.map = {"left": Key.left, "right": Key.right, "jump": Key.up,
                    "slide": Key.down, "enter": Key.enter}

    def _tap(self, key: str, hold: float) -> None:
        k = self.map[key]
        self.kb.press(k)
        threading.Timer(hold, self.kb.release, args=(k,)).start()

    def release_all(self) -> None:
        for k in self.map.values():
            try:
                self.kb.release(k)
            except Exception:
                pass
        time.sleep(0.01)
