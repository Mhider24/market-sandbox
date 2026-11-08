"""Local decision and order history for a single paper credential scope."""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


class AuditUnavailable(Exception):
    pass


class PaperAudit:
    def __init__(self, path: Path, scope: str):
        self.path = path
        self.scope = scope

    def record(self, event: str, summary: str, detail: dict | None = None) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path)) as connection, connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS paper_events (
                       id INTEGER PRIMARY KEY,
                       scope TEXT NOT NULL,
                       timestamp TEXT NOT NULL,
                       event TEXT NOT NULL,
                       summary TEXT NOT NULL,
                       detail TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    "INSERT INTO paper_events (scope, timestamp, event, summary, detail) VALUES (?, ?, ?, ?, ?)",
                    (self.scope, datetime.now(timezone.utc).isoformat(), event, summary,
                     json.dumps(detail or {}, allow_nan=False)),
                )
        except (OSError, sqlite3.Error, ValueError):
            raise AuditUnavailable("Paper journal could not save this event.") from None

    def recent(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with closing(sqlite3.connect(self.path)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    """SELECT timestamp, event, summary, detail FROM paper_events
                       WHERE scope = ? ORDER BY id DESC LIMIT 30""",
                    (self.scope,),
                ).fetchall()
                return [{**dict(row), "detail": json.loads(row["detail"])} for row in rows]
        except (OSError, sqlite3.Error, ValueError):
            raise AuditUnavailable("Paper journal could not load recent events.") from None
