"""Level 4: top pins for a keyword, via PinClicks' own Export.

Why export instead of scraping the table:

* The export CSV already contains every field the template needs -- Title,
  Description, Keyword Annotations, URL, Image URL, Pin Score, Saves -- plus
  Position, Repins, Reactions and Comments for the Phase-2 rules.
* It is a product feature, not an internal detail, so it survives restyling.
* Annotations are populated there (20/23 measured) where the embedded page JSON
  left them empty.

Depth: the first batch is ~21 pins, arrives in 2-3s, and has been reliable in
every run. Asking for more triggers a live "Getting more pins from Pinterest..."
fetch which measured 303s and 25s on the same keyword minutes apart, adding zero
pins both times. So going deeper is opt-in, bounded, and never fatal -- whatever
loaded still gets exported.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import heartbeat
from ..logging_setup import get_logger
from ..models import NA
from ..paths import DATA_DIR, load_settings
from .browser import manager, pace
from .resilience import guarded

log = get_logger("pins")

#: Raw exports are kept, not discarded -- if a parse ever looks wrong you can
#: open exactly what PinClicks handed us.
DOWNLOAD_DIR = DATA_DIR / "downloads"

#: export CSV column -> our field name
COLUMN_MAP = {
    "Title": "spy_title",
    "Description": "spy_description",
    "Keyword Annotations": "annotation",
    "URL": "pin_url",
    "Image URL": "image_url",
    "Pin Score": "pin_score",
    "Saves": "saves_count",
    "Position": "position",
    "Repins": "repins",
    "Reactions": "reactions",
    "Comments": "comments",
    "Is Repin": "is_repin",
    "Created At": "created_at",
    "Board URL": "board_url",
    "Profile URL": "profile_url",
    "ID": "pin_id",
}


def _base() -> str:
    return load_settings().get("base_url", "https://app.pinclicks.com").rstrip("/")


def search_url(keyword: str) -> str:
    return f"{_base()}/pins?search={keyword.replace(' ', '+')}"


# ---------------------------------------------------------------- lazy load

async def _row_count(page) -> int:
    return await page.locator("tr[data-key]").count()


async def _loading_visible(page) -> bool:
    banner = load_settings().get("scroll", {}).get(
        "loading_banner_text", "Getting more pins"
    )
    try:
        return await page.locator(f"text={banner}").count() > 0
    except Exception:
        return False


async def _to_bottom(page) -> None:
    """Jump both scrollers to the bottom."""
    await page.evaluate(
        """() => {
            const el = document.querySelector('#ui-table-wrapper');
            if (el) el.scrollTop = el.scrollHeight;
            window.scrollTo(0, document.body.scrollHeight);
        }"""
    )


async def _scroll_by(page, dy: int) -> None:
    """Move the table and the window together.

    Either can be the scroller that owns the lazy-load sentinel depending on
    window size, so both are moved rather than guessing.
    """
    await page.evaluate(
        """(dy) => {
            const el = document.querySelector('#ui-table-wrapper');
            if (el) el.scrollTop = Math.max(0, el.scrollTop + dy);
            window.scrollBy(0, dy);
        }""",
        dy,
    )


async def _nudge(page, distance: int = 600, pause_ms: int = 300) -> None:
    """Re-arm the loader by moving up and back down.

    THIS is what makes deep loading work. A lazy-loader fires when its
    sentinel ENTERS the viewport, not while it sits inside it. Parking at the
    bottom leaves the sentinel permanently intersecting, so it never fires
    again -- which is why waiting there did nothing for 300 seconds.

    Measured on "fruit pizza", starting from 23 pins:
        park (scroll down, wait)      48 pins in 48.6s
        big up/down bounce            73 pins in 16.0s
        small nudge (this)            73 pins in  8.4s
    """
    await _scroll_by(page, -distance)
    await page.wait_for_timeout(pause_ms)
    await _scroll_by(page, distance)


async def _load_until(page, target: int, job_id: int | None = None) -> int:
    """Load pins until ``target`` is reached, or growth genuinely stops.

    Scroll to the bottom, and whenever the count stops rising, nudge to
    re-trigger the loader. Never raises: a stall degrades to "fewer pins"
    rather than losing the keyword.
    """
    import time

    cfg = load_settings().get("scroll", {})
    settle_ms = cfg.get("post_load_settle_ms", 3000)
    stall_before_nudge = cfg.get("nudge_after_ms", 3000) / 1000
    give_up_after = cfg.get("give_up_after_ms", 20000) / 1000
    close_enough_after = cfg.get("close_enough_after_ms", 6000) / 1000
    exhausted_after = cfg.get("exhausted_after_ms", 7000) / 1000
    total_budget = cfg.get("total_budget_ms", 180000) / 1000
    nudge_px = cfg.get("nudge_distance_px", 600)

    # Let the first batch settle before touching anything.
    try:
        await page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    await page.wait_for_timeout(settle_ms)
    try:
        # Only if this tab is actually in the background. Calling it
        # unconditionally also un-minimises the whole window, which is exactly
        # what the browser is minimised to avoid -- and with one tab open it
        # was never needed anyway.
        if await page.evaluate("document.visibilityState") != "visible":
            await page.bring_to_front()
    except Exception:
        pass

    started = time.monotonic()
    have = await _row_count(page)
    last_growth = time.monotonic()

    while have < target and time.monotonic() - started < total_budget:
        await _to_bottom(page)
        await page.wait_for_timeout(1500)

        if job_id is not None:
            heartbeat(job_id)

        now = await _row_count(page)
        if now > have:
            log.info("  +%d pins -> %d (%.1fs)", now - have, now,
                     time.monotonic() - started)
            have = now
            last_growth = time.monotonic()
            continue

        stalled = time.monotonic() - last_growth

        # PinClicks serves pins in fixed batches (25 at a time), so asking for
        # 100 realistically lands on 98. Waiting the full stall budget for the
        # last couple is dead time -- accept a near miss much sooner.
        if have >= target * 0.9 and stalled > close_enough_after:
            log.info("Stopping at %d of %d requested (batch boundary)", have, target)
            break

        # Plenty of keywords simply do not have many pins. The banner is what
        # separates "PinClicks is still fetching" from "there is nothing left":
        # while it shows, waiting is justified; without it, a stall means the
        # well is dry and holding on burns time on every thin keyword.
        fetching = await _loading_visible(page)
        if not fetching and stalled > exhausted_after:
            log.info("No more pins available: stopped at %d (asked %d)",
                     have, target)
            break

        if stalled > give_up_after:
            log.info("Fetch stuck %.0fs -> stopping at %d (asked %d)",
                     stalled, have, target)
            break
        if stalled > stall_before_nudge:
            await _nudge(page, nudge_px)

    return have



async def _export_pins(page, keyword: str) -> list[dict[str, Any]]:
    """Select every loaded row and pull PinClicks' own CSV export.

    Retries the export ITSELF rather than letting the caller retry, because a
    caller-level retry re-scrapes every pin first. Exporting ~100 rows has been
    seen to kill the browser mid-download ("Target page, context or browser has
    been closed"), and re-loading 98 pins to recover from that cost minutes.
    """
    attempts = 3
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            if page.is_closed():
                raise RuntimeError("page closed before export")

            header_cb = page.locator("thead input[type='checkbox']").first
            if await header_cb.count():
                await header_cb.check()
                await page.wait_for_timeout(1200)

            export_btn = page.locator("button:has-text('Export')").first
            if not await export_btn.count():
                raise RuntimeError("Export button not found on Top Pins")

            async with page.expect_download(timeout=120000) as dl:
                await export_btn.click()
                await page.wait_for_timeout(1200)
                await page.locator("text=Pin Data").last.click()
            download = await dl.value

            # Copy out of Playwright's temp area immediately, while the context
            # is still alive.
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
            slug = "".join(c if c.isalnum() else "-" for c in keyword).strip("-")
            saved = DOWNLOAD_DIR / f"{slug or 'export'}.csv"
            await download.save_as(str(saved))
            return parse_export_csv(saved.read_bytes(), keyword)

        except Exception as exc:
            last = exc
            log.warning("Export attempt %d/%d for %r failed: %s",
                        attempt, attempts, keyword, str(exc)[:120])
            if attempt < attempts and not page.is_closed():
                await page.wait_for_timeout(2000)
                continue
            break

    raise RuntimeError(f"Export failed for {keyword!r}: {last}")



# ---------------------------------------------------------------- read page

#: Pull every pin record out of the Livewire snapshots embedded in the page.
#:
#: This replaces PinClicks' CSV export. The export was chosen originally
#: because a first look suggested annotations were empty in the page data --
#: that was wrong, it was read from the wrong record. ``visual_annotation`` is
#: fully populated here, and reading the page avoids the download entirely,
#: which was failing intermittently with "Target page, context or browser has
#: been closed" regardless of how many pins were loaded.
_EXTRACT_PINS = r"""() => {
  const unwrap = (v) =>
    (Array.isArray(v) && v.length === 2 && v[1] && typeof v[1] === 'object' && 's' in v[1])
      ? v[0] : v;

  const found = new Map();
  const walk = (node, depth = 0) => {
    if (!node || typeof node !== 'object' || depth > 12) return;
    if (Array.isArray(node)) {
      for (const v of node.slice(0, 400)) walk(unwrap(v), depth + 1);
      return;
    }
    if (node.id !== undefined && node.title !== undefined && node.saves !== undefined) {
      found.set(String(node.id), node);
      return;
    }
    for (const v of Object.values(node)) walk(unwrap(v), depth + 1);
  };

  // Attribute names containing a colon need CSS escaping, which has now been
  // got wrong twice through Python-string layers. Skip the selector entirely.
  for (const el of document.getElementsByTagName('*')) {
    const snap = el.getAttribute('wire:snapshot');
    if (!snap) continue;
    try { walk(JSON.parse(snap)); } catch (e) {}
  }

  // Thumbnails only exist in the DOM, so pair them up by row key.
  const thumbs = {};
  for (const tr of document.querySelectorAll('tr[data-key]')) {
    const img = tr.querySelector('img');
    if (img) thumbs[tr.getAttribute('data-key')] = img.getAttribute('src') || '';
  }

  const flat = (v) => {
    const a = unwrap(v);
    if (!Array.isArray(a)) return [];
    return a.flatMap(x => Array.isArray(unwrap(x)) ? unwrap(x) : [unwrap(x)])
            .filter(x => typeof x === 'string');
  };

  return [...found.values()].map(p => ({
    id: String(p.id),
    title: p.title || '',
    description: p.description || '',
    annotations: flat(p.visual_annotation),
    saves: p.saves ?? p.total_saves ?? null,
    pin_score: p.pin_score ?? null,
    pin_grade: p.pin_grade ?? null,
    position: p.position ?? null,
    repins: p.total_repins ?? null,
    reactions: p.total_reactions ?? null,
    comments: p.total_comments ?? null,
    is_repin: p.is_repin ?? null,
    created_at: p.created_at ?? null,
    board_url: p.board_url || '',
    profile_url: p.pinner_username ? '/' + p.pinner_username + '/' : '',
    link: p.link || '',
    domain: p.domain || '',
    thumb: thumbs[String(p.id)] || '',
  }));
}"""


def _full_size(thumb: str) -> str:
    """PinClicks renders 136px thumbnails; the same asset exists at 736px."""
    if not thumb:
        return NA
    for small in ("/136x136/", "/75x75_RS/", "/236x/", "/170x/"):
        if small in thumb:
            return thumb.replace(small, "/736x/")
    return thumb


def _iso(ts: Any) -> str:
    """created_at arrives as a unix timestamp."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%b %d, %Y")
    except (TypeError, ValueError, OSError):
        return NA


