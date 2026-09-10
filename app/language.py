"""Language filtering for niches and keywords.

PinClicks returns keywords in every language Pinterest has data for, so a niche
comes back mixing "Dinner Recipes" with "Бенто Торт", "Bolo De Aniversário
Feminino" and "Kue Ulang Tahun". Only one language is usually wanted.

**Why this is not a language detector.** A real detector was tried first
(lingua) and it is badly wrong on one- and two-word strings, which is almost
every keyword:

    Fruit         -> Dutch          Dessert  -> German
    Salad         -> Spanish        Pizza    -> Italian
    Pasta Recipes -> Portuguese

Shipping that would delete the best English keywords. So this works the other
way round: **keep by default, exclude only on positive evidence of another
language.** A few foreign keywords surviving is a far cheaper mistake than
throwing away "Pizza".

Evidence used, strongest first:
  1. Non-Latin script      -- exact, a Cyrillic string is never English
  2. Letters English lacks -- á ã ç ñ ü ...
  3. Known foreign words   -- "de", "recetas", "kuchen", ...
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .logging_setup import get_logger
from .paths import load_settings

log = get_logger("language")

#: Unicode script names -> the language family they imply.
SCRIPTS = {
    "DEVANAGARI": "hi",
    "ARABIC": "ar",
    "CYRILLIC": "ru",
    "CJK": "zh",
    "HIRAGANA": "ja",
    "KATAKANA": "ja",
    "HANGUL": "ko",
    "THAI": "th",
    "HEBREW": "he",
    "GREEK": "el",
}

#: Letters that point at a specific language when they appear.
ACCENT_HINTS = {
    "ñ": "es", "¿": "es", "¡": "es",
    "ã": "pt", "õ": "pt",
    "ß": "de",
    "ı": "tr", "ğ": "tr", "ş": "tr",
    "ł": "pl", "ż": "pl", "ź": "pl", "ą": "pl", "ę": "pl", "ć": "pl",
    "ő": "hu", "ű": "hu",
}

#: Any of these means "not English", even when the language is unclear.
FOREIGN_CHARS = set("áàâãäåçéèêëíìîïñóòôõöúùûüýÿœæßğışłżźćąęőű")

#: Common words per language. Used both to exclude (when keeping English) and
#: to include (when keeping that language). Deliberately excludes anything that
#: could plausibly appear in an English recipe keyword.
LANG_WORDS = {
    "es": {"de", "los", "las", "del", "con", "para", "por",
           "una", "uno", "recetas", "comida", "cocina", "saludables",
           "desayunos", "postres", "pollo", "carne", "queso", "casero",
           "rapido", "cumpleanos", "tortas", "pastel"},
    "pt": {"de", "dos", "na", "um", "uma",
           "receitas", "bolo", "aniversario", "caseiro", "frango", "queijo",
           "salgado", "doce", "frigideira", "liquidificador", "jaca"},
    "de": {"und", "mit", "fur", "der", "ein", "zum",
           "kuchen", "rezepte", "rezept", "essen", "backen", "einfach",
           "schnell", "lecker", "torte", "brot", "kekse"},
    "fr": {"et", "le", "les", "des", "du", "aux", "pour", "avec", "recettes",
           "facile", "faciles", "cuisine", "gateau", "poulet", "fromage",
           "chocolat"},
    "it": {"il", "gli", "alla", "ricette", "dolci", "torte",
           "ricetta", "pollo", "formaggio", "veloce"},
    "tr": {"yemek", "tarifi", "tarifler", "kolay", "nefis", "modelleri",
           "evde", "hamuru", "sulu", "corba", "boregi"},
    "id": {"kue", "makanan", "resep", "seblak", "ulang", "tahun", "enak",
           "sederhana", "ayam", "nasi", "goreng", "minuman"},
    "nl": {"recepten", "eten", "makkelijk", "gerechten", "lekker", "taart"},
    "pl": {"przepis", "przepisy", "ciasto", "obiad", "latwe", "szybkie"},
}

#: Evidence threshold before a Latin-script string is called foreign. Roughly
#: one distinctive word ("recetas" = 7) or several short ones. Set by the
#: false positives it prevents: "de" alone scores 2 and no longer rejects.
MIN_FOREIGN_SCORE = 6

#: Single words that are unmistakably not English on their own.
FOREIGN_SINGLETONS = {
    "kuchen": "de", "verde": "es", "makanan": "id", "seblak": "id",
    "tarifi": "tr", "yemek": "tr", "essen": "de", "gateau": "fr",
    "dolci": "it", "postres": "es", "comida": "es", "cocina": "es",
    "eten": "nl", "recept": "nl", "torte": "de", "bolo": "pt",
}


def _script_language(text: str) -> str | None:
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        for script, code in SCRIPTS.items():
            if script in name:
                return code
    return None


def classify(text: str) -> tuple[str, str]:
    """Return ``(language_code, reason)``.

    ``en`` means "no evidence of another language", not "proven English" --
    short keywords carry too little signal to prove anything.
    """
    if not text or not text.strip():
        return "en", ""

    script = _script_language(text)
    if script:
        return script, f"{script} script"

    low = text.lower()

    # A distinctive letter names the language outright.
    for ch, code in ACCENT_HINTS.items():
        if ch in low:
            return code, f"{code} letter ({ch})"

    words = set(re.findall(r"[a-zà-ÿ]+", low))

    # Score by how many of each language's words appear.
    best, best_score, best_hits = None, 0, set()
    for code, vocab in LANG_WORDS.items():
        hits = words & vocab
        if not hits:
            continue
        score = sum(len(w) for w in hits)
        if score > best_score:
            best, best_score, best_hits = code, score, hits

    # A single short function word is not enough. "de" alone appears in
    # English titles, and treating it as proof rejected real English pins.
    # Something long ("recetas") or several words together is required.
    if best and best_score >= MIN_FOREIGN_SCORE:
        return best, f"{best} words ({', '.join(sorted(best_hits)[:2])})"

    if len(words) == 1:
        only = next(iter(words))
        if only in FOREIGN_SINGLETONS:
            code = FOREIGN_SINGLETONS[only]
            return code, f"{code} word"

    # Any remaining foreign letter means "not English", language unknown.
    accents = FOREIGN_CHARS & set(low)
    if accents:
        return "other", f"accented letters ({''.join(sorted(accents))})"

    return "en", ""


def _settings() -> dict[str, Any]:
    return load_settings().get("language_filter", {}) or {}


def is_wanted(text: str) -> tuple[bool, str]:
    """Should this name be kept, given the configured languages?"""
    cfg = _settings()
    if not cfg.get("enabled", True):
        return True, ""

    keep = [c.lower() for c in cfg.get("keep", ["en"])]
    extra = {w.lower() for w in cfg.get("extra_foreign_words", [])}

    code, reason = classify(text)

    if extra and code == "en":
        words = set(re.findall(r"[a-z]+", text.lower()))
        hit = words & extra
        if hit:
            return False, f"excluded word ({sorted(hit)[0]})"

    if code in keep:
        return True, ""
    # "other" means "some non-listed language"; keeping it requires an
    # explicit wildcard rather than being the default.
    if "all" in keep:
        return True, ""
    return False, reason


def keep_only(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    """Drop rows in other languages outright.

    Greying them out was tried first and rejected: the point of the filter is
    not to see them at all. Rows are removed here, server side, so they never
    reach the browser.
    """
    out = []
    dropped = 0
    for row in rows:
        ok, _ = is_wanted(str(row.get(field, "")))
        if ok:
            out.append(row)
        else:
            dropped += 1
    if dropped:
        log.info("Language filter removed %d of %d rows", dropped, len(rows))
    return out
