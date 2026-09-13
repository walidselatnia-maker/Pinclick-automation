"""REST API.

Settings, runs, log tail, presets, session lifecycle, selector
calibration/preflight, and the four-level mining flow:

    L1  GET  /api/niches                 Interests panel
    L3  POST /api/keywords               keywords for a main keyword
        POST /api/keywords/filter        volume / negative-word filtering
    L4  POST /api/scrape                 start a pin run (background)
        GET  /api/scrape/status          progress for the UI
        POST /api/export                 write the template CSV
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import asyncio

from . import db, export as export_mod, filtering, language
from .logging_setup import get_logger
from pathlib import Path

from .paths import (
    EXPORTS_DIR,
    SETTINGS_FILE,
    load_selectors,
    load_settings,
    save_json,
)
from .scraper import (
    discovery,
    interests as interests_mod,
    pins as pins_mod,
    session as session_mod,
)
from .scraper import browser as browser_mod
from .scraper.browser import manager
from .scraper.selectors import SelectorRegistry
from .ai import keyring, ollama_client

log = get_logger("api")
router = APIRouter(prefix="/api")


# ---------------------------------------------------------------- settings

@router.get("/settings")
def get_settings() -> dict[str, Any]:
    return load_settings()


class SettingsPatch(BaseModel):
    sites: list[str] | None = None
    pins_per_keyword_default: int | None = None
    site_default_value: str | None = None
    status_default_value: str | None = None
    #: Which languages to keep, e.g. ["en"] or ["all"].
    language_keep: list[str] | None = None


@router.patch("/settings")
def patch_settings(patch: SettingsPatch) -> dict[str, Any]:
    """Only the fields a user legitimately edits from the UI are patchable.

    Timeouts, pacing and retry policy stay file-only on purpose -- they are
    safety limits, not preferences, and shouldn't be tweakable by accident.
    """
    settings = load_settings()
    values = patch.model_dump(exclude_none=True)

    # Nested setting, so it cannot just be copied across like the flat ones.
    keep = values.pop("language_keep", None)
    if keep is not None:
        settings.setdefault("language_filter", {})["keep"] = keep

    for key, value in values.items():
        settings[key] = value

    if not settings.get("sites"):
        raise HTTPException(400, "At least one site column is required")

    save_json(SETTINGS_FILE, settings)
    log.info("Settings updated: %s", list(patch.model_dump(exclude_none=True)))
    return settings


# ---------------------------------------------------------------- health

@router.get("/health")
def health() -> dict[str, Any]:
    """Startup status for the UI banner.

    ``selectors_calibrated`` is deliberately surfaced: until the selectors have
    been calibrated against the live site, any scrape would produce garbage,
    and the UI needs to say so rather than let you start a run.
    """
    selectors = load_selectors()
    run = db.latest_run()
    return {
        "ok": True,
        "selectors_calibrated": bool(selectors.get("_calibrated")),
        "selectors_calibrated_at": selectors.get("_calibrated_at"),
        "current_run_id": run["id"] if run else None,
    }


# ---------------------------------------------------------------- runs

@router.post("/runs")
def new_run(label: str = "") -> dict[str, Any]:
    run_id = db.create_run(label)
    db.log(run_id, "Run created")
    log.info("Created run %s", run_id)
    return {"run_id": run_id}


@router.get("/runs/current")
def current_run() -> dict[str, Any]:
    run = db.latest_run()
    if run is None:
        return {"run_id": None}
    return dict(run)


@router.get("/runs/{run_id}/log")
def run_log(run_id: int, after_id: int = 0) -> dict[str, Any]:
    entries = db.tail_log(run_id, after_id=after_id)
    return {
        "entries": entries,
        "last_id": entries[-1]["id"] if entries else after_id,
    }


# ---------------------------------------------------------------- presets

class Preset(BaseModel):
    name: str
    payload: dict[str, Any]


@router.get("/presets")
def get_presets() -> list[dict[str, Any]]:
    return db.list_presets()


@router.post("/presets")
def put_preset(preset: Preset) -> dict[str, str]:
    db.save_preset(preset.name, preset.payload)
    return {"status": "saved"}


@router.delete("/presets/{name}")
def remove_preset(name: str) -> dict[str, str]:
    db.delete_preset(name)
    return {"status": "deleted"}


# ---------------------------------------------------------------- session

@router.get("/session/status")
async def session_status() -> dict[str, Any]:
    """Drives the Active/Expired badge. Always cached, never navigates.

    This used to run a live probe whenever a browser happened to be open,
    which meant a page refresh could navigate the tab out from under an
    in-flight login. Reading the badge must never move the browser -- use
    POST /session/probe for an explicit live check.
    """
    cached = db.get_state("session_status")
    if cached:
        return {**cached, "cached": True}
    return {"status": "unknown", "reason": "not checked yet", "cached": False}


@router.post("/session/login")
async def session_login() -> dict[str, Any]:
    """Open a visible window on the login page. Never handles credentials."""
    try:
        return await session_mod.open_login_window()
    except Exception as exc:
        # A bare 500 tells the user nothing actionable; surface the real cause.
        log.exception("Login window failed")
        raise HTTPException(502, f"Could not open the login window: {exc}") from exc


@router.get("/session/observe")
async def session_observe() -> dict[str, Any]:
    """Passive check: reads the current page without navigating it.

    Safe to poll while the user is typing their password. ``/session/probe``
    is NOT -- it navigates, which wipes the login form.
    """
    result = await session_mod.observe()
    if result.get("status") == "active":
        db.set_state("session_status", result)
    return result


@router.post("/session/probe")
async def session_probe() -> dict[str, Any]:
    """Force a live re-check by navigating (used by the Re-check button).

    Do not call this on a timer during login.
    """
    result = await session_mod.probe()
    db.set_state("session_status", result)
    return result


@router.post("/session/close")
async def session_close() -> dict[str, str]:
    await manager.close()
    return {"status": "closed"}


# ---------------------------------------------------------------- reset

@router.post("/reset")
def reset_everything() -> dict[str, Any]:
    """Wipe all history: niches, keywords, pins, filter results, AI verdicts,
    overrides, runs and logs. The login, sites and API keys survive."""
    if _scrape_state["running"]:
        raise HTTPException(409, "A scrape is running. Stop it first.")
    counts = db.wipe_history()
    _filter_state.clear()
    _scrape_state.update(keyword="", done=0, total=0, pins=0, failed=[],
                         finished=False, error="", results=[])
    log.warning("History wiped: %s", counts)
    return {"status": "cleared", **counts}


# ---------------------------------------------------------------- selectors

@router.get("/selectors")
def get_selectors() -> dict[str, Any]:
    return load_selectors()


@router.post("/selectors/calibrate")
async def selectors_calibrate() -> dict[str, Any]:
    """Dump the live DOM of key pages to debug/ so selectors can be written."""
    run = db.latest_run()
    result = await session_mod.calibrate(run["id"] if run else "manual")
    return result


@router.post("/selectors/mark-calibrated")
def selectors_mark_calibrated() -> dict[str, Any]:
    """Flip the calibrated flag once selectors.json has been filled in."""
    registry = SelectorRegistry()
    registry.mark_calibrated()
    return {
        "calibrated": True,
        "calibrated_at": registry.config.get("_calibrated_at"),
    }


@router.post("/selectors/preflight")
async def selectors_preflight() -> dict[str, Any]:
    """Verify every required selector resolves before a run is allowed."""
    registry = SelectorRegistry()
    base = load_settings().get("base_url", "").rstrip("/")
    async with manager.operation() as page:
        try:
            await page.goto(base + registry.url("dashboard"), wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)  # let Livewire finish rendering
        except Exception as exc:
            raise HTTPException(502, f"Could not reach PinClicks: {exc}") from exc
        return await registry.preflight(page)


# ---------------------------------------------------------------- L1 niches

def _cache_ttl() -> float:
    """How long browse results stay fresh, in seconds.

    Interest trees and keyword lists barely move day to day, so a long TTL is
    safe and makes browsing feel instant. ``refresh=true`` always bypasses it.
    """
    return float(load_settings().get("browse_cache_seconds", 86400))


@router.get("/niches")
async def get_niches(refresh: bool = False) -> dict[str, Any]:
    """L1: the big niches from the Interests panel."""
    key = "niches:top"
    if not refresh:
        cached = db.cache_get(key, _cache_ttl())
        if cached is not None:
            return {"niches": language.keep_only(cached, "name"), "cached": True}

    try:
        rows = await interests_mod.fetch_top_interests()
    except Exception as exc:
        log.exception("Niche fetch failed")
        raise HTTPException(502, f"Could not load niches: {exc}") from exc

    db.cache_set(key, rows)
    return {"niches": language.keep_only(rows, "name"), "cached": False}


class DrillRequest(BaseModel):
    #: Ancestor ids from the top level down to the interest being opened.
    path: list[str]
    refresh: bool = False


@router.post("/niches/children")
async def get_sub_niches(req: DrillRequest) -> dict[str, Any]:
    """L2+: the sub-niches inside a niche, addressed by its full path.

    The whole path is required because the Interests panel is stateful and not
    URL-addressable: a child's control only exists once its parent is open,
    and a reload resets to the top. Sending just the child id fails with
    "no drill-down control" -- which is exactly the bug this replaced.
    """
    if not req.path:
        raise HTTPException(400, "A niche path is required")

    key = "niches:" + "/".join(req.path)
    if not req.refresh:
        cached = db.cache_get(key, _cache_ttl())
        if cached is not None:
            return {"path": req.path,
                    "niches": language.keep_only(cached, "name"), "cached": True}

    try:
        rows = await interests_mod.drill_path(req.path)
    except Exception as exc:
        log.exception("Drill-down failed for path %s", req.path)
        raise HTTPException(502, f"Could not open that niche: {exc}") from exc

    db.cache_set(key, rows)
    return {"path": req.path,
            "niches": language.keep_only(rows, "name"), "cached": False}


@router.post("/cache/clear")
def clear_cache(prefix: str = "") -> dict[str, Any]:
    """Drop cached browse results. Empty prefix clears everything."""
    removed = db.cache_clear(prefix)
    log.info("Cleared %d cache entries (prefix=%r)", removed, prefix)
    return {"cleared": removed}


# ------------------------------------------------------------ open in tab

class OpenPinsRequest(BaseModel):
    keyword: str


@router.post("/open-pins")
async def open_pins(req: OpenPinsRequest) -> dict[str, Any]:
    """Open a keyword's Top Pins page in a new browser tab for the user.

    Research-only: this shows the page, it does not scrape or export. It also
    must not interrupt the research screen, so the caller stays where it is.
    """
    keyword = req.keyword.strip()
    if not keyword:
        raise HTTPException(400, "A keyword is required")
    try:
        url = await pins_mod.open_top_pins_tab(keyword)
    except RuntimeError as exc:
        # Busy is a normal state, not a fault: 409 so the UI can say "wait"
        # rather than presenting it as a failure.
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        log.exception("Could not open Top Pins tab")
        raise HTTPException(502, f"Could not open Top Pins: {exc}") from exc
    return {"opened": url, "keyword": keyword}


# ---------------------------------------------------------------- L3 keywords

class KeywordQuery(BaseModel):
    search: str
    min_volume: int = 0
    max_volume: int | None = None
    negative_words: list[str] = []
    refresh: bool = False


@router.post("/keywords")
async def get_keywords(query: KeywordQuery) -> dict[str, Any]:
    """L3: keywords for a main keyword, already tagged include/exclude.

    Excluded rows are returned too, with the reason, so the UI can show *why*
    a keyword is not being scraped instead of silently dropping it.
    """
    search = query.search.strip()
    if not search:
        raise HTTPException(400, "A main keyword is required")

    # Cache the SCRAPE, not the filtered result. Filtering is pure and cheap,
    # so changing min/max/negative words re-filters instantly instead of
    # hitting PinClicks again.
    key = f"keywords:{search.lower()}"
    rows = None if query.refresh else db.cache_get(key, _cache_ttl())
    was_cached = rows is not None

    if rows is None:
        try:
            keywords = await discovery.fetch_keywords(search)
        except Exception as exc:
            log.exception("Keyword fetch failed")
            raise HTTPException(502, f"Could not load keywords: {exc}") from exc
        rows = [{"keyword": k.keyword, "volume": k.volume} for k in keywords]
        db.cache_set(key, rows)

    tagged = discovery.apply_filters(
        rows,
        min_volume=query.min_volume,
        max_volume=query.max_volume,
        negative_words=query.negative_words,
    )
    # Removed outright, not flagged: other languages should not appear at all.
    tagged = language.keep_only(tagged, "keyword")

    return {
        "search": search,
        "keywords": tagged,
        "total": len(tagged),
        "included": sum(1 for r in tagged if r["included"]),
        "cached": was_cached,
    }


# ---------------------------------------------------------------- L4 scrape

#: Progress for the running job. Single-run tool, so one slot is enough.
_scrape_state: dict[str, Any] = {
    "running": False,
    "keyword": "",
    "done": 0,
    "total": 0,
    "pins": 0,
    "failed": [],
    "finished": False,
    "error": "",
}
_scrape_task: asyncio.Task | None = None


class ScrapeRequest(BaseModel):
    keywords: list[str]
    pins_per_keyword: int = 50
    niche: str = ""
    refresh: bool = False
    #: True keeps what is already collected and adds to it; False starts
    #: clean. The UI asks when there is anything to lose.
    append: bool = False


async def _run_scrape(req: ScrapeRequest, run_id: int) -> None:
    """Scrape pins for each keyword, storing results as they arrive.

    One keyword failing never ends the run -- it is recorded and skipped, so a
    single bad keyword cannot cost you the other forty.

    Results are cached per keyword and the whole run is persisted, so a browser
    refresh (or an app restart) does not throw away work that took minutes.
    """
    # Carry the previous results forward when appending, so several sessions
    # of research build one set instead of overwriting each other.
    collected: list[dict[str, Any]] = []
    names = working_keywords() if req.append else []
    if req.append:
        collected = list(_current_pins())
        log.info("Appending to %d existing pin(s)", len(collected))

    try:
        for index, keyword in enumerate(req.keywords, start=1):
            _scrape_state.update(keyword=keyword, done=index - 1)

            # Space out real scrapes. Cache hits are free and skip this.
            if index > 1 and not _scrape_state.get("last_was_cached"):
                await browser_mod.pace_between_keywords()

            # Same contract as the niche and keyword caches: a cached result
            # is reused as-is, and Refresh is how you force a fresh scrape.
            # Depth is deliberately NOT part of the decision -- a keyword that
            # only has 25 pins would otherwise be re-scraped forever whenever
            # you asked for 50.
            cached = None if req.refresh else db.cache_get(
                _pins_cache_key(keyword), _cache_ttl()
            )
            _scrape_state["last_was_cached"] = bool(cached)
            if cached:
                collected.extend(cached)
                names.append(keyword)
                _scrape_state["pins"] = len(collected)
                db.log(run_id, f"  {len(cached)} pins for {keyword!r} (cached)")
                _scrape_state["done"] = index
                continue

            db.log(run_id, f"Scraping pins for {keyword!r} ({index}/{len(req.keywords)})")
            try:
                pins = await pins_mod.fetch_pins(keyword, target=req.pins_per_keyword)
                for pin in pins:
                    pin.setdefault("seed_keyword", keyword)
                collected.extend(pins)
                names.append(keyword)
                db.cache_set(_pins_cache_key(keyword), pins)
                _scrape_state["pins"] = len(collected)
                db.log(run_id, f"  {len(pins)} pins for {keyword!r}")
            except Exception as exc:
                _scrape_state["failed"].append(keyword)
                db.log(run_id, f"  FAILED {keyword!r}: {exc}", level="error")
                log.error("Keyword %r failed: %s", keyword, exc)
            _scrape_state["done"] = index

        _scrape_state["results"] = collected
        # Only names are stored; the pins live in the cache and are never
        # thrown away, so nothing here can destroy a day's work.
        set_working_keywords(names)
        db.set_state("last_scrape", {
            "run_id": run_id,
            "niche": req.niche,
            "keywords": req.keywords,
            "failed": _scrape_state["failed"],
        })
        db.log(run_id, f"Run complete: {len(collected)} pins")
    except Exception as exc:
        _scrape_state["error"] = str(exc)
        log.exception("Scrape run failed")
    finally:
        _scrape_state["running"] = False
        _scrape_state["finished"] = True


@router.post("/scrape")
async def start_scrape(req: ScrapeRequest) -> dict[str, Any]:
    """Kick off a pin run in the background and return immediately."""
    global _scrape_task

    if _scrape_state["running"]:
        raise HTTPException(409, "A scrape is already running")
    if not req.keywords:
        raise HTTPException(400, "No keywords selected")

    run = db.latest_run()
    run_id = run["id"] if run else db.create_run("scrape")

    # A new set of pins invalidates the saved filter result; leaving it would
    # show yesterday's split next to today's pins.
    db.set_state("last_filter", None)
    _filter_state.clear()

    _scrape_state.update(
        running=True, keyword="", done=0, total=len(req.keywords),
        pins=0, failed=[], finished=False, error="", results=[],
        niche=req.niche or (req.keywords[0] if req.keywords else ""),
    )
    _scrape_task = asyncio.create_task(_run_scrape(req, run_id))
    return {"status": "started", "run_id": run_id, "total": len(req.keywords)}


@router.get("/scrape/existing")
def scrape_existing() -> dict[str, Any]:
    """How many pins are already collected, and from which keywords."""
    pins = _current_pins()
    return {
        "count": len(pins),
        "keywords": sorted({str(p.get("seed_keyword", "")) for p in pins if p.get("seed_keyword")}),
    }


@router.get("/scrape/status")
def scrape_status() -> dict[str, Any]:
    """Progress poll. Excludes the pin payload -- that would be huge."""
    return {k: v for k, v in _scrape_state.items() if k != "results"}


@router.get("/scrape/results")
def scrape_results() -> dict[str, Any]:
    """Return the current run's pins, or the last completed run after a refresh."""
    rows = _scrape_state.get("results", [])
    if rows:
        return {"pins": rows, "count": len(rows), "restored": False}

    saved = db.get_state("last_scrape") or {}
    rows = _current_pins()
    return {
        "pins": rows,
        "count": len(rows),
        "restored": bool(rows),
        "keywords": saved.get("keywords", []),
        "niche": saved.get("niche", ""),
    }


