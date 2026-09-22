"""事件与传播清单：原始素材、派生件、投放账号、渠道负责人、可核验点位。"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.clock import clock, normalize_ts
from app.db import one
from app.errors import NotFound, ValidationFailed
from app.models import IncidentCreate, ManifestIngest
from app.refs import asset_ref, incident_ref, new_id, placement_ref
from app.services.audit import log_event


def get_incident(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = one(conn, "SELECT * FROM incidents WHERE ref = ?", (ref,))
    if row is None:
        raise NotFound(f"事件 {ref} 不存在")
    return row


def create_incident(conn: sqlite3.Connection, data: IncidentCreate, actor_id: str) -> sqlite3.Row:
    # 同一来源系统 + 序列号的风险通知只产生一个事件（来源对事实负责）
    duplicate = one(
        conn,
        "SELECT * FROM incidents WHERE source_system = ? AND source_sequence = ?",
        (data.source_system, data.source_sequence),
    )
    if duplicate is not None:
        return duplicate

    incident_id = new_id()
    ref = incident_ref(conn)
    conn.execute(
        "INSERT INTO incidents(incident_id, ref, title, product_ref, formula_revision, "
        "risk_type, source_system, source_sequence, detail_ref, status, risk_owner_id, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            incident_id, ref, data.title, data.product_ref, data.formula_revision,
            data.risk_type, data.source_system, data.source_sequence, data.detail_ref,
            "open", actor_id, clock.now_ts(),
        ),
    )
    row = get_incident(conn, ref)
    log_event(
        conn, incident_id, "incident.opened", entity_type="incident", entity_ref=ref,
        actor_id=actor_id, source_system=data.source_system, source_sequence=data.source_sequence,
        payload={"title": data.title, "risk_type": data.risk_type, "product_ref": data.product_ref},
    )
    return row


def ingest_manifest(conn: sqlite3.Connection, incident_ref_: str, data: ManifestIngest) -> dict[str, Any]:
    incident = get_incident(conn, incident_ref_)
    incident_id = incident["incident_id"]
    now = clock.now_ts()

    # 1) 素材（含裁剪/改字派生件）
    key_to_asset_id: dict[str, str] = {}
    asset_rows: list[sqlite3.Row] = []
    for item in data.assets:
        if item.kind == "derivative" and not item.derived_from_client_key:
            raise ValidationFailed(f"素材 {item.client_key} 为派生件，必须指定 derived_from_client_key")
        existing = one(
            conn,
            "SELECT * FROM assets WHERE incident_id = ? AND sha256 = ?",
            (incident_id, item.sha256),
        )
        if existing is not None:
            key_to_asset_id[item.client_key] = existing["asset_id"]
            asset_rows.append(existing)
            continue
        parent_id: str | None = None
        if item.derived_from_client_key:
            parent_id = key_to_asset_id.get(item.derived_from_client_key)
            if parent_id is None:
                raise ValidationFailed(
                    f"素材 {item.client_key} 的父素材 {item.derived_from_client_key} 必须在清单中先出现"
                )
        asset_id = new_id()
        ref = asset_ref(conn)
        conn.execute(
            "INSERT INTO assets(asset_id, ref, incident_id, kind, derived_from_asset_id, sha256, "
            "transform_note, consumer_material, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                asset_id, ref, incident_id, item.kind, parent_id, item.sha256,
                item.transform_note, 1 if item.consumer_material else 0,
                "in_circulation", now,
            ),
        )
        key_to_asset_id[item.client_key] = asset_id
        row = one(conn, "SELECT * FROM assets WHERE asset_id = ?", (asset_id,))
        asset_rows.append(row)
        log_event(
            conn, incident_id, "asset.registered", entity_type="asset", entity_ref=ref,
            payload={"kind": item.kind, "sha256": item.sha256,
                     "derivative": bool(parent_id), "consumer_material": item.consumer_material},
        )

    # 2) 点位（可核验展示位置），渠道负责人来自渠道字典
    placement_rows: list[sqlite3.Row] = []
    for item in data.placements:
        channel = one(conn, "SELECT * FROM channels WHERE code = ?", (item.channel_code,))
        if channel is None:
            raise ValidationFailed(f"未知渠道 {item.channel_code}")
        asset_id = key_to_asset_id.get(item.asset_client_key)
        if asset_id is None:
            raise ValidationFailed(f"点位 {item.client_key} 引用了清单中不存在的素材 {item.asset_client_key}")

        account_id = None
        if item.account_ref:
            account = one(
                conn,
                "SELECT * FROM accounts WHERE channel_id = ? AND account_ref = ?",
                (channel["channel_id"], item.account_ref),
            )
            if account is None:
                # 账号归属随渠道负责人：直营/线下归品牌，经销商号归经销商，明星号归代言团队
                conn.execute(
                    "INSERT INTO accounts(account_id, channel_id, account_ref, org_type, org_id) "
                    "VALUES (?,?,?,?,?)",
                    (new_id(), channel["channel_id"], item.account_ref,
                     channel["owner_org_type"], channel["owner_org_id"]),
                )
                account = one(
                    conn, "SELECT * FROM accounts WHERE channel_id = ? AND account_ref = ?",
                    (channel["channel_id"], item.account_ref),
                )
            account_id = account["account_id"]

        existing = one(
            conn,
            "SELECT * FROM placements WHERE incident_id = ? AND channel_id = ? AND locator = ?",
            (incident_id, channel["channel_id"], item.locator),
        )
        if existing is not None:
            placement_rows.append(existing)
            continue

        placement_id = new_id()
        ref = placement_ref(conn)
        first_seen = normalize_ts(item.first_seen_at) or now
        conn.execute(
            "INSERT INTO placements(placement_id, ref, incident_id, asset_id, channel_id, account_id, "
            "locator, owner_org_type, owner_org_id, status, first_seen_at, last_observed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                placement_id, ref, incident_id, asset_id, channel["channel_id"], account_id,
                item.locator, channel["owner_org_type"], channel["owner_org_id"],
                "identified", first_seen, first_seen,
            ),
        )
        row = one(conn, "SELECT * FROM placements WHERE placement_id = ?", (placement_id,))
        placement_rows.append(row)
        log_event(
            conn, incident_id, "placement.listed", entity_type="placement", entity_ref=ref,
            payload={"channel_code": item.channel_code, "locator": item.locator,
                     "account_ref": item.account_ref, "asset_sha256": _sha(conn, asset_id)},
        )

    return {"assets": asset_rows, "placements": placement_rows}


def _sha(conn: sqlite3.Connection, asset_id: str) -> str | None:
    row = one(conn, "SELECT sha256 FROM assets WHERE asset_id = ?", (asset_id,))
    return row["sha256"] if row else None
