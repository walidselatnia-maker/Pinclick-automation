"""Open Pinterest in the APP's browser profile so you can log in there.

The app drives its own Chrome profile (user_data/), separate from your personal
Chrome. A Pinterest login in your own profile is invisible to it, which is why
the probe still saw the logged-out page.

This opens Pinterest in the app's profile and waits while you log in, then
confirms by checking whether search results expose real pin links (they are
absent for logged-out visitors). Credentials are never seen, stored, or typed
by this script -- you log in yourself in the window it opens.

    .venv\\Scripts\\python.exe tools\\login_pinterest.py
"""

import asyncio
import re
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "user_data"

LOGIN_URL = "https://www.pinterest.com/login/"
CHECK_URL = "https://www.pinterest.com/search/pins/?q=fruit%20pizza"

WAIT_MINUTES = 10
POLL_S = 5


async def logged_in(ctx) -> tuple[bool, str]:
    """Detect login from COOKIES, never by navigating.

    The first version of this polled by calling page.goto() on the very tab the
    user was typing into, which reloaded the login form every 10 seconds and
    made logging in impossible. Reading cookies touches nothing on screen.

    Pinterest sets ``_auth=1`` once authenticated; it is absent or 0 otherwise.
    """
    for c in await ctx.cookies():
        if c.get("name") == "_auth" and str(c.get("value")) == "1":
            return True, "_auth=1"
    return False, "no _auth cookie"


async def main() -> int:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            # Playwright disables Chrome's sandbox by default for
            # persistent contexts, which shows an "unsupported
            # command-line flag: --no-sandbox" bar and is an obvious
            # abnormal-browser signal. Keep the real sandbox on.
            chromium_sandbox=True,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(45000)

        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.bring_to_front()

        print("=" * 62)
        print("A Chrome window is open on the Pinterest login page.")
        print("Log in there yourself. This script never handles credentials,")
        print("and never touches that tab while you type.")
        print(f"Waiting up to {WAIT_MINUTES} minutes.")
        print("=" * 62)

        deadline = time.monotonic() + WAIT_MINUTES * 60
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            try:
                ok, why = await logged_in(ctx)
            except Exception as exc:
                print(f"  cookie read failed ({type(exc).__name__}); retrying")
                continue

            if ok:
                print(f"\nLOGGED IN ({why}). Session saved to user_data/.")
                # Only now, once login is finished, is it safe to navigate.
                try:
                    await page.goto(CHECK_URL, wait_until="domcontentloaded")
                    await page.wait_for_timeout(4000)
                    html = await page.content()
                    links = set(re.findall(r'href="(/pin/[^"]+)"', html))
                    print(f"search page exposes {len(links)} pin links")
                except Exception as exc:
                    print(f"verification navigation failed: {exc}")
                await ctx.close()
                return 0

            left = int(deadline - time.monotonic())
            print(f"  waiting... ({why}, {left}s left)")

        print("\nTimed out. Re-run when you are ready.")
        await ctx.close()
        return 1


sys.exit(asyncio.run(main()))