# ---------------------------------------------------------------- api keys

class KeyEntry(BaseModel):
    value: str
    label: str = ""


class KeySet(BaseModel):
    keys: list[KeyEntry]


@router.get("/keys")
def list_keys() -> dict[str, Any]:
    """Pool status. Keys come back MASKED -- the real values never leave."""
    return keyring.pool.status()


@router.put("/keys")
def set_keys(payload: KeySet) -> dict[str, Any]:
    """Replace the key list.

    Entries whose value is the masked placeholder are left as they were, so
    editing a label does not require re-typing the secret.
    """
    current = {k["masked"]: None for k in keyring.pool.status()["keys"]}
    existing = {keyring.mask(k.value): k.value for k in keyring.pool._keys}

    entries = []
    for item in payload.keys:
        value = item.value.strip()
        if not value:
            continue
        # The UI shows masked values; sending one back means "unchanged".
        if value in existing:
            value = existing[value]
        entries.append({"value": value, "label": item.label.strip()})

    keyring.pool.save(entries)
    log.info("API keys updated: %d in pool", len(entries))
    return keyring.pool.status()


@router.post("/keys/test")
def test_keys() -> dict[str, Any]:
    """Send one tiny request so a bad key is found now, not mid-run."""
    result = ollama_client.check()
    return {**result, "pool": keyring.pool.status()}


