"""Diagnostic: which browser can reach app.pinclicks.com?

Cloudflare rejected Playwright's bundled Chromium. This checks whether the
real installed Chrome gets through instead.

Read-only. Loads the login page and reports the HTTP status and title.
No fingerprint spoofing, no challenge solving -- just a different browser binary.

    .venv\\Scripts\\python.exe tools\\probe_browser.py
"""

import asyncio
import shutil
from pathlib import Path

from playwright.async_api import async_playwright

TARGET = "https://app.pinclicks.com/login"
PROFILE = Path(__file__).resolve().parent.parent / "user_data_probe"


async def check(page, label: str) -> None:
    try:
        resp = await page.goto(TARGET, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2500)
        status = resp.status if resp else "?"
        title = await page.title()
        blocked = status == 403 or "Cloudflare" in title
        print(f"  [{'BLOCKED' if blocked else 'OK'}] {label}: {status} | {title!r}")
    except Exception as exc:
        print(f"  [ERROR] {label}: {type(exc).__name__}: {str(exc)[:110]}")


async def main() -> None:
    async with async_playwright() as pw:
        print("A. Bundled Chromium (what failed before)")
        b = await pw.chromium.launch(headless=True)
        await check(await b.new_page(), "bundled headless")
        await b.close()

        print("B. Real Chrome, fresh profile")
        for headless in (True, False):
            try:
                b = await pw.chromium.launch(channel="chrome", headless=headless)
                await check(await b.new_page(), f"chrome headless={headless}")
                await b.close()
            except Exception as exc:
                print(f"  [ERROR] chrome headless={headless}: {str(exc)[:110]}")

        print("C. Real Chrome, persistent profile (headed)")
        shutil.rmtree(PROFILE, ignore_errors=True)
        try:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE), channel="chrome", headless=False,
                viewport={"width": 1440, "height": 900},
            )
            await check(ctx.pages[0] if ctx.pages else await ctx.new_page(), "chrome persistent")
            await ctx.close()
        except Exception as exc:
            print(f"  [ERROR] chrome persistent: {str(exc)[:110]}")
        shutil.rmtree(PROFILE, ignore_errors=True)


asyncio.run(main())
