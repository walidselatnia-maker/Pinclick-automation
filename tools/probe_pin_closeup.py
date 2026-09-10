"""Do annotations exist on an individual Pinterest pin page?

The search grid feed carries title, description and saves, but no annotations.
PinClicks' "Keyword Annotations" column comes from Pinterest's own annotation
data, which is attached to a pin's closeup (detail) view rather than the grid.

This opens ONE pin and reports whether annotations are retrievable there, and
how long a single pin costs -- which decides whether filling that column for
~100 pins per keyword is affordable at all.

    .venv\\Scripts\\python.exe tools\\probe_pin_closeup.py <pin_url>
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
OUT = ROOT / "debug" / "pin_closeup"

PIN_URL = sys.argv[1] if len(sys.argv) > 1 else \
    "https://www.pinterest.com/pin/247768416994405793/"

ANNOTATION_KEYS = (
    "visual_annotation", "annotations", "annotations_with_links",
    "pin_join", "visual_objects", "gen_ai_topics", "interests",
)


def hunt(node, results, path="", depth=0):
    """Report any annotation-ish key found anywhere in the payload."""
    if depth > 12:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ANNOTATION_KEYS and v:
                results.append((path + "/" + k, v))
            hunt(v, results, path + "/" + k, depth + 1)
    elif isinstance(node, list):
        for i, v in enumerate(node[:40]):
            hunt(v, results, f"{path}[{i}]", depth + 1)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payloads: list[dict] = []

    async def on_response(resp):
        if "pinterest.com" not in resp.url:
            return
        if not any(s in resp.url for s in ("/resource/", "/_/", "/graphql")):
            return
        try:
            if "application/json" in (resp.headers.get("content-type") or ""):
                payloads.append(await resp.json())
        except Exception:
            pass

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            chromium_sandbox=True, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("response", on_response)
        page.set_default_timeout(45000)

        print(f"Loading {PIN_URL}")
        t0 = time.monotonic()
        await page.goto(PIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)
        elapsed = time.monotonic() - t0
        print(f"loaded in {elapsed:.1f}s, {len(payloads)} JSON responses")

        found: list[tuple[str, object]] = []
        for data in payloads:
            hunt(data, found)

        # Also check the embedded page data.
        embedded = await page.evaluate(
            """() => {
                const el = document.querySelector('#__PWS_DATA__');
                return el ? el.textContent : '';
            }"""
        )
        if embedded:
            try:
                hunt(json.loads(embedded), found, path="PWS")
            except Exception:
                pass

        print(f"\nannotation-ish hits: {len(found)}")
        seen = set()
        for path, value in found[:12]:
            key = path.split("/")[-1]
            if key in seen:
                continue
            seen.add(key)
            print(f"\n  {key}   (at {path[-70:]})")
            print(f"    {json.dumps(value)[:300]}")

        print(f"\nCOST: {elapsed:.1f}s for one pin. "
              f"100 pins ~= {elapsed * 100 / 60:.1f} min sequentially.")

        (OUT / "hits.json").write_text(
            json.dumps([{"path": p, "value": v} for p, v in found[:40]],
                       indent=2, default=str)[:200000], encoding="utf-8")
        await ctx.close()


asyncio.run(main())