def _shape(raw: dict[str, Any], keyword: str, index: int) -> dict[str, Any]:
    """Map one page record onto the export template's fields."""
    def txt(value):
        value = "" if value is None else str(value).strip()
        return value or NA

    return {
        "seed_keyword": keyword,
        "pin_id": txt(raw.get("id")),
        "spy_title": txt(raw.get("title")),
        "spy_description": txt(raw.get("description")),
        "annotation": ", ".join(raw.get("annotations") or []) or NA,
        "pin_url": f"https://www.pinterest.com/pin/{raw['id']}" if raw.get("id") else NA,
        "image_url": _full_size(raw.get("thumb") or ""),
        "pin_score": txt(raw.get("pin_score")),
        "saves_count": txt(raw.get("saves")),
        "position": raw.get("position") if raw.get("position") is not None else index,
        "repins": txt(raw.get("repins")),
        "reactions": txt(raw.get("reactions")),
        "comments": txt(raw.get("comments")),
        "is_repin": txt(raw.get("is_repin")),
        "created_at": _iso(raw.get("created_at")),
        "board_url": txt(raw.get("board_url")),
        "profile_url": txt(raw.get("profile_url")),
        "destination_url": txt(raw.get("link")),
        "domain": txt(raw.get("domain")),
    }


async def read_pins_from_page(page, keyword: str) -> list[dict[str, Any]]:
    """Read every loaded pin straight out of the page."""
    raw = await page.evaluate(_EXTRACT_PINS)
    pins = [_shape(r, keyword, i) for i, r in enumerate(raw, start=1)]
    pins.sort(key=lambda p: p["position"] if isinstance(p["position"], int) else 9999)
    return pins



