"""Timeout, retry and hang handling (PRD Gap 5).

The failure mode on PinClicks is rarely an exception -- it is a page that just
sits there. A bare try/except cannot catch that, because nothing is raised.
So every risky step runs under an explicit deadline:

    result = await guarded(page, "load L3 keywords", fetch, step_ms=45000)

If the deadline passes, the step is cancelled, the browser is healed, and the
step is retried with backoff. After the final attempt the caller decides
whether to skip the item (circuit breaker) or fail the run.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

from ..db import heartbeat
from ..logging_setup import get_logger
from ..paths import load_settings
from .browser import manager

log = get_logger("resilience")

T = TypeVar("T")


class StepTimeout(RuntimeError):
    """A step exceeded its deadline -- almost always a hang, not an error."""


class StepFailed(RuntimeError):
    """A step exhausted all retries."""


async def guarded(
    label: str,
    fn: Callable[[], Awaitable[T]],
    *,
    step_ms: int | None = None,
    attempts: int | None = None,
    job_id: int | None = None,
    heal_on_retry: bool = True,
) -> T:
    """Run ``fn`` under a deadline, retrying with backoff.

    ``fn`` must be a zero-arg coroutine factory-ish callable so each attempt
    gets a fresh awaitable -- an already-created coroutine cannot be retried.
    """
    settings = load_settings()
    timeouts = settings.get("timeouts", {})
    retry = settings.get("retry", {})

    deadline = (step_ms or timeouts.get("step_ms", 45000)) / 1000
    max_attempts = attempts or retry.get("max_attempts", 3)
    backoff = retry.get("backoff_seconds", [3, 6, 12])

    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        if job_id is not None:
            heartbeat(job_id)
        try:
            return await asyncio.wait_for(fn(), timeout=deadline)
        except asyncio.TimeoutError as exc:
            last_error = StepTimeout(f"{label} exceeded {deadline:.0f}s")
            log.warning("HANG: %s (attempt %d/%d)", last_error, attempt, max_attempts)
        except Exception as exc:  # noqa: BLE001 - retry any failure uniformly
            last_error = exc
            log.warning("%s failed (attempt %d/%d): %s", label, attempt, max_attempts, exc)

        if attempt >= max_attempts:
            break

        if heal_on_retry:
            # A hung page does not recover by being asked nicely. Rebuild the
            # context; the session survives on disk in user_data/.
            try:
                await manager.heal()
            except Exception as exc:
                log.error("Heal failed: %s", exc)

        delay = backoff[min(attempt - 1, len(backoff) - 1)]
        log.info("Retrying %s in %ds", label, delay)
        await asyncio.sleep(delay)

    raise StepFailed(f"{label} failed after {max_attempts} attempts: {last_error}")


async def wait_for_condition(
    check: Callable[[], Awaitable[bool]],
    *,
    timeout_s: float,
    poll_s: float = 1.0,
    job_id: int | None = None,
    on_poll: Callable[[float], None] | None = None,
) -> bool:
    """Poll ``check`` until true or the timeout expires.

    Returns True if the condition was met. Used for things that have their own
    progress signal -- notably the "Getting more pins from Pinterest..." banner,
    where row counts are useless for telling "working" from "finished".
    """
    loop = asyncio.get_event_loop()
    start = loop.time()
    while loop.time() - start < timeout_s:
        if await check():
            return True
        if job_id is not None:
            heartbeat(job_id)
        if on_poll is not None:
            on_poll(loop.time() - start)
        await asyncio.sleep(poll_s)
    return False
