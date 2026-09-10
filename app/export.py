"""CSV export in the user's template format.

Column contract, from keywords-template.csv with the raw pin fields inserted
where the user asked (after ``status``, before the domain columns):

    seed_keyword, spy_title, spy_description, annotation, status,
    pin_url, image_url, pin_score, saves_count,
    <domain 1>, <domain 2>, ... <domain N>

One row per pin. ``seed_keyword`` repeats across that keyword's pins.
"""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any, Iterable

from .logging_setup import get_logger
from .models import NA
from .paths import EXPORTS_DIR, load_settings

log = get_logger("export")

#: Fixed columns, in order, before the per-site columns.
BASE_COLUMNS = [
    "seed_keyword",
    "spy_title",
    "spy_description",
    "annotation",
    "status",
    "pin_url",
    "image_url",
    "pin_score",
    "saves_count",
]

#: Extra columns written only to the rejects file.
REJECT_COLUMNS = ["reject_reason"]


def build_header(sites: Iterable[str]) -> list[str]:
    return [*BASE_COLUMNS, *sites]


def _row(pin: dict[str, Any], sites: list[str], settings: dict[str, Any]) -> dict[str, str]:
    site_default = settings.get("site_default_value", "go")
    status_default = settings.get("status_default_value", "go")

    row = {
        "seed_keyword": pin.get("seed_keyword", ""),
        "spy_title": pin.get("spy_title", NA),
        "spy_description": pin.get("spy_description", NA),
        "annotation": pin.get("annotation", NA),
        "status": pin.get("status") or status_default,
        "pin_url": pin.get("pin_url", NA),
        "image_url": pin.get("image_url", NA),
        "pin_score": str(pin.get("pin_score", NA)),
        "saves_count": str(pin.get("saves_count", NA)),
    }
    for site in sites:
        row[site] = site_default
    return row


def write_csv(
    pins: list[dict[str, Any]],
    path: Path,
    *,
    sites: list[str] | None = None,
    include_reject_reason: bool = False,
) -> Path:
    """Write pins to ``path`` using the template contract.

    UTF-8 **with BOM** and CRLF line endings: pin titles and descriptions are
    full of emoji and accented characters, and without the BOM Excel mangles
    them on open.
    """
    settings = load_settings()
    sites = sites if sites is not None else list(settings.get("sites", []))

    header = build_header(sites)
    if include_reject_reason:
        header = [*header, *REJECT_COLUMNS]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, lineterminator="\r\n",
                                extrasaction="ignore")
        writer.writeheader()
        for pin in pins:
            row = _row(pin, sites, settings)
            if include_reject_reason:
                row["reject_reason"] = pin.get("reject_reason", "")
            writer.writerow(row)

    log.info("Wrote %d rows -> %s", len(pins), path)
    return path


def export_run(
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]] | None = None,
    *,
    run_id: int | str = "manual",
    label: str = "keywords",
) -> dict[str, str]:
    """Write the main CSV plus, if any, a separate rejects file."""
    stamp = time.strftime("%Y-%m-%d")
    out: dict[str, str] = {}

    main = EXPORTS_DIR / f"{label}-{stamp}-run{run_id}.csv"
    out["accepted"] = str(write_csv(accepted, main))

    if rejected:
        rej = EXPORTS_DIR / f"rejected-{stamp}-run{run_id}.csv"
        out["rejected"] = str(write_csv(rej_pins := rejected, rej,
                                        include_reject_reason=True))
        log.info("%d rejected pins -> %s", len(rej_pins), rej)

    return out
