"""Append-only local audit log, independent of the target server's own audit_log.

See docs/01-implementation-plan.md section 4. Every API call attempt - reads
included, dry-run or not - gets one JSONL line. Never overwrite or delete a line
once written (AGENTS.md rule 8).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_correlation_id() -> str:
    """A locally generated id independent of any server-issued operation_id.

    Lets us trace a call even if the request never reached the server (e.g. a
    network failure before any response, including an operation_id, came back).
    """
    return str(uuid.uuid4())


@dataclass(frozen=True)
class AuditRecord:
    correlation_id: str
    timestamp: str
    resource: str | None
    operation: str | None
    mode: str | None
    method: str
    url: str
    request_payload: Any
    dry_run: bool
    status_code: int | None = None
    response_payload: Any = None
    error: str | None = None
    operation_id: str | None = None

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "correlation_id": self.correlation_id,
                "timestamp": self.timestamp,
                "resource": self.resource,
                "operation": self.operation,
                "mode": self.mode,
                "method": self.method,
                "url": self.url,
                "request_payload": self.request_payload,
                "dry_run": self.dry_run,
                "status_code": self.status_code,
                "response_payload": self.response_payload,
                "error": self.error,
                "operation_id": self.operation_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )


class AuditLog:
    """Writes one JSONL file per UTC calendar day under log_dir."""

    def __init__(self, log_dir: str | Path) -> None:
        self.log_dir = Path(log_dir)

    def _current_log_path(self) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.log_dir / f"{day}.jsonl"

    def record(
        self,
        *,
        correlation_id: str,
        method: str,
        url: str,
        request_payload: Any = None,
        dry_run: bool = False,
        resource: str | None = None,
        operation: str | None = None,
        mode: str | None = None,
        status_code: int | None = None,
        response_payload: Any = None,
        error: str | None = None,
        operation_id: str | None = None,
    ) -> AuditRecord:
        record = AuditRecord(
            correlation_id=correlation_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            resource=resource,
            operation=operation,
            mode=mode,
            method=method,
            url=url,
            request_payload=request_payload,
            dry_run=dry_run,
            status_code=status_code,
            response_payload=response_payload,
            error=error,
            operation_id=operation_id,
        )
        path = self._current_log_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(record.to_json_line())
            f.write("\n")
        return record
