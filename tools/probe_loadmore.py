"""Diagnose how more pins are loaded on the Top Pins page.

Programmatic scrolling of #ui-table-wrapper did not add rows. This reports the
actual scroll geometry, any load-more/pagination controls, and the Livewire
state flags, so the real trigger can be identified instead of guessed at.

    .venv\\Scripts\\python.exe tools\\probe_loadmore.py "Chicken Breast Recipe"
"""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "user_data"
OUT = ROOT / "debug" / "loadmore"

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "Chicken Breast Recipe"
#: "pins" = Pinterest Search tab, "explore" = PinClicks Database / Pin Explorer.
TAB = sys.argv[2] if len(sys.argv) > 2 else "pins"

REPORT = """() => {
    const el = document.querySelector('#ui-table-wrapper');
    const rows = document.querySelectorAll('tr[data-key]').length;

    const buttons = [...document.querySelectorAll('button, a')]
        .map(b => (b.innerText || '').trim())
        .filter(t => t && t.length < 40 &&
            /more|load|next|page|show|all/i.test(t));

    // Anything Livewire exposes about paging.
    const wireAttrs = [...document.querySelectorAll('[wire\\\\:click]')]
        .map(e => e.getAttribute('wire:click'))
        .filter(v => /page|more|load/i.test(v));

    return {
        rows,
        wrapperFound: !!el,
        scrollHeight: el ? el.scrollHeight : null,
        clientHeight: el ? el.clientHeight : null,
        scrollTop: el ? el.scrollTop : null,
        scrollable: el ? el.scrollHeight > el.clientHeight + 5 : null,
        bodyScrollable: document.body.scrollHeight > window.innerHeight + 5,
        candidateButtons: [...new Set(buttons)],
        pagingWireClicks: [...new Set(wireAttrs)],
    };
}"""


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    url = f"https://app.pinclicks.com/pins?search={KEYWORD.replace(' ', '+')}"
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            no_viewport=True, args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(20000)

        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)

        print("BEFORE scrolling:")
        print(json.dumps(await page.evaluate(REPORT), indent=2))

        # Real wheel events over the table, which is what a user actually does.
        table = page.locator("#ui-table-wrapper")
        if await table.count():
            await table.hover()
        for _ in range(12):
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(900)

        print("\nAFTER 12 wheel scrolls over the table:")
        print(json.dumps(await page.evaluate(REPORT), indent=2))

        await page.screenshot(path=str(OUT / "after-wheel.png"), full_page=True)
        (OUT / "after-wheel.html").write_text(await page.content(), encoding="utf-8")
        print(f"\ncaptured -> {OUT}")

        await ctx.close()


asyncio.run(main())
