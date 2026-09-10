"""Shared enums and dataclasses.

Kept deliberately dependency-free so both the web layer and the scraper can
import it without pulling in FastAPI or Playwright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class JobType(str, Enum):
    LEVEL1 = "level1"          # niche discovery
    LEVEL2 = "level2"          # keyword drill-down
    PINS = "pins"              # top-pins extraction


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    RECOVERING = "recovering"      # watchdog fired, browser being rebuilt
    AWAITING_AUTH = "awaiting_auth"  # session expired mid-run, waiting on user
    PAUSED = "paused"
    FAILED = "failed"
    DONE = "done"


#: States a job can be resumed from when the app restarts.
RESUMABLE_STATES = {
    JobState.QUEUED,
    JobState.RUNNING,
    JobState.RETRYING,
    JobState.RECOVERING,
    JobState.AWAITING_AUTH,
    JobState.PAUSED,
}


class ScrapeStatus(str, Enum):
    """Per-keyword progress through the pin extraction phase."""

    PENDING = "pending"
    RUNNING = "running"
    SUSPECT = "suspect"   # completed, but quality gate tripped (Gap 2)
    FAILED = "failed"     # circuit breaker tripped, skipped
    DONE = "done"


class FilterStatus(str, Enum):
    """Phase-2 classification (Gap 3)."""

    UNFILTERED = "unfiltered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SessionState(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


#: Placeholder written whenever an optional field cannot be extracted.
NA = "N/A"


@dataclass
class Pin:
    """One scraped pin. Field names match the CSV export contract."""

    seed_keyword: str
    spy_title: str = NA
    spy_description: str = NA
    annotation: str = NA          # comma-separated Pinterest annotations
    pin_url: str = NA
    image_url: str = NA
    pin_score: str = NA
    saves_count: str = NA

    def missing_required(self) -> bool:
        """True if this pin lacks the fields that make it usable at all.

        Feeds the batch quality gate: a run where >30% of pins are missing
        these is almost certainly a broken selector, not a bad keyword.
        """
        return self.spy_title in (NA, "") or self.pin_url in (NA, "")


@dataclass
class Niche:
    """One Level-1 result."""

    name: str
    volume: int
    category: str = ""


@dataclass
class Keyword:
    """One Level-2 result."""

    keyword: str
    volume: int
    niche: str = ""


@dataclass
class SelectorSpec:
    """A single entry from config/selectors.json."""

    key: str
    primary: str
    fallbacks: list[str] = field(default_factory=list)
    required: bool = False
    attr: str | None = None
    multiple: bool = False

    @property
    def candidates(self) -> list[str]:
        """Every selector to try, in order."""
        return [self.primary, *self.fallbacks]

    @classmethod
    def from_config(cls, key: str, raw: dict[str, Any]) -> SelectorSpec:
        return cls(
            key=key,
            primary=raw["primary"],
            fallbacks=list(raw.get("fallbacks", [])),
            required=bool(raw.get("required", False)),
            attr=raw.get("attr"),
            multiple=bool(raw.get("multiple", False)),
        )
