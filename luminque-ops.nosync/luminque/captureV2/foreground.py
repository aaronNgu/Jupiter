"""Foreground window metadata via raw user32 calls — no pywinauto, no COM.

Each call costs microseconds. Returns None on any failure: UIPI blocks these
calls when an elevated window is focused, and the desktop may have no
foreground window during lock/UAC transitions. Non-Windows platforms always
return None (window stamping is a Windows-only feature; the capture loop
tolerates its absence).
"""

import sys


def get_foreground_window() -> dict | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

        return {
            "title": buf.value,
            "left": rect.left,
            "top": rect.top,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
            "window_id": str(hwnd),
        }
    except Exception:
        return None
