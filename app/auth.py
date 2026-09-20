"""基于请求头的角色识别与数据可见性控制。

角色：
- risk_owner        风险负责人：圈定范围与期限、签发/升级/关闭
- channel_owner     渠道负责人（经销商）：仅可见并处理自己的点位
- endorsement_team  代言团队：不可见配方与消费者材料
- management        管理层：只读，掌握覆盖率与逾期风险
- reviewer          独立审核人：签发替代素材放行凭证
- system            外部系统：平台回调等机器来源
"""

from dataclasses import dataclass

from fastapi import Header, HTTPException

ROLES = {
    "risk_owner",
    "channel_owner",
    "endorsement_team",
    "management",
    "reviewer",
    "system",
}

# 代言团队不可见的素材类别
RESTRICTED_MATERIAL_CLASSES = {"formula", "consumer"}


@dataclass(frozen=True)
class Actor:
    ref: str
    role: str


def get_actor(
    x_actor_ref: str | None = Header(default=None),
    x_actor_role: str | None = Header(default=None),
) -> Actor:
    if not x_actor_ref or not x_actor_role:
        raise HTTPException(status_code=401, detail="缺少操作者身份头 X-Actor-Ref / X-Actor-Role")
    if x_actor_role not in ROLES:
        raise HTTPException(status_code=403, detail=f"未知角色：{x_actor_role}")
    return Actor(ref=x_actor_ref, role=x_actor_role)


def require_role(actor: Actor, *roles: str) -> None:
    if actor.role not in roles:
        raise HTTPException(status_code=403, detail=f"角色 {actor.role} 无权执行该操作")


def can_view_material(actor: Actor, material_class: str) -> bool:
    if actor.role == "endorsement_team" and material_class in RESTRICTED_MATERIAL_CLASSES:
        return False
    return True
