"""全链路事件日志：保留来源系统对事实的责任边界。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.clock import clock


def log_event(
    conn: sqlite3.Connection,
    incident_id: str,
    event_type: str,
    *,
    entity_type: str | None = None,
    entity_ref: str | None = None,
    actor_id: str | None = None,
    source_system: str | None = None,
    source_sequence: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO event_log(incident_id, event_type, entity_type, entity_ref, actor_id, "
        "source_system, source_sequence, occurred_at, payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            incident_id,
            event_type,
            entity_type,
            entity_ref,
            actor_id,
            source_system,
            source_sequence,
            clock.now_ts(),
            json.dumps(payload, ensure_ascii=False) if payload is not None else None,
        ),
    )
