"""鉴权与数据范围。

角色：
- risk_owner / legal / compliance / field_auditor / executive / system：品牌内部
- dealer：经销商，只可见本主体 (org_type='dealer', org_id) 的点位与通知
- talent：代言团队，只可见本主体点位，且不可见配方与消费者材料
"""

from __future__ import annotations

import sqlite3
from typing import Any

from app.db import one
from app.errors import PermissionDenied, Unauthorized

INTERNAL_ROLES = {"risk_owner", "legal", "compliance", "field_auditor", "executive", "system"}

# 角色能力矩阵
CAN_MANAGE_INCIDENT = {"risk_owner"}
CAN_INGEST = {"risk_owner", "legal"}
CAN_SCOPE = {"risk_owner"}
CAN_ISSUE = {"risk_owner", "legal"}
CAN_VERIFY = {"risk_owner", "legal"}
CAN_APPROVE = {"compliance"}
CAN_OBSERVE = {"system", "field_auditor", "risk_owner", "legal"}
CAN_SCAN = {"system", "risk_owner", "legal"}
CAN_DASHBOARD = {"executive", "risk_owner", "legal"}
CAN_HANDLE_ESCALATION = {"risk_owner", "legal"}


class Actor:
    def __init__(self, row: sqlite3.Row) -> None:
        self.id: str = row["actor_id"]
        self.name: str = row["display_name"]
        self.role: str = row["role"]
        self.org_type: str | None = row["org_type"]
        self.org_id: str | None = row["org_id"]

    @property
    def is_internal(self) -> bool:
        return self.role in INTERNAL_ROLES

    @property
    def sees_sensitive(self) -> bool:
        # 代言团队看不到配方；其余内部角色可见
        return self.role != "talent"

    def require_role(self, allowed: set[str]) -> None:
        if self.role not in allowed:
            raise PermissionDenied(f"角色 {self.role} 无权执行该操作")

    def owns_org(self, org_type: str, org_id: str) -> bool:
        return self.org_type == org_type and self.org_id == org_id

    def can_access_placement(self, placement: sqlite3.Row | dict[str, Any]) -> bool:
        if self.is_internal:
            return True
        return self.owns_org(placement["owner_org_type"], placement["owner_org_id"])

    def require_placement(self, placement: sqlite3.Row | dict[str, Any]) -> None:
        if not self.can_access_placement(placement):
            raise PermissionDenied("经销商/代言团队只能处理自己的点位")


def authenticate(conn: sqlite3.Connection, x_api_token: str | None) -> Actor:
    if not x_api_token:
        raise Unauthorized("缺少 X-API-Token")
    row = one(conn, "SELECT * FROM actors WHERE api_token = ?", (x_api_token,))
    if row is None:
        raise Unauthorized("令牌无效")
    return Actor(row)
