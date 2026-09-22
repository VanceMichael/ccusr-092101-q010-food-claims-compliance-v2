"""HTTP 请求契约（Pydantic）。响应以字典返回，时间统一 ISO 8601 带偏移量。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Sha256 = str
Action = Literal["takedown", "replace"]
CoverageState = Literal["removed", "replaced", "still_present", "partial"]
VerifiedState = Literal["removed", "replaced", "mismatch", "missing"]


class IncidentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    product_ref: str = Field(min_length=1)
    formula_revision: int | None = None
    risk_type: str = Field(min_length=1, examples=["mistaken_medicinal_claim"])
    source_system: str = Field(min_length=1, description="最初风险通知来源系统")
    source_sequence: str = Field(min_length=1, description="来源系统内唯一序列号")
    detail_ref: str | None = None


class AssetIn(BaseModel):
    client_key: str = Field(min_length=1)
    kind: Literal["original", "derivative", "replacement"]
    sha256: Sha256
    derived_from_client_key: str | None = None
    transform_note: str | None = None
    consumer_material: bool = False

    @field_validator("sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("sha256 必须为 64 位十六进制")
        return v


class PlacementIn(BaseModel):
    client_key: str = Field(min_length=1)
    asset_client_key: str
    channel_code: str
    account_ref: str | None = None
    locator: str = Field(min_length=1, description="可核验位置：URL / 屏幕编号 / 页面路径")
    first_seen_at: str | None = None


class ManifestIngest(BaseModel):
    assets: list[AssetIn]
    placements: list[PlacementIn]


class ScopeRequest(BaseModel):
    placement_refs: list[str] | None = None
    default_action: Action = "takedown"
    actions: dict[str, Action] | None = None
    replacement_map: dict[str, str] | None = Field(
        default=None, description="点位编号 → 替代素材编号（仅 replace 需要）"
    )
    deadline_hours: float = Field(gt=0, le=24 * 30)


class IssueNoticesRequest(BaseModel):
    placement_refs: list[str] | None = None
    delivery_channel: Literal["email", "platform_api"] = "email"


class CoverageIn(BaseModel):
    placement_ref: str
    claimed_state: CoverageState
    claimed_replacement_ref: str | None = None


class EvidenceIn(BaseModel):
    notice_ref: str | None = None
    placement_ref: str | None = None
    kind: Literal["platform_callback", "manual_screenshot", "field_recheck", "contact_record"]
    delivery_id: str | None = Field(default=None, description="平台回调投递唯一编号，用于去重")
    source_system: str = Field(min_length=1)
    contact_result: Literal["unreachable", "refused", "acknowledged"] | None = None
    claimed_complete: bool = False
    summary: str | None = None
    attachment_sha256: str | None = None
    coverage: list[CoverageIn] = Field(default_factory=list)

    @field_validator("attachment_sha256")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("sha256 必须为 64 位十六进制")
        return v


class VerifyCoverage(BaseModel):
    placement_ref: str
    verified_state: VerifiedState


class EvidenceVerify(BaseModel):
    decision: Literal["verified", "rejected"]
    note: str | None = None
    coverage_results: list[VerifyCoverage] | None = None


class ReleaseApprovalIn(BaseModel):
    decision: Literal["approved", "rejected"]
    review_batch_ref: str = Field(min_length=1, description="独立审核批次编号")
    basis_note: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None


class EscalationAdvance(BaseModel):
    note: str
    reissue_notice: bool = False
    delivery_channel: Literal["email", "platform_api"] = "email"
    deadline_hours: float | None = Field(default=None, gt=0, le=24 * 30,
                                         description="重发通知的新期限；默认 48 小时")


class EscalationResolve(BaseModel):
    resolution_note: str


class ObservationIn(BaseModel):
    channel_code: str
    account_ref: str | None = None
    locator: str
    asset_sha256: str
    transform_note: str | None = None
    source: Literal["monitor", "report", "platform_feed"] = "monitor"
    observed_at: str | None = None

    @field_validator("asset_sha256")
    @classmethod
    def _sha(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("sha256 必须为 64 位十六进制")
        return v
