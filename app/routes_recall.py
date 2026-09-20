"""多渠道撤回与替换 HTTP 接口。

所有写操作都是业务事件：请求体必须携带 source_system 与 source_seq，
服务端按 (来源, 序列号, 动作) 幂等，重复投递返回首次处理结果。
"""

from typing import Callable

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from . import db, services
from .auth import Actor, get_actor, require_role

router = APIRouter(prefix="/recall", tags=["recall"])


class Event(BaseModel):
    source_system: str = Field(min_length=1)
    source_seq: str = Field(min_length=1)


class MaterialIn(Event):
    material_ref: str
    fingerprint_sha256: str
    parent_ref: str | None = None
    derivative_kind: str = "original"
    material_class: str = "campaign"


class PropagationEntryIn(Event):
    entry_ref: str
    material_ref: str
    channel_code: str
    account_ref: str
    channel_owner_ref: str
    locations: list[str]
    discovered_at: str | None = None


class CaseIn(Event):
    case_ref: str
    risk_notice_ref: str
    reason: str
    material_refs: list[str]
    deadline_at: str
    ack_deadline_at: str
    requires_replacement: bool = False


class EvidenceIn(Event):
    evidence_type: str
    submitted_by: str | None = None
    callback_id: str | None = None
    locations_cleared: list[str] = []
    attachment_sha256: str | None = None
    final: bool = False


class EscalateIn(Event):
    kind: str
    detail: str = ""


class ResolveIn(Event):
    detail: str = ""


class CredentialIn(Event):
    credential_ref: str
    material_ref: str
    expires_at: str | None = None


class ReplacementIn(Event):
    material_ref: str
    credential_ref: str


class SightingIn(Event):
    fingerprint_sha256: str
    seen_at: str
    channel_code: str | None = None
    account_ref: str | None = None
    location_ref: str | None = None


def _run(action: str, event: Event, produce: Callable) -> dict:
    """在单事务内执行幂等业务事件，produce 接收数据库连接。"""
    conn = db.connect()
    try:
        with conn:
            result, duplicated = services.run_idempotent(
                conn, event.source_system, event.source_seq, action,
                lambda: produce(conn),
            )
        return {"deduplicated": duplicated, **result}
    finally:
        conn.close()


