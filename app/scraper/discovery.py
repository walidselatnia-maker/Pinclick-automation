"""Level 1 (niches) and Level 3 (keywords) extraction.

The four-level model, verified against the live app:

    L1  big niche        "Food And Drink"   Interests panel on /keyword-explorer
    L2  main keyword     "Pizza"            chosen by the user from L1 results
    L3  keyword list     "pizza dough recipe" 596K, ...
                                            GET /keyword-explorer?search=pizza
    L4  top pins         ~21 per keyword    see pins.py

L3 is a plain URL, which is why this module is short: navigate, read the table,
parse. No form driving, no click sequences to go stale.
"""

from __future__ import annotations

import re
from typing import Any

from ..logging_setup import get_logger
from ..models import Keyword, Niche
from ..paths import load_settings
from .browser import manager, pace
from .resilience import guarded
from .selectors import SelectorRegistry

log = get_logger("discovery")

#: "596,454" -> 596454 ; "824.2K" -> 824200 ; "2.8M" -> 2800000
_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_volume(raw: str) -> int:
    """Parse the volume formats PinClicks mixes on one page.

    The Interests panel uses "824.2K" while the results table uses "596,454",
    so both have to work.
    """
    if not raw:
        return 0
    text = raw.strip().replace(",", "").replace(" ", "")
    match = re.search(r"([\d.]+)\s*([kmb])?", text, re.I)
    if not match:
        return 0
    try:
        value = float(match.group(1))
    except ValueError:
        return 0
    suffix = (match.group(2) or "").lower()
    return int(value * _SUFFIX.get(suffix, 1))


def _base() -> str:
    return load_settings().get("base_url", "https://app.pinclicks.com").rstrip("/")


# ---------------------------------------------------------------- Level 1

async def fetch_niches(job_id: int | None = None) -> list[Niche]:
    """Read the Interests panel -- the L1 big niches with their volumes."""
    registry = SelectorRegistry()
    url = _base() + registry.url("keywords")

    async def run() -> list[Niche]:
        async with manager.operation() as page:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)  # Livewire render

            rows = await registry.find_all(page, "level1.interest_row")
            out: list[Niche] = []
            for row in rows:
                name_el = await row.query_selector("div")
                vol_el = await row.query_selector(
                    "button[wire\\:click^='setTopInterest'] span"
                )
                if not name_el:
                    continue
                name = (await name_el.inner_text() or "").strip()
                volume = parse_volume(await vol_el.inner_text() if vol_el else "")
                if name:
                    out.append(Niche(name=name, volume=volume))
            return out

    niches = await guarded("fetch L1 niches", run, job_id=job_id)
    log.info("L1: %d niches", len(niches))
    return niches


# ---------------------------------------------------------------- Level 3

async def fetch_keywords(search: str, job_id: int | None = None) -> list[Keyword]:
    """L3: every keyword PinClicks returns for a main keyword.

    ``GET /keyword-explorer?search=<term>`` returns ~100 rows, so the whole
    level is one navigation. Each row links to /keyword/{slug}/{id}; the slug
    is not used as the keyword because the visible text carries the real
    spacing and accents.
    """
    registry = SelectorRegistry()
    url = f"{_base()}{registry.url('keywords')}?search={search.replace(' ', '+')}"

    async def run() -> list[Keyword]:
        async with manager.operation() as page:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            rows = await page.query_selector_all("tr[data-key]")
            out: list[Keyword] = []
            for row in rows:
                link = await row.query_selector("a[href^='/keyword/']")
                if link is None:
                    continue
                keyword = (await link.inner_text() or "").strip()
                if not keyword:
                    continue

                # Volume is the first cell that looks like a number. Reading it
                # positionally would break the moment a column is added.
                volume = 0
                for cell in await row.query_selector_all("td"):
                    text = (await cell.inner_text() or "").strip()
                    if re.fullmatch(r"[\d.,]+\s*[KMB]?", text, re.I):
                        volume = parse_volume(text)
                        if volume:
                            break

                out.append(Keyword(keyword=keyword, volume=volume, niche=search))
            return out

    await pace()
    keywords = await guarded(f"fetch L3 keywords for {search!r}", run, job_id=job_id)
    log.info("L3 %r: %d keywords", search, len(keywords))
    return keywords


# ---------------------------------------------------------------- filtering

def apply_filters(
    keywords: list[dict[str, Any]],
    *,
    min_volume: int = 0,
    max_volume: int | None = None,
    negative_words: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Tag each keyword as included or excluded, with the reason.

    Nothing is dropped -- excluded rows stay visible in the UI (struck through)
    so it is obvious *why* a keyword is not being scraped. Silently vanishing
    rows are how you lose trust in a filter.
    """
    negatives = [w.strip().lower() for w in (negative_words or []) if w.strip()]
    out = []
    for kw in keywords:
        volume = kw.get("volume", 0)
        text = kw.get("keyword", "").lower()
        reason = ""

        if volume < min_volume:
            reason = f"volume < {min_volume:,}"
        elif max_volume is not None and volume > max_volume:
            reason = f"volume > {max_volume:,}"
        else:
            hit = next((w for w in negatives if w in text), None)
            if hit:
                reason = f"negative word: {hit!r}"

        out.append({**kw, "excluded_by": reason, "included": not reason})
    return out
