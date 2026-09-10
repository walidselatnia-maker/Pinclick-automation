"""One request: what does the APP's browser profile get from PinClicks?

The user can reach pinclicks.com normally in their own Chrome profile, but the
app's profile is blocked. Same machine, same IP -- so the block is not purely
IP based, and something about this profile's state is the difference.

Deliberately ONE page load. The IP is already under a block; this is not the
moment to generate traffic.

    .venv\\Scripts\\python.exe tools\\probe_block.py
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

TARGET = "https://app.pinclicks.com/login"

#: Cloudflare state that lives in the profile rather than at the IP.
CF_COOKIES = ("cf_clearance", "__cf_bm", "__cflb", "cf_chl_")


async def main() -> None:
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            chromium_sandbox=True, no_viewport=True, args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(45000)

        print("Cookies held for pinclicks.com BEFORE the request:")
        found = False
        for c in await ctx.cookies():
            if "pinclicks" in (c.get("domain") or "") or \
               any(c.get("name", "").startswith(p) for p in CF_COOKIES):
                found = True
                name = c.get("name")
                dom = c.get("domain")
                val = str(c.get("value"))
                print(f"  {name:<18} {dom:<24} len={len(val)}")
        if not found:
            print("  (none)")

        print(f"\nRequesting {TARGET} ... (one request only)")
        try:
            resp = await page.goto(TARGET, wait_until="domcontentloaded")
            status = resp.status if resp else "?"
            title = await page.title()
            body = (await page.inner_text("body"))[:300].replace("\n", " ")
            print(f"  status : {status}")
            print(f"  title  : {title!r}")
            print(f"  url    : {page.url}")
            print(f"  text   : {body[:220]}")

            blocked = status in (403, 429) or "blocked" in title.lower()
            print(f"\n  BLOCKED: {blocked}")
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {str(exc).splitlines()[0][:110]}")

        await ctx.close()


asyncio.run(main())
