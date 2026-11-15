"""Security layer 4: one JSON line for every SQL that reaches the executor."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["api", "eval", "mcp"]
AuditStatus = Literal["ok", "rejected", "error", "timeout"]


class QueryContext(BaseModel):
    """Who asked for a query and why, so that the audit log can tell requests apart."""

    request_id: str
    source: Source
    question: str | None = None


class AuditRecord(BaseModel):
    """One line of the audit log."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    request_id: str
    source: Source
    question: str | None
    sql: str
    status: AuditStatus
    duration_ms: float
    row_count: int | None
    error: str | None


class AuditLog:
    """Append-only JSON Lines file, safe to share between threads."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, record: AuditRecord) -> None:
        """Append one record to the log, creating the folder if needed.

        Args:
            record: The execution to log.
        """
        line = record.model_dump_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock, self.path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