# ---------------------------------------------------------------- filtering

#: Last filter pass, so Export can write exactly what Review shows.
_filter_state: dict[str, Any] = {}


def _pins_cache_key(keyword: str) -> str:
    return f"pins:{keyword.strip().lower()}"


#: The working set is a LIST OF KEYWORD NAMES, not a copy of the pins.
#: Pins themselves live in the per-keyword cache and are never deleted, so
#: "start fresh" forgets a selection rather than destroying data. An earlier
#: design stored a copy of the pins here, and a single fresh run replaced 555
#: pins with 74 -- unrecoverable if the cache had not happened to hold them.
WORKING_KEY = "working_keywords"


def working_keywords() -> list[str]:
    return list(db.get_state(WORKING_KEY) or [])


def set_working_keywords(names: list[str]) -> None:
    seen, ordered = set(), []
    for n in names:
        k = (n or "").strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            ordered.append(k)
    db.set_state(WORKING_KEY, ordered)


def _current_pins() -> list[dict[str, Any]]:
    """Every pin in the working set, read from the per-keyword cache."""
    out: list[dict[str, Any]] = []
    for keyword in working_keywords():
        cached = db.cache_get(_pins_cache_key(keyword), -1)  # never expires here
        if cached:
            for pin in cached:
                pin.setdefault("seed_keyword", keyword)
            out.extend(cached)
    return out


