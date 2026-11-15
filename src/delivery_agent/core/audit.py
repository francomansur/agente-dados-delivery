"""Security layer 4: one JSON line for every SQL that reaches the executor and each step of a write."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

Source = Literal["api", "eval", "mcp"]
AuditStatus = Literal["ok", "rejected", "error", "timeout"]
# blocked: the rules refused the request; rejected: the person said no; write_failed: the database did.
ActionEvent = Literal["proposed", "blocked", "approved", "rejected", "written", "write_failed"]


class QueryContext(BaseModel):
    """Who asked for a query and why, so that the audit log can tell requests apart."""

    request_id: str
    source: Source
    question: str | None = None


class ActionContext(QueryContext):
    """The request of a write action plus the action's own id, which the approval refers to."""

    action_id: str


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


class ActionRecord(BaseModel):
    """One step of a write action, in the same log as the SQL: from the proposal to the write."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    request_id: str
    source: Source
    question: str | None
    action_id: str
    action: Literal["register_review"]
    event: ActionEvent
    order_id: int | None
    rating: int | None
    comment: str | None
    error: str | None


class AuditLog:
    """Append-only JSON Lines file, safe to share between threads and between instances."""

    _lock = threading.Lock()  # one for the whole process: the SQL tools and the review actions share the file

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, record: AuditRecord | ActionRecord) -> None:
        """Append one record to the log, creating the folder if needed.

        Args:
            record: The execution or the step of a write action to log.
        """
        line = record.model_dump_json()
        self.path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock, self.path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
