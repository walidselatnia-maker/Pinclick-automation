"""Find the scroll behaviour that actually loads more pins.

User observation: scrolling to the bottom and waiting does nothing, but
scrolling UP and back DOWN fetches the next batch immediately -- and repeating
that yielded 50+ pins in under 30 seconds by hand.

That fits how lazy-loading works. An IntersectionObserver fires when its
sentinel ENTERS the viewport, not while it sits there. Parking at the bottom
leaves the sentinel permanently intersecting, so it never fires again. Moving
away and coming back re-triggers it.

This compares strategies head to head and reports pins-per-second for each:

    park    scroll to bottom once, then wait (what the app does today)
    bounce  scroll up, pause, scroll back down each time it stalls
    nudge   small up/down jiggle without leaving the bottom region

    .venv\\Scripts\\python.exe tools\\probe_bounce.py "fruit pizza" 60
"""

import asyncio
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

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "fruit pizza"
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 60
BUDGET_S = 120

URL = "https://app.pinclicks.com/pins?search={}"
WRAP = "#ui-table-wrapper"


async def rows(page) -> int:
    return await page.locator("tr[data-key]").count()


async def to_bottom(page):
    await page.evaluate(
        f"""() => {{
            const el = document.querySelector('{WRAP}');
            if (el) el.scrollTop = el.scrollHeight;
            window.scrollTo(0, document.body.scrollHeight);
        }}"""
    )


async def scroll_by(page, dy: int):
    """Move both the inner table and the window, since either may be the
    scroller that owns the sentinel."""
    await page.evaluate(
        f"""(dy) => {{
            const el = document.querySelector('{WRAP}');
            if (el) el.scrollTop = Math.max(0, el.scrollTop + dy);
            window.scrollBy(0, dy);
        }}""",
        dy,
    )


async def prepare(page):
    await page.goto(URL.format(KEYWORD.replace(" ", "+")), wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)
    for _ in range(30):
        if await rows(page) > 0:
            break
        await page.wait_for_timeout(500)
    await page.bring_to_front()


async def run(page, strategy: str) -> tuple[int, float]:
    await prepare(page)
    start = time.monotonic()
    have = await rows(page)
    print(f"\n[{strategy}] start with {have} pins")

    stalled_since = time.monotonic()
    while have < TARGET and time.monotonic() - start < BUDGET_S:
        await to_bottom(page)
        await page.wait_for_timeout(1500)

        now = await rows(page)
        if now > have:
            print(f"  +{now - have:3d} -> {now:3d}   at {time.monotonic() - start:5.1f}s")
            have = now
            stalled_since = time.monotonic()
            continue

        stalled = time.monotonic() - stalled_since
        if strategy == "park":
            if stalled > 45:
                print(f"  parked {stalled:.0f}s with no growth; giving up")
                break
            continue

        if stalled < 3:
            continue

        # Re-arm the observer by leaving the bottom and returning.
        if strategy == "bounce":
            await scroll_by(page, -4000)
            await page.wait_for_timeout(700)
            await to_bottom(page)
        else:  # nudge
            await scroll_by(page, -600)
            await page.wait_for_timeout(300)
            await scroll_by(page, 600)
        stalled_since = time.monotonic()

    elapsed = time.monotonic() - start
    print(f"[{strategy}] {have} pins in {elapsed:.1f}s "
          f"({have / max(elapsed, 0.1):.2f} pins/s)")
    return have, elapsed


async def main() -> None:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            chromium_sandbox=True, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(45000)

        results = {}
        for strategy in ("bounce", "nudge", "park"):
            results[strategy] = await run(page, strategy)

        print("\n" + "=" * 46)
        for name, (n, t) in results.items():
            print(f"  {name:<7} {n:3d} pins  {t:6.1f}s")
        best = max(results, key=lambda k: results[k][0])
        print(f"\nBEST: {best}")

        await ctx.close()


asyncio.run(main())
