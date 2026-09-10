"""Verify Level 1 and the Level-2 drill-down through the real app modules.

    L1  fetch_top_interests()          big niches
    L2  drill_interest(<id>)           sub-niches inside one

Expected for "Food And Drink": Dinner Recipes 4.1M, Pizza 4M, Fruit 2.6M,
Dessert 2.1M, Snacks 1.8M -- NOT "food and drink logo" / "food and drink
worksheet", which is what a text search returns.

Stop the app server first -- Chrome locks user_data/.

    .venv\\Scripts\\python.exe tools\\test_interests.py "Food And Drink"
"""

import asyncio
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scraper import interests  # noqa: E402
from app.scraper.browser import manager  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "Food And Drink"

#: Names that prove we drilled rather than text-searched.
EXPECT_ANY = {"dinner recipes", "pizza", "fruit", "dessert", "snacks", "pasta recipes"}
#: The tell for the broken text search is that every row CONTAINS the parent
#: phrase ("food and drink logo", "food and drink worksheet"). Words like
#: "aesthetic" are not a tell -- "Desayunos Aesthetic" is a real sub-interest.


async def main() -> int:
    ok = True
    try:
        print("L1: top interests")
        top = await interests.fetch_top_interests()
        print(f"  {len(top)} interests")
        for row in top[:5]:
            drill = ">" if row["has_children"] else " "
            print(f"    {row['name']:<22} {row['volume']:>12,} {drill}")
        if not top:
            print("  FAIL: none returned")
            return 1

        match = next((r for r in top if r["name"].lower() == TARGET.lower()), None)
        if match is None:
            print(f"  FAIL: {TARGET!r} not in the list")
            return 1
        print(f"\n  matched {match['name']!r} id={match['id']} "
              f"volume={match['volume']:,} has_children={match['has_children']}")

        print(f"\nL2: drilling into {match['name']!r}")
        subs = await interests.drill_interest(match["id"])
        print(f"  {len(subs)} sub-niches")
        for row in subs[:12]:
            drill = ">" if row["has_children"] else " "
            print(f"    {row['name']:<30} {row['volume']:>12,} {drill}")

        names = {r["name"].lower() for r in subs}
        hits = EXPECT_ANY & names
        print(f"\n  expected sub-niches present: {sorted(hits) or 'NONE'}")
        if not hits:
            ok = False
            print("  FAIL: none of the expected sub-niches came back")

        phrase = TARGET.lower()
        echoes = [n for n in names if phrase in n]
        if len(echoes) > len(names) * 0.5:
            ok = False
            print(f"  FAIL: {len(echoes)}/{len(names)} rows echo {TARGET!r} "
                  f"-- this is a text search, not a drill-down: {echoes[:4]}")
        else:
            print(f"  rows echoing the parent phrase: {len(echoes)} (expected ~0)")

        missing_volume = [r["name"] for r in subs if not r["volume"]]
        print(f"  rows missing a volume: {len(missing_volume)}")
        if missing_volume[:3]:
            print(f"    e.g. {missing_volume[:3]}")
    finally:
        await manager.shutdown()

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


sys.exit(asyncio.run(main()))