class OverrideRequest(BaseModel):
    pin_id: str
    #: "accepted", "rejected", or empty to clear the override.
    status: str = ""


@router.post("/filter/override")
def override_pin(req: OverrideRequest) -> dict[str, Any]:
    """Manually accept or reject one pin, overruling the filters.

    Kept in the database rather than in memory so the decision survives
    re-running the filters, editing the rules, and restarting the app.
    """
    if req.status and req.status not in ("accepted", "rejected"):
        raise HTTPException(400, "status must be accepted, rejected or empty")

    overrides = filtering.set_override(req.pin_id, req.status or None)

    # Re-sort the cached result so the moved pin lands in the right column
    # without needing a full re-filter.
    if _filter_state:
        merged = (_filter_state.get("accepted", [])
                  + _filter_state.get("rejected", [])
                  + _filter_state.get("unreviewed", []))
        merged = filtering.apply_overrides(merged)
        _filter_state["accepted"] = filtering.sort_pins(
            [p for p in merged if p["filter_status"] == "accepted"])
        _filter_state["rejected"] = filtering.sort_pins(
            [p for p in merged if p["filter_status"] == "rejected"])
        _filter_state["unreviewed"] = filtering.sort_pins(
            [p for p in merged if p["filter_status"] == "unreviewed"])

    return {
        "overrides": len(overrides),
        "accepted": len(_filter_state.get("accepted", [])),
        "rejected": len(_filter_state.get("rejected", [])),
    }


