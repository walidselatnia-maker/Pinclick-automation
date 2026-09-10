"""Verify the Level-3 page: keywords extracted from a main keyword.

The user's model is four levels:

    L1  big niche          -> "Food and Drink"      (Interests panel)
    L2  main keyword       -> "Pizza"               (keyword table)
    L3  related keywords   -> "Pizza Dough Recipe 596.5K", ...   <-- verify this
    L4  top pins (~21)     -> /pins?search=<keyword>  + Export

L1, L2 and L4 are confirmed. L3 is assumed to live at /keyword/{slug}/{id},
which has never been opened. This finds a real keyword link, follows it, and
reports whether that page lists sub-keywords with search volumes.

    .venv\\Scripts\\python.exe tools\\probe_level3.py pizza
"""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "user_data"
OUT = ROOT / "debug" / "level3"

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "pizza"


async def snap(page, label: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{label}.html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(OUT / f"{label}.png"), full_page=True)
    print(f"  captured {label}")


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(30000)

        # Does keyword-explorer accept a search in the URL, like /pins does?
        url = f"https://app.pinclicks.com/keyword-explorer?search={KEYWORD}"
        print(f"Loading {url}")
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(7000)

        rows = await page.locator("tr[data-key]").count()
        print(f"  keyword table rows: {rows}")
        await snap(page, f"L2-{KEYWORD}")

        links = page.locator("a[href^='/keyword/']")
        n = await links.count()
        print(f"  keyword drill-down links: {n}")
        if not n:
            print("  ! no /keyword/ links -- URL search may not work here")
            await ctx.close()
            return

        href = await links.first.get_attribute("href")
        text = (await links.first.inner_text()).strip()
        print(f"\nFollowing L3 link: {text!r} -> {href}")

        await page.goto(f"https://app.pinclicks.com{href}", wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)
        print(f"  landed: {page.url}")
        print(f"  title:  {await page.title()!r}")
        await snap(page, f"L3-{KEYWORD}")

        sub = await page.locator("tr[data-key]").count()
        print(f"\n  L3 table rows: {sub}")

        # Sample the first few rows so we can see whether they really are
        # keywords with volumes.
        for i in range(min(8, sub)):
            row = page.locator("tr[data-key]").nth(i)
            txt = (await row.inner_text()).replace("\n", " | ").strip()
            print(f"    {i + 1}. {txt[:110]}")

        await ctx.close()


asyncio.run(main())
