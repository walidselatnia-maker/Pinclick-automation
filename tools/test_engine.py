"""End-to-end test of the L1/L3/L4 engine against the live site.

Exercises the real modules the app uses -- not a reimplementation -- so a pass
here means the app's pipeline works:

    L1  fetch_niches()          Interests panel
    L3  fetch_keywords("pizza") keyword list + volumes
    L4  fetch_pins(<keyword>)   ~21 pins via PinClicks' Export

Stop the app server first -- Chrome locks user_data/.

    .venv\\Scripts\\python.exe tools\\test_engine.py
"""

import asyncio
import sys
from pathlib import Path

# Pin titles contain emoji; the default Windows console codec (cp1252) cannot
# encode them and would crash the script on print, not on scrape.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scraper import discovery, pins  # noqa: E402
from app.scraper.browser import manager  # noqa: E402

MAIN_KEYWORD = sys.argv[1] if len(sys.argv) > 1 else "pizza"


async def main() -> None:
    ok = True
    try:
        print("=" * 60)
        print("L1: niches (Interests panel)")
        niches = await discovery.fetch_niches()
        print(f"  {len(niches)} niches")
        for n in niches[:5]:
            print(f"    {n.name:<24} {n.volume:>12,}")
        if not niches:
            ok = False
            print("  FAIL: no niches")

        print("=" * 60)
        print(f"L3: keywords for {MAIN_KEYWORD!r}")
        keywords = await discovery.fetch_keywords(MAIN_KEYWORD)
        print(f"  {len(keywords)} keywords")
        for k in keywords[:8]:
            print(f"    {k.keyword:<34} {k.volume:>12,}")
        if not keywords:
            ok = False
            print("  FAIL: no keywords")

        print("=" * 60)
        print("filtering")
        rows = [{"keyword": k.keyword, "volume": k.volume} for k in keywords]
        filtered = discovery.apply_filters(
            rows, min_volume=50_000, negative_words=["aesthetic"]
        )
        kept = [r for r in filtered if r["included"]]
        dropped = [r for r in filtered if not r["included"]]
        print(f"  kept {len(kept)}, excluded {len(dropped)}")
        for r in dropped[:4]:
            print(f"    - {r['keyword']:<30} ({r['excluded_by']})")

        if not kept:
            print("  (nothing passed the filter; using the top keyword instead)")
            kept = rows[:1]

        target_kw = kept[0]["keyword"]
        print("=" * 60)
        print(f"L4: pins for {target_kw!r} via Export")
        got = await pins.fetch_pins(target_kw, target=21)
        print(f"  {len(got)} pins")
        if not got:
            ok = False
            print("  FAIL: no pins")
        else:
            p = got[0]
            for field in ("spy_title", "spy_description", "annotation",
                          "pin_url", "image_url", "pin_score", "saves_count"):
                value = str(p.get(field, ""))
                print(f"    {field:<16} {value[:70]}")
            filled = sum(
                1 for row in got
                if row.get("annotation") and row["annotation"] != "N/A"
            )
            print(f"  annotations present on {filled}/{len(got)} pins")
    finally:
        await manager.shutdown()

    print("=" * 60)
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


asyncio.run(main())