class FilterRequest(BaseModel):
    niche: str = ""
    use_ai: bool = True


@router.get("/filter/last")
def last_filter() -> dict[str, Any]:
    """The previous result, without recomputing anything.

    What the page asks for on load. Re-running the full pass on every refresh
    meant a 96-second wait to look at work that was already finished.
    """
    if _filter_state.get("accepted") is not None:
        return {
            "summary": _filter_state.get("summary", {}),
            "accepted": _filter_state.get("accepted", []),
            "rejected": _filter_state.get("rejected", []),
            "unreviewed": _filter_state.get("unreviewed", []),
            "cached": True,
        }

    saved = db.get_state("last_filter") or {}
    if saved:
        _filter_state.update(saved)
        return {**saved, "cached": True}

    # Nothing saved -- usually because a scrape just invalidated it. Rather
    # than showing an empty screen until a 90-second AI pass finishes, run the
    # instant rules now and say the AI part is still outstanding. Seeing your
    # pins straight away matters more than seeing the final verdict.
    pins = _current_pins()
    if not pins:
        return {"summary": {}, "accepted": [], "rejected": [], "unreviewed": [],
                "cached": False, "empty": True}

    niche = (db.get_state("last_scrape") or {}).get("niche", "")
    result = filtering.filter_pins(pins, niche, use_ai=False)
    for key in ("accepted", "rejected", "unreviewed"):
        result[key] = filtering.sort_pins(result[key])

    summary = dict(result["summary"])
    summary["ai_pending"] = filtering.ollama_client.is_configured()
    return {
        "summary": summary,
        "accepted": result["accepted"],
        "rejected": result["rejected"],
        "unreviewed": result["unreviewed"],
        "cached": False,
        "quick": True,
    }


