"""多渠道撤回与替换的核心业务规则。

关键约束：
- 处置通知带唯一编号，按传播清单逐点位签发；
- 任务关闭需要至少两类独立来源的有效结果（平台回调/人工截图/现场复核），
  重复回调只记录、不计入，不能提前关闭任务；
- 无法联系 / 拒绝下架 / 旧图再次出现 / 只替换部分页面 分别进入四条升级路径，
  存在未结升级时任务不得关闭；
- 替代素材必须引用独立审核签发的放行凭证，旧素材已撤回不代表替代素材合规；
- 已撤回内容再次出现时，相关任务与事件自动重开并进入“旧图再次出现”升级。
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from .auth import Actor, can_view_material

EVIDENCE_TYPES = {"platform_callback", "manual_screenshot", "field_recheck"}
ESCALATION_KINDS = {"unreachable", "refused", "reappeared", "partial_replacement"}
DERIVATIVE_KINDS = {"original", "cropped", "reworded", "reformatted"}
# 关闭所需：至少两类有效结果、至少两个不同提交人
MIN_EVIDENCE_TYPES = 2
MIN_SUBMITTERS = 2


# ---------------------------------------------------------------- 基础工具

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field} 不是合法时间：{value}") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail=f"{field} 必须带时区偏移量")
    return parsed


def new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def audit(
    conn: sqlite3.Connection,
    actor: Actor,
    action: str,
    case_ref: str | None = None,
    notice_no: str | None = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_events(case_ref, notice_no, actor_ref, action, detail_json, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (case_ref, notice_no, actor.ref, action, json.dumps(detail or {}, ensure_ascii=False), now_iso()),
    )


def run_idempotent(conn: sqlite3.Connection, source_system: str, source_seq: str, action: str, produce):
    """业务事件按 (来源, 序列号, 动作) 幂等：重复事件直接返回首次结果。"""
    row = conn.execute(
        "SELECT result_json FROM idempotency_keys WHERE source_system=? AND source_seq=? AND action=?",
        (source_system, source_seq, action),
    ).fetchone()
    if row:
        return json.loads(row["result_json"]), True
    result = produce()
    conn.execute(
        "INSERT INTO idempotency_keys(source_system, source_seq, action, result_json, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (source_system, source_seq, action, json.dumps(result, ensure_ascii=False), now_iso()),
    )
    return result, False


def _get_case(conn: sqlite3.Connection, case_ref: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM recall_cases WHERE case_ref=?", (case_ref,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"撤回事件不存在：{case_ref}")
    return row


def _get_task(conn: sqlite3.Connection, notice_no: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM disposal_tasks WHERE notice_no=?", (notice_no,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"处置通知不存在：{notice_no}")
    return row


def _get_material(conn: sqlite3.Connection, material_ref: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM materials WHERE material_ref=?", (material_ref,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"素材不存在：{material_ref}")
    return row


# ---------------------------------------------------------------- 传播清单

def register_material(
    conn: sqlite3.Connection,
    actor: Actor,
    material_ref: str,
    fingerprint_sha256: str,
    parent_ref: str | None = None,
    derivative_kind: str = "original",
    material_class: str = "campaign",
) -> dict:
    if derivative_kind not in DERIVATIVE_KINDS:
        raise HTTPException(status_code=422, detail=f"未知派生类型：{derivative_kind}")
    if parent_ref:
        _get_material(conn, parent_ref)
    if derivative_kind != "original" and not parent_ref:
        raise HTTPException(status_code=422, detail="派生件必须指明原始素材 parent_ref")
    existing = conn.execute("SELECT material_ref FROM materials WHERE material_ref=?", (material_ref,)).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail=f"素材已存在：{material_ref}")
    conn.execute(
        "INSERT INTO materials(material_ref, parent_ref, derivative_kind, material_class, fingerprint_sha256, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (material_ref, parent_ref, derivative_kind, material_class, fingerprint_sha256, now_iso()),
    )
    audit(conn, actor, "material_registered", detail={"material_ref": material_ref, "derivative_kind": derivative_kind})
    return {"material_ref": material_ref, "status": "active"}


def list_propagation_entry(
    conn: sqlite3.Connection,
    actor: Actor,
    entry_ref: str,
    material_ref: str,
    channel_code: str,
    account_ref: str,
    channel_owner_ref: str,
    locations: list[str],
    discovered_at: str | None = None,
) -> dict:
    _get_material(conn, material_ref)
    if not locations:
        raise HTTPException(status_code=422, detail="传播点位必须给出可核验展示位置")
    if discovered_at:
        parse_ts(discovered_at, "discovered_at")
    existing = conn.execute("SELECT entry_ref FROM propagation_entries WHERE entry_ref=?", (entry_ref,)).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail=f"传播点位已存在：{entry_ref}")
    conn.execute(
        "INSERT INTO propagation_entries(entry_ref, material_ref, channel_code, account_ref,"
        " channel_owner_ref, locations_json, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entry_ref, material_ref, channel_code, account_ref, channel_owner_ref,
         json.dumps(locations, ensure_ascii=False), discovered_at or now_iso()),
    )
    audit(conn, actor, "propagation_listed", detail={"entry_ref": entry_ref, "material_ref": material_ref})
    return {"entry_ref": entry_ref, "status": "listed"}


# ---------------------------------------------------------------- 撤回事件与通知签发

def open_case(
    conn: sqlite3.Connection,
    actor: Actor,
    case_ref: str,
    risk_notice_ref: str,
    reason: str,
    material_refs: list[str],
    deadline_at: str,
    ack_deadline_at: str,
    requires_replacement: bool = False,
) -> dict:
    deadline = parse_ts(deadline_at, "deadline_at")
    ack_deadline = parse_ts(ack_deadline_at, "ack_deadline_at")
    if ack_deadline > deadline:
        raise HTTPException(status_code=422, detail="签收期限不能晚于撤回期限")
    if not material_refs:
        raise HTTPException(status_code=422, detail="撤回范围至少包含一个素材")
    existing = conn.execute("SELECT case_ref FROM recall_cases WHERE case_ref=?", (case_ref,)).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail=f"撤回事件已存在：{case_ref}")
    for material_ref in material_refs:
        _get_material(conn, material_ref)
    conn.execute(
        "INSERT INTO recall_cases(case_ref, risk_notice_ref, owner_ref, reason, deadline_at,"
        " ack_deadline_at, requires_replacement, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (case_ref, risk_notice_ref, actor.ref, reason, deadline_at, ack_deadline_at,
         1 if requires_replacement else 0, now_iso()),
    )
    for material_ref in material_refs:
        conn.execute(
            "INSERT INTO case_materials(case_ref, material_ref) VALUES (?, ?)", (case_ref, material_ref)
        )
        conn.execute("UPDATE materials SET status='withdrawn' WHERE material_ref=?", (material_ref,))
    audit(conn, actor, "case_opened", case_ref=case_ref,
          detail={"risk_notice_ref": risk_notice_ref, "material_refs": material_refs,
                  "deadline_at": deadline_at})
    return {"case_ref": case_ref, "status": "open", "scope_size": len(material_refs)}


def issue_notices(conn: sqlite3.Connection, actor: Actor, case_ref: str) -> dict:
    """按传播清单为每个涉及点位签发带唯一编号的处置通知。"""
    case = _get_case(conn, case_ref)
    if case["status"] != "open":
        raise HTTPException(status_code=409, detail="事件已关闭，不能签发新通知")
    entries = conn.execute(
        "SELECT e.* FROM propagation_entries e"
        " JOIN case_materials cm ON cm.material_ref = e.material_ref"
        " WHERE cm.case_ref=? AND e.status='listed' ORDER BY e.entry_ref",
        (case_ref,),
    ).fetchall()
    if not entries:
        raise HTTPException(status_code=422, detail="传播清单为空，无可签发点位")
    issued = []
    for entry in entries:
        dup = conn.execute(
            "SELECT notice_no FROM disposal_tasks WHERE case_ref=? AND entry_ref=? AND status != 'closed'",
            (case_ref, entry["entry_ref"]),
        ).fetchone()
        if dup:
            continue
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(task_seq), 0) + 1 AS next_seq FROM disposal_tasks WHERE case_ref=?",
            (case_ref,),
        ).fetchone()
        seq = seq_row["next_seq"]
        notice_no = f"DSP-{case_ref}-{seq:03d}"
        conn.execute(
            "INSERT INTO disposal_tasks(notice_no, case_ref, entry_ref, assignee_ref,"
            " required_locations_json, deadline_at, ack_deadline_at, task_seq, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (notice_no, case_ref, entry["entry_ref"], entry["channel_owner_ref"],
             entry["locations_json"], case["deadline_at"], case["ack_deadline_at"], seq, now_iso()),
        )
        audit(conn, actor, "notice_issued", case_ref=case_ref, notice_no=notice_no,
              detail={"entry_ref": entry["entry_ref"], "assignee_ref": entry["channel_owner_ref"],
                      "deadline_at": case["deadline_at"]})
        issued.append({"notice_no": notice_no, "entry_ref": entry["entry_ref"],
                       "assignee_ref": entry["channel_owner_ref"]})
    return {"case_ref": case_ref, "issued": issued, "issued_count": len(issued)}


# ---------------------------------------------------------------- 任务流转

def _visible_task(conn: sqlite3.Connection, actor: Actor, notice_no: str) -> sqlite3.Row:
    task = _get_task(conn, notice_no)
    if actor.role == "channel_owner" and task["assignee_ref"] != actor.ref:
        raise HTTPException(status_code=403, detail="渠道负责人只能处理自己的点位")
    return task


def acknowledge_notice(conn: sqlite3.Connection, actor: Actor, notice_no: str) -> dict:
    task = _visible_task(conn, actor, notice_no)
    if task["status"] == "closed":
        raise HTTPException(status_code=409, detail="任务已关闭")
    if task["status"] != "issued":
        return {"notice_no": notice_no, "status": task["status"]}
    conn.execute("UPDATE disposal_tasks SET status='acknowledged' WHERE notice_no=?", (notice_no,))
    audit(conn, actor, "notice_acknowledged", case_ref=task["case_ref"], notice_no=notice_no)
    return {"notice_no": notice_no, "status": "acknowledged"}


def refuse_notice(conn: sqlite3.Connection, actor: Actor, notice_no: str, detail: str = "") -> dict:
    task = _visible_task(conn, actor, notice_no)
    if task["status"] == "closed":
        raise HTTPException(status_code=409, detail="任务已关闭")
    escalation = _open_escalation(conn, actor, task, "refused", detail or "渠道拒绝下架")
    return {"notice_no": notice_no, "status": "escalated", "escalation_ref": escalation}


def _open_escalation(
    conn: sqlite3.Connection, actor: Actor, task: sqlite3.Row, kind: str, detail: str
) -> str:
    existing = conn.execute(
        "SELECT escalation_ref FROM escalations WHERE notice_no=? AND kind=? AND status='open'",
        (task["notice_no"], kind),
    ).fetchone()
    if existing:
        return existing["escalation_ref"]
    escalation_ref = new_ref("ESC")
    conn.execute(
        "INSERT INTO escalations(escalation_ref, notice_no, kind, detail, opened_by, opened_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (escalation_ref, task["notice_no"], kind, detail, actor.ref, now_iso()),
    )
    if task["status"] != "closed":
        conn.execute("UPDATE disposal_tasks SET status='escalated' WHERE notice_no=?", (task["notice_no"],))
    audit(conn, actor, "escalation_opened", case_ref=task["case_ref"], notice_no=task["notice_no"],
          detail={"escalation_ref": escalation_ref, "kind": kind, "detail": detail})
    return escalation_ref


def escalate_notice(conn: sqlite3.Connection, actor: Actor, notice_no: str, kind: str, detail: str = "") -> dict:
    if kind not in ESCALATION_KINDS - {"reappeared"}:
        raise HTTPException(status_code=422, detail=f"不支持手工发起该升级类型：{kind}")
    task = _get_task(conn, notice_no)
    if task["status"] == "closed":
        raise HTTPException(status_code=409, detail="任务已关闭")
    escalation_ref = _open_escalation(conn, actor, task, kind, detail)
    return {"notice_no": notice_no, "escalation_ref": escalation_ref, "kind": kind}


def resolve_escalation(conn: sqlite3.Connection, actor: Actor, escalation_ref: str, detail: str = "") -> dict:
    row = conn.execute("SELECT * FROM escalations WHERE escalation_ref=?", (escalation_ref,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"升级单不存在：{escalation_ref}")
    if row["status"] == "resolved":
        return {"escalation_ref": escalation_ref, "status": "resolved"}
    conn.execute(
        "UPDATE escalations SET status='resolved', resolved_at=? WHERE escalation_ref=?",
        (now_iso(), escalation_ref),
    )
    task = _get_task(conn, row["notice_no"])
    if task["status"] == "escalated":
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM escalations WHERE notice_no=? AND status='open'", (row["notice_no"],)
        ).fetchone()["n"]
        if remaining == 0:
            conn.execute("UPDATE disposal_tasks SET status='in_progress' WHERE notice_no=?", (row["notice_no"],))
    audit(conn, actor, "escalation_resolved", case_ref=task["case_ref"], notice_no=row["notice_no"],
          detail={"escalation_ref": escalation_ref, "kind": row["kind"], "detail": detail})
    return {"escalation_ref": escalation_ref, "status": "resolved"}


# ---------------------------------------------------------------- 结果接收与关闭判定

def _closure_state(conn: sqlite3.Connection, task: sqlite3.Row) -> dict:
    """汇总任务当前的有效结果与未满足的关闭条件。"""
    evidences = conn.execute(
        "SELECT * FROM evidence WHERE notice_no=? AND is_duplicate=0", (task["notice_no"],)
    ).fetchall()
    types = {e["evidence_type"] for e in evidences}
    submitters = {e["submitted_by"] for e in evidences}
    cleared: set[str] = set()
    for e in evidences:
        cleared.update(json.loads(e["locations_cleared_json"]))
    required = set(json.loads(task["required_locations_json"]))
    open_escalations = conn.execute(
        "SELECT kind FROM escalations WHERE notice_no=? AND status='open'", (task["notice_no"],)
    ).fetchall()

    missing = []
    if open_escalations:
        missing.append("存在未结升级：" + ",".join(sorted({r["kind"] for r in open_escalations})))
    if len(types) < MIN_EVIDENCE_TYPES or len(submitters) < MIN_SUBMITTERS:
        missing.append("有效结果来源不足（需至少两类结果且来自不同提交人）")
    if not required <= cleared:
        missing.append("未覆盖全部展示位置：" + ",".join(sorted(required - cleared)))

    case = _get_case(conn, task["case_ref"])
    if case["requires_replacement"]:
        problem = _replacement_problem(conn, task, case)
        if problem:
            missing.append(problem)
    return {"closable": not missing, "missing": missing,
            "evidence_types": sorted(types), "locations_cleared": sorted(cleared)}


def _replacement_problem(conn: sqlite3.Connection, task: sqlite3.Row, case: sqlite3.Row) -> str | None:
    """替代素材必须引用独立审核的放行凭证；旧素材撤回不构成任何放行依据。"""
    if not task["replacement_material_ref"] or not task["replacement_credential_ref"]:
        return "缺少替代素材或放行凭证"
    credential = conn.execute(
        "SELECT * FROM release_credentials WHERE credential_ref=?",
        (task["replacement_credential_ref"],),
    ).fetchone()
    if not credential:
        return "放行凭证不存在"
    if credential["status"] != "valid":
        return "放行凭证已撤销"
    if credential["material_ref"] != task["replacement_material_ref"]:
        return "放行凭证与替代素材不匹配"
    if credential["reviewer_ref"] == case["owner_ref"]:
        return "放行凭证非独立审核签发"
    if credential["expires_at"] and parse_ts(credential["expires_at"], "expires_at") <= datetime.now(timezone.utc):
        return "放行凭证已过期"
    return None


def submit_evidence(
    conn: sqlite3.Connection,
    actor: Actor,
    notice_no: str,
    evidence_type: str,
    submitted_by: str | None = None,
    callback_id: str | None = None,
    locations_cleared: list[str] | None = None,
    attachment_sha256: str | None = None,
    final: bool = False,
) -> dict:
    if evidence_type not in EVIDENCE_TYPES:
        raise HTTPException(status_code=422, detail=f"未知结果类型：{evidence_type}")
    task = _get_task(conn, notice_no)
    if task["status"] == "closed":
        raise HTTPException(status_code=409, detail="任务已关闭，结果仅作留档")
    submitter = submitted_by or actor.ref
    locations_cleared = locations_cleared or []

    # 重复平台回调：留档但不计入关闭条件，绝不允许凭重复回调提前关闭任务
    if evidence_type == "platform_callback" and callback_id:
        dup = conn.execute(
            "SELECT evidence_ref FROM evidence WHERE notice_no=? AND callback_id=?",
            (notice_no, callback_id),
        ).fetchone()
        if dup:
            evidence_ref = new_ref("EV")
            conn.execute(
                "INSERT INTO evidence(evidence_ref, notice_no, evidence_type, submitted_by, callback_id,"
                " locations_cleared_json, attachment_sha256, is_duplicate, received_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (evidence_ref, notice_no, evidence_type, submitter, callback_id,
                 json.dumps(locations_cleared, ensure_ascii=False), attachment_sha256, now_iso()),
            )
            audit(conn, actor, "evidence_duplicate_ignored", case_ref=task["case_ref"],
                  notice_no=notice_no, detail={"callback_id": callback_id, "evidence_ref": evidence_ref})
            return {"evidence_ref": evidence_ref, "duplicate": True, "task_status": task["status"]}

    evidence_ref = new_ref("EV")
    conn.execute(
        "INSERT INTO evidence(evidence_ref, notice_no, evidence_type, submitted_by, callback_id,"
        " locations_cleared_json, attachment_sha256, is_duplicate, received_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (evidence_ref, notice_no, evidence_type, submitter, callback_id,
         json.dumps(locations_cleared, ensure_ascii=False), attachment_sha256, now_iso()),
    )
    if task["status"] in ("issued", "acknowledged"):
        conn.execute("UPDATE disposal_tasks SET status='in_progress' WHERE notice_no=?", (notice_no,))
    audit(conn, actor, "evidence_received", case_ref=task["case_ref"], notice_no=notice_no,
          detail={"evidence_ref": evidence_ref, "evidence_type": evidence_type,
                  "locations_cleared": locations_cleared})

    task = _get_task(conn, notice_no)
    state = _closure_state(conn, task)
    result: dict = {"evidence_ref": evidence_ref, "duplicate": False,
                    "task_status": task["status"], "closable": state["closable"]}

    if final and not state["closable"]:
        required = set(json.loads(task["required_locations_json"]))
        uncovered = required - set(state["locations_cleared"])
        if uncovered:
            # 只替换/下架了部分页面：进入独立升级路径
            escalation_ref = _open_escalation(
                conn, actor, task, "partial_replacement",
                "宣称完成但未覆盖位置：" + ",".join(sorted(uncovered)),
            )
            result["escalation_ref"] = escalation_ref
            result["task_status"] = "escalated"
        result["missing"] = state["missing"]

    if state["closable"]:
        result.update(_close_task(conn, actor, task, state))
    return result


def _close_task(conn: sqlite3.Connection, actor: Actor, task: sqlite3.Row, state: dict) -> dict:
    closed_at = now_iso()
    summary = f"凭 {len(state['evidence_types'])} 类独立结果关闭，覆盖全部展示位置"
    conn.execute(
        "UPDATE disposal_tasks SET status='closed', closed_at=?, close_summary=? WHERE notice_no=?",
        (closed_at, summary, task["notice_no"]),
    )
    conn.execute("UPDATE propagation_entries SET status='cleared' WHERE entry_ref=?", (task["entry_ref"],))
    audit(conn, actor, "task_closed", case_ref=task["case_ref"], notice_no=task["notice_no"],
          detail={"closed_at": closed_at, "evidence_types": state["evidence_types"]})
    # 全部任务关闭后事件自动关闭
    open_left = conn.execute(
        "SELECT COUNT(*) AS n FROM disposal_tasks WHERE case_ref=? AND status != 'closed'",
        (task["case_ref"],),
    ).fetchone()["n"]
    case_closed = False
    if open_left == 0:
        conn.execute("UPDATE recall_cases SET status='closed' WHERE case_ref=?", (task["case_ref"],))
        audit(conn, actor, "case_closed", case_ref=task["case_ref"])
        case_closed = True
    return {"task_status": "closed", "closed_at": closed_at, "case_closed": case_closed}


# ---------------------------------------------------------------- 放行凭证与替代素材

def issue_credential(
    conn: sqlite3.Connection,
    actor: Actor,
    credential_ref: str,
    material_ref: str,
    expires_at: str | None = None,
) -> dict:
    _get_material(conn, material_ref)
    if expires_at:
        parse_ts(expires_at, "expires_at")
    existing = conn.execute(
        "SELECT credential_ref FROM release_credentials WHERE credential_ref=?", (credential_ref,)
    ).fetchone()
    if existing:
        raise HTTPException(status_code=409, detail=f"放行凭证已存在：{credential_ref}")
    conn.execute(
        "INSERT INTO release_credentials(credential_ref, material_ref, reviewer_ref, issued_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (credential_ref, material_ref, actor.ref, now_iso(), expires_at),
    )
    audit(conn, actor, "credential_issued", detail={"credential_ref": credential_ref, "material_ref": material_ref})
    return {"credential_ref": credential_ref, "status": "valid"}


def register_replacement(
    conn: sqlite3.Connection, actor: Actor, notice_no: str, material_ref: str, credential_ref: str
) -> dict:
    task = _visible_task(conn, actor, notice_no)
    if task["status"] == "closed":
        raise HTTPException(status_code=409, detail="任务已关闭")
    _get_material(conn, material_ref)
    case = _get_case(conn, task["case_ref"])
    conn.execute(
        "UPDATE disposal_tasks SET replacement_material_ref=?, replacement_credential_ref=? WHERE notice_no=?",
        (material_ref, credential_ref, notice_no),
    )
    audit(conn, actor, "replacement_registered", case_ref=task["case_ref"], notice_no=notice_no,
          detail={"material_ref": material_ref, "credential_ref": credential_ref})
    task = _get_task(conn, notice_no)
    problem = _replacement_problem(conn, task, case)
    if problem:
        raise HTTPException(status_code=422, detail=f"替代素材未通过放行校验：{problem}")
    state = _closure_state(conn, task)
    result: dict = {"notice_no": notice_no, "replacement_material_ref": material_ref,
                    "credential_ref": credential_ref, "closable": state["closable"]}
    if state["closable"]:
        result.update(_close_task(conn, actor, task, state))
    return result


# ---------------------------------------------------------------- 再传播发现与自动升级

def report_sighting(
    conn: sqlite3.Connection,
    actor: Actor,
    fingerprint_sha256: str,
    seen_at: str,
    channel_code: str | None = None,
    account_ref: str | None = None,
    location_ref: str | None = None,
) -> dict:
    parse_ts(seen_at, "seen_at")
    sighting_ref = new_ref("SIGHT")
    material = conn.execute(
        "SELECT * FROM materials WHERE fingerprint_sha256=? ORDER BY created_at LIMIT 1",
        (fingerprint_sha256,),
    ).fetchone()
    reopened: list[str] = []
    matched_case_ref = None
    if material:
        cases = conn.execute(
            "SELECT case_ref FROM case_materials WHERE material_ref=?", (material["material_ref"],)
        ).fetchall()
        for case_row in cases:
            matched_case_ref = case_row["case_ref"]
            case_reopened = False
            tasks = conn.execute(
                "SELECT t.* FROM disposal_tasks t JOIN propagation_entries e ON e.entry_ref=t.entry_ref"
                " WHERE t.case_ref=? AND e.material_ref=?",
                (matched_case_ref, material["material_ref"]),
            ).fetchall()
            for task in tasks:
                entry = conn.execute(
                    "SELECT * FROM propagation_entries WHERE entry_ref=?", (task["entry_ref"],)
                ).fetchone()
                if account_ref and entry["account_ref"] != account_ref:
                    continue
                if location_ref and location_ref not in json.loads(entry["locations_json"]):
                    continue
                # 旧图再次出现：任务重开并进入专属升级路径
                conn.execute(
                    "UPDATE disposal_tasks SET status='in_progress', closed_at=NULL, close_summary=NULL"
                    " WHERE notice_no=?",
                    (task["notice_no"],),
                )
                conn.execute("UPDATE propagation_entries SET status='listed' WHERE entry_ref=?",
                             (task["entry_ref"],))
                fresh_task = _get_task(conn, task["notice_no"])
                _open_escalation(conn, actor, fresh_task, "reappeared",
                                 f"已撤回内容再次出现于 {location_ref or account_ref or '未知位置'}")
                reopened.append(task["notice_no"])
                case_reopened = True
                audit(conn, actor, "task_reopened", case_ref=matched_case_ref,
                      notice_no=task["notice_no"], detail={"sighting_ref": sighting_ref})
            if case_reopened:
                conn.execute("UPDATE recall_cases SET status='open' WHERE case_ref=?", (matched_case_ref,))
    conn.execute(
        "INSERT INTO sightings(sighting_ref, fingerprint_sha256, seen_at, channel_code, account_ref,"
        " location_ref, reporter_ref, matched_material_ref, matched_case_ref, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sighting_ref, fingerprint_sha256, seen_at, channel_code, account_ref, location_ref,
         actor.ref, material["material_ref"] if material else None, matched_case_ref, now_iso()),
    )
    audit(conn, actor, "sighting_reported", case_ref=matched_case_ref,
          detail={"sighting_ref": sighting_ref, "matched": material is not None, "reopened": reopened})
    return {"sighting_ref": sighting_ref, "matched": material is not None,
            "matched_material_ref": material["material_ref"] if material else None,
            "reopened_notices": reopened}


def sweep_case(conn: sqlite3.Connection, actor: Actor, case_ref: str) -> dict:
    """扫描逾期未签收的任务，自动进入“无法联系”升级路径。"""
    case = _get_case(conn, case_ref)
    now = datetime.now(timezone.utc)
    escalated = []
    tasks = conn.execute(
        "SELECT * FROM disposal_tasks WHERE case_ref=? AND status='issued'", (case_ref,)
    ).fetchall()
    for task in tasks:
        if parse_ts(task["ack_deadline_at"], "ack_deadline_at") < now:
            escalation_ref = _open_escalation(conn, actor, task, "unreachable",
                                              "签收期限已过且未获确认")
            escalated.append({"notice_no": task["notice_no"], "escalation_ref": escalation_ref})
    return {"case_ref": case["case_ref"], "unreachable_escalated": escalated}


# ---------------------------------------------------------------- 查询视图

def get_manifest(conn: sqlite3.Connection, actor: Actor, case_ref: str) -> dict:
    case = _get_case(conn, case_ref)
    rows = conn.execute(
        "SELECT e.*, m.derivative_kind, m.material_class, m.parent_ref, t.notice_no, t.status AS task_status"
        " FROM propagation_entries e"
        " JOIN case_materials cm ON cm.material_ref = e.material_ref AND cm.case_ref=?"
        " JOIN materials m ON m.material_ref = e.material_ref"
        " LEFT JOIN disposal_tasks t ON t.case_ref=? AND t.entry_ref=e.entry_ref"
        " ORDER BY e.entry_ref",
        (case_ref, case_ref),
    ).fetchall()
    entries, redacted = [], 0
    for row in rows:
        if actor.role == "channel_owner" and row["channel_owner_ref"] != actor.ref:
            continue
        if not can_view_material(actor, row["material_class"]):
            redacted += 1
            continue
        entries.append({
            "entry_ref": row["entry_ref"], "material_ref": row["material_ref"],
            "derivative_kind": row["derivative_kind"], "parent_ref": row["parent_ref"],
            "channel_code": row["channel_code"], "account_ref": row["account_ref"],
            "channel_owner_ref": row["channel_owner_ref"],
            "locations": json.loads(row["locations_json"]),
            "notice_no": row["notice_no"], "task_status": row["task_status"],
        })
    return {"case_ref": case_ref, "risk_notice_ref": case["risk_notice_ref"],
            "status": case["status"], "deadline_at": case["deadline_at"],
            "entries": entries, "redacted_entries": redacted}


def get_material_view(conn: sqlite3.Connection, actor: Actor, material_ref: str) -> dict:
    material = _get_material(conn, material_ref)
    if not can_view_material(actor, material["material_class"]):
        raise HTTPException(status_code=403, detail="代言团队不可见配方与消费者材料")
    return dict(material)


def list_notices(conn: sqlite3.Connection, actor: Actor, case_ref: str | None = None) -> dict:
    sql = "SELECT * FROM disposal_tasks"
    params: list = []
    if case_ref:
        _get_case(conn, case_ref)
        sql += " WHERE case_ref=?"
        params.append(case_ref)
    if actor.role == "channel_owner":
        sql += " AND" if params else " WHERE"
        sql += " assignee_ref=?"
        params.append(actor.ref)
    sql += " ORDER BY notice_no"
    rows = conn.execute(sql, params).fetchall()
    notices = []
    for row in rows:
        item = dict(row)
        item["required_locations"] = json.loads(item.pop("required_locations_json"))
        notices.append(item)
    return {"notices": notices}


def dashboard_coverage(conn: sqlite3.Connection, actor: Actor, case_ref: str | None = None) -> dict:
    now = datetime.now(timezone.utc)
    cases = conn.execute(
        "SELECT * FROM recall_cases" + (" WHERE case_ref=?" if case_ref else "") + " ORDER BY created_at",
        (case_ref,) if case_ref else (),
    ).fetchall()
    if case_ref and not cases:
        raise HTTPException(status_code=404, detail=f"撤回事件不存在：{case_ref}")
    summaries = []
    for case in cases:
        tasks = conn.execute(
            "SELECT status, deadline_at FROM disposal_tasks WHERE case_ref=?", (case["case_ref"],)
        ).fetchall()
        total = len(tasks)
        closed = sum(1 for t in tasks if t["status"] == "closed")
        overdue = sum(
            1 for t in tasks
            if t["status"] != "closed" and parse_ts(t["deadline_at"], "deadline_at") < now
        )
        escalation_rows = conn.execute(
            "SELECT e.kind FROM escalations e JOIN disposal_tasks t ON t.notice_no=e.notice_no"
            " WHERE t.case_ref=? AND e.status='open'",
            (case["case_ref"],),
        ).fetchall()
        by_kind: dict[str, int] = {}
        for row in escalation_rows:
            by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + 1
        summaries.append({
            "case_ref": case["case_ref"], "status": case["status"],
            "deadline_at": case["deadline_at"], "tasks_total": total, "tasks_closed": closed,
            "coverage": (closed / total) if total else 0.0,
            "overdue_open": overdue, "open_escalations": by_kind,
        })
    return {"generated_at": now_iso(), "cases": summaries}


def case_trace(conn: sqlite3.Connection, actor: Actor, case_ref: str) -> dict:
    case = _get_case(conn, case_ref)
    events = conn.execute(
        "SELECT id, notice_no, actor_ref, action, detail_json, created_at FROM audit_events"
        " WHERE case_ref=? ORDER BY id",
        (case_ref,),
    ).fetchall()
    tasks = conn.execute(
        "SELECT notice_no, assignee_ref, status, closed_at FROM disposal_tasks WHERE case_ref=? ORDER BY notice_no",
        (case_ref,),
    ).fetchall()
    return {
        "case_ref": case_ref,
        "risk_notice_ref": case["risk_notice_ref"],
        "status": case["status"],
        "tasks": [dict(t) for t in tasks],
        "timeline": [
            {"seq": e["id"], "at": e["created_at"], "actor_ref": e["actor_ref"],
             "action": e["action"], "notice_no": e["notice_no"],
             "detail": json.loads(e["detail_json"])}
            for e in events
        ],
    }
