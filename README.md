# PinClicks Niche & Content Mining Dashboard

Local desktop tool that automates Pinterest keyword research via PinClicks:
niche discovery → keyword drill-down → top-pins extraction → rule-based
filtering → CSV export in your existing template format.

## Run it

```
run.bat
```

First run creates the venv, installs dependencies, downloads Chromium, and
opens http://127.0.0.1:8756. Later runs just start the server.

## Build status

| Milestone | State |
|---|---|
| M1 Foundation — server, DB, config, 5-screen UI | Done |
| M2 Session — real-Chrome driver, login, probe, preflight, DOM capture | Code done; selector calibration pending login |
| M3 Discovery — Level-1/Level-2 scrapers + watchdog | Not started |
| M4 Pins — extraction engine, checkpoints, auto-heal | Not started |
| M5 Filter & Export — rules engine, CSV writer | Not started |

## Things learned the hard way

**PinClicks lives at `app.pinclicks.com`.** The apex `pinclicks.com` and
`www.pinclicks.com` are the WordPress marketing site — `www./dashboard`
redirects to `wp-login.php`, which is *not* the app. `app./dashboard` is a 404;
`/keywords` is the real protected route.

**Cloudflare blocks headless browsers.** Measured against `app.pinclicks.com`:

| Browser | Result |
|---|---|
| Playwright bundled Chromium, headless | 403 |
| Real Chrome, headless | 403 |
| Real Chrome, **headed** | 200 |

So `config/settings.json` sets `browser.channel = "chrome"` and
`browser.headless = false`. **The browser window must stay visible while
scraping** — this is not optional, and setting `headless: true` makes every
request 403. Re-check with `tools/probe_browser.py` if this changes.

No fingerprint spoofing, UA rotation, or challenge solving is used anywhere in
this codebase, and none should be added. If PinClicks blocks the account, the
tool stops and reports it.

**A polluted profile causes false blocks.** If you see Cloudflare 403s that you
cannot explain, delete `user_data/` and retry — a profile created by a
different browser build carries state that trips the WAF.

**PinClicks is a Laravel Livewire + Filament app.** Server-rendered HTML
updated over AJAX, with `wire:model` / `wire:submit` attributes and no
`data-testid` hooks. Write selectors against element ids, `wire:model` names,
and `href` patterns. Never against Tailwind utility classes — those change with
any restyle.

**The pin data is embedded as JSON — do not scrape the pin table cell by cell.**
The Top Pins page carries the full dataset in the Livewire component's
`wire:snapshot` attribute: `id`, `position`, `title`, `description`,
`total_saves`, `total_repins`, `total_reactions`, `pin_score`, `dominant_color`,
`alt_text`, `is_repin`, `image[]` (incl. the full 736px URL), and
`visual_annotation` / `annotations_with_links`.

That is every field the CSV template needs, for every pin on the page, from a
single parse — no per-cell selectors, no clicking into 40 detail views. It is
also far more stable, because `wire:snapshot` is a framework contract rather
than a design detail. Scrape the DOM only as a fallback.

Caveat: on the pins sampled, `visual_annotation` and `annotations_with_links`
were **empty arrays**. Annotations may be sparse; do not assume they are always
populated.

**Use PinClicks' own Export for pins. Do not scrape the pin table at all.**
On `/pins?search=<kw>`, tick the header checkbox then Export → "Pin Data".
It downloads a CSV containing every field the template needs:

```
ID, Title, URL, Pin Score, Saves, Position, Is Repin, Created At, Comments,
Repins, Reactions, Keyword Annotations, Image URL, Board URL, Profile URL,
Description
```

Measured on "Chicken Breast Recipe": 23 pins, 23 with Description, 20 with
Keyword Annotations, full-size 736px Image URLs. `Keyword Annotations` is
already comma-separated, matching the template's `annotation` column exactly.

This supersedes both the DOM-scraping plan *and* the `wire:snapshot` JSON idea:
the export is an explicit product feature rather than an internal detail, and
it populates annotations where the embedded JSON left them empty.

Caveat: Export covers the **currently loaded rows**, so it yields ~23 pins per
page. Reaching the PRD's 40–50 requires paging first, then exporting.

