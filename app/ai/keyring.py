"""A rotating pool of Ollama API keys.

A single key stops a long run dead the moment it hits a rate limit, and a run
here can be thousands of keywords. So the pool holds several keys and moves to
the next one the instant the current one refuses.

How a key is treated when it fails:

  rate limited (429)   -> rested for ``cooldown_seconds``, then reused
  auth failed (401/403)-> disabled for the session; a bad key never recovers
                          by being retried, so retrying it just wastes calls
  anything else        -> not the key's fault; the caller retries the request

If every key is resting, callers wait for the soonest one instead of failing
the run -- stopping a 4,000-keyword job because of a one-minute limit would be
the worst possible outcome.

Keys are secrets: they live in config/secrets.json (gitignored), are never
logged, and every API response here shows only a masked form.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger
from ..paths import CONFIG_DIR, load_settings

#: Same file the single-key reader uses. Defined here rather than
#: imported from ollama_client to avoid a circular import.
SECRETS_FILE = CONFIG_DIR / "secrets.json"

log = get_logger("keyring")

#: A disabled key is out for the session; a rested one comes back.
DISABLED = "disabled"
RESTING = "resting"
READY = "ready"


def mask(key: str) -> str:
    """Show enough to identify a key, never enough to use it."""
    k = (key or "").strip()
    if len(k) <= 8:
        return "•" * len(k)
    return f"{k[:4]}…{k[-4:]}"


@dataclass
class _Key:
    value: str
    label: str = ""
    disabled: bool = False
    resting_until: float = 0.0
    calls: int = 0
    failures: int = 0
    last_error: str = ""

    def state(self) -> str:
        if self.disabled:
            return DISABLED
        if self.resting_until > time.time():
            return RESTING
        return READY

    def public(self) -> dict[str, Any]:
        return {
            "label": self.label or mask(self.value),
            "masked": mask(self.value),
            "state": self.state(),
            "calls": self.calls,
            "failures": self.failures,
            "resting_for": max(0, round(self.resting_until - time.time())),
            "last_error": self.last_error[:120],
        }


class KeyPool:
    """Thread-safe rotating pool. One instance per process."""

    def __init__(self) -> None:
        self._keys: list[_Key] = []
        self._i = 0
        self._lock = threading.Lock()
        self._loaded = False

    # ------------------------------------------------------------ storage

    def _cooldown(self) -> int:
        return int(load_settings().get("ai", {}).get("key_cooldown_seconds", 60))

    def load(self, force: bool = False) -> None:
        if self._loaded and not force:
            return
        raw: list[dict[str, Any]] = []
        if SECRETS_FILE.exists():
            try:
                data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
                raw = data.get("ollama_api_keys") or []
                # Accept the older single-key shape so upgrading loses nothing.
                single = str(data.get("ollama_api_key", "")).strip()
                if single and not any(k.get("value") == single for k in raw):
                    raw.append({"value": single, "label": "primary"})
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Could not read %s: %s", SECRETS_FILE.name, exc)

        with self._lock:
            existing = {k.value: k for k in self._keys}
            self._keys = []
            for item in raw:
                value = str(item.get("value", "")).strip()
                if not value:
                    continue
                # Keep runtime state across reloads so adding a key does not
                # un-rest the others.
                prev = existing.get(value)
                self._keys.append(prev or _Key(value=value,
                                               label=str(item.get("label", ""))))
            self._i = 0
            self._loaded = True
        log.info("Key pool loaded: %d key(s)", len(self._keys))

    def save(self, entries: list[dict[str, str]]) -> None:
        data: dict[str, Any] = {}
        if SECRETS_FILE.exists():
            try:
                data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
        data.pop("ollama_api_key", None)          # superseded by the list
        data["ollama_api_keys"] = entries
        SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRETS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.load(force=True)

    # ------------------------------------------------------------ use

    def count(self) -> int:
        self.load()
        return len(self._keys)

    def usable(self) -> bool:
        self.load()
        return any(not k.disabled for k in self._keys)

    def acquire(self, wait: bool = True, max_wait: float = 300.0) -> _Key | None:
        """Return the next ready key, waiting for one to wake if needed."""
        self.load()
        deadline = time.time() + max_wait

        while True:
            with self._lock:
                n = len(self._keys)
                if n == 0:
                    return None
                # Round-robin from wherever the last call left off, so load is
                # spread rather than hammering key #1 until it dies.
                for step in range(n):
                    key = self._keys[(self._i + step) % n]
                    if key.state() == READY:
                        self._i = (self._i + step + 1) % n
                        return key

                resting = [k.resting_until for k in self._keys if not k.disabled]

            if not resting or not wait or time.time() >= deadline:
                return None

            sleep_for = max(1.0, min(resting) - time.time())
            log.info("All keys resting; waiting %.0fs for the next one", sleep_for)
            time.sleep(min(sleep_for, max(1.0, deadline - time.time())))

    def report(self, key: _Key, ok: bool, status: int | None = None,
               error: str = "") -> None:
        """Record how a call went and rest or disable the key as needed."""
        with self._lock:
            key.calls += 1
            if ok:
                key.last_error = ""
                return

            key.failures += 1
            key.last_error = error

            if status in (401, 403):
                key.disabled = True
                log.warning("Key %s rejected (HTTP %s); disabled for this session",
                            mask(key.value), status)
            elif status == 429 or "rate" in error.lower():
                key.resting_until = time.time() + self._cooldown()
                log.warning("Key %s rate limited; resting %ds",
                            mask(key.value), self._cooldown())

    def status(self) -> dict[str, Any]:
        self.load()
        with self._lock:
            return {
                "count": len(self._keys),
                "ready": sum(1 for k in self._keys if k.state() == READY),
                "resting": sum(1 for k in self._keys if k.state() == RESTING),
                "disabled": sum(1 for k in self._keys if k.state() == DISABLED),
                "keys": [k.public() for k in self._keys],
            }


pool = KeyPool()
