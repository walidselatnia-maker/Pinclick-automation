"""Reading the Interests panel (Levels 1 and 2).

The panel is a tree. Top level is the big niches ("Food And Drink"); clicking a
row's chevron calls ``setTopInterest('<id>')`` and replaces the list with that
interest's children ("Dinner Recipes" 4.1M, "Pizza" 4M, "Fruit" 2.6M ...).

**This drill-down is the only correct way to get sub-niches.** Searching the
niche's *name* as text is a different query entirely and returns junk -- for
"Food And Drink" it yields "food and drink logo", "food and drink worksheet",
"food and drink aesthetic". Those are keyword variations of the phrase, not
sub-niches.

Data comes from the page's embedded Livewire snapshot rather than the DOM,
because the snapshot carries:

    {"id":913534722711,"label":"Dinner Recipes","search_volume":4064383,
     "search_volume_display":"4.1M","has_children":false}

which gives exact integers instead of "4.1M" strings, and ``has_children`` so
we know which rows can be drilled. The DOM only shows a volume inside the
chevron button, so rows without children render no volume at all there.
"""

from __future__ import annotations

from typing import Any

from ..logging_setup import get_logger
from ..paths import load_settings
from .browser import manager
from .resilience import guarded

log = get_logger("interests")

#: Pull ``topCategories`` out of whichever Livewire snapshot holds it.
#: Livewire wraps values as ``[value, {"s": "arr"}]``, so everything is
#: unwrapped on the way out.
_EXTRACT = """() => {
  const unwrap = (v) =>
    (Array.isArray(v) && v.length === 2 && v[1] && typeof v[1] === 'object' && 's' in v[1])
      ? v[0] : v;

  const findKey = (node, key, depth = 0) => {
    if (!node || typeof node !== 'object' || depth > 12) return null;
    if (Object.prototype.hasOwnProperty.call(node, key)) return unwrap(node[key]);
    for (const value of Object.values(node)) {
      const hit = findKey(unwrap(value), key, depth + 1);
      if (hit) return hit;
    }
    return null;
  };

  for (const el of document.querySelectorAll('[wire\\\\:snapshot]')) {
    let snap;
    try { snap = JSON.parse(el.getAttribute('wire:snapshot')); } catch { continue; }
    const cats = findKey(snap, 'topCategories');
    if (!cats || typeof cats !== 'object') continue;

    const out = [];
    for (const raw of Object.values(cats)) {
      const item = unwrap(raw);
      if (item && item.label) {
        out.push({
          id: String(item.id),
          name: item.label,
          volume: Number(item.search_volume) || 0,
          volume_display: item.search_volume_display || '',
          has_children: !!item.has_children,
        });
      }
    }
    if (out.length) return out;
  }
  return [];
}"""


def _base() -> str:
    return load_settings().get("base_url", "https://app.pinclicks.com").rstrip("/")


def _explorer_url() -> str:
    return _base() + "/keyword-explorer"


async def _read_panel(page) -> list[dict[str, Any]]:
    rows = await page.evaluate(_EXTRACT)
    return rows or []


async def fetch_top_interests(job_id: int | None = None) -> list[dict[str, Any]]:
    """Level 1: the big niches."""

    async def run() -> list[dict[str, Any]]:
        async with manager.operation() as page:
            await page.goto(_explorer_url(), wait_until="domcontentloaded")
            await page.wait_for_timeout(3500)  # Livewire render
            return await _read_panel(page)

    rows = await guarded("fetch L1 interests", run, job_id=job_id)
    log.info("L1: %d interests", len(rows))
    return rows


async def _click_and_wait(page, interest_id: str, current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open one interest and wait for the panel to actually change."""
    before = {r["id"] for r in current}
    selector = (
        r'button[wire\:click="setTopInterest(' + f"'{interest_id}'" + r')"]'
    )

    if not await page.locator(selector).count():
        raise RuntimeError(
            f"Interest {interest_id} is not open-able at this level. "
            "Its parent must be opened first."
        )
    await page.click(selector)

    rows = current
    for _ in range(60):
        await page.wait_for_timeout(500)
        rows = await _read_panel(page)
        if rows and {r["id"] for r in rows} != before:
            return rows
    raise RuntimeError(f"Panel did not change after opening {interest_id}")


async def drill_path(
    path: list[str],
    job_id: int | None = None,
) -> list[dict[str, Any]]:
    """Open a chain of interests and return the deepest level's rows.

    The panel is STATEFUL and not URL-addressable: the control for a child
    only exists once its parent is open, and reloading resets to the top
    level. Verified against the live site -- the URL stays
    /keyword-explorer no matter how deep you go.

    So a nested niche cannot be opened directly; the whole path has to be
    replayed from the top on a fresh page. Passing only the child's id is what
    produced "Interest <id> has no drill-down control".
    """

    async def run() -> list[dict[str, Any]]:
        async with manager.operation() as page:
            await page.goto(_explorer_url(), wait_until="domcontentloaded")
            await page.wait_for_timeout(3500)

            rows = await _read_panel(page)
            for interest_id in path:
                rows = await _click_and_wait(page, interest_id, rows)
            return rows

    rows = await guarded(f"drill path {'/'.join(path)}", run, job_id=job_id)
    log.info("L2: %d sub-niches at path %s", len(rows), "/".join(path))
    return rows


async def drill_interest(
    interest_id: str,
    job_id: int | None = None,
) -> list[dict[str, Any]]:
    """Open a single top-level interest. Thin wrapper over :func:`drill_path`."""
    return await drill_path([interest_id], job_id=job_id)
