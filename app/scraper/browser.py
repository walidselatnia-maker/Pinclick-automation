"""Browser lifecycle: persistent context, pacing, and auto-healing.

Three things make this file worth reading carefully:

1. **We drive the real installed Chrome, visibly. Not headless, not bundled
   Chromium.** app.pinclicks.com is behind Cloudflare, and measurement showed:

       bundled Chromium, headless -> 403
       real Chrome,      headless -> 403
       real Chrome,      headed   -> 200

   Headless is the trigger. So ``channel="chrome"`` and ``headless=False`` are
   effectively mandatory -- flipping either one makes every run fail with 403.
   Re-verify with ``tools/probe_browser.py`` if PinClicks ever changes this.

2. **Exactly one persistent context may exist at a time.** Chrome locks
   ``user_data/``; a second ``launch_persistent_context`` against the same
   directory fails. Everything therefore goes through the module-level
   ``manager`` singleton, guarded by an asyncio lock.

3. **Healing is a full teardown.** When the watchdog decides the browser is
   wedged, half-measures (reload, new page) do not reliably clear it. We close
   the context outright and relaunch. The session survives because it lives in
   ``user_data/`` on disk, not in memory.
"""

from __future__ import annotations

import asyncio
import random
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from ..logging_setup import get_logger
from ..paths import USER_DATA_DIR, ensure_dirs, load_settings
from . import window as win

log = get_logger("browser")


