"""Can the pin data be read from the page instead of exported?

Downloading a CSV and reading it back is a lot of moving parts, and
Playwright's download handling has been failing intermittently. The export was
chosen because an early look showed empty annotations in the page data -- but
that was a single glance at initial load, and the Top Pins table scrolls
horizontally, so columns may exist that were never inspected.

This dumps every column header and the full cell contents of the first rows,
plus any pin-shaped records in the Livewire snapshot, and reports which export
template fields are actually present in the page.

    .venv\\Scripts\\python.exe tools\\probe_table_fields.py "fruit pizza"
"""

import asyncio
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "user_data"
OUT = ROOT / "debug" / "table_fields"

KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "fruit pizza"
URL = "https://app.pinclicks.com/pins?search={}"

HEADERS = """() => [...document.querySelectorAll('thead th')]
    .map(th => (th.innerText || '').replace(/\\s+/g, ' ').trim())"""

FIRST_ROWS = """() => [...document.querySelectorAll('tr[data-key]')].slice(0, 3).map(tr => ({
    key: tr.getAttribute('data-key'),
    cells: [...tr.querySelectorAll('td')].map(td =>
        (td.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 160)),
    links: [...tr.querySelectorAll('a')].map(a => a.getAttribute('href')).slice(0, 4),
    imgs:  [...tr.querySelectorAll('img')].map(i => i.getAttribute('src')).slice(0, 2),
}))"""

#: Pull anything pin-shaped out of the Livewire snapshots on the page.
SNAPSHOT = """() => {
  const unwrap = (v) =>
    (Array.isArray(v) && v.length === 2 && v[1] && typeof v[1] === 'object' && 's' in v[1])
      ? v[0] : v;
  const out = [];
  const walk = (node, depth = 0) => {
    if (!node || typeof node !== 'object' || depth > 10 || out.length > 3) return;
    if (Array.isArray(node)) { node.slice(0, 60).forEach(v => walk(unwrap(v), depth + 1)); return; }
    const keys = Object.keys(node);
    if (keys.some(k => /annotation/i.test(k)) ||
        (node.title !== undefined && node.saves !== undefined)) {
      out.push(node);
      return;
    }
    for (const v of Object.values(node)) walk(unwrap(v), depth + 1);
  };
  for (const el of document.querySelectorAll('[wire\\\\:snapshot]')) {
    try { walk(JSON.parse(el.getAttribute('wire:snapshot'))); } catch {}
  }
  return out;
}"""


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="chrome", headless=False,
            chromium_sandbox=True, no_viewport=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--start-maximized"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.set_default_timeout(30000)

        await page.goto(URL.format(KEYWORD.replace(" ", "+")),
                        wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        headers = await page.evaluate(HEADERS)
        print(f"TABLE COLUMNS ({len(headers)}):")
        for i, h in enumerate(headers):
            print(f"  {i:2d}. {h!r}")

        rows = await page.evaluate(FIRST_ROWS)
        print(f"\nFIRST ROW CELLS ({len(rows[0]['cells']) if rows else 0} cells):")
        if rows:
            for i, c in enumerate(rows[0]["cells"]):
                print(f"  {i:2d}. {c!r}")
            print(f"\n  links: {rows[0]['links']}")
            print(f"  imgs : {rows[0]['imgs']}")

        snaps = await page.evaluate(SNAPSHOT)
        print(f"\nSNAPSHOT pin-ish records: {len(snaps)}")
        for rec in snaps[:2]:
            keys = sorted(rec.keys())[:24]
            print(f"  keys: {keys}")
            for k in rec:
                if "annot" in k.lower():
                    print(f"  {k} = {json.dumps(rec[k])[:220]}")

        (OUT / "headers.json").write_text(json.dumps(headers, indent=2), encoding="utf-8")
        (OUT / "rows.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        (OUT / "snapshot.json").write_text(
            json.dumps(snaps, indent=2)[:300000], encoding="utf-8")
        print(f"\nsaved -> {OUT}")

        await ctx.close()


asyncio.run(main())
