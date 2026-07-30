from __future__ import annotations

import ctypes


def is_desktop_unlocked() -> bool:
    try:
        user32 = ctypes.windll.user32
    except AttributeError:
        return True
    desktop = user32.OpenInputDesktop(0, False, 0x0100)
    if not desktop:
        return False
    user32.CloseDesktop(desktop)
    return True
