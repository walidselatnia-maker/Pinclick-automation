"""Ollama Cloud client for semantic pin classification.

Cloud only -- https://ollama.com/api -- authenticated with OLLAMA_API_KEY as a
Bearer token. No local Ollama, by request.

Note: Ollama Cloud serves open-weight models (gpt-oss, deepseek, qwen, glm,
kimi, gemma). It does NOT host Anthropic's Claude, so the model name is a
setting rather than something hardcoded.

The key is read from the environment or config/secrets.json (gitignored). It is
never logged, never written to the database, and never included in an export.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..paths import CONFIG_DIR, load_settings

log = get_logger("ollama")

from .keyring import SECRETS_FILE, pool  # noqa: E402


class OllamaError(RuntimeError):
    """Any failure talking to Ollama Cloud."""


def get_api_key() -> str | None:
    """Read the API key from the environment, else config/secrets.json."""
    key = os.environ.get("OLLAMA_API_KEY", "").strip()
    if key:
        return key
    if SECRETS_FILE.exists():
        try:
            data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
            key = str(data.get("ollama_api_key", "")).strip()
            return key or None
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read %s: %s", SECRETS_FILE.name, exc)
    return None


def is_configured() -> bool:
    """True when at least one usable key exists, in the pool or the env."""
    return pool.usable() or bool(get_api_key())


def _cfg() -> dict[str, Any]:
    return load_settings().get("ai", {})


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float | None = None,
    timeout: int | None = None,
    fmt: str | None = "json",
) -> str:
    """Send a chat completion and return the assistant's raw text.

    Raises OllamaError on any failure. Callers must treat a failure as
    "unknown", never as "accepted" -- silently passing everything when the
    classifier is down is worse than returning nothing.
    """
    cfg = _cfg()
    attempts = max(1, int(cfg.get("key_attempts", 4)))
    last_error = "no key available"

    # Try successive keys. A rate-limited or rejected key must not end the
    # run: with thousands of keywords to classify, one exhausted key is a
    # routine event, not a failure.
    for _ in range(attempts):
        entry = pool.acquire()
        key = entry.value if entry else get_api_key()
        if not key:
            raise OllamaError(
                "No Ollama API key. Add one in Settings, or set OLLAMA_API_KEY."
            )

        payload: dict[str, Any] = {
            "model": model or cfg.get("model", "gpt-oss:120b-cloud"),
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature if temperature is not None
                        else cfg.get("temperature", 0)},
        }
        if fmt:
            payload["format"] = fmt

        base = cfg.get("base_url", "https://ollama.com").rstrip("/")
        request = urllib.request.Request(
            f"{base}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(
                request, timeout=timeout or cfg.get("timeout_seconds", 120)
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
            if entry:
                pool.report(entry, ok=True)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            last_error = f"HTTP {exc.code}: {detail}"
            if entry:
                pool.report(entry, ok=False, status=exc.code, error=detail)
            # 429/401/403 mean "try another key"; other codes are the
            # request's fault and another key will not help.
            if exc.code not in (401, 403, 429):
                raise OllamaError(last_error) from exc
        except Exception as exc:
            last_error = str(exc)
            if entry:
                pool.report(entry, ok=False, error=last_error)
            raise OllamaError(last_error) from exc
    else:
        raise OllamaError(f"All API keys failed. Last error: {last_error}")

    content = (body.get("message") or {}).get("content", "")
    if not content:
        raise OllamaError(f"Empty response: {str(body)[:200]}")
    return content


def check() -> dict[str, Any]:
    """Cheap connectivity probe for the UI's AI status badge."""
    if not is_configured():
        return {"ok": False, "reason": "no API key configured"}
    try:
        reply = chat(
            [{"role": "user",
              "content": 'Reply with exactly {"ok":true} and nothing else.'}],
            timeout=45,
        )
        return {"ok": True, "model": _cfg().get("model"), "reply": reply[:80]}
    except OllamaError as exc:
        return {"ok": False, "reason": str(exc)[:200]}
