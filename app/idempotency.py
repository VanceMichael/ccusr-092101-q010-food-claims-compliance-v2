"""写接口幂等：相同 Idempotency-Key 回放首次结果。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import Any

from app.clock import clock
from app.db import one


def idempotent(conn: sqlite3.Connection, key: str | None, result_ref: str, fn: Callable[[], Any]) -> Any:
    if not key:
        return fn()
    existing = one(conn, "SELECT result_json FROM idempotency WHERE idempotency_key=?", (key,))
    if existing is not None:
        return json.loads(existing["result_json"])
    result = fn()
    ref = result.get("ref") if isinstance(result, dict) else result_ref
    conn.execute(
        "INSERT INTO idempotency(idempotency_key, result_ref, result_json, created_at) "
        "VALUES (?,?,?,?)",
        (key, ref or result_ref, json.dumps(result, ensure_ascii=False), clock.now_ts()),
    )
    return result
