"""持续监测：已撤回内容的再次传播发现、逾期扫描、覆盖率统计。"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.clock import clock, normalize_ts
from app.db import one
from app.errors import Conflict, NotFound, ValidationFailed
from app.models import ObservationIn
from app.refs import new_id, observation_ref, placement_ref
from app.services.audit import log_event
from app.services.workflow import open_escalation


def record_observation(conn: sqlite3.Connection, req: ObservationIn, actor_id: str) -> dict[str, Any]:
    now_ts = normalize_ts(req.observed_at) or clock.now_ts()
    channel = one(conn, "SELECT * FROM channels WHERE code = ?", (req.channel_code,))
    if channel is None:
        raise ValidationFailed(f"未知渠道 {req.channel_code}")

    # 用内容指纹匹配已登记素材（原始件与裁剪/改字派生件各自有指纹）
    asset = one(conn, "SELECT * FROM assets WHERE sha256 = ? ORDER BY created_at LIMIT 1",
                (req.asset_sha256,))

    account_id = None
    if req.account_ref:
        account = one(
            conn, "SELECT * FROM accounts WHERE channel_id=? AND account_ref=?",
            (channel["channel_id"], req.account_ref),
        )
        if account is not None:
            account_id = account["account_id"]

    observation_id = new_id()
    obs_ref = observation_ref(conn)
    incident_id = asset["incident_id"] if asset else None
    conn.execute(
        "INSERT INTO observations(observation_id, incident_id, channel_code, account_ref, locator, "
        "asset_sha256, transform_note, source, observed_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (observation_id, incident_id, req.channel_code, req.account_ref, req.locator,
         req.asset_sha256, req.transform_note, req.source, now_ts),
    )

    result: dict[str, Any] = {"ref": obs_ref, "matched": asset is not None}
    if asset is None:
        result["action"] = "unmatched"
        return result

    same_place = one(
        conn,
        "SELECT * FROM placements WHERE incident_id=? AND channel_id=? AND locator=?",
        (incident_id, channel["channel_id"], req.locator),
    )

    escalation = None
    if same_place is not None:
        conn.execute(
            "UPDATE observations SET matched_placement_id=? WHERE observation_id=?",
            (same_place["placement_id"], observation_id),
        )
        if same_place["status"] == "resolved":
            # 旧图在已闭环点位再次出现
            escalation = _reopen_placement(
                conn, same_place, asset["asset_id"], now_ts, obs_ref, observation_id,
                incident_id, actor_id,
                reason=f"已撤回内容在原点位 {same_place['ref']} 再次出现（{req.source}）",
            )
            result["action"] = "recurrence"
            result["placement_ref"] = same_place["ref"]
        else:
            conn.execute("UPDATE placements SET last_observed_at=? WHERE placement_id=?",
                         (now_ts, same_place["placement_id"]))
            result["action"] = "still_in_progress"
            result["placement_ref"] = same_place["ref"]
    else:
        # 新点位：若该素材曾全部撤回（asset.status=withdrawn），属于已撤回内容换址传播
        if asset["status"] == "withdrawn" or _asset_was_ever_resolved(conn, asset["asset_id"]):
            new_p = _create_recurring_placement(
                conn, incident_id, asset, channel, account_id, req.locator, now_ts
            )
            conn.execute(
                "UPDATE observations SET incident_id=?, matched_placement_id=? WHERE observation_id=?",
                (incident_id, new_p["placement_id"], observation_id),
            )
            escalation = _reopen_placement(
                conn, new_p, asset["asset_id"], now_ts, obs_ref, observation_id,
                incident_id, actor_id,
                reason=f"已撤回内容在新点位 {new_p['ref']} 再次传播（{req.source}）",
            )
            result["action"] = "recurrence_new_placement"
            result["placement_ref"] = new_p["ref"]
        else:
            # 素材仍在处置中：发现新的传播点位，纳入清单待圈定
            new_p = _create_placement(
                conn, incident_id, asset, channel, account_id, req.locator, now_ts
            )
            conn.execute(
                "UPDATE observations SET incident_id=?, matched_placement_id=? WHERE observation_id=?",
                (incident_id, new_p["placement_id"], observation_id),
            )
            log_event(
                conn, incident_id, "placement.discovered", entity_type="placement",
                entity_ref=new_p["ref"], actor_id=actor_id,
                payload={"observation": obs_ref, "locator": req.locator},
            )
            result["action"] = "new_placement"
            result["placement_ref"] = new_p["ref"]

    if escalation is not None:
        conn.execute("UPDATE observations SET escalation_id=? WHERE observation_id=?",
                     (escalation["escalation_id"], observation_id))
        result["escalation_ref"] = escalation["ref"]
    return result


def _asset_was_ever_resolved(conn: sqlite3.Connection, asset_id: str) -> bool:
    row = one(
        conn,
        "SELECT COUNT(*) FROM placements WHERE asset_id=? AND resolved_at IS NOT NULL",
        (asset_id,),
    )
    return row[0] > 0


def _create_placement(
    conn, incident_id, asset, channel, account_id, locator, now_ts, status="identified"
) -> sqlite3.Row:
    pid = new_id()
    ref = placement_ref(conn)
    conn.execute(
        "INSERT INTO placements(placement_id, ref, incident_id, asset_id, channel_id, account_id, "
        "locator, owner_org_type, owner_org_id, status, first_seen_at, last_observed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, ref, incident_id, asset["asset_id"], channel["channel_id"], account_id, locator,
         channel["owner_org_type"], channel["owner_org_id"], status, now_ts, now_ts),
    )
    return one(conn, "SELECT * FROM placements WHERE placement_id=?", (pid,))


def _create_recurring_placement(conn, incident_id, asset, channel, account_id, locator, now_ts):
    return _create_placement(
        conn, incident_id, asset, channel, account_id, locator, now_ts, status="recurred"
    )


def _reopen_placement(
    conn, placement, asset_id, now_ts, obs_ref, observation_id, incident_id, actor_id, *, reason
) -> dict[str, Any]:
    conn.execute(
        "UPDATE placements SET status='recurred', asset_id=?, last_observed_at=?, overdue_flag=0, "
        "resolved_at=NULL WHERE placement_id=?",
        (asset_id, now_ts, placement["placement_id"]),
    )
    esc = open_escalation(
        conn, incident_id, "recurrence", reason=reason, actor_id=actor_id,
        notice_id=placement["current_notice_id"], placement_id=placement["placement_id"],
    )
    # 监测期事件重新激活
    conn.execute("UPDATE incidents SET status='open', closed_at=NULL WHERE incident_id=?",
                 (incident_id,))
    log_event(
        conn, incident_id, "placement.recurred", entity_type="placement",
        entity_ref=placement["ref"], actor_id=actor_id,
        payload={"observation": obs_ref, "reason": reason, "escalation": esc["ref"]},
    )
    return esc


def scan_overdue(conn: sqlite3.Connection, actor_id: str | None = None) -> dict[str, Any]:
    """把已过期限但未闭环的点位/通知标记逾期（幂等），供管理层实时掌握。"""

    now_ts = clock.now_ts()
    rows = conn.execute(
        "SELECT p.* FROM placements p WHERE p.status IN ('scoped','notified','recurred') "
        "AND p.deadline IS NOT NULL AND p.deadline < ? AND p.overdue_flag = 0",
        (now_ts,),
    ).fetchall()
    flagged: list[str] = []
    incidents: set[str] = set()
    for r in rows:
        conn.execute("UPDATE placements SET overdue_flag=1 WHERE placement_id=?",
                     (r["placement_id"],))
        flagged.append(r["ref"])
        incidents.add(r["incident_id"])
        log_event(
            conn, r["incident_id"], "placement.overdue", entity_type="placement",
            entity_ref=r["ref"], actor_id=actor_id, payload={"deadline": r["deadline"]},
        )
    # 通知逾期是派生事实：期限已过且名下仍有未闭环点位；记录事件但不改通知状态
    overdue_notices = conn.execute(
        "SELECT n.ref, n.incident_id FROM notices n "
        "WHERE n.status IN ('issued','delivered','failed') AND n.due_at < ? "
        "AND EXISTS (SELECT 1 FROM notice_placements np "
        "JOIN placements p ON np.placement_id=p.placement_id WHERE np.notice_id=n.notice_id "
        "AND p.status != 'resolved')",
        (now_ts,),
    ).fetchall()
    for nr in overdue_notices:
        log_event(conn, nr["incident_id"], "notice.overdue", entity_type="notice",
                  entity_ref=nr["ref"], actor_id=actor_id, payload={})
    return {"overdue_placements": flagged,
            "overdue_notices": [nr["ref"] for nr in overdue_notices],
            "checked_at": now_ts}


def coverage(conn: sqlite3.Connection, incident_ref_: str | None = None) -> dict[str, Any]:
    where, params = "", []
    if incident_ref_:
        where, params = " WHERE i.ref = ?", [incident_ref_]
    rows = conn.execute(
        "SELECT p.status AS status, COUNT(*) AS n, SUM(p.overdue_flag) AS overdue "
        "FROM placements p JOIN incidents i ON p.incident_id=i.incident_id"
        + where + " GROUP BY p.status",
        params,
    ).fetchall()
    by_status = {r["status"]: {"count": r["n"], "overdue": r["overdue"] or 0} for r in rows}
    total = sum(v["count"] for v in by_status.values())
    resolved = by_status.get("resolved", {}).get("count", 0)

    esc_rows = conn.execute(
        "SELECT e.type AS type, e.status AS status, COUNT(*) AS n FROM escalations e "
        "JOIN incidents i ON e.incident_id=i.incident_id"
        + where + " GROUP BY e.type, e.status",
        params,
    ).fetchall()
    escalations: dict[str, dict[str, int]] = {}
    for r in esc_rows:
        escalations.setdefault(r["type"], {})[r["status"]] = r["n"]

    notice_rows = conn.execute(
        "SELECT n.status AS status, COUNT(*) AS c FROM notices n "
        "JOIN incidents i ON n.incident_id=i.incident_id"
        + where + " GROUP BY n.status",
        params,
    ).fetchall()

    overdue_total = sum(v["overdue"] for v in by_status.values())
    return {
        "incident_ref": incident_ref_,
        "as_of": clock.now_ts(),
        "placements_total": total,
        "by_status": by_status,
        "resolved": resolved,
        "coverage_rate": round(resolved / total, 4) if total else 1.0,
        "overdue_total": overdue_total,
        "notices": {r["status"]: r["c"] for r in notice_rows},
        "escalations": escalations,
    }


def overdue_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT p.ref AS placement_ref, i.ref AS incident_ref, c.code AS channel_code, p.locator, "
        "p.deadline, p.status, p.owner_org_type, p.owner_org_id "
        "FROM placements p JOIN incidents i ON p.incident_id=i.incident_id "
        "JOIN channels c ON p.channel_id=c.channel_id "
        "WHERE p.overdue_flag=1 AND p.status != 'resolved' ORDER BY p.deadline"
    ).fetchall()
    return [dict(r) for r in rows]


def close_incident(conn: sqlite3.Connection, incident_ref_: str, actor_id: str) -> dict[str, Any]:
    from app.services.incidents import get_incident

    incident = get_incident(conn, incident_ref_)
    unresolved = one(
        conn, "SELECT COUNT(*) FROM placements WHERE incident_id=? AND status != 'resolved'",
        (incident["incident_id"],),
    )[0]
    open_esc = one(
        conn, "SELECT COUNT(*) FROM escalations WHERE incident_id=? AND status='open'",
        (incident["incident_id"],),
    )[0]
    if unresolved:
        raise Conflict(f"仍有 {unresolved} 个点位未闭环，不能结束处置")
    if open_esc:
        raise Conflict(f"仍有 {open_esc} 条未关闭升级，不能结束处置")
    now_ts = clock.now_ts()
    # 处置结束转入持续监测；监测发现复发会自动重新激活
    conn.execute("UPDATE incidents SET status='monitoring', closed_at=? WHERE incident_id=?",
                 (now_ts, incident["incident_id"]))
    log_event(
        conn, incident["incident_id"], "incident.phase_monitoring", entity_type="incident",
        entity_ref=incident_ref_, actor_id=actor_id,
        payload={"resolved_placements": _count(conn, incident["incident_id"], "resolved")},
    )
    return {"ref": incident_ref_, "status": "monitoring", "closed_at": now_ts,
            "message": "处置阶段结束，持续监测继续运行；发现复发将自动升级"}


def _count(conn, incident_id: str, status: str) -> int:
    return one(
        conn,
        "SELECT COUNT(*) FROM placements WHERE incident_id=? AND status=?", (incident_id, status)
    )[0]


def timeline(conn: sqlite3.Connection, incident_ref_: str) -> list[dict[str, Any]]:
    from app.serializers import timeline_item

    incident = one(conn, "SELECT * FROM incidents WHERE ref=?", (incident_ref_,))
    if incident is None:
        raise NotFound(f"事件 {incident_ref_} 不存在")
    rows = conn.execute(
        "SELECT * FROM event_log WHERE incident_id=? ORDER BY occurred_at, event_id",
        (incident["incident_id"],),
    ).fetchall()
    items = []
    for r in rows:
        d = timeline_item(r)
        d["incident_ref"] = incident_ref_
        items.append(d)
    return items
