"""How is interest drill-down state addressed?

Bug: drill_interest() reloads /keyword-explorer and then clicks
setTopInterest('<child id>'). That control only exists once the PARENT has
been opened, so drilling into a second-level niche (e.g. Dessert, inside Food
And Drink) always fails with "no drill-down control".

Two possible fixes, and this decides which:

  A) the URL encodes the open interest -> navigate straight to it (stateless)
  B) it does not                       -> replay the click path from the top

Reports the URL after each drill, plus whether a child's control is present
before and after opening its parent.

    .venv\\Scripts\\python.exe tools\\probe_drill_state.py
"""

import asyncio
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "user_data"
URL = "https://app.pinclicks.com/keyword-explorer"

FOOD_AND_DRINK = "918530398158"
DESSERT = "897118241776"          # a child of Food And Drink


def ctl(interest_id: str) -> str:
    return f"button[wire\\:click=\"setTopInterest('{interest_id}')\"]"


async def main() -> None:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(30000)

        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        print(f"top-level url: {page.url}")
        print(f"  Food And Drink control present: {await page.locator(ctl(FOOD_AND_DRINK)).count()}")
        print(f"  Dessert control present:        {await page.locator(ctl(DESSERT)).count()}"
              "   <- expected 0 at top level")

        print("\nclick Food And Drink")
        await page.click(ctl(FOOD_AND_DRINK))
        await page.wait_for_timeout(6000)
        print(f"  url now: {page.url}")
        print(f"  Dessert control present: {await page.locator(ctl(DESSERT)).count()}"
              "   <- expected >0 now")

        print("\nclick Dessert")
        if await page.locator(ctl(DESSERT)).count():
            await page.click(ctl(DESSERT))
            await page.wait_for_timeout(6000)
            print(f"  url now: {page.url}")
            rows = await page.evaluate(
                "() => [...document.querySelectorAll(\"label[for^='filter-']\")]"
                ".slice(0,6).map(e => (e.querySelector('div')||{}).innerText)"
            )
            print(f"  first rows: {rows}")

        # Does a reload keep us where we are, or reset to the top?
        print("\nreload the current url")
        await page.goto(page.url, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        rows = await page.evaluate(
            "() => [...document.querySelectorAll(\"label[for^='filter-']\")]"
            ".slice(0,6).map(e => (e.querySelector('div')||{}).innerText)"
        )
        print(f"  first rows after reload: {rows}")
        print("\nVERDICT: URL-addressable"
              if "?" in page.url else "\nVERDICT: NOT URL-addressable -> must replay click path")

        await ctx.close()


asyncio.run(main())
