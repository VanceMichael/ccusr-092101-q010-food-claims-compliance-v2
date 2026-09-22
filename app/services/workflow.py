"""撤回/替换工作流：圈定 → 带唯一编号通知 → 凭证受理与核验 → 升级/闭环。

关键不变量：
1. 重复回调（相同 delivery_id）原样返回首条凭证，不推进任何点位状态，任务不会被提前关闭。
2. 只有核验通过的点位才 resolved；通知下全部点位 resolved 后通知才 completed。
3. 替代素材必须在核验时点处于有效放行窗口内，旧素材撤回不产生放行。
4. 无法联系/拒绝/旧图再现/只替换部分页面分别进入不同升级类型。
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from app.clock import clock, format_ts, normalize_ts
from app.db import one
from app.errors import Conflict, NotFound, ValidationFailed
from app.models import (
    EscalationAdvance,
    EvidenceIn,
    EvidenceVerify,
    IssueNoticesRequest,
    ReleaseApprovalIn,
    ScopeRequest,
)
from app.refs import approval_ref, escalation_ref, evidence_ref, new_id, notice_ref
from app.services.audit import log_event

NON_TERMINAL_PLACEMENT = ("identified", "scoped", "notified", "recurred")


# ---------- 基础查询 ----------

def placement_by_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = one(conn, "SELECT * FROM placements WHERE ref = ?", (ref,))
    if row is None:
        raise NotFound(f"点位 {ref} 不存在")
    return row


def notice_by_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = one(conn, "SELECT * FROM notices WHERE ref = ?", (ref,))
    if row is None:
        raise NotFound(f"通知 {ref} 不存在")
    return row


def evidence_by_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = one(conn, "SELECT * FROM evidences WHERE ref = ?", (ref,))
    if row is None:
        raise NotFound(f"凭证 {ref} 不存在")
    return row


def _incident_placements(conn: sqlite3.Connection, incident_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM placements WHERE incident_id = ? ORDER BY first_seen_at, ref", (incident_id,)
        )
    )


# ---------- 风险负责人圈定撤回范围与期限 ----------

def scope(conn: sqlite3.Connection, incident_ref_: str, req: ScopeRequest, actor_id: str) -> dict[str, Any]:
    from app.services.incidents import get_incident

    incident = get_incident(conn, incident_ref_)
    incident_id = incident["incident_id"]
    now = clock.now()
    deadline = format_ts(now + timedelta(hours=req.deadline_hours))

    selected = _select_placements(conn, incident_id, req.placement_refs)
    if not selected:
        raise ValidationFailed("圈定范围为空")

    replacement_ids: dict[str, str] = {}
    if req.replacement_map:
        for pl_ref, asset_ref_ in req.replacement_map.items():
            asset = one(conn, "SELECT * FROM assets WHERE ref = ? AND incident_id = ?",
                        (asset_ref_, incident_id))
            if asset is None:
                raise ValidationFailed(f"替代素材 {asset_ref_} 不存在")
            if asset["kind"] != "replacement":
                raise ValidationFailed(f"素材 {asset_ref_} 不是替代素材")
            replacement_ids[pl_ref] = asset["asset_id"]

    scoped: list[str] = []
    for p in selected:
        if p["status"] not in NON_TERMINAL_PLACEMENT:
            # 已闭环点位不能被重新圈定（复发会先回到 recurred）
            continue
        action = (req.actions or {}).get(p["ref"], req.default_action)
        replacement_id = replacement_ids.get(p["ref"])
        if action == "replace" and replacement_id is None:
            raise ValidationFailed(f"替换点位 {p['ref']} 必须在 replacement_map 中指定替代素材")
        conn.execute(
            "UPDATE placements SET status='scoped', required_action=?, replacement_asset_id=?, "
            "deadline=?, overdue_flag=0 WHERE placement_id=?",
            (action, replacement_id, deadline, p["placement_id"]),
        )
        scoped.append(p["ref"])

    if not scoped:
        raise Conflict("所选点位均已闭环，无需圈定")
    log_event(
        conn, incident_id, "scope.defined", entity_type="incident", entity_ref=incident_ref_,
        actor_id=actor_id,
        payload={"placements": scoped, "deadline": deadline,
                 "actions": {r: (req.actions or {}).get(r, req.default_action) for r in scoped}},
    )
    return {"incident_ref": incident_ref_, "deadline": deadline, "scoped": scoped}


def _select_placements(
    conn: sqlite3.Connection, incident_id: str, refs: list[str] | None
) -> list[sqlite3.Row]:
    if refs is None:
        return [
            r for r in _incident_placements(conn, incident_id)
            if r["status"] in NON_TERMINAL_PLACEMENT
        ]
    out: list[sqlite3.Row] = []
    for ref in refs:
        row = one(
            conn, "SELECT * FROM placements WHERE ref = ? AND incident_id = ?", (ref, incident_id)
        )
        if row is None:
            raise NotFound(f"点位 {ref} 不在该事件内")
        out.append(row)
    return out


# ---------- 发出带唯一编号的处置通知 ----------

def issue_notices(
    conn: sqlite3.Connection, incident_ref_: str, req: IssueNoticesRequest, actor_id: str
) -> list[dict[str, Any]]:
    from app.services.incidents import get_incident

    incident = get_incident(conn, incident_ref_)
    incident_id = incident["incident_id"]
    now_ts = clock.now_ts()

    placements = [
        p for p in _select_placements(conn, incident_id, req.placement_refs)
        if p["status"] == "scoped"
    ]
    if not placements:
        raise Conflict("没有已圈定待通知的点位")

    # 按渠道负责人分组：每个负责人一封带唯一编号的通知，覆盖其名下多个点位
    groups: dict[tuple[str, str | None], list[sqlite3.Row]] = {}
    for p in placements:
        channel = one(conn, "SELECT * FROM channels WHERE channel_id = ?", (p["channel_id"],))
        groups.setdefault((p["channel_id"], channel["owner_actor_id"]), []).append(p)

    notices: list[dict[str, Any]] = []
    for (channel_id, recipient_id), group in groups.items():
        due_at = min(p["deadline"] for p in group if p["deadline"])
        ref = notice_ref(conn, incident_ref_)
        notice_id = new_id()
        conn.execute(
            "INSERT INTO notices(notice_id, ref, incident_id, channel_id, recipient_actor_id, "
            "required_action, round, delivery_channel, issued_at, due_at, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (notice_id, ref, incident_id, channel_id, recipient_id,
             _group_action(group), 1, req.delivery_channel, now_ts, due_at, "issued"),
        )
        for p in group:
            conn.execute(
                "INSERT INTO notice_placements(notice_id, placement_id) VALUES (?,?)",
                (notice_id, p["placement_id"]),
            )
            conn.execute(
                "UPDATE placements SET status='notified', notified_at=?, current_notice_id=?, "
                "overdue_flag=0 WHERE placement_id=?",
                (now_ts, notice_id, p["placement_id"]),
            )
        notices.append({
            "ref": ref, "channel_id": channel_id, "recipient_actor_id": recipient_id,
            "placement_refs": [p["ref"] for p in group],
            "required_action": _group_action(group), "due_at": due_at,
            "delivery_channel": req.delivery_channel, "issued_at": now_ts,
        })
        log_event(
            conn, incident_id, "notice.issued", entity_type="notice", entity_ref=ref,
            actor_id=actor_id,
            payload={"placements": [p["ref"] for p in group], "due_at": due_at,
                     "delivery_channel": req.delivery_channel},
        )
    return notices


def _group_action(group: list[sqlite3.Row]) -> str:
    actions = {p["required_action"] for p in group}
    if len(actions) == 1:
        return next(iter(actions))
    # 同一负责人下混合动作时以更严格的 replace 为准，逐点位动作仍以点位表为准
    return "replace"


def reissue_notice(
    conn: sqlite3.Connection,
    placement: sqlite3.Row,
    delivery_channel: str,
    actor_id: str,
    deadline_hours: float | None = None,
) -> sqlite3.Row:
    """复发后按新一轮次重新发出通知（新唯一编号），旧通知标记 superseded。"""

    incident = one(conn, "SELECT * FROM incidents WHERE incident_id = ?", (placement["incident_id"],))
    now_ts = clock.now_ts()
    new_round = placement["round"] + 1
    old_notice_id = placement["current_notice_id"]
    supersede_old = False
    if old_notice_id:
        # 仅当旧通知名下没有其他未闭环点位时才整体作废；否则旧通知继续覆盖其余点位
        other_open = one(
            conn,
            "SELECT COUNT(*) FROM notice_placements np JOIN placements p "
            "ON np.placement_id=p.placement_id WHERE np.notice_id=? "
            "AND np.placement_id != ? AND p.status != 'resolved'",
            (old_notice_id, placement["placement_id"]),
        )[0]
        supersede_old = other_open == 0
        if supersede_old:
            conn.execute("UPDATE notices SET status='superseded' WHERE notice_id=?",
                         (old_notice_id,))
    # 新一轮给予新期限（默认 48 小时），并同步到点位
    hours = deadline_hours if deadline_hours is not None else 48.0
    due_at = format_ts(clock.now() + timedelta(hours=hours))
    channel = one(conn, "SELECT * FROM channels WHERE channel_id = ?", (placement["channel_id"],))
    ref = notice_ref(conn, incident["ref"])
    notice_id = new_id()
    conn.execute(
        "INSERT INTO notices(notice_id, ref, incident_id, channel_id, recipient_actor_id, "
        "required_action, round, delivery_channel, issued_at, due_at, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (notice_id, ref, incident["incident_id"], placement["channel_id"],
         channel["owner_actor_id"],
         placement["required_action"] or "takedown", new_round, delivery_channel, now_ts, due_at,
         "issued"),
    )
    conn.execute(
        "INSERT INTO notice_placements(notice_id, placement_id) VALUES (?,?)",
        (notice_id, placement["placement_id"]),
    )
    if old_notice_id:
        # 复发点位改由新通知接管，从旧通知覆盖集合移除
        conn.execute("DELETE FROM notice_placements WHERE notice_id=? AND placement_id=?",
                     (old_notice_id, placement["placement_id"]))
        maybe_complete_notice(conn, old_notice_id)
    conn.execute(
        "UPDATE placements SET status='notified', notified_at=?, current_notice_id=?, round=?, "
        "deadline=?, overdue_flag=0 WHERE placement_id=?",
        (now_ts, notice_id, new_round, due_at, placement["placement_id"]),
    )
    log_event(
        conn, incident["incident_id"], "notice.reissued", entity_type="notice", entity_ref=ref,
        actor_id=actor_id, payload={"placement_ref": placement["ref"], "round": new_round},
    )
    return notice_by_ref(conn, ref)


# ---------- 凭证受理（平台回调 / 截图摘要 / 现场复核 / 联系结果） ----------

def submit_evidence(
    conn: sqlite3.Connection,
    incident_ref_: str,
    req: EvidenceIn,
    actor_id: str,
) -> dict[str, Any]:
    from app.services.incidents import get_incident

    incident = get_incident(conn, incident_ref_)
    incident_id = incident["incident_id"]
    now_ts = clock.now_ts()

    notice_id: str | None = None
    notice_ref_ = req.notice_ref
    if notice_ref_:
        notice = notice_by_ref(conn, notice_ref_)
        if notice["incident_id"] != incident_id:
            raise ValidationFailed("通知不属于该事件")
        notice_id = notice["notice_id"]

    placement_id: str | None = None
    placement_ref_ = req.placement_ref
    if placement_ref_:
        placement = placement_by_ref(conn, placement_ref_)
        if placement["incident_id"] != incident_id:
            raise ValidationFailed("点位不属于该事件")
        placement_id = placement["placement_id"]
        if notice_id is None and placement["current_notice_id"]:
            notice_id = placement["current_notice_id"]

    # 先校验覆盖明细并收集点位
    coverage_placements: list[sqlite3.Row] = []
    for c in req.coverage:
        p = placement_by_ref(conn, c.placement_ref)
        if p["incident_id"] != incident_id:
            raise ValidationFailed(f"凭证明细 {c.placement_ref} 不属于该事件")
        if c.claimed_replacement_ref:
            rid = one(conn, "SELECT asset_id FROM assets WHERE ref = ?",
                      (c.claimed_replacement_ref,))
            if rid is None:
                raise ValidationFailed(f"声称的替代素材 {c.claimed_replacement_ref} 不存在")
        coverage_placements.append(p)

    # 凭证未显式指定通知/点位时，按覆盖点位的当前通知自动归属
    if notice_id is None and coverage_placements:
        current = {p["current_notice_id"] for p in coverage_placements if p["current_notice_id"]}
        if len(current) == 1:
            notice_id = next(iter(current))
    if placement_id is None and len(coverage_placements) == 1:
        placement_id = coverage_placements[0]["placement_id"]

    # 幂等：相同 delivery_id 的重复回调直接返回首条凭证，绝不推进状态
    if req.delivery_id:
        dup = one(
            conn, "SELECT * FROM evidences WHERE delivery_id = ?", (req.delivery_id,)
        )
        if dup is not None:
            log_event(
                conn, incident_id, "evidence.duplicate_received", entity_type="evidence",
                entity_ref=dup["ref"], source_system=req.source_system,
                payload={"delivery_id": req.delivery_id},
            )
            return {"ref": dup["ref"], "duplicate": True, "status": dup["status"],
                    "applied": False, "message": "重复回调已忽略，未改变任何点位状态"}

    ref = evidence_ref(conn)
    evidence_id = new_id()
    conn.execute(
        "INSERT INTO evidences(evidence_id, ref, incident_id, notice_id, placement_id, kind, "
        "status, source_system, delivery_id, contact_result, claimed_complete, summary, "
        "attachment_sha256, submitted_by_actor_id, submitted_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (evidence_id, ref, incident_id, notice_id, placement_id, req.kind, "pending_verification",
         req.source_system, req.delivery_id, req.contact_result,
         1 if req.claimed_complete else 0, req.summary, req.attachment_sha256, actor_id, now_ts),
    )

    for c, p in zip(req.coverage, coverage_placements):
        rid = one(conn, "SELECT asset_id FROM assets WHERE ref = ?", (c.claimed_replacement_ref,))
        conn.execute(
            "INSERT INTO evidence_coverage(evidence_id, placement_id, claimed_state, "
            "claimed_replacement_asset_id) VALUES (?,?,?,?)",
            (evidence_id, p["placement_id"], c.claimed_state,
             rid["asset_id"] if rid else None),
        )

    log_event(
        conn, incident_id, "evidence.received", entity_type="evidence", entity_ref=ref,
        actor_id=actor_id, source_system=req.source_system,
        payload={"kind": req.kind, "notice_ref": notice_ref_, "placement_ref": placement_ref_,
                 "delivery_id": req.delivery_id, "coverage": len(req.coverage),
                 "claimed_complete": req.claimed_complete},
    )

    escalations: list[str] = []
    # 联系结果：无法联系 / 拒绝下架，立即进入各自升级路径
    if req.kind == "contact_record" and req.contact_result in ("unreachable", "refused"):
        target_notice = notice_id
        pl = placement_id
        esc = open_escalation(
            conn, incident_id, req.contact_result,
            reason=f"处置联系结果：{req.contact_result}",
            actor_id=actor_id, notice_id=target_notice, placement_id=pl,
        )
        escalations.append(esc["ref"])
        conn.execute("UPDATE evidences SET status='verified', verified_by_actor_id=?, verified_at=?, "
                     "verify_note='联系结果事实记录，触发升级' WHERE evidence_id=?",
                     (actor_id, now_ts, evidence_id))
        if req.contact_result == "unreachable" and target_notice:
            conn.execute(
                "UPDATE notices SET contact_attempts = contact_attempts + 1 WHERE notice_id=?",
                (target_notice,),
            )

    return {"ref": ref, "duplicate": False, "status": "pending_verification", "applied": False,
            "escalations": escalations,
            "message": "凭证已登记，须经核验通过点位才会闭环"}


# ---------- 凭证核验：真正的闭环关口 ----------

def verify_evidence(
    conn: sqlite3.Connection, evidence_ref_: str, req: EvidenceVerify, actor_id: str
) -> dict[str, Any]:
    evidence = evidence_by_ref(conn, evidence_ref_)
    if evidence["status"] in ("verified", "rejected") and not req.coverage_results:
        raise Conflict(f"凭证 {evidence_ref_} 已完成核验（{evidence['status']}）")
    incident_id = evidence["incident_id"]
    now_ts = clock.now_ts()

    rows = list(conn.execute(
        "SELECT ec.*, p.* FROM evidence_coverage ec JOIN placements p ON ec.placement_id = p.placement_id "
        "WHERE ec.evidence_id = ?", (evidence["evidence_id"],)
    ))
    if not rows:
        # 无逐条明细时，对凭证关联点位整体判定
        if evidence["placement_id"]:
            rows = list(conn.execute(
                "SELECT p.*, NULL AS claimed_state FROM placements p WHERE p.placement_id = ?",
                (evidence["placement_id"],),
            ))

    explicit = {c.placement_ref: c.verified_state for c in (req.coverage_results or [])}
    notice_id = evidence["notice_id"]
    resolved_now: list[str] = []
    blocked: list[dict[str, str]] = []
    still_open: list[str] = []

    for r in rows:
        claimed = r["claimed_state"] if "claimed_state" in r.keys() else None
        state = explicit.get(r["ref"], _default_verdict(claimed, r["required_action"]))
        conn.execute(
            "UPDATE evidence_coverage SET verified_state=? WHERE evidence_id=? AND placement_id=?",
            (state, evidence["evidence_id"], r["placement_id"]),
        )
        if state in ("removed", "replaced"):
            if state == "replaced":
                # 替换闭环必须引用点位指定的替代素材，且该替代素材持有独立放行
                approved, reason = _replacement_is_releasable(conn, r, now_ts)
                if not approved:
                    blocked.append({"placement_ref": r["ref"], "reason": reason})
                    still_open.append(r["ref"])
                    continue
            _resolve_placement(conn, r, state, now_ts)
            resolved_now.append(r["ref"])
        else:
            # 仍在线/缺失/不符：凭证不予通过，点位保持开放，由逾期或显式拒绝路径推进
            still_open.append(r["ref"])

    # 声称"全部完成"却漏报通知下部分点位：不能据此关闭通知，按只替换部分页面升级
    unreported: list[str] = []
    if evidence["claimed_complete"] and notice_id:
        covered_ids = {r["placement_id"] for r in rows}
        missing = conn.execute(
            "SELECT p.* FROM notice_placements np JOIN placements p "
            "ON np.placement_id=p.placement_id WHERE np.notice_id=? AND p.status != 'resolved'",
            (notice_id,),
        ).fetchall()
        for p in missing:
            if p["placement_id"] not in covered_ids:
                unreported.append(p["ref"])
                still_open.append(p["ref"])

    partial_esc = None
    if blocked or still_open:
        conn.execute("UPDATE evidences SET status='rejected', verified_by_actor_id=?, verified_at=?, "
                     "verify_note=? WHERE evidence_id=?",
                     (actor_id, now_ts, req.note or "存在未通过核验的点位", evidence["evidence_id"]))
        # 只替换部分页面：同一通知下部分点位已闭环、部分仍在线或被漏报
        if notice_id and (resolved_now or unreported):
            reasons = []
            if unreported:
                reasons.append(f"声称全部完成但缺少 {','.join(unreported)} 的处置凭证")
            if blocked:
                reasons.append(";".join(b["reason"] for b in blocked))
            open_refs = [r for r in still_open if r not in unreported]
            anchor = open_refs[0] if open_refs else (unreported[0] if unreported else None)
            partial_esc = open_escalation(
                conn, incident_id, "partial_replace",
                reason="；".join(reasons) or f"通知下仅部分点位闭环：{','.join(still_open)}",
                actor_id=actor_id, notice_id=notice_id,
                placement_id=_placement_id_by_ref(conn, anchor) if anchor else None,
            )
        elif blocked:
            partial_esc = open_escalation(
                conn, incident_id, "partial_replace",
                reason="替代素材缺少有效放行凭证：" + ";".join(b["reason"] for b in blocked),
                actor_id=actor_id, notice_id=notice_id,
                placement_id=_placement_id_by_ref(conn, blocked[0]["placement_ref"]),
            )
    else:
        conn.execute("UPDATE evidences SET status='verified', verified_by_actor_id=?, verified_at=?, "
                     "verify_note=? WHERE evidence_id=?",
                     (actor_id, now_ts, req.note, evidence["evidence_id"]))

    maybe_complete_notice(conn, notice_id)
    # 凭证可能通过覆盖明细涉及其他通知（自动归属之外的情形），逐一检查
    if rows:
        touched_notices = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT np.notice_id FROM notice_placements np WHERE np.placement_id IN ({})".format(
                    ",".join("?" * len(rows))
                ),
                [r["placement_id"] for r in rows],
            ).fetchall()
        }
        touched_notices.add(notice_id)
        for nid in touched_notices:
            maybe_complete_notice(conn, nid)
    _refresh_asset_status(conn, incident_id)

    log_event(
        conn, incident_id, "evidence.verified", entity_type="evidence",
        entity_ref=evidence_ref_, actor_id=actor_id,
        payload={"decision": "verified" if not (blocked or still_open) else "rejected",
                 "resolved": resolved_now, "blocked": blocked, "still_open": still_open,
                 "escalation": partial_esc["ref"] if partial_esc else None},
    )
    return {
        "ref": evidence_ref_,
        "status": "rejected" if (blocked or still_open) else "verified",
        "resolved": resolved_now, "blocked": blocked, "still_open": still_open,
        "escalation": partial_esc["ref"] if partial_esc else None,
    }


def _default_verdict(claimed: str | None, action: str | None) -> str:
    if claimed == "removed":
        return "removed"
    if claimed == "replaced":
        return "replaced"
    if claimed == "partial":
        return "missing"
    if claimed == "still_present":
        return "mismatch"
    return "removed" if action == "takedown" else "replaced"


def _replacement_is_releasable(
    conn: sqlite3.Connection, placement: sqlite3.Row, now_ts: str
) -> tuple[bool, str]:
    asset_id = placement["replacement_asset_id"]
    if not asset_id:
        row = one(
            conn,
            "SELECT claimed_replacement_asset_id FROM evidence_coverage ec "
            "JOIN evidences e ON ec.evidence_id = e.evidence_id "
            "WHERE ec.placement_id = ? ORDER BY e.submitted_at DESC LIMIT 1",
            (placement["placement_id"],),
        )
        asset_id = row["claimed_replacement_asset_id"] if row else None
    if not asset_id:
        return False, f"点位 {placement['ref']} 未指定替代素材"
    approval = one(
        conn,
        "SELECT * FROM release_approvals WHERE asset_id = ? AND decision = 'approved' "
        "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) ORDER BY decided_at DESC",
        (asset_id, now_ts, now_ts),
    )
    if approval is None:
        asset = one(conn, "SELECT ref FROM assets WHERE asset_id = ?", (asset_id,))
        ref = asset["ref"] if asset else asset_id
        return False, f"替代素材 {ref} 无独立审核放行凭证或放行已失效"
    return True, ""


def _resolve_placement(conn: sqlite3.Connection, row: sqlite3.Row, state: str, now_ts: str) -> None:
    asset_id = row["replacement_asset_id"]
    # 实际下架/替换时间以首次核验通过为准，重复核验不得覆盖（追溯链稳定性）
    conn.execute(
        "UPDATE placements SET status='resolved', "
        "resolved_at=COALESCE(resolved_at, ?), last_observed_at=?, "
        "replacement_asset_id=COALESCE(replacement_asset_id, ?), overdue_flag=0 "
        "WHERE placement_id=?",
        (now_ts, now_ts, asset_id, row["placement_id"]),
    )


def maybe_complete_notice(conn: sqlite3.Connection, notice_id: str | None) -> None:
    if not notice_id:
        return
    total = one(
        conn, "SELECT COUNT(*) FROM notice_placements WHERE notice_id=?", (notice_id,)
    )[0]
    done = one(
        conn, "SELECT COUNT(*) FROM notice_placements np JOIN placements p ON np.placement_id=p.placement_id "
        "WHERE np.notice_id=? AND p.status='resolved'", (notice_id,)
    )[0]
    notice = one(conn, "SELECT status FROM notices WHERE notice_id=?", (notice_id,)
    )
    if total > 0 and total == done and notice and notice["status"] not in ("superseded", "completed"):
        conn.execute("UPDATE notices SET status='completed', completed_at=? WHERE notice_id=?",
                     (clock.now_ts(), notice_id))


def _refresh_asset_status(conn: sqlite3.Connection, incident_id: str) -> None:
    rows = conn.execute(
        "SELECT asset_id, kind FROM assets WHERE incident_id=? AND kind != 'replacement'",
        (incident_id,),
    )
    for asset_id, kind in rows:
        unresolved = one(
            conn,
            "SELECT COUNT(*) FROM placements WHERE asset_id=? AND status != 'resolved'",
            (asset_id,),
        )[0]
        conn.execute(
            "UPDATE assets SET status=? WHERE asset_id=?",
            ("withdrawn" if unresolved == 0 else "in_circulation", asset_id),
        )


def _placement_id_by_ref(conn: sqlite3.Connection, ref: str) -> str | None:
    row = one(conn, "SELECT placement_id FROM placements WHERE ref=?", (ref,))
    return row["placement_id"] if row else None


# ---------- 升级 ----------

def open_escalation(
    conn: sqlite3.Connection,
    incident_id: str,
    esc_type: str,
    *,
    reason: str,
    actor_id: str | None,
    notice_id: str | None = None,
    placement_id: str | None = None,
    level: int = 1,
) -> dict[str, Any]:
    # 同一点位同一类型的未关闭升级不重复开
    existing = one(
        conn,
        "SELECT * FROM escalations WHERE incident_id=? AND type=? AND status='open' "
        "AND IFNULL(placement_id,'')=IFNULL(?, '')",
        (incident_id, esc_type, placement_id),
    )
    if existing is not None:
        return dict(existing)
    ref = escalation_ref(conn)
    conn.execute(
        "INSERT INTO escalations(escalation_id, ref, incident_id, notice_id, placement_id, type, "
        "level, status, reason, opened_by_actor_id, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (new_id(), ref, incident_id, notice_id, placement_id, esc_type, level, "open", reason,
         actor_id, clock.now_ts()),
    )
    row = one(conn, "SELECT * FROM escalations WHERE ref=?", (ref,))
    log_event(
        conn, incident_id, "escalation.opened", entity_type="escalation", entity_ref=ref,
        actor_id=actor_id, payload={"type": esc_type, "level": level, "reason": reason},
    )
    return dict(row)


def advance_escalation(
    conn: sqlite3.Connection, escalation_ref_: str, req: EscalationAdvance, actor_id: str
) -> dict[str, Any]:
    row = one(conn, "SELECT * FROM escalations WHERE ref=?", (escalation_ref_,))
    if row is None:
        raise NotFound(f"升级 {escalation_ref_} 不存在")
    if row["status"] != "open":
        raise Conflict(f"升级 {escalation_ref_} 已关闭")
    new_level = min(3, row["level"] + 1)
    conn.execute("UPDATE escalations SET level=? WHERE escalation_id=?",
                 (new_level, row["escalation_id"]))
    reissued = None
    if req.reissue_notice:
        if not row["placement_id"]:
            raise ValidationFailed("该升级未关联点位，不能重发通知")
        placement = one(conn, "SELECT * FROM placements WHERE placement_id=?", (row["placement_id"],))
        notice = reissue_notice(
            conn, placement, req.delivery_channel, actor_id, req.deadline_hours
        )
        reissued = notice["ref"]
    log_event(
        conn, row["incident_id"], "escalation.advanced", entity_type="escalation",
        entity_ref=escalation_ref_, actor_id=actor_id,
        payload={"level": new_level, "note": req.note, "reissued_notice": reissued},
    )
    return {"ref": escalation_ref_, "level": new_level, "reissued_notice": reissued}


def resolve_escalation(
    conn: sqlite3.Connection, escalation_ref_: str, note: str, actor_id: str
) -> dict[str, Any]:
    row = one(conn, "SELECT * FROM escalations WHERE ref=?", (escalation_ref_,))
    if row is None:
        raise NotFound(f"升级 {escalation_ref_} 不存在")
    if row["status"] != "open":
        raise Conflict(f"升级 {escalation_ref_} 已关闭")
    conn.execute(
        "UPDATE escalations SET status='resolved', resolved_by_actor_id=?, resolved_at=?, "
        "resolution_note=? WHERE escalation_id=?",
        (actor_id, clock.now_ts(), note, row["escalation_id"]),
    )
    log_event(
        conn, row["incident_id"], "escalation.resolved", entity_type="escalation",
        entity_ref=escalation_ref_, actor_id=actor_id, payload={"note": note},
    )
    return {"ref": escalation_ref_, "status": "resolved"}


# ---------- 替代素材独立放行 ----------

def approve_release(
    conn: sqlite3.Connection, asset_ref_: str, req: ReleaseApprovalIn, actor_id: str
) -> dict[str, Any]:
    asset = one(conn, "SELECT * FROM assets WHERE ref=?", (asset_ref_,))
    if asset is None:
        raise NotFound(f"素材 {asset_ref_} 不存在")
    if asset["kind"] != "replacement":
        raise ValidationFailed("仅替代素材需要放行审核")
    existing = one(conn, "SELECT * FROM release_approvals WHERE asset_id=?", (asset["asset_id"],))
    if existing is not None and existing["decision"] == "approved" and req.decision == "approved":
        raise Conflict(f"素材 {asset_ref_} 已在放行批次 {existing['review_batch_ref']} 中放行")
    now_ts = clock.now_ts()
    valid_from = normalize_ts(req.valid_from) or now_ts
    valid_to = normalize_ts(req.valid_to)
    ref = approval_ref(conn)
    conn.execute(
        "INSERT INTO release_approvals(approval_id, ref, asset_id, decision, reviewer_actor_id, "
        "review_batch_ref, basis_note, decided_at, valid_from, valid_to) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (new_id(), ref, asset["asset_id"], req.decision, actor_id, req.review_batch_ref,
         req.basis_note, now_ts, valid_from, valid_to),
    )
    log_event(
        conn, asset["incident_id"], "release.decided", entity_type="release_approval",
        entity_ref=ref, actor_id=actor_id,
        payload={"asset_ref": asset_ref_, "decision": req.decision,
                 "review_batch_ref": req.review_batch_ref, "valid_from": valid_from,
                 "valid_to": valid_to},
    )
    return {"ref": ref, "asset_ref": asset_ref_, "decision": req.decision,
            "review_batch_ref": req.review_batch_ref, "valid_from": valid_from,
            "valid_to": valid_to}
