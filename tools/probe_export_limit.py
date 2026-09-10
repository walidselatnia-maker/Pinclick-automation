"""At how many loaded pins does PinClicks' Export stop working?

Export was reliable all day at 21-25 pins. After the nudge fix made deep
loading fast, exports began failing with
"Download.save_as: Target page, context or browser has been closed" and with
120s download timeouts -- 6 failures and 4 browser restarts in one run.

The obvious suspect is the number of rows selected at export time. This loads
to a given depth, exports, and reports what happened, for several depths, each
in a FRESH context so one crash cannot poison the next measurement.

    .venv\\Scripts\\python.exe tools\\probe_export_limit.py "fruit pizza"
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
OUT = ROOT / "debug" / "export_limit"

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "fruit pizza"
DEPTHS = [25, 50, 75, 100]

URL = "https://app.pinclicks.com/pins?search={}"
WRAP = "#ui-table-wrapper"


async def rows(page):
    return await page.locator("tr[data-key]").count()


async def to_bottom(page):
    await page.evaluate(
        f"""() => {{
            const el = document.querySelector('{WRAP}');
            if (el) el.scrollTop = el.scrollHeight;
            window.scrollTo(0, document.body.scrollHeight);
        }}"""
    )


async def nudge(page, d=600):
    await page.evaluate(
        f"""(dy) => {{
            const el = document.querySelector('{WRAP}');
            if (el) el.scrollTop = Math.max(0, el.scrollTop + dy);
            window.scrollBy(0, dy);
        }}""", -d)
    await page.wait_for_timeout(300)
    await page.evaluate(
        f"""(dy) => {{
            const el = document.querySelector('{WRAP}');
            if (el) el.scrollTop = Math.max(0, el.scrollTop + dy);
            window.scrollBy(0, dy);
        }}""", d)


async def attempt(pw, depth: int) -> str:
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE), channel="chrome", headless=False,
        chromium_sandbox=True, no_viewport=True, accept_downloads=True,
        args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    page.set_default_timeout(30000)
    crashed = {"v": False}
    page.on("crash", lambda _: crashed.update(v=True))

    try:
        await page.goto(URL.format(KEYWORD.replace(" ", "+")),
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        have = await rows(page)
        t0 = time.monotonic()
        last = time.monotonic()
        while have < depth and time.monotonic() - t0 < 90:
            await to_bottom(page)
            await page.wait_for_timeout(1500)
            now = await rows(page)
            if now > have:
                have, last = now, time.monotonic()
            elif time.monotonic() - last > 8:
                break
            elif time.monotonic() - last > 3:
                await nudge(page)

        load_s = time.monotonic() - t0
        cb = page.locator("thead input[type='checkbox']").first
        if await cb.count():
            await cb.check()
            await page.wait_for_timeout(1200)

        t1 = time.monotonic()
        try:
            async with page.expect_download(timeout=60000) as dl:
                await page.locator("button:has-text('Export')").first.click()
                await page.wait_for_timeout(1200)
                await page.locator("text=Pin Data").last.click()
            download = await dl.value
            OUT.mkdir(parents=True, exist_ok=True)
            target = OUT / f"depth{depth}.csv"
            await download.save_as(str(target))
            size = target.stat().st_size
            return (f"depth {depth:3d} | loaded {have:3d} in {load_s:5.1f}s | "
                    f"EXPORT OK  {time.monotonic() - t1:5.1f}s  {size}B")
        except Exception as exc:
            return (f"depth {depth:3d} | loaded {have:3d} in {load_s:5.1f}s | "
                    f"EXPORT FAIL after {time.monotonic() - t1:5.1f}s "
                    f"crashed={crashed['v']} :: {type(exc).__name__}: "
                    f"{str(exc).splitlines()[0][:70]}")
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def main() -> None:
    async with async_playwright() as pw:
        print(f"keyword: {KEYWORD!r}\n")
        for depth in DEPTHS:
            print(await attempt(pw, depth), flush=True)


asyncio.run(main())
