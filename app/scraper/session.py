"""PinClicks authentication and session lifecycle (Gap 4).

Deliberate constraint: **this app never sees your credentials.** There is no
password field, no keyring, no credential row in the database. You log in
yourself in a visible Chromium window, and the only thing that persists is the
browser profile on disk in ``user_data/``.

Consequences worth knowing:

* Whatever login method PinClicks offers (password, Google SSO, magic link)
  works unchanged, because we are not automating the login -- you are doing it.
* Session expiry mid-run is recoverable: the job parks in AWAITING_AUTH and
  resumes from its checkpoint after you re-authenticate. It is never fatal.
"""

from __future__ import annotations

from typing import Any

from playwright.async_api import Page

from ..logging_setup import get_logger
from ..models import SessionState
from ..paths import load_settings
from .browser import manager
from .window import window as chrome_window
from .selectors import SelectorRegistry, capture

log = get_logger("session")

#: URL fragments that mean "you are not logged in", used as the calibration-free
#: fallback probe. A redirect to a login page is a reliable signal even before
#: selectors have been calibrated.
#:
#: ``wp-login.php`` is in here because the first version of this probe reported
#: a WordPress login screen as ACTIVE -- a false positive is the worst outcome
#: for this check, since it lets a doomed run start.
LOGIN_URL_HINTS = ("/login", "/signin", "/sign-in", "/auth", "wp-login.php")

#: Page titles that mean Cloudflare rejected us rather than PinClicks answering.
#: Almost always means headless got switched on -- see browser.py.
BLOCK_TITLE_HINTS = ("attention required", "just a moment", "access denied")


def _base_url() -> str:
    return load_settings().get("base_url", "https://pinclicks.com").rstrip("/")


async def open_login_window() -> dict[str, Any]:
    """Open a visible browser on the PinClicks login page.

    Returns as soon as the page is open -- it does not wait for the user to
    finish logging in. The UI polls ``probe`` afterwards to detect success.
    """
    url = _base_url() + SelectorRegistry().url("login")
    log.info("Opening login window at %s", url)
    async with manager.operation() as page:
        # Signing in is the one moment you have to see the browser. Every
        # other moment it stays minimised -- see scraper/window.py.
        await chrome_window.find(page)
        chrome_window.restore()
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            # Most often the user closed the window mid-navigation. One retry
            # on a freshly launched context is enough to recover.
            log.warning("Login navigation failed (%s); retrying once", exc)
            await manager.heal()
            retry = await manager.first_page()
            await retry.goto(url, wait_until="domcontentloaded")
    return {"status": "opened", "url": url}


async def observe() -> dict[str, Any]:
    """Read the current page WITHOUT navigating it.

    This exists because ``probe`` navigates, and navigating while the user is
    typing their password wipes the login form. Anything that runs on a timer
    during login must use this instead.

    Detection is passive: PinClicks itself redirects away from /login once
    credentials are accepted, so "URL is no longer a login URL" is the signal.
    """
    if not manager.is_running:
        return {"status": SessionState.UNKNOWN.value, "reason": "browser not open"}

    try:
        page = await manager.first_page()
        landed = page.url or ""
        title = (await page.title() or "").lower()
    except Exception as exc:
        return {"status": SessionState.UNKNOWN.value, "reason": f"page unavailable: {exc}"}

    if any(hint in title for hint in BLOCK_TITLE_HINTS):
        return {
            "status": SessionState.UNKNOWN.value,
            "reason": "Blocked by Cloudflare — check browser.headless is false "
                      "in config/settings.json",
            "url": landed,
            "blocked": True,
        }

    on_login = any(hint in landed.lower() for hint in LOGIN_URL_HINTS)
    if not landed or landed.startswith("about:"):
        status, reason = SessionState.UNKNOWN, "no page loaded yet"
    elif on_login:
        status, reason = SessionState.EXPIRED, "still on the login page"
    else:
        status, reason = SessionState.ACTIVE, "navigated away from login"

    if status is SessionState.ACTIVE:
        # Login is done, so put it back in the taskbar without being asked.
        chrome_window.minimise()

    return {
        "status": status.value,
        "reason": reason,
        "url": landed,
        "blocked": False,
    }


