"""SQLite persistence.

Design notes:

* Connection-per-operation. SQLite in WAL mode handles this well, and it means
  the async web layer and the background scraper never share a connection --
  which is the usual source of "SQLite objects created in a thread" errors.
* ``pins`` has a UNIQUE (keyword_id, pin_url) constraint. That single line is
  what makes crash-resume idempotent: replaying a checkpoint re-inserts pins we
  already have, and the ON CONFLICT clause turns those into no-ops instead of
  duplicate rows.
"""

from __future__ import annotations

import json
import logging as log_mod
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .models import FilterStatus, JobState, ScrapeStatus
from .paths import DB_FILE, ensure_dirs

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS niches (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    category TEXT NOT NULL DEFAULT '',
    name     TEXT NOT NULL,
    volume   INTEGER NOT NULL DEFAULT 0,
    selected INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, category, name)
);

CREATE TABLE IF NOT EXISTS keywords (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    niche_id      INTEGER REFERENCES niches(id) ON DELETE SET NULL,
    keyword       TEXT NOT NULL,
    volume        INTEGER NOT NULL DEFAULT 0,
    approved      INTEGER NOT NULL DEFAULT 0,
    excluded_by   TEXT NOT NULL DEFAULT '',
    scrape_status TEXT NOT NULL DEFAULT 'pending',
    pins_target   INTEGER NOT NULL DEFAULT 40,
    pins_scraped  INTEGER NOT NULL DEFAULT 0,
    heal_count    INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id, keyword)
);

CREATE TABLE IF NOT EXISTS pins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_id      INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL DEFAULT 0,
    spy_title       TEXT NOT NULL DEFAULT 'N/A',
    spy_description TEXT NOT NULL DEFAULT 'N/A',
    annotation      TEXT NOT NULL DEFAULT 'N/A',
    pin_url         TEXT NOT NULL DEFAULT 'N/A',
    image_url       TEXT NOT NULL DEFAULT 'N/A',
    pin_score       TEXT NOT NULL DEFAULT 'N/A',
    saves_count     TEXT NOT NULL DEFAULT 'N/A',
    scraped_at      REAL NOT NULL,
    filter_status   TEXT NOT NULL DEFAULT 'unfiltered',
    reject_reason   TEXT NOT NULL DEFAULT '',
    manual_override INTEGER NOT NULL DEFAULT 0,
    UNIQUE (keyword_id, pin_url)
);

CREATE INDEX IF NOT EXISTS idx_pins_keyword ON pins(keyword_id);
CREATE INDEX IF NOT EXISTS idx_pins_filter  ON pins(filter_status);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'queued',
    checkpoint    TEXT NOT NULL DEFAULT '{}',
    heartbeat_at  REAL NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS rule_presets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    payload    TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS browse_cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS run_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts      REAL NOT NULL,
    level   TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_run_log_run ON run_log(run_id, id);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    ensure_dirs()
    with connect() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# app_state (small key/value store for session badge, last run, etc.)
# --------------------------------------------------------------------------

def get_state(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def create_run(label: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (label, created_at) VALUES (?, ?)", (label, time.time())
        )
    return int(cur.lastrowid)


def finish_run(run_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (time.time(), run_id))


def latest_run() -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


# --------------------------------------------------------------------------
# niches / keywords
# --------------------------------------------------------------------------

def upsert_niches(run_id: int, category: str, niches: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO niches (run_id, category, name, volume) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_id, category, name) DO UPDATE SET volume = excluded.volume",
            [(run_id, category, n["name"], n.get("volume", 0)) for n in niches],
        )


def list_niches(run_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM niches WHERE run_id = ? ORDER BY volume DESC", (run_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def upsert_keywords(run_id: int, niche_id: int | None, keywords: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.executemany(
            "INSERT INTO keywords (run_id, niche_id, keyword, volume) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_id, keyword) DO UPDATE SET volume = excluded.volume",
            [(run_id, niche_id, k["keyword"], k.get("volume", 0)) for k in keywords],
        )


def list_keywords(run_id: int, approved_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM keywords WHERE run_id = ?"
    if approved_only:
        sql += " AND approved = 1"
    sql += " ORDER BY volume DESC"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, (run_id,)).fetchall()]


def set_keyword_status(keyword_id: int, status: ScrapeStatus, error: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE keywords SET scrape_status = ?, last_error = ? WHERE id = ?",
            (status.value, error, keyword_id),
        )


# --------------------------------------------------------------------------
# pins
# --------------------------------------------------------------------------

def insert_pins(keyword_id: int, pins: list[dict[str, Any]]) -> int:
    """Insert a checkpoint batch. Re-inserting an existing pin is a no-op.

    Returns the number of rows actually added, which is how the caller knows
    whether a scroll produced anything new.
    """
    now = time.time()
    with connect() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM pins WHERE keyword_id = ?", (keyword_id,)
        ).fetchone()["c"]
        conn.executemany(
            """
            INSERT INTO pins (keyword_id, position, spy_title, spy_description,
                              annotation, pin_url, image_url, pin_score,
                              saves_count, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(keyword_id, pin_url) DO NOTHING
            """,
            [
                (
                    keyword_id,
                    p.get("position", 0),
                    p.get("spy_title", "N/A"),
                    p.get("spy_description", "N/A"),
                    p.get("annotation", "N/A"),
                    p.get("pin_url", "N/A"),
                    p.get("image_url", "N/A"),
                    p.get("pin_score", "N/A"),
                    p.get("saves_count", "N/A"),
                    now,
                )
                for p in pins
            ],
        )
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM pins WHERE keyword_id = ?", (keyword_id,)
        ).fetchone()["c"]
        conn.execute(
            "UPDATE keywords SET pins_scraped = ? WHERE id = ?", (after, keyword_id)
        )
    return after - before


