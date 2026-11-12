"""SQLite persistence: tracked PRs (last fetched data), user marks, poll metadata."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .status import UserState, parse_ts

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
  id TEXT PRIMARY KEY,
  first_seen TEXT NOT NULL,
  relations TEXT NOT NULL,
  data TEXT NOT NULL,
  gone_at TEXT              -- set when it merged/closed; kept briefly for the "merged" row
);
CREATE TABLE IF NOT EXISTS marks (
  id TEXT PRIMARY KEY,
  seen_at TEXT,
  hidden_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

GONE_RETENTION = timedelta(days=7)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    # ---- tracked PRs ----------------------------------------------------

    def open_ids(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT id FROM prs WHERE gone_at IS NULL")]

    def save_pr(self, pr: dict[str, Any], relations: set[str], now: datetime) -> None:
        gone = iso(now) if pr.get("state") in ("MERGED", "CLOSED") else None
        with self._lock:
            self._db.execute(
                """INSERT INTO prs (id, first_seen, relations, data, gone_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET relations=excluded.relations, data=excluded.data,
                     gone_at=COALESCE(prs.gone_at, excluded.gone_at)""",
                (pr["id"], iso(now), json.dumps(sorted(relations)), json.dumps(pr), gone),
            )
            self._db.commit()

    def drop_pr(self, pr_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM prs WHERE id = ?", (pr_id,))
            self._db.execute("DELETE FROM marks WHERE id = ?", (pr_id,))
            self._db.commit()

    def prune(self, now: datetime) -> int:
        cutoff = iso(now - GONE_RETENTION)
        with self._lock:
            ids = [r[0] for r in self._db.execute("SELECT id FROM prs WHERE gone_at < ?", (cutoff,))]
            self._db.executemany("DELETE FROM prs WHERE id = ?", [(i,) for i in ids])
            self._db.executemany("DELETE FROM marks WHERE id = ?", [(i,) for i in ids])
            self._db.commit()
        return len(ids)

    def all_prs(self) -> list[tuple[dict[str, Any], set[str], UserState]]:
        with self._lock:
            rows = self._db.execute(
                """SELECT p.data, p.relations, p.first_seen, m.seen_at, m.hidden_at
                   FROM prs p LEFT JOIN marks m ON m.id = p.id"""
            ).fetchall()
        return [
            (json.loads(d), set(json.loads(rel)), UserState(parse_ts(fs), parse_ts(seen), parse_ts(hidden)))
            for d, rel, fs, seen, hidden in rows
        ]

    def find_id(self, repo: str, number: int) -> str | None:
        with self._lock:
            for pid, data in self._db.execute("SELECT id, data FROM prs"):
                d = json.loads(data)
                if d.get("number") == number and d.get("repository", {}).get("nameWithOwner", "").lower() == repo.lower():
                    return pid
        return None

    # ---- user marks -----------------------------------------------------

    def _mark(self, pr_id: str, column: str, value: str | None) -> None:
        assert column in ("seen_at", "hidden_at")
        with self._lock:
            self._db.execute(
                f"INSERT INTO marks (id, {column}) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET {column}=excluded.{column}",
                (pr_id, value),
            )
            self._db.commit()

    def mark_seen(self, pr_id: str, now: datetime) -> None:
        self._mark(pr_id, "seen_at", iso(now))

    def hide(self, pr_id: str, now: datetime) -> None:
        self._mark(pr_id, "hidden_at", iso(now))

    def unhide(self, pr_id: str) -> None:
        self._mark(pr_id, "hidden_at", None)

    # ---- meta -----------------------------------------------------------

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self._db.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default