async def probe(page: Page | None = None) -> dict[str, Any]:
    """Determine whether the stored session is still valid.

    Runs at app start, before each keyword batch, and after any unexpected
    redirect. Two strategies, in order of reliability:

    1. Calibrated selectors: look for the logged-in marker.
    2. Uncalibrated fallback: did navigating to the dashboard bounce us to a
       login URL? Crude, but correct often enough to drive the badge before
       calibration has happened.

    Pass ``page`` only if you already hold the browser operation lock;
    otherwise this acquires it, so a probe can never interrupt a login.
    """
    if page is not None:
        return await _probe_on_page(page)
    async with manager.operation() as owned:
        return await _probe_on_page(owned)


async def _probe_on_page(page: Page) -> dict[str, Any]:
    registry = SelectorRegistry()
    dashboard = _base_url() + registry.url("dashboard")
    try:
        await page.goto(dashboard, wait_until="domcontentloaded")
    except Exception as exc:
        log.warning("Session probe navigation failed: %s", exc)
        return {"status": SessionState.UNKNOWN.value, "reason": str(exc)}

    landed = page.url or ""
    bounced_to_login = any(hint in landed.lower() for hint in LOGIN_URL_HINTS)

    # Distinguish "Cloudflare blocked us" from "your session expired". Without
    # this they look identical in the UI, and the fixes are completely
    # different -- one is a config problem, the other needs you to log in.
    title = (await page.title() or "").lower()
    if any(hint in title for hint in BLOCK_TITLE_HINTS):
        log.error("Cloudflare block page returned (title=%r)", title)
        return {
            "status": SessionState.UNKNOWN.value,
            "reason": "Blocked by Cloudflare — check that browser.headless is "
                      "false and browser.channel is 'chrome' in config/settings.json",
            "url": landed,
            "calibrated": registry.calibrated,
            "blocked": True,
        }

    if registry.calibrated:
        marker = await registry.find(page, "auth.logged_in_marker")
        if marker is not None:
            status, reason = SessionState.ACTIVE, "logged-in marker present"
        elif bounced_to_login:
            status, reason = SessionState.EXPIRED, "redirected to login"
        else:
            status, reason = SessionState.EXPIRED, "logged-in marker missing"
    else:
        if bounced_to_login:
            status, reason = SessionState.EXPIRED, "redirected to login"
        else:
            status = SessionState.ACTIVE
            reason = "no login redirect (selectors not calibrated — best effort)"

    log.info("Session probe: %s (%s)", status.value, reason)
    return {
        "status": status.value,
        "reason": reason,
        "url": landed,
        "calibrated": registry.calibrated,
        "blocked": False,
    }


async def require_active() -> None:
    """Raise if the session is not usable. Call before starting any scrape."""
    result = await probe()
    if result["status"] != SessionState.ACTIVE.value:
        raise PermissionError(f"PinClicks session is not active: {result['reason']}")


async def calibrate(run_id: int | str = "manual") -> dict[str, Any]:
    """Capture the live DOM of the key pages so selectors can be written.

    This does not guess selectors for you -- it dumps the real HTML of each
    page to ``debug/``, which is what you need in order to fill in
    ``config/selectors.json`` correctly.
    """
    registry = SelectorRegistry()
    base = _base_url()
    captures = []

    async with manager.operation() as page:
        for label, url_key in (("keyword-explorer", "keywords"), ("top-pins", "top_pins")):
            target = base + registry.url(url_key)
            try:
                await page.goto(target, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)  # let Livewire finish rendering
                captures.append({"label": label, **await capture(page, label, run_id)})
            except Exception as exc:
                log.warning("Calibration capture failed for %s: %s", label, exc)
                captures.append({"label": label, "error": str(exc)})

        preflight = await registry.preflight(page, sections=("auth",))
    return {"captures": captures, "auth_preflight": preflight}
