"""多渠道撤回与替换后端 HTTP 接口。

鉴权：X-API-Token（演示令牌见迁移 002）。
数据范围：经销商仅本主体点位；代言团队仅本主体点位且响应剥离配方与消费者材料。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, FastAPI, Header, Query
from fastapi.responses import JSONResponse

from app import serializers
from app.auth import (
    CAN_APPROVE, CAN_DASHBOARD, CAN_HANDLE_ESCALATION, CAN_INGEST, CAN_ISSUE,
    CAN_OBSERVE, CAN_SCAN, CAN_SCOPE, CAN_VERIFY, Actor, authenticate,
)
from app.clock import clock
from app.db import all_rows, get_conn, one
from app.errors import DomainError, PermissionDenied
from app.idempotency import idempotent
from app.models import (
    EscalationAdvance, EscalationResolve, EvidenceIn, EvidenceVerify, IncidentCreate,
    IssueNoticesRequest, ManifestIngest, ObservationIn, ReleaseApprovalIn, ScopeRequest,
)
from app.services import incidents as incident_svc
from app.services import monitoring as monitor_svc
from app.services import workflow as wf

app = FastAPI(title="食品品牌宣传合规台 · 多渠道撤回与替换后端", version="2.0")


@app.exception_handler(DomainError)
def domain_error_handler(_request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def get_actor(
    conn: sqlite3.Connection = Depends(get_conn),
    x_api_token: str | None = Header(default=None, alias="X-API-Token"),
) -> Actor:
    return authenticate(conn, x_api_token)


def _scoped_placements(conn, actor: Actor, incident_id: str) -> list[sqlite3.Row]:
    rows = all_rows(
        conn,
        "SELECT * FROM placements WHERE incident_id=? ORDER BY first_seen_at, ref",
        (incident_id,),
    )
    if actor.is_internal:
        return rows
    return [r for r in rows if actor.owns_org(r["owner_org_type"], r["owner_org_id"])]


def _require_placement_access(conn: sqlite3.Connection, actor: Actor, incident_id: str, refs: list[str]) -> None:
    for ref in refs:
        p = wf.placement_by_ref(conn, ref)
        if p["incident_id"] != incident_id:
            from app.errors import ValidationFailed
            raise ValidationFailed(f"点位 {ref} 不属于该事件")
        actor.require_placement(p)


# ---------- 风险事件 ----------

@app.post("/v1/incidents", status_code=201)
def create_incident(
    body: IncidentCreate,
    conn: sqlite3.Connection = Depends(get_conn),
    actor: Actor = Depends(get_actor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    actor.require_role({"risk_owner"})

    def do() -> dict[str, Any]:
        row = incident_svc.create_incident(conn, body, actor.id)
        return serializers.incident_dict(row, actor)

    return idempotent(conn, idempotency_key, "incident", do)


@app.get("/v1/incidents/{ref}")
def get_incident(
    ref: str, conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    return serializers.incident_dict(incident_svc.get_incident(conn, ref), actor)


@app.post("/v1/incidents/{ref}/manifest", status_code=201)
def ingest_manifest(
    ref: str, body: ManifestIngest,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    actor.require_role(CAN_INGEST)
    incident = incident_svc.get_incident(conn, ref)

    def do() -> dict[str, Any]:
        result = incident_svc.ingest_manifest(conn, ref, body)
        return {
            "incident_ref": ref,
            "assets": [serializers.asset_dict(a, actor) for a in result["assets"]],
            "placements": [serializers.placement_dict(p, actor) for p in result["placements"]],
        }

    return idempotent(conn, idempotency_key, f"manifest:{incident['incident_id']}", do)


@app.get("/v1/incidents/{ref}/placements")
def list_placements(
    ref: str, conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    incident = incident_svc.get_incident(conn, ref)
    rows = _scoped_placements(conn, actor, incident["incident_id"])
    assets = {
        a["asset_id"]: a
        for a in conn.execute("SELECT * FROM assets WHERE incident_id=?", (incident["incident_id"],))
    }
    return {
        "incident_ref": ref,
        "placements": [
            {
                **serializers.placement_dict(p, actor),
                "channel_code": one(conn, "SELECT code FROM channels WHERE channel_id=?",
                                    (p["channel_id"],))["code"],
                "asset": serializers.asset_dict(assets[p["asset_id"]], actor),
            }
            for p in rows
        ],
    }


# ---------- 圈定与通知 ----------

@app.post("/v1/incidents/{ref}/scope")
def define_scope(
    ref: str, body: ScopeRequest,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require_role(CAN_SCOPE)
    return wf.scope(conn, ref, body, actor.id)


@app.post("/v1/incidents/{ref}/notices", status_code=201)
def issue_notices(
    ref: str, body: IssueNoticesRequest,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    actor.require_role(CAN_ISSUE)

    def do() -> dict[str, Any]:
        notices = wf.issue_notices(conn, ref, body, actor.id)
        return {"incident_ref": ref, "notices": notices}

    return idempotent(conn, idempotency_key, f"notices:{ref}", do)


@app.get("/v1/incidents/{ref}/notices")
def list_notices(
    ref: str, conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    incident = incident_svc.get_incident(conn, ref)
    rows = conn.execute(
        "SELECT n.* FROM notices n WHERE n.incident_id=? ORDER BY n.issued_at",
        (incident["incident_id"],),
    ).fetchall()
    out = []
    for n in rows:
        placement_rows = conn.execute(
            "SELECT p.ref, p.owner_org_type, p.owner_org_id FROM notice_placements np "
            "JOIN placements p ON np.placement_id=p.placement_id "
            "WHERE np.notice_id=? ORDER BY p.ref", (n["notice_id"],)
        ).fetchall()
        if not actor.is_internal:
            placement_rows = [
                p for p in placement_rows if actor.owns_org(p["owner_org_type"], p["owner_org_id"])
            ]
            if not placement_rows:
                continue
        d = dict(n)
        d.pop("notice_id", None)
        d.pop("incident_id", None)
        d["placement_refs"] = [p["ref"] for p in placement_rows]
        out.append(d)
    return {"incident_ref": ref, "notices": out}


# ---------- 结果凭证 ----------

@app.post("/v1/incidents/{ref}/evidence", status_code=201)
def submit_evidence(
    ref: str, body: EvidenceIn,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    # 平台回调/联系记录由品牌侧受理；截图与现场复核可由经销商/代言团队就自己点位提交
    actor.require_role(
        {"risk_owner", "legal", "field_auditor", "system", "dealer", "talent"}
    )
    incident = incident_svc.get_incident(conn, ref)
    if not actor.is_internal and body.kind in ("platform_callback", "contact_record"):
        raise PermissionDenied("平台回调与联系结果只能由品牌侧登记")
    refs = [c.placement_ref for c in body.coverage]
    if body.placement_ref:
        refs.append(body.placement_ref)
    if not actor.is_internal and refs:
        _require_placement_access(conn, actor, incident["incident_id"], refs)
    return wf.submit_evidence(conn, ref, body, actor.id)


@app.get("/v1/incidents/{ref}/evidence")
def list_evidence(
    ref: str, conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    incident = incident_svc.get_incident(conn, ref)
    rows = conn.execute(
        "SELECT * FROM evidences WHERE incident_id=? ORDER BY submitted_at",
        (incident["incident_id"],),
    ).fetchall()
    items = []
    for r in rows:
        if not actor.is_internal and r["placement_id"]:
            p = one(conn, "SELECT * FROM placements WHERE placement_id=?", (r["placement_id"],))
            if not actor.can_access_placement(p):
                continue
        items.append(serializers.evidence_dict(r, actor))
    return {"incident_ref": ref, "evidence": items}


@app.get("/v1/evidence/{evidence_ref}")
def get_evidence(
    evidence_ref: str, conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    row = wf.evidence_by_ref(conn, evidence_ref)
    if not actor.is_internal and row["placement_id"]:
        p = one(conn, "SELECT * FROM placements WHERE placement_id=?", (row["placement_id"],))
        actor.require_placement(p)
    coverage = conn.execute(
        "SELECT p.ref AS placement_ref, ec.claimed_state, ec.verified_state, "
        "a.ref AS claimed_replacement_ref "
        "FROM evidence_coverage ec "
        "JOIN placements p ON ec.placement_id=p.placement_id "
        "LEFT JOIN assets a ON ec.claimed_replacement_asset_id=a.asset_id "
        "WHERE ec.evidence_id=? ORDER BY p.ref", (row["evidence_id"],)
    ).fetchall()
    d = serializers.evidence_dict(row, actor)
    d.pop("evidence_id", None)
    d.pop("incident_id", None)
    d["coverage"] = [dict(c) for c in coverage]
    return d


@app.post("/v1/evidence/{evidence_ref}/verify")
def verify_evidence(
    evidence_ref: str, body: EvidenceVerify,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require_role(CAN_VERIFY)
    return wf.verify_evidence(conn, evidence_ref, body, actor.id)


# ---------- 替代素材独立放行 ----------

@app.post("/v1/assets/{asset_ref}/release", status_code=201)
def decide_release(
    asset_ref: str, body: ReleaseApprovalIn,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require_role(CAN_APPROVE)
    return wf.approve_release(conn, asset_ref, body, actor.id)


@app.get("/v1/assets/{asset_ref}/release")
def get_release(
    asset_ref: str, conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    asset = one(conn, "SELECT * FROM assets WHERE ref=?", (asset_ref,))
    if asset is None:
        from app.errors import NotFound
        raise NotFound(f"素材 {asset_ref} 不存在")
    rows = conn.execute(
        "SELECT ra.ref, ra.decision, ra.review_batch_ref, ra.basis_note, ra.decided_at, "
        "ra.valid_from, ra.valid_to, act.display_name AS reviewer_name "
        "FROM release_approvals ra JOIN actors act ON ra.reviewer_actor_id=act.actor_id "
        "WHERE ra.asset_id=? ORDER BY ra.decided_at DESC", (asset["asset_id"],)
    ).fetchall()
    return {"asset_ref": asset_ref, "approvals": [dict(r) for r in rows]}


# ---------- 升级 ----------

@app.get("/v1/escalations")
def list_escalations(
    status: str = Query(default="open"),
    type_: str | None = Query(default=None, alias="type"),
    incident_ref: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require_role(CAN_HANDLE_ESCALATION | CAN_DASHBOARD | {"field_auditor", "system"})
    sql = (
        "SELECT e.ref, i.ref AS incident_ref, e.type, e.level, e.status, e.reason, "
        "p.ref AS placement_ref, e.opened_at, e.resolved_at, e.resolution_note "
        "FROM escalations e JOIN incidents i ON e.incident_id=i.incident_id "
        "LEFT JOIN placements p ON e.placement_id=p.placement_id WHERE 1=1"
    )
    params: list[Any] = []
    if status != "all":
        sql += " AND e.status=?"
        params.append(status)
    if type_:
        sql += " AND e.type=?"
        params.append(type_)
    if incident_ref:
        sql += " AND i.ref=?"
        params.append(incident_ref)
    sql += " ORDER BY e.opened_at"
    return {"escalations": [dict(r) for r in conn.execute(sql, params)]}


@app.post("/v1/escalations/{escalation_ref}/advance")
def advance_escalation(
    escalation_ref: str, body: EscalationAdvance,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require_role(CAN_HANDLE_ESCALATION)
    return wf.advance_escalation(conn, escalation_ref, body, actor.id)


@app.post("/v1/escalations/{escalation_ref}/resolve")
def resolve_escalation(
    escalation_ref: str, body: EscalationResolve,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require_role(CAN_HANDLE_ESCALATION)
    return wf.resolve_escalation(conn, escalation_ref, body.resolution_note, actor.id)


# ---------- 持续监测 ----------

@app.post("/v1/observations", status_code=201)
def create_observation(
    body: ObservationIn,
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor),
) -> dict[str, Any]:
    actor.require_role(CAN_OBSERVE)
    return monitor_svc.record_observation(conn, body, actor.id)


@app.post("/v1/scan/overdue")
def scan_overdue(
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    actor.require_role(CAN_SCAN)
    return monitor_svc.scan_overdue(conn, actor.id)


@app.get("/v1/overdue")
def list_overdue(
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    actor.require_role(CAN_DASHBOARD | CAN_SCAN)
    items = monitor_svc.overdue_list(conn)
    return {"overdue": items, "count": len(items), "as_of": clock.now_ts()}


# ---------- 管理层视图 ----------

@app.get("/v1/dashboard/coverage")
def dashboard(
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    actor.require_role(CAN_DASHBOARD)
    return monitor_svc.coverage(conn)


@app.get("/v1/incidents/{ref}/coverage")
def incident_coverage(
    ref: str, conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    actor.require_role(CAN_DASHBOARD)
    incident_svc.get_incident(conn, ref)
    return monitor_svc.coverage(conn, ref)


@app.post("/v1/incidents/{ref}/close")
def close_incident(
    ref: str, conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    actor.require_role({"risk_owner"})
    return monitor_svc.close_incident(conn, ref, actor.id)


# ---------- 全链路追溯 ----------

@app.get("/v1/incidents/{ref}/trace")
def trace(
    ref: str, conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    """从最初风险通知追到每个渠道点位实际下架/替换的时间。"""

    incident = incident_svc.get_incident(conn, ref)
    incident_id = incident["incident_id"]
    placements = _scoped_placements(conn, actor, incident_id)

    placement_out = []
    for p in placements:
        notice_rows = conn.execute(
            "SELECT n.* FROM notices n JOIN notice_placements np ON n.notice_id=np.notice_id "
            "WHERE np.placement_id=? ORDER BY n.issued_at", (p["placement_id"],)
        ).fetchall()
        notices = []
        for n in notice_rows:
            nd = {k: n[k] for k in ("ref", "required_action", "round", "delivery_channel",
                                    "issued_at", "due_at", "status", "completed_at",
                                    "contact_attempts")}
            notices.append(nd)
        evidence_rows = conn.execute(
            "SELECT e.* FROM evidences e WHERE e.placement_id=? "
            "OR e.notice_id IN (SELECT notice_id FROM notice_placements WHERE placement_id=?) "
            "ORDER BY e.submitted_at", (p["placement_id"], p["placement_id"])
        ).fetchall()
        ev_out = []
        for e in evidence_rows:
            cov = conn.execute(
                "SELECT p.ref AS placement_ref, ec.claimed_state, ec.verified_state "
                "FROM evidence_coverage ec JOIN placements p ON ec.placement_id=p.placement_id "
                "WHERE ec.evidence_id=?", (e["evidence_id"],)
            ).fetchall()
            ed = {k: e[k] for k in ("ref", "kind", "status", "source_system", "submitted_at",
                                    "verified_at", "verify_note", "contact_result")}
            if actor.sees_sensitive:
                ed["summary"] = e["summary"]
            ed["coverage"] = [dict(c) for c in cov if actor.is_internal
                              or c["placement_ref"] == p["ref"]]
            ev_out.append(ed)
        esc_rows = conn.execute(
            "SELECT ref, type, level, status, reason, opened_at, resolved_at, resolution_note "
            "FROM escalations WHERE placement_id=? ORDER BY opened_at", (p["placement_id"],)
        ).fetchall()
        placement_out.append({
            **serializers.placement_dict(p, actor),
            "notices": notices,
            "evidence": ev_out,
            "escalations": [dict(x) for x in esc_rows],
        })

    timeline = monitor_svc.timeline(conn, ref)
    if not actor.is_internal:
        # 外部主体只能看到与自己点位相关的事件链；内部评审/圈定事件不外泄
        visible_placement_ids = {p["placement_id"] for p in placements}
        visible_notice_ids = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT np.notice_id FROM notice_placements np WHERE np.placement_id IN ({})".format(
                    ",".join("?" * len(visible_placement_ids))
                ),
                list(visible_placement_ids),
            ).fetchall()
        }
        visible_evidence_ids = {
            r[0] for r in conn.execute(
                "SELECT e.evidence_id FROM evidences e WHERE e.placement_id IN ({}) "
                "OR e.notice_id IN ({})".format(
                    ",".join("?" * len(visible_placement_ids)) or "''",
                    ",".join("?" * len(visible_notice_ids)) or "''",
                ),
                [*visible_placement_ids, *visible_notice_ids],
            ).fetchall()
        }
        visible_esc_ids = {
            r[0] for r in conn.execute(
                "SELECT escalation_id FROM escalations WHERE placement_id IN ({})".format(
                    ",".join("?" * len(visible_placement_ids)) or "''"
                ),
                list(visible_placement_ids),
            ).fetchall()
        }
        ref_sets = {
            "placement": {p["ref"] for p in placements},
            "notice": {
                r[0] for r in conn.execute(
                    "SELECT ref FROM notices WHERE notice_id IN ({})".format(
                        ",".join("?" * len(visible_notice_ids)) or "''"
                    ), list(visible_notice_ids),
                )
            },
            "evidence": {
                r[0] for r in conn.execute(
                    "SELECT ref FROM evidences WHERE evidence_id IN ({})".format(
                        ",".join("?" * len(visible_evidence_ids)) or "''"
                    ), list(visible_evidence_ids),
                )
            },
            "escalation": {
                r[0] for r in conn.execute(
                    "SELECT ref FROM escalations WHERE escalation_id IN ({})".format(
                        ",".join("?" * len(visible_esc_ids)) or "''"
                    ), list(visible_esc_ids),
                )
            },
        }
        allowed_incident_events = {"incident.opened"}
        timeline = [
            e for e in timeline
            if (e["entity_type"] in ref_sets and e["entity_ref"] in ref_sets[e["entity_type"]])
            or (e["entity_type"] == "incident" and e["event_type"] in allowed_incident_events)
        ]

    return {
        "incident": serializers.incident_dict(incident, actor),
        "placements": placement_out,
        "timeline": timeline,
    }


@app.get("/v1/channels")
def list_channels(
    conn: sqlite3.Connection = Depends(get_conn), actor: Actor = Depends(get_actor)
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT c.code, c.name, c.owner_org_type, c.owner_org_id, a.display_name AS owner_name "
        "FROM channels c LEFT JOIN actors a ON c.owner_actor_id=a.actor_id ORDER BY c.code"
    ).fetchall()
    return {"channels": [dict(r) for r in rows]}