class BrowserManager:
    """Owns the single Playwright persistent context."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._context: BrowserContext | None = None
        self._lock = asyncio.Lock()
        #: Serialises ALL PinClicks traffic. Two coroutines driving the same
        #: tab produce "Navigation interrupted by another navigation"; two
        #: driving different tabs produce concurrent requests from one IP,
        #: which is what got that IP blocked by Cloudflare.
        self._op_lock = asyncio.Lock()
        self._busy = False

    # ------------------------------------------------------------ lifecycle

    @property
    def is_running(self) -> bool:
        return self._context is not None

    def _make_close_handler(self, ctx: BrowserContext):
        """Build a close handler bound to one specific context.

        It must compare identity before clearing. During heal() the old
        context's close event can arrive *after* the replacement has been
        launched; an unconditional handler then nulls out the live context and
        the next call dies with "Target page, context or browser has been
        closed". That is exactly what broke a keyword mid-run.
        """

        def handler(*_: Any) -> None:
            if self._context is ctx:
                log.info("Browser context closed; clearing cached handle")
                self._context = None
                win.window.forget()
            else:
                log.debug("Ignoring close event from a superseded context")

        return handler

    async def start(self) -> BrowserContext:
        """Launch the context, or return the existing live one."""
        async with self._lock:
            if self._context is not None:
                # The close event is not always delivered (killed process,
                # crashed renderer), so confirm liveness rather than trust it.
                try:
                    _ = self._context.pages
                    return self._context
                except Exception:
                    log.warning("Cached context is dead; relaunching")
                    self._context = None
            return await self._launch_locked(load_settings())

    @asynccontextmanager
    async def operation(self) -> AsyncIterator[Page]:
        """Exclusive use of the working tab.

        Every navigation goes through here so that a background session probe
        cannot yank the page out from under an in-flight login.
        """
        async with self._op_lock:
            self._busy = True
            try:
                yield await self.first_page()
            finally:
                self._busy = False

    @asynccontextmanager
    async def exclusive(self, timeout_s: float | None = None) -> AsyncIterator[None]:
        """Hold the browser without taking the working tab.

        For work that needs its own tab but must still not run alongside a
        scrape. Opening a second tab against PinClicks while a run was in
        flight meant two concurrent streams of traffic from one IP, which is
        the pattern that got the IP blocked -- and it also let heal() tear down
        a page another operation was using.

        Raises TimeoutError rather than queueing forever, so a UI action can
        say "busy, try again" instead of hanging.
        """
        if timeout_s is None:
            await self._op_lock.acquire()
        else:
            try:
                await asyncio.wait_for(self._op_lock.acquire(), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                raise TimeoutError("browser is busy with another run") from exc
        self._busy = True
        try:
            yield
        finally:
            self._busy = False
            self._op_lock.release()

    @property
    def is_busy(self) -> bool:
        return self._busy

    async def _launch_locked(self, settings: dict[str, Any]) -> BrowserContext:
        ensure_dirs()
        if self._pw is None:
            self._pw = await async_playwright().start()

        browser_cfg = settings.get("browser", {})
        channel = browser_cfg.get("channel", "chrome")
        headless = bool(browser_cfg.get("headless", False))
        if headless:
            # Loud, because the symptom (403 on every page) looks like a ban.
            log.warning(
                "headless=true is set. app.pinclicks.com returns 403 to headless "
                "Chrome -- expect every request to fail."
            )

        # MEASURED, do not remove again:
        #   with this flag     navigator.webdriver = False
        #   without it         navigator.webdriver = True
        # It was removed once because Chrome shows an "unsupported
        # command-line flag" warning bar, on the mistaken assumption that the
        # bar was the only effect. Every request then declared itself
        # automated, and Cloudflare blocked the client within minutes -- a
        # block that outlasted 24 hours because the signal was sent on every
        # request, not because the IP was banned.
        #
        # The warning bar is cosmetic. This flag is not.
        args: list[str] = ["--disable-blink-features=AutomationControlled"]
        if browser_cfg.get("maximized", True):
            # Required, not cosmetic: the pin table is max-h-[calc(100vh-16rem)],
            # so a small window leaves ~286px of scroll area and the "load more
            # pins" trigger never fires. Measured -- see settings.json.
            args.append("--start-maximized")
        position = browser_cfg.get("window_position")
        if position:
            args.append(f"--window-position={position}")

        log.info("Launching %s (headless=%s)", channel, headless)
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel=channel,
            headless=headless,
            no_viewport=True,
            # Without this Playwright launches with --no-sandbox, which Chrome
            # flags in a warning bar and which makes the browser look
            # abnormal to the sites we visit. Keep Chrome's real sandbox.
            chromium_sandbox=True,
            accept_downloads=True,   # the pin pipeline runs on PinClicks' Export
            args=args,
        )

        self._context.on("close", self._make_close_handler(self._context))

        if browser_cfg.get("start_minimized", True):
            # Straight down to the taskbar, before anything navigates, so it
            # never pops up over your work.
            page = self._context.pages[0] if self._context.pages \
                else await self._context.new_page()
            if await win.window.find(page):
                win.window.minimise()

        timeouts = settings.get("timeouts", {})
        self._context.set_default_timeout(timeouts.get("element_ms", 5000))
        self._context.set_default_navigation_timeout(timeouts.get("navigation_ms", 45000))
        return self._context

    async def _close_locked(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:  # already dead is fine -- we wanted it gone
                log.warning("Context close failed (ignoring): %s", exc)
            self._context = None
            win.window.forget()

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def shutdown(self) -> None:
        await self.close()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def heal(self) -> BrowserContext:
        """Hard-restart the browser after a hang (Gap 5).

        Cookies survive because they are on disk in ``user_data/``, so the
        caller can resume from its checkpoint without re-authenticating.
        """
        log.warning("Healing browser context (hard restart)")
        await self.close()
        return await self.start()

    # ------------------------------------------------------------ pages

    async def new_page(self) -> Page:
        ctx = await self.start()
        return await ctx.new_page()

    async def first_page(self) -> Page:
        """Reuse the context's working tab instead of piling up tabs.

        Skips pages the user has closed -- a closed handle still appears in
        ``ctx.pages`` briefly, and navigating it raises TargetClosedError.
        """
        ctx = await self.start()
        for page in ctx.pages:
            if not page.is_closed():
                return page
        return await ctx.new_page()


#: Module-level singleton. Import this, never construct BrowserManager yourself.
manager = BrowserManager()


async def pace() -> None:
    """Randomised delay between actions (Gap 4).

    Jittered rather than fixed: a perfectly regular request cadence is itself a
    bot signal. Sequential + jittered is the whole anti-block strategy -- there
    is deliberately no parallelism to tune.
    """
    pacing = load_settings().get("pacing", {})
    lo = pacing.get("action_delay_min_ms", 1500) / 1000
    hi = pacing.get("action_delay_max_ms", 4000) / 1000
    await asyncio.sleep(random.uniform(lo, hi))


async def pace_between_keywords() -> None:
    """Longer, jittered gap between whole keywords in a bulk run.

    A bulk run is the heaviest thing this tool does -- each keyword is a page
    load plus several lazy-load fetches that PinClicks forwards to Pinterest.
    Back to back with no gap, twenty keywords is a burst that looks nothing
    like a person, which is how the IP ended up blocked.
    """
    pacing = load_settings().get("pacing", {})
    lo = pacing.get("keyword_gap_min_ms", 4000) / 1000
    hi = pacing.get("keyword_gap_max_ms", 9000) / 1000
    delay = random.uniform(lo, hi)
    log.info("Pausing %.1fs before the next keyword", delay)
    await asyncio.sleep(delay)
