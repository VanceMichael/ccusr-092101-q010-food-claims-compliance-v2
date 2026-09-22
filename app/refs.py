"""引用编号：内部主键用短随机串；对外编号采用可读前缀 + 序号。

通知按事件内序号编号（N-INC-0001-0003），一个通知覆盖同一渠道负责人的多个点位；
点位复发重发通知会产生新编号，因此"重复通知/重复回调"可以严格区分。
"""

from __future__ import annotations

import uuid


def new_id() -> str:
    return uuid.uuid4().hex


def next_seq(conn, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] + 1


def incident_ref(conn) -> str:
    return f"INC-{next_seq(conn, 'incidents'):04d}"


def asset_ref(conn) -> str:
    return f"MAT-{next_seq(conn, 'assets'):04d}"


def placement_ref(conn) -> str:
    return f"PL-{next_seq(conn, 'placements'):04d}"


def notice_ref(conn, incident_ref: str) -> str:
    # 事件内序号
    n = conn.execute(
        "SELECT COUNT(*) FROM notices n JOIN incidents i ON n.incident_id = i.incident_id "
        "WHERE i.ref = ?",
        (incident_ref,),
    ).fetchone()[0] + 1
    return f"N-{incident_ref}-{n:03d}"


def evidence_ref(conn) -> str:
    return f"EV-{next_seq(conn, 'evidences'):04d}"


def escalation_ref(conn) -> str:
    return f"ESC-{next_seq(conn, 'escalations'):04d}"


def approval_ref(conn) -> str:
    return f"REL-{next_seq(conn, 'release_approvals'):04d}"


def observation_ref(conn) -> str:
    return f"OBS-{next_seq(conn, 'observations'):04d}"
