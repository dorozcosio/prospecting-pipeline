"""
SQLite-backed page cache with configurable TTL.

Usage:
    from src.cache import get, set, is_fresh
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "cache.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS page_cache (
    url      TEXT PRIMARY KEY,
    content  TEXT NOT NULL,
    cached_at TEXT NOT NULL
)
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


def get(url: str) -> str | None:
    """Return cached content for url, or None if not cached."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT content FROM page_cache WHERE url = ?", (url,)
        ).fetchone()
    return row[0] if row else None


def set(url: str, content: str) -> None:
    """Store content for url with the current UTC timestamp."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO page_cache (url, content, cached_at) VALUES (?, ?, ?)",
            (url, content, datetime.now(timezone.utc).isoformat()),
        )


def is_fresh(url: str, ttl_days: int | None = None) -> bool:
    """
    Return True if the cached entry exists and is younger than ttl_days.

    ttl_days defaults to settings.scholar_cache_days if not provided.
    """
    if ttl_days is None:
        from src.config import get_config
        ttl_days = get_config().settings.scholar_cache_days

    with _connect() as conn:
        row = conn.execute(
            "SELECT cached_at FROM page_cache WHERE url = ?", (url,)
        ).fetchone()
    if not row:
        return False
    cached_at = datetime.fromisoformat(row[0])
    return datetime.now(timezone.utc) - cached_at < timedelta(days=ttl_days)
