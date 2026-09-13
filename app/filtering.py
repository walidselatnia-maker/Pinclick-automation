"""Phase-2 pin filtering.

The user's four rules:

  1. no duplicates
  2. drop pins where BOTH title and description are empty
  3. drop roundup/listicle titles ("25 recipes for...", "10 best chicken...")
  4. drop pins unrelated to the niche (food-adjacent but not a real recipe)

Design: deterministic first, AI second.

Rules 1 and 2 are exact -- they are facts about the data, so code decides them
and the answer is always the same. Rule 3 is *mostly* exact: a leading count
("25 Recipes...") is unambiguous and regex catches it for free. Rule 4 is a
genuine judgement call, and is the reason the AI is here.

Running the cheap rules first is not only an optimisation: every pin they
resolve is one the model cannot get wrong.

Failure policy: if the classifier is unavailable, surviving pins are marked
UNREVIEWED -- never silently accepted. A filter that quietly passes everything
when the API is down is worse than no filter, because you stop checking it.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .ai import ollama_client
from .logging_setup import get_logger
from .models import NA
from . import language
from .paths import load_rules, load_settings

log = get_logger("filtering")

ACCEPTED = "accepted"
REJECTED = "rejected"
UNREVIEWED = "unreviewed"

# --------------------------------------------------------------- rule 3

#: A count of items at or near the start: "25 Recipes", "Top 10 Best Chicken
#: Dinners", "15+ Easy Ideas". Anchored near the start so a single recipe that
#: merely contains a number ("Chicken 65") is not caught.
#: Words that are plural in form but singular in meaning, so their trailing
#: "s" says nothing about counting.
_FALSE_PLURALS = {
    "christmas", "thanksgiving", "hummus", "couscous", "molasses", "swiss",
    "asparagus", "citrus", "bass", "grass", "glass", "cress", "dress",
    "delicious", "famous", "various", "less", "press", "class", "chips",
    "crisps", "greens", "oats", "beans", "noodles", "grits", "brussels",
}


def _is_plural(word: str) -> bool:
    """Rough plural test for an English noun.

    Deliberately conservative: it must be confident, because a false positive
    here throws away a real recipe.
    """
    w = word.lower().strip("-–—.,!?:;()[]\"'")
    if len(w) < 4 or w in _FALSE_PLURALS:
        return False
    if w.endswith("ss") or w.endswith("us") or w.endswith("is"):
        return False
    return w.endswith("s") or w.endswith("ies")


#: The number is counting ITEMS when a plural noun follows it directly --
#: "25 Recipes", "12 Sides", "40 Appetizers". It is describing ONE recipe when
#: a singular noun follows -- "3 Ingredient Cookies", "15 Minute Pasta",
#: "1 Pan Chicken" -- because English attributive nouns are always singular.
#:
#: This is grammar rather than vocabulary, so it needs no word list to
#: maintain. It is intentionally narrow: cases with an adjective in between
#: ("35 Cute Thanksgiving-Themed Appetizers") cannot be settled without
#: understanding the sentence, and are left to the AI stage instead of being
#: guessed at. An earlier version tried to enumerate the nouns that could
#: follow a number and missed every word nobody thought of.
_NUMBER_THEN_WORD = re.compile(r"^\W*(\d{1,3})\s*\+?\s+([A-Za-z][\w-]*)", re.I)

#: Openers that are roundups whatever follows: "Top 10 ...", "10 Best ...".
_RANKED_ROUNDUP = re.compile(
    r"^\W*(?:top|best)\s+\d{1,3}\b|^\W*\d{1,3}\s+(?:best|top)\b", re.I
)

_LISTICLE_PATTERNS = [
    re.compile(r"\b(?:round[- ]?up|listicle)\b", re.I),
]


def looks_like_listicle(title: str) -> bool:
    """Certain roundups only.

    Anything requiring judgement is left UNREVIEWED for the AI stage rather
    than being decided by a rule that cannot know. Over-rejecting here deletes
    real recipes, which is the more expensive mistake.
    """
    text = (title or "").strip()
    if not text or text == NA:
        return False

    if _RANKED_ROUNDUP.search(text):
        return True

    match = _NUMBER_THEN_WORD.match(text)
    if match and _is_plural(match.group(2)):
        return True

    return any(p.search(text) for p in _LISTICLE_PATTERNS)


# --------------------------------------------------------------- helpers

def _as_int(value: Any) -> int:
    """Numbers arrive as strings from CSV and as ints from the scraper."""
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0


def sort_pins(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order pins by whichever signal the rules name.

    Sorting happens after filtering so the ranking reflects what survived,
    and it is applied to accepted and rejected alike -- a rejected list is
    easier to scan when it is ordered the same way.
    """
    cfg = load_rules().get("sort", {})
    field = cfg.get("by", "saves_count")
    descending = bool(cfg.get("descending", True))

    if field == "position":
        # Position is a rank where 1 is best, so "best first" is ascending --
        # the opposite of the other fields. descending flips it.
        return sorted(pins,
                      key=lambda p: _as_int(p.get("position")) or 10**6,
                      reverse=not descending)
    return sorted(pins, key=lambda p: _as_int(p.get(field)), reverse=descending)


