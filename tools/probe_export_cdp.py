"""Export without Playwright's Download object.

`download.save_as()` fails intermittently with "Target page, context or browser
has been closed" -- measured at 25, 50 and 100 pins, and succeeding at 98, so
it is not depth related. Playwright's download handling depends on the page
that started the download still being alive, and PinClicks' export appears to
tear something down.

This bypasses that: Chrome is told via CDP to write downloads straight into a
directory, then the file is read from disk. Nothing depends on the page
surviving the download.

    .venv\\Scripts\\python.exe tools\\probe_export_cdp.py "fruit pizza" 50
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
DL = ROOT / "data" / "downloads" / "cdp"

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "fruit pizza"
DEPTH = int(sys.argv[2]) if len(sys.argv) > 2 else 50
ROUNDS = 3

URL = "https://app.pinclicks.com/pins?search={}"
WRAP = "#ui-table-wrapper"


async def rows(page):
    return await page.locator("tr[data-key]").count()


async def scroll(page, dy=None):
    if dy is None:
        await page.evaluate(
            f"""() => {{
                const el = document.querySelector('{WRAP}');
                if (el) el.scrollTop = el.scrollHeight;
                window.scrollTo(0, document.body.scrollHeight);
            }}""")
    else:
        await page.evaluate(
            f"""(dy) => {{
                const el = document.querySelector('{WRAP}');
                if (el) el.scrollTop = Math.max(0, el.scrollTop + dy);
                window.scrollBy(0, dy);
            }}""", dy)


def newest_csv(before: set[Path]) -> Path | None:
    files = {p for p in DL.glob("*.csv")} - before
    ready = [p for p in files if p.stat().st_size > 0
             and not p.with_suffix(p.suffix + ".crdownload").exists()]
    return max(ready, key=lambda p: p.stat().st_mtime) if ready else None


async def one_round(pw, n: int) -> str:
    DL.mkdir(parents=True, exist_ok=True)
    before = set(DL.glob("*.csv"))

    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE), channel="chrome", headless=False,
        chromium_sandbox=True, no_viewport=True,
        # OFF on purpose: with accept_downloads Playwright intercepts the
        # download and the CDP download path is ignored, so no file lands.
        accept_downloads=False,
        args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    page.set_default_timeout(30000)

    try:
        # Tell Chrome itself where downloads go; then the file exists on disk
        # whatever happens to the page afterwards.
        client = await ctx.new_cdp_session(page)
        await client.send("Browser.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": str(DL),
            "eventsEnabled": True,
        })

        await page.goto(URL.format(KEYWORD.replace(" ", "+")),
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)

        have = await rows(page)
        t0 = time.monotonic()
        last = time.monotonic()
        while have < DEPTH and time.monotonic() - t0 < 90:
            await scroll(page)
            await page.wait_for_timeout(1500)
            now = await rows(page)
            if now > have:
                have, last = now, time.monotonic()
            elif time.monotonic() - last > 8:
                break
            elif time.monotonic() - last > 3:
                await scroll(page, -600)
                await page.wait_for_timeout(300)
                await scroll(page, 600)
        load_s = time.monotonic() - t0

        cb = page.locator("thead input[type='checkbox']").first
        if await cb.count():
            await cb.check()
            await page.wait_for_timeout(1200)

        t1 = time.monotonic()
        await page.locator("button:has-text('Export')").first.click()
        await page.wait_for_timeout(1200)
        await page.locator("text=Pin Data").last.click()

        # Poll the directory rather than awaiting a Download object.
        found = None
        while time.monotonic() - t1 < 60:
            found = newest_csv(before)
            if found:
                break
            await asyncio.sleep(0.5)

        if not found:
            return f"round {n} | loaded {have:3d} in {load_s:5.1f}s | NO FILE after 60s"

        lines = found.read_text(encoding="utf-8-sig", errors="replace").count("\n")
        return (f"round {n} | loaded {have:3d} in {load_s:5.1f}s | "
                f"OK {time.monotonic() - t1:5.1f}s  {found.stat().st_size}B  "
                f"~{lines - 1} rows")
    except Exception as exc:
        return f"round {n} | FAIL :: {type(exc).__name__}: {str(exc).splitlines()[0][:80]}"
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def main() -> None:
    async with async_playwright() as pw:
        print(f"keyword={KEYWORD!r} depth={DEPTH}\n")
        for n in range(1, ROUNDS + 1):
            print(await one_round(pw, n), flush=True)


asyncio.run(main())