@router.post("/filter")
def run_filters(req: FilterRequest) -> dict[str, Any]:
    """Re-run Phase-2 rules over the pins already collected.

    Deliberately reads stored pins: changing a rule and re-filtering must never
    trigger scraping. Sorting is applied here too, so Review and the CSV agree.
    """
    pins = _current_pins()
    if not pins:
        raise HTTPException(400, "No pins to filter - run a scrape first")

    niche = req.niche or (db.get_state("last_scrape") or {}).get("niche", "")
    # Pass the user's intent through; filtering decides whether the model is
    # actually reachable, so it can report "asked for AI but skipped it".
    result = filtering.filter_pins(pins, niche, use_ai=req.use_ai)
    for key in ("accepted", "rejected", "unreviewed"):
        result[key] = filtering.sort_pins(result[key])

    _filter_state.clear()
    _filter_state.update(result)

    # Persist so a refresh -- or a restart -- shows the result immediately.
    db.set_state("last_filter", {
        "summary": result["summary"],
        "accepted": result["accepted"],
        "rejected": result["rejected"],
        "unreviewed": result["unreviewed"],
    })

    summary = dict(result["summary"])
    summary["ai_configured"] = filtering.ollama_client.is_configured()
    if req.use_ai and not summary["ai_configured"]:
        summary["note"] = ("Off-niche filtering needs OLLAMA_API_KEY. "
                           "Deterministic rules ran; the rest are unreviewed.")
    log.info("Filter: %s", summary)
    return {"summary": summary,
            "accepted": result["accepted"],
            "rejected": result["rejected"],
            "unreviewed": result["unreviewed"]}


