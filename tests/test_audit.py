"""The audit log on its own; test_sql_tools.py checks that every execution reaches it."""

import json
from pathlib import Path

from delivery_agent.core.audit import AuditLog, AuditRecord


def test_records_are_appended_as_json_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "audit.jsonl"
    audit_log = AuditLog(log_path)
    common = {"request_id": "req-1", "source": "api", "question": "Quantos pedidos há?"}

    audit_log.record(
        AuditRecord(**common, sql="SELECT 1", status="ok", duration_ms=1.5, row_count=1, error=None)
    )
    audit_log.record(
        AuditRecord(
            **common, sql="DELETE FROM orders", status="rejected", duration_ms=0.1, row_count=None, error="x"
        )
    )

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

    assert [record["status"] for record in records] == ["ok", "rejected"]
    assert records[0]["timestamp"].endswith("Z")
    assert records[1]["row_count"] is None