def _read(read: Callable) -> dict:
    conn = db.connect()
    try:
        return read(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- 传播清单

@router.post("/materials", status_code=201)
def register_material(payload: MaterialIn, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner", "system")
    return _run("register_material", payload, lambda conn: services.register_material(
        conn, actor, payload.material_ref, payload.fingerprint_sha256,
        parent_ref=payload.parent_ref, derivative_kind=payload.derivative_kind,
        material_class=payload.material_class,
    ))


@router.get("/materials/{material_ref}")
def get_material(material_ref: str, actor: Actor = Depends(get_actor)):
    return _read(lambda conn: services.get_material_view(conn, actor, material_ref))


@router.post("/propagation-entries", status_code=201)
def list_propagation_entry(payload: PropagationEntryIn, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner", "system")
    return _run("list_propagation_entry", payload, lambda conn: services.list_propagation_entry(
        conn, actor, payload.entry_ref, payload.material_ref, payload.channel_code,
        payload.account_ref, payload.channel_owner_ref, payload.locations,
        discovered_at=payload.discovered_at,
    ))


# ---------------------------------------------------------------- 事件与通知

@router.post("/cases", status_code=201)
def open_case(payload: CaseIn, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner")
    return _run("open_case", payload, lambda conn: services.open_case(
        conn, actor, payload.case_ref, payload.risk_notice_ref, payload.reason,
        payload.material_refs, payload.deadline_at, payload.ack_deadline_at,
        requires_replacement=payload.requires_replacement,
    ))


@router.post("/cases/{case_ref}/issue-notices", status_code=201)
def issue_notices(case_ref: str, payload: Event, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner")
    return _run("issue_notices", payload,
                lambda conn: services.issue_notices(conn, actor, case_ref))


@router.post("/cases/{case_ref}/sweep")
def sweep_case(case_ref: str, payload: Event, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner", "system")
    return _run("sweep_case", payload,
                lambda conn: services.sweep_case(conn, actor, case_ref))


@router.get("/cases/{case_ref}/manifest")
def get_manifest(case_ref: str, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner", "management", "endorsement_team", "channel_owner")
    return _read(lambda conn: services.get_manifest(conn, actor, case_ref))


@router.get("/cases/{case_ref}/trace")
def case_trace(case_ref: str, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner", "management")
    return _read(lambda conn: services.case_trace(conn, actor, case_ref))


# ---------------------------------------------------------------- 处置任务

@router.get("/notices")
def list_notices(case_ref: str | None = None, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner", "management", "channel_owner")
    return _read(lambda conn: services.list_notices(conn, actor, case_ref))


@router.post("/notices/{notice_no}/ack")
def acknowledge_notice(notice_no: str, payload: Event, actor: Actor = Depends(get_actor)):
    require_role(actor, "channel_owner", "risk_owner")
    return _run("acknowledge_notice", payload,
                lambda conn: services.acknowledge_notice(conn, actor, notice_no))


@router.post("/notices/{notice_no}/refuse")
def refuse_notice(notice_no: str, payload: ResolveIn, actor: Actor = Depends(get_actor)):
    require_role(actor, "channel_owner")
    return _run("refuse_notice", payload,
                lambda conn: services.refuse_notice(conn, actor, notice_no, payload.detail))


@router.post("/notices/{notice_no}/evidence", status_code=201)
def submit_evidence(notice_no: str, payload: EvidenceIn, actor: Actor = Depends(get_actor)):
    require_role(actor, "channel_owner", "risk_owner", "system")
    return _run(f"submit_evidence:{notice_no}", payload, lambda conn: services.submit_evidence(
        conn, actor, notice_no, payload.evidence_type,
        submitted_by=payload.submitted_by, callback_id=payload.callback_id,
        locations_cleared=payload.locations_cleared,
        attachment_sha256=payload.attachment_sha256, final=payload.final,
    ))


@router.post("/notices/{notice_no}/escalate", status_code=201)
def escalate_notice(notice_no: str, payload: EscalateIn, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner")
    return _run("escalate_notice", payload,
                lambda conn: services.escalate_notice(conn, actor, notice_no, payload.kind, payload.detail))


@router.post("/notices/{notice_no}/replacement")
def register_replacement(notice_no: str, payload: ReplacementIn, actor: Actor = Depends(get_actor)):
    require_role(actor, "channel_owner", "risk_owner")
    return _run("register_replacement", payload, lambda conn: services.register_replacement(
        conn, actor, notice_no, payload.material_ref, payload.credential_ref,
    ))


# ---------------------------------------------------------------- 升级与凭证

@router.post("/escalations/{escalation_ref}/resolve")
def resolve_escalation(escalation_ref: str, payload: ResolveIn, actor: Actor = Depends(get_actor)):
    require_role(actor, "risk_owner")
    return _run("resolve_escalation", payload,
                lambda conn: services.resolve_escalation(conn, actor, escalation_ref, payload.detail))


@router.post("/credentials", status_code=201)
def issue_credential(payload: CredentialIn, actor: Actor = Depends(get_actor)):
    require_role(actor, "reviewer")
    return _run("issue_credential", payload, lambda conn: services.issue_credential(
        conn, actor, payload.credential_ref, payload.material_ref, expires_at=payload.expires_at,
    ))


# ---------------------------------------------------------------- 再传播与看板

@router.post("/sightings", status_code=201)
def report_sighting(payload: SightingIn, actor: Actor = Depends(get_actor)):
    return _run("report_sighting", payload, lambda conn: services.report_sighting(
        conn, actor, payload.fingerprint_sha256, payload.seen_at,
        channel_code=payload.channel_code, account_ref=payload.account_ref,
        location_ref=payload.location_ref,
    ))


@router.get("/dashboard/coverage")
def dashboard_coverage(case_ref: str | None = None, actor: Actor = Depends(get_actor)):
    require_role(actor, "management", "risk_owner")
    return _read(lambda conn: services.dashboard_coverage(conn, actor, case_ref))
