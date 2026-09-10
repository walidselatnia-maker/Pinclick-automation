"""Measure infinite-scroll behaviour on the Top Pins page.

Key mechanic (confirmed from the live UI): when you scroll to the bottom of the
pin table, PinClicks shows

    "Getting more pins from Pinterest..."

and fetches the next batch from Pinterest itself. That call is SLOW and is the
thing the user reports as "stuck / takes a long time".

That banner is the correct signal to wait on. Counting "empty scrolls" is not:
an earlier version of this script used a 30s budget, saw no new rows while the
banner was still spinning, and wrongly concluded there were only 23 pins. There
are far more -- the user observed position 44+.

Rules encoded here, which M4 should mirror:
  * banner visible          -> still working, keep waiting (do NOT give up)
  * banner gone + no growth -> genuinely the end of results
  * nothing at all for HARD_STALL_S -> hung, hand over to the watchdog

    .venv\\Scripts\\python.exe tools\\probe_scroll.py "nails" 50
"""

import asyncio
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "user_data"

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "nails"
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 50

ROW = "tr[data-key]"
LOADING_TEXT = "Getting more pins"

POLL_S = 2
#: Give up on a batch only after this long with the banner showing.
BATCH_BUDGET_S = 300
#: No banner and no new rows for this long -> end of results.
QUIET_END_S = 25
#: Overall cap so a wedged page cannot run forever.
TOTAL_BUDGET_S = 900


async def count_rows(page) -> int:
    return await page.locator(ROW).count()


async def loading_visible(page) -> bool:
    try:
        return await page.locator(f"text={LOADING_TEXT}").count() > 0
    except Exception:
        return False


async def scroll_bottom(page) -> None:
    """Drive the scroll the way a user does.

    Assigning scrollTop alone was not enough to arm the loader. Real wheel
    events over the table are, so do both: jump the containers to the bottom,
    then emit actual wheel events with the cursor over the table.
    """
    await page.evaluate(
        """() => {
            const el = document.querySelector('#ui-table-wrapper');
            if (el) el.scrollTop = el.scrollHeight;
            window.scrollTo(0, document.body.scrollHeight);
        }"""
    )
    try:
        table = page.locator("#ui-table-wrapper")
        if await table.count():
            await table.hover(timeout=5000)
    except Exception:
        pass
    for _ in range(4):
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(250)


async def main() -> None:
    url = f"https://app.pinclicks.com/pins?search={KEYWORD.replace(' ', '+')}"
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            no_viewport=True,
            # Maximised matters: the table is max-h-[calc(100vh-16rem)], so a
            # short window gives a ~286px scroll area that may never reach the
            # lazy-load trigger.
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(30000)

        print(f"Loading {url}  (target {TARGET} pins)")
        t0 = time.monotonic()
        await page.goto(url, wait_until="domcontentloaded")

        for _ in range(60):
            if await count_rows(page) > 0:
                break
            await page.wait_for_timeout(1000)

        rows = await count_rows(page)
        print(f"initial: {rows} rows in {time.monotonic() - t0:.1f}s\n")

        batches: list[tuple[int, float]] = []
        last_growth = time.monotonic()
        batch_start = time.monotonic()
        was_loading = False

        while rows < TARGET and time.monotonic() - t0 < TOTAL_BUDGET_S:
            await scroll_bottom(page)
            await page.wait_for_timeout(POLL_S * 1000)

            now = time.monotonic()
            new_rows = await count_rows(page)
            loading = await loading_visible(page)

            if loading and not was_loading:
                batch_start = now
                print("  banner: 'Getting more pins from Pinterest...'")
            was_loading = loading

            if new_rows > rows:
                took = now - batch_start
                batches.append((new_rows - rows, took))
                print(f"  +{new_rows - rows:2d} pins -> {new_rows:3d} total  "
                      f"(batch took {took:.0f}s)")
                rows = new_rows
                last_growth = now
                batch_start = now
                continue

            quiet = now - last_growth
            if loading:
                if quiet > BATCH_BUDGET_S:
                    print(f"  STALLED: banner showing but no rows for {quiet:.0f}s "
                          f"-> watchdog territory")
                    break
                print(f"    loading... {quiet:.0f}s", end="\r", flush=True)
            else:
                if quiet > QUIET_END_S:
                    print(f"\n  no banner and no growth for {quiet:.0f}s -> end of results")
                    break

        total = time.monotonic() - t0
        print(f"\nFINAL: {rows} pins in {total:.0f}s")
        if batches:
            times = [t for _, t in batches]
            print(f"batches: {len(batches)}  sizes={[n for n, _ in batches]}")
            print(f"batch time: min={min(times):.0f}s max={max(times):.0f}s "
                  f"avg={sum(times) / len(times):.0f}s")
            print(f"SUGGESTED settings.json timeouts.step_ms = {int(max(times) * 3 * 1000)}")

        await ctx.close()


asyncio.run(main())
