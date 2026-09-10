"""Filesystem layout and config loading.

Single source of truth for where things live, so no other module has to guess.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
EXPORTS_DIR = ROOT / "exports"
LOGS_DIR = ROOT / "logs"
DEBUG_DIR = ROOT / "debug"
USER_DATA_DIR = ROOT / "user_data"

SETTINGS_FILE = CONFIG_DIR / "settings.json"
SELECTORS_FILE = CONFIG_DIR / "selectors.json"
RULES_FILE = CONFIG_DIR / "rules.json"

DB_FILE = DATA_DIR / "pinclicks.db"

#: Directories created on startup. user_data/ is created by Playwright itself.
RUNTIME_DIRS = (DATA_DIR, EXPORTS_DIR, LOGS_DIR, DEBUG_DIR, USER_DATA_DIR)


def ensure_dirs() -> None:
    for d in RUNTIME_DIRS:
        d.mkdir(parents=True, exist_ok=True)


def _strip_readme(obj: Any) -> Any:
    """Drop the ``_README`` documentation keys before the config is used."""
    if isinstance(obj, dict):
        return {k: _strip_readme(v) for k, v in obj.items() if k != "_README"}
    return obj


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return _strip_readme(json.load(fh))


def save_json(path: Path, data: dict[str, Any]) -> None:
    """Write atomically so a crash mid-write cannot corrupt a config file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    tmp.replace(path)


def load_settings() -> dict[str, Any]:
    return load_json(SETTINGS_FILE)


def load_selectors() -> dict[str, Any]:
    return load_json(SELECTORS_FILE)


def load_rules() -> dict[str, Any]:
    return load_json(RULES_FILE)
