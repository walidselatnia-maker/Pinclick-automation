"""Feasibility probe: scrape pins from Pinterest directly.

PinClicks' own "Getting more pins from Pinterest..." is PinClicks calling
Pinterest and waiting, so the latency originates upstream. Going direct removes
the middleman -- but only if Pinterest actually gives us the fields the export
template needs, at a usable speed, without a bot wall.

This measures, it does not build:

  * are we logged in to Pinterest in this Chrome profile?
  * how many pins load, and how fast do they grow while scrolling?
  * which template fields are present -- title, description, annotations,
    pin url, image url, saves?

Nothing is written to the app. Stop the app server first (Chrome locks
user_data/).

    .venv\\Scripts\\python.exe tools\\probe_pinterest.py "fruit pizza" 60
"""

import asyncio
import json
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
OUT = ROOT / "debug" / "pinterest"

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "fruit pizza"
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 60
SCROLL_BUDGET_S = 90

SEARCH = "https://www.pinterest.com/search/pins/?q={}"

#: Pinterest renders pins as anchors to /pin/<id>/. Reading the DOM is enough
#: for a feasibility check; a real implementation would prefer the embedded
#: JSON if it turns out to carry richer fields.
COLLECT = """() => {
  const seen = new Map();
  for (const a of document.querySelectorAll('a[href*="/pin/"]')) {
    const m = a.getAttribute('href').match(/\\/pin\\/([^\\/?#]+)/);
    if (!m) continue;
    const id = m[1];
    if (seen.has(id)) continue;

    const card = a.closest('[data-test-id="pin"]') || a.closest('div[role="listitem"]') || a;
    const img = card.querySelector('img');
    const texts = [...card.querySelectorAll('div,span,h3')]
      .map(e => (e.innerText || '').trim())
      .filter(t => t && t.length > 3 && t.length < 300);

    seen.set(id, {
      id,
      url: 'https://www.pinterest.com/pin/' + id + '/',
      image: img ? (img.getAttribute('src') || '') : '',
      alt: img ? (img.getAttribute('alt') || '') : '',
      texts: texts.slice(0, 4),
    });
  }
  return [...seen.values()];
}"""

LOGGED_IN = """() => {
  const html = document.documentElement.innerHTML;
  return {
    hasLoginWall: !!document.querySelector('[data-test-id="login-modal"], [aria-label="Log in"]'),
    mentionsSignup: /Sign up|Log in to see more|Continue with/i.test(
      (document.body.innerText || '').slice(0, 4000)),
    hasPwsData: html.includes('__PWS_DATA__'),
  };
}"""


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    url = SEARCH.format(KEYWORD.replace(" ", "%20"))

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            # Playwright disables Chrome's sandbox by default for
            # persistent contexts, which shows an "unsupported
            # command-line flag: --no-sandbox" bar and is an obvious
            # abnormal-browser signal. Keep the real sandbox on.
            chromium_sandbox=True,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(45000)

        print(f"Loading {url}")
        t0 = time.monotonic()
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        print(f"  title: {await page.title()!r}")
        print(f"  url:   {page.url}")
        print(f"  state: {json.dumps(await page.evaluate(LOGGED_IN))}")

        # Pinterest VIRTUALISES the grid: pins scrolled out of view are removed
        # from the DOM. Re-reading the DOM each scroll therefore LOSES earlier
        # pins, so accumulate across reads keyed by pin id.
        collected: dict[str, dict] = {}

        def absorb(batch) -> int:
            fresh = 0
            for pin in batch:
                if pin["id"] not in collected:
                    collected[pin["id"]] = pin
                    fresh += 1
            return fresh

        absorb(await page.evaluate(COLLECT))
        print(f"\ninitial pins: {len(collected)} after {time.monotonic() - t0:.1f}s")

        stagnant = 0
        start = time.monotonic()
        while len(collected) < TARGET and time.monotonic() - start < SCROLL_BUDGET_S:
            await page.mouse.wheel(0, 6000)
            await page.wait_for_timeout(1200)
            fresh = absorb(await page.evaluate(COLLECT))
            if fresh:
                print(f"  {time.monotonic() - start:5.1f}s  {len(collected)} pins "
                      f"(+{fresh})")
                stagnant = 0
            else:
                stagnant += 1
                if stagnant >= 8:
                    print("  no new pins for 8 scrolls -> stopping")
                    break

        pins = list(collected.values())
        elapsed = time.monotonic() - t0
        print(f"\nTOTAL: {len(pins)} pins in {elapsed:.1f}s")

        # Field coverage against the export template.
        with_img = sum(1 for p in pins if p["image"])
        with_alt = sum(1 for p in pins if p["alt"])
        with_text = sum(1 for p in pins if p["texts"])
        print(f"  image url : {with_img}/{len(pins)}")
        print(f"  alt text  : {with_alt}/{len(pins)}")
        print(f"  any text  : {with_text}/{len(pins)}")

        for p in pins[:3]:
            print(f"\n  {p['url']}")
            print(f"    alt   : {p['alt'][:90]}")
            print(f"    texts : {p['texts']}")

        (OUT / "search.html").write_text(await page.content(), encoding="utf-8")
        (OUT / "pins.json").write_text(json.dumps(pins[:40], indent=2), encoding="utf-8")
        await page.screenshot(path=str(OUT / "search.png"), full_page=False)
        print(f"\ncaptured -> {OUT}")

        await ctx.close()


asyncio.run(main())