def _blank(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or text == NA


def _dedupe_key(pin: dict[str, Any]) -> str:
    """Prefer the pin URL; fall back to title+image so blanks still collapse."""
    url = str(pin.get("pin_url") or "").strip()
    if url and url != NA:
        return "url:" + url.lower()
    title = str(pin.get("spy_title", "")).strip().lower()
    image = str(pin.get("image_url", "")).strip().lower()
    return "ti:" + title + "|" + image


# --------------------------------------------------------------- stage 1

def _title_key(pin: dict[str, Any]) -> str:
    """Normalised title, for spotting the same content re-pinned."""
    text = str(pin.get("spy_title", "")).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def apply_deterministic(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rules 1-3. Every pin comes back tagged; nothing is dropped silently."""
    rules = load_rules().get("deterministic", {})
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_titles: set[str] = set()

    # Work through the strongest pins first so that when the same content has
    # been pinned several times, the copy that survives deduplication is the
    # one with the most saves rather than whichever happened to be scraped
    # first.
    ordered = sorted(pins, key=lambda p: _as_int(p.get("saves_count")), reverse=True)

    for pin in ordered:
        row = dict(pin)
        reason = ""

        if rules.get("drop_duplicates", True):
            key = _dedupe_key(row)
            if key in seen:
                reason = "duplicate"
            else:
                seen.add(key)

        if not reason and rules.get("drop_duplicate_titles", True):
            tkey = _title_key(row)
            if tkey and tkey in seen_titles:
                reason = "same title as a stronger pin"
            elif tkey:
                seen_titles.add(tkey)

        if not reason and rules.get("require_title_or_description", True):
            if _blank(row.get("spy_title")) and _blank(row.get("spy_description")):
                reason = "no title and no description"

        # Foreign-language pins are as useless as foreign keywords, and the
        # language filter previously ran only on keywords, so German and
        # Danish pin titles sailed through.
        if not reason and rules.get("drop_other_languages", True):
            title = str(row.get("spy_title", ""))
            desc = str(row.get("spy_description", ""))
            ok_title, why = language.is_wanted(title) if title and title != NA else (True, "")
            if not ok_title:
                # A foreign title with an English description is still usable.
                ok_desc, _ = language.is_wanted(desc) if desc and desc != NA else (False, "")
                if not ok_desc:
                    reason = f"other language ({why})" if why else "other language"

        if not reason and rules.get("drop_listicles", True):
            if looks_like_listicle(str(row.get("spy_title", ""))):
                reason = "listicle / roundup title"

        # Popularity floors. A pin nobody saved is not evidence of anything,
        # whatever its title says.
        if not reason:
            floor = int(rules.get("min_saves", 0) or 0)
            if floor and _as_int(row.get("saves_count")) < floor:
                reason = f"under {floor:,} saves"

        if not reason:
            floor = int(rules.get("min_pin_score", 0) or 0)
            if floor and _as_int(row.get("pin_score")) < floor:
                reason = f"pin score under {floor:,}"

        row["filter_status"] = REJECTED if reason else UNREVIEWED
        row["reject_reason"] = reason
        out.append(row)

    rejected = sum(1 for r in out if r["filter_status"] == REJECTED)
    log.info("Deterministic: %d rejected, %d to review", rejected, len(out) - rejected)
    return out


# --------------------------------------------------------------- stage 2

_SYSTEM = (
    "You judge whether Pinterest pins belong in a content research set for a "
    "specific niche.\n\n"
    "For each pin decide:\n"
    "  keep     - is it genuinely about the niche, AND a single specific item "
    "(one recipe, one project, one guide)?\n"
    "  listicle - is it a roundup of many items (\"25 recipes for...\", "
    "\"10 best...\")?\n\n"
    "Reject when the pin is only loosely related to the niche (for example food "
    "photography, restaurant marketing, kitchen gadgets or diet talk when the "
    "niche is a recipe), or when it is not a real, specific, actionable item.\n\n"
    "Return ONLY JSON, no prose:\n"
    '{"results":[{"i":<index>,"keep":true,"listicle":false,'
    '"reason":"<max 8 words>"}]}\n\n'
    "Include exactly one entry per pin, using the given index."
)


def _build_prompt(niche: str, batch: list[dict[str, Any]]) -> str:
    lines = ["Niche: " + niche, "", "Pins:"]
    for index, pin in enumerate(batch):
        title = str(pin.get("spy_title", "") or "")[:200]
        desc = str(pin.get("spy_description", "") or "")[:300]
        annotation = str(pin.get("annotation", "") or "")[:200]
        lines.append("[" + str(index) + "] title: " + title)
        if desc and desc != NA:
            lines.append("     description: " + desc)
        if annotation and annotation != NA:
            lines.append("     annotations: " + annotation)
    return "\n".join(lines)


def _parse_reply(raw: str, size: int) -> dict[int, dict[str, Any]]:
    """Parse the model's JSON, tolerating fenced or prefixed output."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text, flags=re.I | re.S)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ollama_client.OllamaError("Unparseable reply: " + str(exc)) from exc

    out: dict[int, dict[str, Any]] = {}
    for item in data.get("results", []):
        try:
            index = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= index < size:
            out[index] = item
    return out


def _topic(pin: dict[str, Any], fallback: str) -> str:
    """What a pin is judged against: the keyword it was scraped from.

    Not the niche of whatever run happened most recently. The working set
    holds pins from many keywords, and judging "fruit pizza" pins against
    "pizza dough" -- which is what a single run-wide niche did -- rejected a
    whole earlier keyword as off-topic the moment a new one was scraped.
    """
    seed = str(pin.get("seed_keyword") or "").strip()
    return seed or fallback


def apply_ai(
    pins: list[dict[str, Any]],
    niche: str,
    *,
    batch_size: int | None = None,
) -> list[dict[str, Any]]:
    """Rule 4 (plus a second opinion on rule 3) via Ollama Cloud.

    Only pins still UNREVIEWED are sent; anything the deterministic pass has
    already rejected is left alone.

    Each pin is judged against ITS OWN seed keyword, and the verdict is cached
    under that keyword. So scraping a new keyword never re-judges -- or even
    re-asks about -- pins from earlier keywords: their verdicts come straight
    back from the cache, and only the new pins cost a model call.
    """
    settings = load_settings().get("ai", {})
    batch_size = batch_size or settings.get("batch_size", 15)

    pending = [p for p in pins if p.get("filter_status") == UNREVIEWED]
    if not pending:
        return pins

    if not ollama_client.is_configured():
        log.warning("AI filtering skipped: no API key. %d pins left UNREVIEWED",
                    len(pending))
        return pins

    attempts = max(1, int(settings.get("batch_attempts", 3)))
    backoff = settings.get("batch_backoff_seconds", [5, 20, 60])

    from . import db
    cache: dict[str, Any] = db.get_state(VERDICT_KEY) or {}

    def ckey(pin: dict[str, Any]) -> str:
        return f"{_topic(pin, niche).lower()}|{_override_id(pin)}"

    verdicts: dict[int, dict[str, Any]] = {}
    failed_batches = 0

    # Reuse anything already judged, and group what is left by topic so each
    # batch is asked about one keyword. A refresh with no new pins then costs
    # nothing, and a new keyword costs exactly its own pins.
    fresh_by_topic: dict[str, list[dict[str, Any]]] = {}
    for pin in pending:
        hit = cache.get(ckey(pin))
        if hit is None:
            fresh_by_topic.setdefault(_topic(pin, niche), []).append(pin)
        else:
            verdicts[id(pin)] = hit

    if verdicts:
        log.info("Reusing %d cached AI verdict(s)", len(verdicts))
    fresh_total = sum(len(v) for v in fresh_by_topic.values())
    if fresh_by_topic:
        log.info("Asking the model about %d new pin(s) across %d keyword(s): %s",
                 fresh_total, len(fresh_by_topic),
                 ", ".join(f"{k} ({len(v)})" for k, v in fresh_by_topic.items()))

    batches = [(topic, group[i:i + batch_size])
               for topic, group in fresh_by_topic.items()
               for i in range(0, len(group), batch_size)]
    total_batches = len(batches)

    for number, (topic, batch) in enumerate(batches, start=1):
        parsed = None

        # Retry the batch rather than abandoning it. A failure here is usually
        # every key being rate limited at the same moment, which passes -- and
        # skipping the batch would silently leave those pins unjudged in a run
        # of thousands, which is exactly the outcome to avoid.
        for attempt in range(1, attempts + 1):
            try:
                reply = ollama_client.chat([
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": _build_prompt(topic, batch)},
                ])
                parsed = _parse_reply(reply, len(batch))
                break
            except ollama_client.OllamaError as exc:
                if attempt >= attempts:
                    failed_batches += 1
                    log.error("AI batch %d/%d gave up after %d attempts: %s",
                              number, total_batches, attempts, exc)
                    break
                pause = backoff[min(attempt - 1, len(backoff) - 1)]
                log.warning("AI batch %d/%d attempt %d failed (%s); retrying in %ds",
                            number, total_batches, attempt, str(exc)[:90], pause)
                time.sleep(pause)

        if parsed is None:
            continue  # left UNREVIEWED, never guessed at

        for index, verdict in parsed.items():
            verdicts[id(batch[index])] = verdict
            cache[ckey(batch[index])] = verdict

        if number % 10 == 0 or number == total_batches:
            log.info("AI progress: %d/%d batches", number, total_batches)

    reviewed = 0
    for pin in pins:
        verdict = verdicts.get(id(pin))
        if verdict is None:
            continue
        reviewed += 1
        if verdict.get("listicle"):
            reason = str(verdict.get("reason", "")).strip()
            pin["filter_status"] = REJECTED
            pin["reject_reason"] = ("AI: listicle " + reason).strip()
        elif verdict.get("keep"):
            pin["filter_status"] = ACCEPTED
            pin["reject_reason"] = ""
        else:
            pin["filter_status"] = REJECTED
            pin["reject_reason"] = "AI: " + str(verdict.get("reason", "not relevant"))

    if fresh_by_topic:
        db.set_state(VERDICT_KEY, cache)

    if failed_batches:
        log.warning("%d batch(es) could not be reviewed; those pins stay "
                    "UNREVIEWED rather than being assumed good", failed_batches)
    log.info("AI reviewed %d/%d pins (%d from cache, %d newly judged)",
             reviewed, len(pending), reviewed - min(reviewed, fresh_total), fresh_total)
    return pins



# --------------------------------------------------------------- overrides

#: Your decisions beat the rules. Stored by pin URL so they survive
#: re-filtering, a rule change, and an app restart -- an override that
#: evaporated the next time you pressed "Re-run filters" would be useless.
OVERRIDE_KEY = "pin_overrides"

#: Cached AI verdicts, keyed by the pin's own seed keyword + pin. Asking the
#: model twice about the same pin costs money and roughly a minute a run for
#: no new information -- the verdict cannot change unless the pin does. Keying
#: by the pin's own keyword (not the latest run's niche) is what keeps earlier
#: keywords untouched when a new one is scraped.
VERDICT_KEY = "ai_verdicts"


def _override_id(pin: dict[str, Any]) -> str:
    url = str(pin.get("pin_url") or "").strip()
    if url and url != NA:
        return url.lower()
    return _title_key(pin)


def load_overrides() -> dict[str, str]:
    from . import db
    return db.get_state(OVERRIDE_KEY) or {}


def set_override(pin_id: str, status: str | None) -> dict[str, str]:
    """Pin an outcome, or clear it by passing None."""
    from . import db
    current = load_overrides()
    if status:
        current[pin_id] = status
    else:
        current.pop(pin_id, None)
    db.set_state(OVERRIDE_KEY, current)
    return current


def apply_overrides(pins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Last word: applied after every rule, including the AI."""
    overrides = load_overrides()
    if not overrides:
        return pins

    hits = 0
    for pin in pins:
        wanted = overrides.get(_override_id(pin))
        if not wanted or pin.get("filter_status") == wanted:
            continue
        pin["filter_status"] = wanted
        pin["reject_reason"] = "" if wanted == ACCEPTED else "you rejected this"
        pin["manual"] = True
        hits += 1

    if hits:
        log.info("Applied %d manual override(s)", hits)
    return pins


# --------------------------------------------------------------- entry point

def filter_pins(
    pins: list[dict[str, Any]],
    niche: str,
    *,
    use_ai: bool = True,
) -> dict[str, Any]:
    """Run the full Phase-2 pass and return the split plus a summary."""
    result = apply_deterministic(pins)

    if use_ai and ollama_client.is_configured():
        result = apply_ai(result, niche)
    else:
        # Without the AI stage there is no second opinion coming, so holding
        # pins as "unreviewed" would mean exporting nothing at all. Anything
        # the exact rules did not reject is accepted, and the summary says the
        # off-niche check was skipped so the result is not mistaken for a full
        # pass.
        for pin in result:
            if pin["filter_status"] == UNREVIEWED:
                pin["filter_status"] = ACCEPTED

    result = apply_overrides(result)

    accepted = [p for p in result if p["filter_status"] == ACCEPTED]
    rejected = [p for p in result if p["filter_status"] == REJECTED]
    unreviewed = [p for p in result if p["filter_status"] == UNREVIEWED]

    return {
        "accepted": accepted,
        "rejected": rejected,
        "unreviewed": unreviewed,
        "summary": {
            "total": len(result),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "unreviewed": len(unreviewed),
            "ai_used": use_ai and ollama_client.is_configured(),
            "ai_skipped": use_ai and not ollama_client.is_configured(),
        },
    }
