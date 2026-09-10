"""Selector resolution, preflight health check, and DOM capture (Gap 2).

The premise: PinClicks will change its markup eventually, and the failure mode
we must avoid is not a crash -- it is a *silent* one, where the scraper happily
writes 2,000 rows of "N/A" and you only notice days later.

Three defences live here:

* ``resolve``    -- primary then fallbacks, so cosmetic markup changes are absorbed.
* ``preflight``  -- refuses to start a run when a *required* selector is missing.
* ``capture``    -- dumps the real DOM so broken selectors can actually be fixed.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from ..logging_setup import get_logger
from ..models import NA, SelectorSpec
from ..paths import DEBUG_DIR, SELECTORS_FILE, load_selectors, save_json

log = get_logger("selectors")

#: Config sections that hold SelectorSpec entries (everything else is metadata).
SPEC_SECTIONS = ("auth", "level1", "level2", "pins")


class SelectorError(RuntimeError):
    """A required selector could not be resolved."""


class SelectorRegistry:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config if config is not None else load_selectors()

    # ------------------------------------------------------------ lookup

    def spec(self, key: str) -> SelectorSpec:
        """``key`` is dotted, e.g. ``pins.title``."""
        section, _, name = key.partition(".")
        try:
            raw = self.config[section][name]
        except KeyError as exc:
            raise SelectorError(f"Unknown selector key: {key}") from exc
        return SelectorSpec.from_config(key, raw)

    def all_specs(self) -> list[SelectorSpec]:
        specs = []
        for section in SPEC_SECTIONS:
            for name, raw in self.config.get(section, {}).items():
                specs.append(SelectorSpec.from_config(f"{section}.{name}", raw))
        return specs

    def url(self, name: str) -> str:
        return self.config.get("urls", {}).get(name, "/")

    @property
    def calibrated(self) -> bool:
        return bool(self.config.get("_calibrated"))

    # ------------------------------------------------------------ resolving

    async def find(self, scope: Page | Any, key: str):
        """Return the first matching element handle, or None.

        Tries primary, then each fallback in order.
        """
        spec = self.spec(key)
        for candidate in spec.candidates:
            try:
                el = await scope.query_selector(candidate)
            except Exception:
                continue  # malformed selector in config -- try the next one
            if el is not None:
                return el
        return None

    async def find_all(self, scope: Page | Any, key: str) -> list[Any]:
        spec = self.spec(key)
        for candidate in spec.candidates:
            try:
                els = await scope.query_selector_all(candidate)
            except Exception:
                continue
            if els:
                return els
        return []

    async def text(self, scope: Page | Any, key: str, default: str = NA) -> str:
        """Extract text (or the configured attribute) for ``key``.

        Missing optional fields become "N/A" and the scrape continues -- a pin
        without a saves-count is still a usable pin. Missing *required* fields
        raise, because that pin is worthless and it signals a broken selector.
        """
        spec = self.spec(key)
        el = await self.find(scope, key)
        if el is None:
            if spec.required:
                raise SelectorError(f"Required selector not found: {key}")
            return default

        if spec.attr:
            value = await el.get_attribute(spec.attr)
        else:
            value = await el.inner_text()

        value = (value or "").strip()
        return value if value else (default if not spec.required else value)

    async def texts(self, scope: Page | Any, key: str) -> list[str]:
        """All matches for a ``multiple: true`` selector (e.g. annotations)."""
        spec = self.spec(key)
        out = []
        for el in await self.find_all(scope, key):
            value = await el.get_attribute(spec.attr) if spec.attr else await el.inner_text()
            value = (value or "").strip()
            if value:
                out.append(value)
        return out

    # ------------------------------------------------------------ preflight

    async def preflight(self, page: Page, sections: tuple[str, ...] = SPEC_SECTIONS) -> dict[str, Any]:
        """Check that every *required* selector in ``sections`` resolves.

        Returns ``{"ok": bool, "broken": [...], "checked": int}``. The caller is
        expected to refuse to start a run when ``ok`` is False -- producing
        nothing is strictly better than producing a file of N/A.
        """
        broken: list[str] = []
        checked = 0
        for spec in self.all_specs():
            if spec.key.split(".")[0] not in sections or not spec.required:
                continue
            checked += 1
            if await self.find(page, spec.key) is None:
                broken.append(spec.key)

        ok = not broken
        if ok:
            log.info("Preflight passed (%d required selectors)", checked)
        else:
            log.error("Preflight FAILED. Broken: %s", ", ".join(broken))
        return {"ok": ok, "broken": broken, "checked": checked}

    # ------------------------------------------------------------ calibration

    def mark_calibrated(self) -> None:
        config = load_selectors()
        config["_calibrated"] = True
        config["_calibrated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_json(SELECTORS_FILE, config)
        self.config = config


async def capture(page: Page, label: str, run_id: int | str = "manual") -> dict[str, str]:
    """Dump the live DOM + a screenshot to ``debug/``.

    This is the tool that makes a broken selector a five-minute fix instead of a
    guessing game: you get the exact HTML the scraper saw.
    """
    out_dir: Path = DEBUG_DIR / str(run_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%H%M%S")

    html_path = out_dir / f"{label}-{stamp}.html"
    png_path = out_dir / f"{label}-{stamp}.png"

    html_path.write_text(await page.content(), encoding="utf-8")
    try:
        await page.screenshot(path=str(png_path), full_page=True)
    except Exception as exc:
        log.warning("Screenshot failed for %s: %s", label, exc)
        png_path = None  # type: ignore[assignment]

    log.info("Captured %s -> %s", label, html_path)
    return {
        "html": str(html_path),
        "screenshot": str(png_path) if png_path else "",
        "url": page.url,
        "title": await page.title(),
    }
