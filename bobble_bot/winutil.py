"""OS helpers (Windows specifics are optional; everything degrades gracefully)."""
from __future__ import annotations

import sys


def make_dpi_aware() -> None:
    """Without this, Windows display scaling (125%, 150%...) breaks coordinates."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def make_overlay_passive(title: str) -> None:
    """Keep the debug window on top without ever taking keyboard focus:
    the game pauses itself as soon as the browser loses focus."""
    if sys.platform != "win32":
        return
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOPMOST = -20, 0x08000000, 0x00000008
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
    HWND_TOPMOST, SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = -1, 0x2, 0x1, 0x10
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def _window_titles():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def focus_window(contains: str, exclude: tuple[str, ...] = ()) -> str | None:
    """Bring the first visible window whose title contains `contains` to the
    foreground. Returns its title, or None if not found / not Windows."""
    if sys.platform != "win32" or not contains:
        return None
    import ctypes
    from ctypes import wintypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    want = contains.lower()
    for hwnd, title in _window_titles():
        t = title.lower()
        if want in t and not any(e.lower() in t for e in exclude):
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            # Windows only lets the foreground thread change focus: borrow its input queue
            fg = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg, None)
            me = kernel32.GetCurrentThreadId()
            user32.AttachThreadInput(fg_thread, me, True)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(fg_thread, me, False)
            return title
    return None