# ---------------------------------------------------------------- export

class ExportRequest(BaseModel):
    label: str = "keywords"


@router.post("/export")
def do_export(req: ExportRequest) -> dict[str, Any]:
    """Write the collected pins to CSV in the template format."""
    # Prefer the filtered set so the CSV matches what Review shows. Falls back
    # to the raw scrape when filters have not been run.
    accepted = _filter_state.get("accepted")
    rejected = _filter_state.get("rejected") or []
    rows = accepted if accepted else _current_pins()
    if not rows:
        raise HTTPException(400, "Nothing to export - run a scrape first")

    run = db.latest_run()
    paths = export_mod.export_run(
        rows, rejected,
        run_id=run["id"] if run else "manual", label=req.label or "keywords"
    )
    # Hand back file NAMES too: a path is useless inside a browser, and the
    # user should not have to go hunting in a file manager.
    return {
        "files": paths,
        "names": {k: Path(v).name for k, v in paths.items()},
        "rows": len(rows),
        "rejected": len(rejected),
        "filtered": bool(accepted),
    }


@router.get("/exports/{name}")
def download_export(name: str) -> FileResponse:
    """Serve a generated CSV so it can be downloaded from the browser."""
    # Filename only -- never let a request walk out of the exports directory.
    safe = Path(name).name
    target = EXPORTS_DIR / safe
    if safe != name or not target.is_file():
        raise HTTPException(404, "No such export")
    return FileResponse(target, media_type="text/csv", filename=safe)