async def _settle_rows(page, job_id: int | None = None) -> int:
    """Wait for the row count to stop moving, then report it.

    Loading stops as soon as the target is close enough, but PinClicks may
    already have a batch in flight. That batch lands a few seconds later, so
    reading immediately captured 48 rows when the page ended up holding 73 --
    a whole batch thrown away. Waiting for stability keeps them.
    """
    import time

    cfg = load_settings().get("scroll", {})
    quiet_needed = cfg.get("settle_quiet_ms", 3000) / 1000
    max_wait = cfg.get("settle_max_ms", 20000) / 1000

    started = time.monotonic()
    count = await _row_count(page)
    last_change = time.monotonic()

    while time.monotonic() - started < max_wait:
        await page.wait_for_timeout(1000)
        if job_id is not None:
            heartbeat(job_id)
        now = await _row_count(page)
        if now != count:
            log.info("  late batch: %d -> %d pins", count, now)
            count = now
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= quiet_needed:
            break

    return count


# ---------------------------------------------------------------- export

async def fetch_pins(
    keyword: str,
    target: int | None = None,
    job_id: int | None = None,
) -> list[dict[str, Any]]:
    """Search Top Pins for ``keyword`` and return parsed pin rows."""
    settings = load_settings()
    target = target or settings.get("pins_per_keyword_default", 21)
    url = search_url(keyword)

    async def run() -> list[dict[str, Any]]:
        async with manager.operation() as page:
            await page.goto(url, wait_until="domcontentloaded")

            # Wait for the first batch to render.
            for _ in range(40):
                if await _row_count(page) > 0:
                    break
                await page.wait_for_timeout(500)

            rows = await _row_count(page)
            if rows == 0:
                log.warning("No pins found for %r", keyword)
                return []

            if rows < target:
                rows = await _load_until(page, target, job_id=job_id)

            # Catch any batch still arriving before we read the page.
            rows = await _settle_rows(page, job_id=job_id)
            log.info("  %d pins on page before extraction", rows)

            # Extraction runs against the page we already loaded. If it
            # fails, retry it HERE -- never let the caller retry, because a
            # caller retry re-navigates and throws away every pin we just
            # spent time loading.
            last: Exception | None = None
            for attempt in range(1, 4):
                try:
                    pins = await read_pins_from_page(page, keyword)
                    if pins:
                        return pins
                    last = RuntimeError("no pin records in page")
                except Exception as exc:
                    last = exc
                    log.warning("Extract attempt %d/3 for %r: %s",
                                attempt, keyword, str(exc)[:110])
                await page.wait_for_timeout(1500)

            raise RuntimeError(f"Could not read pins for {keyword!r}: {last}")

    await pace()
    pins = await guarded(f"fetch pins for {keyword!r}", run,
                         step_ms=600000, attempts=2, job_id=job_id)
    log.info("L4 %r: %d pins", keyword, len(pins))
    return pins