**The keyword table paginates — it does not infinite-scroll.**
`wire:click="gotoPage(N, 'tablePage')"`, roughly 100 pages. Any design based on
scrolling to load more rows is wrong for this site.

## The four-level model (verified end to end)

This supersedes the PRD's two-level flow.

| Level | Meaning | Example | How |
|---|---|---|---|
| **L1** | Big niche | "Food And Drink" (682.5K) | Interests panel on `/keyword-explorer`, drill via `setTopInterest('<id>')` |
| **L2** | Main keyword | "Pizza" | picked from the L1 results table |
| **L3** | Keywords from that main keyword | "pizza dough recipe" 596K, "pizza recipes" 286K … | `GET /keyword-explorer?search=pizza` → ~100 rows |
| **L4** | Top pins for each focused keyword | ~21 pins | `GET /pins?search=<keyword>` → select all → Export → "Pin Data" |

**L3 and L4 are plain URLs.** No form driving, no clicking through the UI:

```
/keyword-explorer?search=<main keyword>     -> ~100 keywords + volumes
/pins?search=<focused keyword>              -> ~21 top pins, then Export
```

`/keyword/{slug}/{id}` is **not** a Level-3 page — it shows pins for that single
keyword. Do not use it for keyword discovery.

Keyword Explorer has filter checkboxes (Search Suggestions, Interests, Related
Interests, Taxonomy) that change which keywords are returned; only "Interests"
is on by default. Both this page and Top Pins have their own Export button.

### Pin depth: target ~21, not 40+

The first batch of ~21 pins loads in 2-3s and has been reliable in every run.
Going beyond it triggers a live "Getting more pins from Pinterest..." fetch that
is **non-deterministic**: measured twice on the same keyword minutes apart, it
span for 303s and then for 25s, and **added zero pins both times**.

Since the table is sorted by position/pin score, pins 21+ are the lowest-ranked
ones anyway — a large stall risk for the worst data. Prefer more keywords at ~21
pins each over fewer keywords chasing 40+.

## Route map (verified)

| Route | Screen |
|---|---|
| `/keyword-explorer` | Keyword Explorer — Level 1 (Interests panel) + Level 2 (results table) |
| `/pins?search=<kw>` | Top Pins, "Pinterest Search" tab — **ranked pins, the PRD's target** |
| `/pins/explore` | "PinClicks Database" tab — actually *Pin Explorer*, a 147M-pin index |
| `/keyword/{slug}/{pinterest_id}` | Single-keyword drill-down |
| `/stats`, `/saved`, `/rankings`, `/searches`, `/saved-keywords`, `/accounts` | Other tools |

`/dashboard` and `/top-pins` are 404s — earlier guesses, do not use them.

Top Pins sortable columns: `position`, `pin_score`, `total_saves`,
`total_repins`, `total_reactions`, `total_comments`, `total_distributions`,
`created_at`, `is_repin`.

## Configuration

| File | Purpose |
|---|---|
| `config/settings.json` | Site list, pacing, timeouts, retry policy, browser mode |
| `config/selectors.json` | Every PinClicks DOM selector, with fallbacks |
| `config/rules.json` | Phase-2 accept/reject rules |

Nothing PinClicks-specific is hardcoded in Python. When the site changes, you
edit `selectors.json` — not code.

## Calibrating selectors

`auth` and `urls` are verified. The `level1` / `level2` / `pins` sections sit
behind the login wall and are still placeholders.

1. Start the app, go to Screen 1, click **Launch Browser & Log In**.
2. Log in to PinClicks in the window that opens. Credentials are never seen,
   stored, or transmitted by this app — only the browser profile in
   `user_data/` persists.
3. Click **Re-check**; the badge should read *Session active*.
4. Click **Calibrate** to dump the authenticated DOM to `debug/`.
5. Fill in `config/selectors.json` from those captures.
6. Click **Run preflight check** — it must pass before any run is allowed.

## Safety properties

- **Credentials are never handled by this app.** No password field, no keyring,
  no credential table. You log in yourself.
- **Preflight refuses to run on broken selectors**, rather than exporting a
  file full of `N/A`.
- **Sequential, jittered pacing.** One browser, one context, no parallelism —
  parallel scraping is the fastest way to get an account blocked.
- `user_data/` holds your session cookies and is gitignored. Never commit it.
