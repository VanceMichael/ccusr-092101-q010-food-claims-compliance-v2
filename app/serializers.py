"""序列化与数据范围投影：代言团队视图剥离配方与消费者材料。"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.auth import Actor

_ASSET_PUBLIC = ("asset_id", "ref", "kind", "derived_from_asset_id", "transform_note", "status")
_PLACEMENT_PUBLIC = (
    "placement_id", "ref", "asset_id", "channel_id", "account_id", "locator",
    "owner_org_type", "owner_org_id", "status", "required_action",
    "replacement_asset_id", "deadline", "round", "current_notice_id",
    "first_seen_at", "notified_at", "resolved_at", "last_observed_at", "overdue_flag",
)


def _filter(row: sqlite3.Row, columns: tuple[str, ...]) -> dict[str, Any]:
    data = dict(row)
    return {k: data.get(k) for k in columns}


def incident_dict(row: sqlite3.Row, actor: Actor) -> dict[str, Any]:
    d = dict(row)
    if not actor.sees_sensitive:
        # 代言团队看不到配方
        d["formula_revision"] = None
    return d


def asset_dict(row: sqlite3.Row, actor: Actor) -> dict[str, Any]:
    d = _filter(row, _ASSET_PUBLIC)
    if not actor.sees_sensitive:
        # 代言团队看不到消费者材料（不暴露其存在与摘要，只保留素材引用）
        d["consumer_material"] = None
        # 派生说明可能直接写出改字内容（涉及消费者材料文案），代言侧也剥离
        d["transform_note"] = None
    else:
        d["consumer_material"] = row["consumer_material"]
    # sha256 是内容指纹而非材料内容，处置方需要它核对目标，始终可见
    d["sha256"] = row["sha256"]
    return d


def placement_dict(row: sqlite3.Row, actor: Actor) -> dict[str, Any]:
    return _filter(row, _PLACEMENT_PUBLIC)


def evidence_dict(row: sqlite3.Row, actor: Actor) -> dict[str, Any]:
    d = dict(row)
    if not actor.sees_sensitive:
        d.pop("summary", None)
        d.pop("attachment_sha256", None)
    return d


def timeline_item(row: sqlite3.Row) -> dict[str, Any]:
    import json

    return {
        "event_type": row["event_type"],
        "entity_type": row["entity_type"],
        "entity_ref": row["entity_ref"],
        "actor_id": row["actor_id"],
        "source_system": row["source_system"],
        "source_sequence": row["source_sequence"],
        "occurred_at": row["occurred_at"],
        "payload": json.loads(row["payload_json"]) if row["payload_json"] else None,
    }
