"""Start the scraping browser minimised (Windows only).

It cannot be headless -- app.pinclicks.com returns 403 to headless Chrome, see
browser.py -- so it has to be a real window. It does not have to be in your
face, though. We minimise it the moment it launches: it sits in the taskbar
like any other window, and never pops up over whatever you are doing.

Signing in is the one exception, since you have to type into it. After login
it goes straight back down to the taskbar.

Finding the window: a persistent context gives us no browser PID, so we tag
the page with a random ``document.title`` and look for the top-level window
whose caption starts with that tag.
"""

from __future__ import annotations

import sys
import time
from uuid import uuid4

from ..logging_setup import get_logger

log = get_logger("window")

WINDOWS = sys.platform == "win32"

if WINDOWS:  # pragma: no cover - platform specific
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)

    SW_RESTORE = 9
    #: Minimise without stealing focus from whatever you are typing in.
    SW_SHOWMINNOACTIVE = 7

    _ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _titles_matching(tag: str) -> list[int]:
    found: list[int] = []
    buf = ctypes.create_unicode_buffer(512)

    def cb(hwnd, _lparam):
        if user32.GetWindowTextW(hwnd, buf, len(buf)) and buf.value.startswith(tag):
            found.append(hwnd)
        return True

    user32.EnumWindows(_ENUM(cb), 0)
    return found


class ChromeWindow:
    """The one automation window."""

    def __init__(self) -> None:
        self.hwnd: int | None = None

    def forget(self) -> None:
        """The context died, so the handle means nothing now."""
        self.hwnd = None

    async def find(self, page, timeout_s: float = 5.0) -> int | None:
        if not WINDOWS:
            return None
        if self.hwnd and user32.IsWindow(self.hwnd):
            return self.hwnd

        tag = f"pcmt-{uuid4().hex[:8]}"
        try:
            original = await page.title()
            await page.evaluate("t => document.title = t", tag)
        except Exception as exc:
            log.warning("Could not tag the page to find its window: %s", exc)
            return None

        deadline = time.time() + timeout_s
        try:
            while time.time() < deadline:
                hits = _titles_matching(tag)
                if hits:
                    self.hwnd = hits[0]
                    return self.hwnd
                time.sleep(0.1)
            log.warning("Automation window not found; leaving it as it is")
            return None
        finally:
            # Put the real title back, so the taskbar entry reads normally.
            try:
                await page.evaluate("t => document.title = t", original)
            except Exception:
                pass

    def minimise(self) -> bool:
        if not WINDOWS or not self.hwnd:
            return False
        try:
            user32.ShowWindow(self.hwnd, SW_SHOWMINNOACTIVE)
            return True
        except Exception as exc:
            log.warning("Could not minimise the automation window: %s", exc)
            return False

    def restore(self) -> bool:
        """Bring it up for sign-in, and only for sign-in."""
        if not WINDOWS or not self.hwnd:
            return False
        try:
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            user32.SetForegroundWindow(self.hwnd)
            return True
        except Exception as exc:
            log.warning("Could not restore the automation window: %s", exc)
            return False


#: Module-level singleton, mirroring browser.manager.
window = ChromeWindow()
