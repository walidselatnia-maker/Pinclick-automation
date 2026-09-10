"""Run the full pipeline from the command line and write the template CSV.

    L3  keywords for a main keyword
    ->  filter by volume / negative words
    ->  L4 top pins for each surviving keyword (PinClicks Export)
    ->  CSV in keywords-template.csv format

Usage:

    .venv\\Scripts\\python.exe tools\\run_flow.py pizza --min 100000 --max-keywords 3
    .venv\\Scripts\\python.exe tools\\run_flow.py pizza --negative aesthetic,story

Stop the app server first -- Chrome locks user_data/.
"""

import argparse
import asyncio
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import export  # noqa: E402
from app.scraper import discovery, pins  # noqa: E402
from app.scraper.browser import manager  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PinClicks mining pipeline")
    p.add_argument("keyword", help="main keyword, e.g. pizza")
    p.add_argument("--min", type=int, default=0, help="minimum search volume")
    p.add_argument("--max", type=int, default=None, help="maximum search volume")
    p.add_argument("--negative", default="", help="comma-separated excluded words")
    p.add_argument("--pins", type=int, default=21, help="pins per keyword")
    p.add_argument("--max-keywords", type=int, default=5,
                   help="how many surviving keywords to scrape pins for")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    negatives = [w.strip() for w in args.negative.split(",") if w.strip()]

    all_pins: list[dict] = []
    failed: list[str] = []

    try:
        print(f"L3: keywords for {args.keyword!r}")
        keywords = await discovery.fetch_keywords(args.keyword)
        print(f"  {len(keywords)} keywords found")
        if not keywords:
            print("  nothing to do")
            return 1

        rows = [{"keyword": k.keyword, "volume": k.volume} for k in keywords]
        filtered = discovery.apply_filters(
            rows, min_volume=args.min, max_volume=args.max, negative_words=negatives
        )
        kept = [r for r in filtered if r["included"]]
        print(f"  {len(kept)} passed filters, {len(filtered) - len(kept)} excluded")

        targets = kept[: args.max_keywords]
        print(f"\nL4: pins for {len(targets)} keyword(s), target {args.pins} each\n")

        for i, row in enumerate(targets, 1):
            kw = row["keyword"]
            print(f"[{i}/{len(targets)}] {kw}  ({row['volume']:,})")
            try:
                got = await pins.fetch_pins(kw, target=args.pins)
                all_pins.extend(got)
                print(f"    {len(got)} pins")
            except Exception as exc:
                # One bad keyword must never end the run (circuit breaker).
                failed.append(kw)
                print(f"    FAILED: {type(exc).__name__}: {str(exc)[:90]}")
    finally:
        await manager.shutdown()

    if not all_pins:
        print("\nNo pins collected.")
        return 1

    paths = export.export_run(all_pins, run_id="cli", label=args.keyword.replace(" ", "-"))
    print(f"\n{len(all_pins)} pins -> {paths['accepted']}")
    if failed:
        print(f"failed keywords ({len(failed)}): {', '.join(failed)}")
    return 0


sys.exit(asyncio.run(main()))
