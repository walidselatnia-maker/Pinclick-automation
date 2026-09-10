"""Can we get saves count and annotations from Pinterest?

The rendered grid shows only image, link and title, so the DOM alone loses
``saves_count`` and ``annotation`` from the export template. But the grid is
populated from Pinterest's own JSON endpoints, and those payloads are usually
much richer than what gets rendered.

This intercepts every JSON response while scrolling a search page and reports
which template fields are actually present, with real coverage counts. It only
reads traffic the page makes on its own -- no private endpoints are called
directly.

    .venv\\Scripts\\python.exe tools\\probe_pinterest_api.py "fruit pizza"
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
OUT = ROOT / "debug" / "pinterest_api"

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "fruit pizza"
SCROLLS = 8

SEARCH = "https://www.pinterest.com/search/pins/?q={}"


def find_pins(node, out, depth=0):
    """Collect dicts that look like pin records, anywhere in the payload."""
    if depth > 10 or len(out) > 400:
        return
    if isinstance(node, dict):
        # A pin record has a numeric-ish id plus at least one pin-only key.
        if "id" in node and any(
            k in node for k in ("aggregated_pin_data", "pin_join", "grid_title", "images")
        ):
            out.append(node)
            return
        for v in node.values():
            find_pins(v, out, depth + 1)
    elif isinstance(node, list):
        for v in node[:80]:
            find_pins(v, out, depth + 1)


def saves_of(pin):
    agg = pin.get("aggregated_pin_data") or {}
    stats = agg.get("aggregated_stats") or {}
    for key in ("saves", "done"):
        if isinstance(stats.get(key), int):
            return stats[key]
    for key in ("repin_count", "save_count"):
        if isinstance(pin.get(key), int):
            return pin[key]
    return None


def annotations_of(pin):
    join = pin.get("pin_join") or {}
    for key in ("visual_annotation", "annotations_with_links", "annotations"):
        val = join.get(key)
        if isinstance(val, list) and val:
            if all(isinstance(x, str) for x in val):
                return val
            names = [x.get("name") for x in val if isinstance(x, dict) and x.get("name")]
            if names:
                return names
        if isinstance(val, dict) and val:
            return list(val.keys())
    val = pin.get("visual_annotation")
    if isinstance(val, list) and val:
        return [x for x in val if isinstance(x, str)]
    return None


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    payloads: list[tuple[str, dict]] = []

    async def on_response(resp):
        url = resp.url
        if "pinterest.com" not in url:
            return
        if not any(s in url for s in ("/resource/", "/_/", "/graphql")):
            return
        try:
            if "application/json" not in (resp.headers.get("content-type") or ""):
                return
            payloads.append((url, await resp.json()))
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

        url = SEARCH.format(KEYWORD.replace(" ", "%20"))
        print(f"Loading {url}")
        t0 = time.monotonic()
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        for _ in range(SCROLLS):
            await page.mouse.wheel(0, 6000)
            await page.wait_for_timeout(1400)

        print(f"captured {len(payloads)} JSON responses in "
              f"{time.monotonic() - t0:.1f}s")

        pins: dict[str, dict] = {}
        for _, data in payloads:
            found: list[dict] = []
            find_pins(data, found)
            for p in found:
                pid = str(p.get("id"))
                if pid and pid not in pins:
                    pins[pid] = p

        print(f"unique pin records: {len(pins)}\n")
        if not pins:
            (OUT / "sample_urls.json").write_text(
                json.dumps([u for u, _ in payloads][:40], indent=2), encoding="utf-8")
            print("no pin records found; endpoint list saved for inspection")
            await ctx.close()
            return

        # Search results carry far more fields than the lightweight "related"
        # records that also appear in these payloads. Inspect the richest ones.
        rows = sorted(pins.values(), key=lambda r: -len(r))
        have_saves = [p for p in rows if saves_of(p) is not None]
        have_annot = [p for p in rows if annotations_of(p)]
        have_desc = [p for p in rows if (p.get("description") or "").strip()]
        have_title = [p for p in rows
                      if (p.get("grid_title") or p.get("title") or "").strip()]

        print("FIELD COVERAGE")
        print(f"  title       : {len(have_title)}/{len(rows)}")
        print(f"  description : {len(have_desc)}/{len(rows)}")
        print(f"  saves count : {len(have_saves)}/{len(rows)}")
        print(f"  annotations : {len(have_annot)}/{len(rows)}")

        for p in rows[:3]:
            print(f"\n  pin {p.get('id')}")
            print(f"    title  : {str(p.get('grid_title') or p.get('title') or '')[:70]}")
            print(f"    desc   : {str(p.get('description') or '')[:70]}")
            print(f"    saves  : {saves_of(p)}")
            print(f"    annot  : {annotations_of(p)}")

        (OUT / "pins_sample.json").write_text(
            json.dumps(rows[:5], indent=2)[:400000], encoding="utf-8")
        (OUT / "all_keys.json").write_text(
            json.dumps(sorted({k for r in rows for k in r}), indent=2),
            encoding="utf-8")
        print(f"\nsample saved -> {OUT / 'pins_sample.json'}")

        await ctx.close()


asyncio.run(main())
