"""Verify the L1 -> L2 interest drill-down.

Bug being fixed: picking the niche "Food And Drink" was running a TEXT SEARCH
for that phrase, which returns junk like "food and drink logo" and "food and
drink worksheet". Wrong level entirely.

The real mechanism is the chevron next to each interest:

    <button wire:click="setTopInterest('<id>')">

Clicking it replaces the Interests panel with that interest's CHILDREN --
Dinner Recipes 4.1M, Pizza 4M, Fruit 2.6M, Dessert 2.1M ...

This confirms that, and reports the child rows plus which of them can be
drilled further.

    .venv\\Scripts\\python.exe tools\\probe_drill.py "Food And Drink"
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
OUT = ROOT / "debug" / "drill"

TARGET = sys.argv[1] if len(sys.argv) > 1 else "Food And Drink"
URL = "https://app.pinclicks.com/keyword-explorer"

ROW = "label[for^='filter-']"

READ_ROWS = """() => {
    return [...document.querySelectorAll("label[for^='filter-']")].map(el => {
        const id = (el.getAttribute('for') || '').replace('filter-', '');
        const btn = el.querySelector("button[wire\\\\:click^='setTopInterest']");
        const span = btn ? btn.querySelector('span') : null;
        const name = (el.querySelector('div') || {}).innerText || '';
        return {
            id,
            name: name.trim(),
            volume: span ? span.innerText.trim() : '',
            canDrill: !!btn
        };
    });
}"""


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

        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        top = await page.evaluate(READ_ROWS)
        print(f"L1: {len(top)} top-level interests")
        for row in top[:6]:
            print(f"   {row['name']:<24} {row['volume']:>8}  drill={row['canDrill']}")

        match = next((r for r in top if r["name"].lower() == TARGET.lower()), None)
        if not match:
            print(f"\n! {TARGET!r} not found")
            await ctx.close()
            return

        print(f"\nDrilling into {match['name']!r} (id {match['id']})")
        await page.click(f"button[wire\\:click=\"setTopInterest('{match['id']}')\"]")
        await page.wait_for_timeout(6000)

        children = await page.evaluate(READ_ROWS)
        print(f"\nL2: {len(children)} sub-niches")
        for row in children[:15]:
            arrow = ">" if row["canDrill"] else " "
            print(f"   {row['name']:<28} {row['volume']:>8} {arrow}")

        # Did it actually change, or are we still looking at L1?
        same = {r["id"] for r in top} == {r["id"] for r in children}
        print(f"\nlist changed: {not same}")

        (OUT / "after-drill.html").write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(OUT / "after-drill.png"), full_page=True)
        print(f"captured -> {OUT}")

        await ctx.close()


asyncio.run(main())