def parse_export_csv(data: bytes, keyword: str) -> list[dict[str, Any]]:
    """Turn the exported CSV into our pin dicts.

    Maps by column NAME, never by position, so PinClicks adding or reordering
    a column cannot silently shift every value one field to the left.
    """
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))

    out: list[dict[str, Any]] = []
    for index, row in enumerate(reader, start=1):
        pin: dict[str, Any] = {"seed_keyword": keyword, "position": index}
        for source, target in COLUMN_MAP.items():
            value = (row.get(source) or "").strip()
            pin[target] = value if value else NA
        # Position from the file if present; otherwise row order.
        try:
            pin["position"] = int(str(pin.get("position", index)).strip() or index)
        except (TypeError, ValueError):
            pin["position"] = index
        out.append(pin)
    return out


async def open_top_pins_tab(keyword: str) -> str:
    """Open a keyword's Top Pins page in a NEW tab for the user to look at.

    Takes the browser lock even though it uses its own tab. Previously it did
    not, so clicking a keyword during a run put a second stream of PinClicks
    requests on the wire from the same IP -- exactly the pattern that got the
    IP blocked. It also let a mid-run heal() close the tab this was using.

    Fails fast rather than queueing: a research click that silently waits five
    minutes behind a scrape is worse than being told the browser is busy.
    """
    url = search_url(keyword)
    try:
        async with manager.exclusive(timeout_s=5):
            ctx = await manager.start()
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as exc:
                # A slow render is fine -- the tab is open and the user can
                # watch it load. Only genuine failures matter here.
                log.warning("Top Pins tab for %r opened but did not settle: %s",
                            keyword, exc)
            await page.bring_to_front()
            log.info("Opened Top Pins tab for %r", keyword)
            return url
    except TimeoutError as exc:
        raise RuntimeError(
            "A scrape is running. Wait for it to finish, then try again."
        ) from exc
