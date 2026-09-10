"""Test PinClicks' own Export button on the Top Pins page.

If PinClicks can hand us a CSV directly, that removes an entire scraping stage
and every selector that goes with it. This finds out what Export actually
produces: which columns, how many rows, and whether it is gated by plan.

Saves any downloaded file to debug/export/ and captures the UI at each step.

Stop the app server first -- Chrome locks user_data/.

    .venv\\Scripts\\python.exe tools\\test_export.py
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "user_data"
OUT = ROOT / "debug" / "export"
KEYWORD = "Chicken Breast Recipe"
URL = f"https://app.pinclicks.com/pins?search={KEYWORD.replace(' ', '+')}"


async def snap(page, label: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(OUT / f"{label}.png"), full_page=True)
    (OUT / f"{label}.html").write_text(await page.content(), encoding="utf-8")
    print(f"  captured {label}")


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            no_viewport=True, accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(20000)

        print(f"Loading {URL}")
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)

        # Select all rows first -- Export is a bulk action and may need a
        # selection before it will do anything.
        header_cb = page.locator("thead input[type='checkbox']").first
        if await header_cb.count():
            await header_cb.check()
            await page.wait_for_timeout(1500)
            print("  selected all rows")

        export = page.locator("button:has-text('Export')").first
        if not await export.count():
            print("  ! no Export button found")
            await snap(page, "no-export-button")
            await ctx.close()
            return

        # Export is a dropdown, not a direct download. It offers two datasets,
        # and "Annotated Interests" is where the pin annotations live.
        for item in ("Pin Data", "Annotated Interests"):
            print(f"Export -> {item}")
            await export.click()
            await page.wait_for_timeout(1500)

            option = page.locator(f"text={item}").last
            if not await option.count():
                print(f"  ! menu item {item!r} not found")
                continue

            try:
                async with page.expect_download(timeout=90000) as dl_info:
                    await option.click()
                download = await dl_info.value
                name = download.suggested_filename or f"{item}.csv"
                target = OUT / name
                await download.save_as(str(target))
                print(f"  DOWNLOADED -> {target} ({target.stat().st_size} bytes)")
            except Exception as exc:
                print(f"  no download for {item!r}: {type(exc).__name__}: {str(exc)[:90]}")
                await snap(page, f"export-{item.replace(' ', '-').lower()}")
            await page.wait_for_timeout(2000)

        await ctx.close()


asyncio.run(main())
