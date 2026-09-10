"""Calibration capture for the Top Pins page.

/pins renders nothing until a keyword is searched, so the pin-card DOM cannot
be captured passively. This runs ONE search and captures both result tabs
("Pinterest Search" and "PinClicks Database") so the two can be compared on
actual field coverage rather than guessed at.

Reuses the logged-in profile in user_data/, so stop the app server first --
Chrome locks that directory and only one process may hold it.

    .venv\\Scripts\\python.exe tools\\capture_pins.py "Chicken Breast Recipe"
"""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "user_data"
OUT = ROOT / "debug" / "pins"
BASE = "https://app.pinclicks.com"

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "Chicken Breast Recipe"


async def snap(page, label: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{label}.html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(OUT / f"{label}.png"), full_page=True)
    print(f"  captured {label}  (url={page.url})")


async def main() -> None:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            no_viewport=True, args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(20000)

        print(f"Searching Top Pins for {KEYWORD!r}")
        await page.goto(f"{BASE}/pins", wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)

        box = page.locator("input[type='text'], input[type='search']").first
        await box.click()
        await box.fill(KEYWORD)
        await box.press("Enter")

        # Livewire fetches over AJAX; give it room, then let images settle.
        await page.wait_for_timeout(9000)
        await snap(page, "pins-pinterest-search")

        # Second tab. Text match rather than a class -- classes are Tailwind.
        for label in ("PinClicks Database", "Pinclicks Database"):
            tab = page.locator(f"text={label}").first
            if await tab.count():
                print(f"Switching to {label!r}")
                await tab.click()
                await page.wait_for_timeout(7000)
                await snap(page, "pins-pinclicks-database")
                break
        else:
            print("  ! could not find the PinClicks Database tab")

        await ctx.close()


asyncio.run(main())