def count_pins(keyword_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM pins WHERE keyword_id = ?", (keyword_id,)
        ).fetchone()["c"]


def list_pins(run_id: int, filter_status: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT p.*, k.keyword AS seed_keyword FROM pins p "
        "JOIN keywords k ON k.id = p.keyword_id WHERE k.run_id = ?"
    )
    params: list[Any] = [run_id]
    if filter_status:
        sql += " AND p.filter_status = ?"
        params.append(filter_status)
    sql += " ORDER BY k.keyword, p.position, p.id"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_pin_filter(pin_id: int, status: FilterStatus, reason: str = "", manual: bool = False) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE pins SET filter_status = ?, reject_reason = ?, manual_override = ? "
            "WHERE id = ?",
            (status.value, reason, int(manual), pin_id),
        )


# --------------------------------------------------------------------------
# jobs (checkpoints + watchdog heartbeat)
# --------------------------------------------------------------------------

def create_job(run_id: int, job_type: str, checkpoint: dict[str, Any] | None = None) -> int:
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (run_id, type, state, checkpoint, heartbeat_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, job_type, JobState.QUEUED.value,
             json.dumps(checkpoint or {}), now, now, now),
        )
    return int(cur.lastrowid)


def get_job(job_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["checkpoint"] = json.loads(job["checkpoint"] or "{}")
    return job


def update_job(
    job_id: int,
    *,
    state: JobState | None = None,
    checkpoint: dict[str, Any] | None = None,
    error: str | None = None,
    bump_attempts: bool = False,
) -> None:
    sets = ["updated_at = ?"]
    params: list[Any] = [time.time()]
    if state is not None:
        sets.append("state = ?")
        params.append(state.value)
    if checkpoint is not None:
        sets.append("checkpoint = ?")
        params.append(json.dumps(checkpoint))
    if error is not None:
        sets.append("last_error = ?")
        params.append(error)
    if bump_attempts:
        sets.append("attempt_count = attempt_count + 1")
    params.append(job_id)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)


def heartbeat(job_id: int) -> None:
    """Stamp progress. The watchdog reads this to detect hangs (Gap 5)."""
    with connect() as conn:
        conn.execute("UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (time.time(), job_id))


def resumable_jobs() -> list[dict[str, Any]]:
    """Jobs left mid-flight by a crash, for startup reconciliation."""
    states = [s.value for s in (JobState.RUNNING, JobState.RETRYING,
                                JobState.RECOVERING, JobState.PAUSED,
                                JobState.AWAITING_AUTH)]
    placeholders = ",".join("?" * len(states))
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY id", states
        ).fetchall()
    jobs = []
    for r in rows:
        j = dict(r)
        j["checkpoint"] = json.loads(j["checkpoint"] or "{}")
        jobs.append(j)
    return jobs


# --------------------------------------------------------------------------
# presets + logging
# --------------------------------------------------------------------------

def save_preset(name: str, payload: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO rule_presets (name, payload, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET payload = excluded.payload",
            (name, json.dumps(payload), time.time()),
        )


def list_presets() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM rule_presets ORDER BY name").fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def delete_preset(name: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM rule_presets WHERE name = ?", (name,))


def log(run_id: int, message: str, level: str = "info") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO run_log (run_id, ts, level, message) VALUES (?, ?, ?, ?)",
            (run_id, time.time(), level, message),
        )


def tail_log(run_id: int, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM run_log WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
            (run_id, after_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- browse cache

def cache_get(key: str, max_age_s: float) -> Any | None:
    """Return a cached value, or None if absent or too old.

    Browsing the niche tree re-fetches the same levels constantly -- going
    deeper and back used to re-scrape every time, which is 20-30s per step for
    data that had not changed. Cached levels make Back instant.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT value, created_at FROM browse_cache WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return None
    if max_age_s >= 0 and (time.time() - row["created_at"]) > max_age_s:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return None


def cache_set(key: str, value: Any) -> None:
    """Store a cache entry.

    Refuses to store an EMPTY result. A failed scrape returns [], and writing
    that over a good entry destroys real data -- which is exactly what happened
    when runs were retried while Cloudflare was blocking us: 94KB of pins
    replaced by "[]". An empty result is never worth caching anyway.
    """
    if value is None or (isinstance(value, (list, dict, str)) and len(value) == 0):
        log_mod.getLogger("pinclicks.db").warning(
            "Refusing to cache an empty result for %r (keeping any existing entry)",
            key,
        )
        return

    with connect() as conn:
        conn.execute(
            "INSERT INTO browse_cache (key, value, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "created_at = excluded.created_at",
            (key, json.dumps(value), time.time()),
        )


def cache_clear(prefix: str = "") -> int:
    """Drop cached entries. Empty prefix clears everything."""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM browse_cache WHERE key LIKE ?", (f"{prefix}%",)
        )
        return cur.rowcount
