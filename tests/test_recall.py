"""多渠道撤回与替换后端端到端测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.clock import clock
from app.main import app
from scripts.migrate import migrate

RISK = "tok-risk-1"
LEGAL = "tok-legal-1"
COMP = "tok-comp-1"
AUDIT = "tok-audit-1"
EXEC = "tok-exec-1"
DEALER_E = "tok-dealer-east"
DEALER_W = "tok-dealer-west"
TALENT = "tok-talent-1"
SYSTEM = "tok-system"

SHA_O = "a" * 64
SHA_D = "b" * 64
SHA_R = "c" * 64
SHA_R2 = "d" * 64


def h(token: str, **extra) -> dict[str, str]:
    d = {"X-API-Token": token}
    d.update(extra)
    return d


class RecallTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "app.sqlite3"
        os.environ["DATABASE_PATH"] = str(self.db_path)
        migrate(self.db_path)
        self.client = TestClient(app)
        clock.set("2026-09-22T08:00:00Z")

    def tearDown(self) -> None:
        clock.set(None)
        self.tmp.cleanup()
        os.environ.pop("DATABASE_PATH", None)

    # --- 场景搭建辅助 ---

    def create_incident(self, seq: str = "RN-1") -> str:
        r = self.client.post("/v1/incidents", headers=h(RISK), json={
            "title": "喉糖涉嫌药效宣传",
            "product_ref": "PROD-LOZENGE",
            "formula_revision": 7,
            "risk_type": "mistaken_medicinal_claim",
            "source_system": "risk-hotline",
            "source_sequence": seq,
        })
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["ref"]

    def full_manifest(self, inc: str) -> dict[str, str]:
        """四个点位：直营页(替换)、东区短视频(下架)、线下屏(下架)、明星号(下架)。"""
        r = self.client.post(f"/v1/incidents/{inc}/manifest", headers=h(RISK), json={
            "assets": [
                {"client_key": "orig", "kind": "original", "sha256": SHA_O,
                 "consumer_material": True},
                {"client_key": "der", "kind": "derivative", "sha256": SHA_D,
                 "derived_from_client_key": "orig",
                 "transform_note": "裁剪并叠加 药食同源 字样"},
                {"client_key": "rep", "kind": "replacement", "sha256": SHA_R},
            ],
            "placements": [
                {"client_key": "p1", "asset_client_key": "orig",
                 "channel_code": "direct_store", "locator": "https://shop/p/lozenge"},
                {"client_key": "p2", "asset_client_key": "der",
                 "channel_code": "dealer_shortvideo", "account_ref": "DSV-77",
                 "locator": "https://video/v/77"},
                {"client_key": "p5", "asset_client_key": "der",
                 "channel_code": "dealer_shortvideo", "account_ref": "DSV-99",
                 "locator": "https://video/v/99"},
                {"client_key": "p3", "asset_client_key": "orig",
                 "channel_code": "offline_screen", "locator": "SCREEN-L4-12"},
                {"client_key": "p4", "asset_client_key": "der",
                 "channel_code": "celebrity_team", "account_ref": "TALENT-MAIN",
                 "locator": "https://talent/post/9"},
            ],
        })
        self.assertEqual(r.status_code, 201, r.text)
        loc = {p["locator"]: p["ref"] for p in r.json()["placements"]}
        assets = {a["kind"]: a["ref"] for a in r.json()["assets"]}
        return {"p1": loc["https://shop/p/lozenge"], "p2": loc["https://video/v/77"],
                "p5": loc["https://video/v/99"],
                "p3": loc["SCREEN-L4-12"], "p4": loc["https://talent/post/9"],
                "orig": assets["original"], "der": assets["derivative"],
                "rep": assets["replacement"]}

    def scope_all(self, inc: str, m: dict[str, str], hours: float = 24) -> None:
        r = self.client.post(f"/v1/incidents/{inc}/scope", headers=h(RISK), json={
            "default_action": "takedown",
            "deadline_hours": hours,
            "actions": {m["p1"]: "replace"},
            "replacement_map": {m["p1"]: m["rep"]},
        })
        self.assertEqual(r.status_code, 200, r.text)

    def issue(self, inc: str) -> list[dict]:
        r = self.client.post(f"/v1/incidents/{inc}/notices", headers=h(RISK), json={})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()["notices"]

    def approve(self, asset_ref: str, batch: str = "RB-1", **kw) -> None:
        payload = {"decision": "approved", "review_batch_ref": batch,
                   "basis_note": "独立审核：去除药效表述"}
        payload.update(kw)
        r = self.client.post(f"/v1/assets/{asset_ref}/release", headers=h(COMP), json=payload)
        self.assertEqual(r.status_code, 201, r.text)

    def notice_for(self, notices: list[dict], code: str) -> str:
        return next(n["ref"] for n in notices if n["channel_id"] == code)

    def submit_cb(self, inc: str, token: str, delivery: str, coverage: list[dict],
                  claimed_complete: bool = False, notice_ref: str | None = None,
                  kind: str = "platform_callback", source: str = "platform",
                  contact_result=None) -> dict:
        body = {"kind": kind, "source_system": source, "delivery_id": delivery,
                "claimed_complete": claimed_complete, "coverage": coverage}
        if notice_ref:
            body["notice_ref"] = notice_ref
        if contact_result:
            body["contact_result"] = contact_result
        r = self.client.post(f"/v1/incidents/{inc}/evidence", headers=h(token), json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def verify(self, ev_ref: str, decision: str = "verified", results=None) -> dict:
        body = {"decision": decision}
        if results:
            body["coverage_results"] = results
        r = self.client.post(f"/v1/evidence/{ev_ref}/verify", headers=h(RISK), json=body)
        self.assertIn(r.status_code, (200, 409), r.text)
        return r.json()


class LifecycleTest(RecallTestBase):
    def test_full_lifecycle_and_trace(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        self.scope_all(inc, m)
        notices = self.issue(inc)
        # 四个渠道负责人 → 四封带唯一编号的通知（东区一封覆盖两个账号点位）
        refs = [n["ref"] for n in notices]
        self.assertEqual(len(refs), 4)
        self.assertEqual(len(set(refs)), 4)
        n_direct = self.notice_for(notices, "ch-direct")

        # 替代素材未经独立放行，替换闭环必须被拦
        ev = self.submit_cb(inc, LEGAL, "CB-1",
                            [{"placement_ref": m["p1"], "claimed_state": "replaced",
                              "claimed_replacement_ref": m["rep"]}])
        out = self.verify(ev["ref"])
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(out["blocked"][0]["placement_ref"], m["p1"])
        self.assertIn("放行", out["blocked"][0]["reason"])

        # 独立合规审核放行（风险负责人无权放行）
        denied = self.client.post(f"/v1/assets/{m['rep']}/release", headers=h(RISK),
                                  json={"decision": "approved", "review_batch_ref": "RB-X"})
        self.assertEqual(denied.status_code, 403)
        self.approve(m["rep"], "RB-2026-3")

        # 重复回调：相同 delivery_id 不改变任何状态
        dup = self.submit_cb(inc, LEGAL, "CB-1",
                             [{"placement_ref": m["p1"], "claimed_state": "replaced",
                               "claimed_replacement_ref": m["rep"]}])
        self.assertTrue(dup["duplicate"])
        self.assertFalse(dup["applied"])

        # 放行后核验通过：点位闭环、通知完成
        ev2 = self.submit_cb(inc, LEGAL, "CB-2",
                             [{"placement_ref": m["p1"], "claimed_state": "replaced",
                               "claimed_replacement_ref": m["rep"]}])
        out = self.verify(ev2["ref"])
        self.assertEqual(out["status"], "verified")
        self.assertEqual(out["resolved"], [m["p1"]])

        # 通知自动完成
        r = self.client.get(f"/v1/incidents/{inc}/notices", headers=h(RISK))
        direct = next(n for n in r.json()["notices"] if n["ref"] == n_direct)
        self.assertEqual(direct["status"], "completed")
        self.assertIsNotNone(direct["completed_at"])

        # trace：从最初通知追到实际替换时间
        r = self.client.get(f"/v1/incidents/{inc}/trace", headers=h(RISK))
        self.assertEqual(r.status_code, 200)
        p1 = next(p for p in r.json()["placements"] if p["ref"] == m["p1"])
        self.assertEqual(p1["status"], "resolved")
        self.assertIsNotNone(p1["resolved_at"])
        types = [e["event_type"] for e in r.json()["timeline"]]
        self.assertIn("incident.opened", types)
        self.assertIn("notice.issued", types)
        self.assertIn("release.decided", types)
        self.assertIn("evidence.duplicate_received", types)

    def test_source_sequence_dedup(self) -> None:
        a = self.create_incident("SAME-1")
        b = self.create_incident("SAME-1")
        self.assertEqual(a, b)


class EscalationTest(RecallTestBase):
    def test_unreachable_and_overdue(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        self.scope_all(inc, m, hours=12)
        self.issue(inc)

        r = self.client.post(f"/v1/incidents/{inc}/evidence", headers=h(LEGAL), json={
            "kind": "contact_record", "source_system": "email-gateway",
            "contact_result": "unreachable", "placement_ref": m["p3"]})
        self.assertEqual(r.status_code, 201)
        esc = r.json()["escalations"]
        self.assertEqual(len(esc), 1)

        # 升级路径：逐级上升
        er = self.client.get("/v1/escalations", headers=h(RISK)).json()["escalations"]
        e = next(x for x in er if x["type"] == "unreachable")
        self.assertEqual(e["level"], 1)
        adv = self.client.post(f"/v1/escalations/{e['ref']}/advance", headers=h(RISK),
                               json={"note": "转线下负责人"})
        self.assertEqual(adv.json()["level"], 2)

        # 逾期扫描
        clock.set("2026-09-22T21:00:00Z")
        r = self.client.post("/v1/scan/overdue", headers=h(RISK))
        self.assertIn(m["p3"], r.json()["overdue_placements"])
        # 再次扫描幂等
        r2 = self.client.post("/v1/scan/overdue", headers=h(RISK))
        self.assertEqual(r2.json()["overdue_placements"], [])

    def test_refused(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        self.scope_all(inc, m)
        self.issue(inc)
        r = self.client.post(f"/v1/incidents/{inc}/evidence", headers=h(LEGAL), json={
            "kind": "contact_record", "source_system": "email-gateway",
            "contact_result": "refused", "placement_ref": m["p4"]})
        self.assertEqual(r.status_code, 201)
        er = self.client.get("/v1/escalations", headers=h(RISK)).json()["escalations"]
        self.assertTrue(any(x["type"] == "refused" for x in er))
        # 点位未闭环
        r = self.client.get(f"/v1/incidents/{inc}/placements", headers=h(RISK))
        p4 = next(p for p in r.json()["placements"] if p["ref"] == m["p4"])
        self.assertNotEqual(p4["status"], "resolved")

    def test_partial_replace_claimed_complete(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        self.scope_all(inc, m)
        notices = self.issue(inc)
        east = next(n for n in notices if n["channel_id"] == "ch-dsv-east")
        self.assertEqual(set(east["placement_refs"]), {m["p2"], m["p5"]})

        # 经销商声称"全部完成"，凭证却只覆盖 p2 —— 不能据此关闭通知
        ev = self.submit_cb(inc, DEALER_E, "SS-1",
                            [{"placement_ref": m["p2"], "claimed_state": "removed"}],
                            claimed_complete=True, kind="manual_screenshot",
                            source="dealer-east", notice_ref=east["ref"])
        out = self.verify(ev["ref"])
        self.assertEqual(out["status"], "rejected")
        self.assertIn(m["p5"], out["still_open"])
        self.assertIsNotNone(out["escalation"])
        esc = self.client.get("/v1/escalations", headers=h(RISK)).json()["escalations"]
        self.assertTrue(any(x["type"] == "partial_replace" for x in esc))
        # p2 已闭环，但通知未完成（p5 缺失）
        nr = self.client.get(f"/v1/incidents/{inc}/notices", headers=h(RISK)).json()
        east_now = next(n for n in nr["notices"] if n["ref"] == east["ref"])
        self.assertNotEqual(east_now["status"], "completed")

    def test_partial_verdict_opens_escalation(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        self.scope_all(inc, m)
        notices = self.issue(inc)
        east = next(n for n in notices if n["channel_id"] == "ch-dsv-east")
        # 现场复核：同一批中有的已删、有的仍在
        r = self.client.post(f"/v1/incidents/{inc}/evidence", headers=h(AUDIT), json={
            "kind": "field_recheck", "source_system": "field-app",
            "notice_ref": east["ref"],
            "coverage": [{"placement_ref": m["p2"], "claimed_state": "still_present"}]})
        out = self.verify(r.json()["ref"])
        self.assertEqual(out["status"], "rejected")
        self.assertIn(m["p2"], out["still_open"])
        placements = self.client.get(f"/v1/incidents/{inc}/placements",
                                     headers=h(RISK)).json()["placements"]
        p2 = next(p for p in placements if p["ref"] == m["p2"])
        self.assertNotEqual(p2["status"], "resolved")


class RecurrenceTest(RecallTestBase):
    def test_recurrence_same_placement_then_reissue(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        self.scope_all(inc, m)
        notices = self.issue(inc)
        east_ref = self.notice_for(notices, "ch-dsv-east")

        ev = self.submit_cb(inc, DEALER_E, "CB-X",
                            [{"placement_ref": m["p2"], "claimed_state": "removed"}],
                            kind="manual_screenshot",
                            source="dealer-east")
        self.assertEqual(self.verify(ev["ref"])["status"], "verified")

        # 旧图在已闭环点位再现
        r = self.client.post("/v1/observations", headers=h(SYSTEM), json={
            "channel_code": "dealer_shortvideo", "account_ref": "DSV-77",
            "locator": "https://video/v/77", "asset_sha256": SHA_D})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["action"], "recurrence")
        esc_ref = r.json()["escalation_ref"]

        # 事件重新激活
        self.assertEqual(self.client.get(f"/v1/incidents/{inc}", headers=h(RISK))
                         .json()["status"], "open")

        # 升级处置：重发新一轮通知；旧通知仍覆盖同负责人名下其他点位(p5)，不作废
        adv = self.client.post(f"/v1/escalations/{esc_ref}/advance", headers=h(RISK), json={
            "note": "要求再次下架", "reissue_notice": True, "deadline_hours": 12})
        self.assertEqual(adv.status_code, 200)
        new_ref = adv.json()["reissued_notice"]
        self.assertNotEqual(new_ref, east_ref)
        notices_after = self.client.get(f"/v1/incidents/{inc}/notices", headers=h(RISK)).json()
        old = next(n for n in notices_after["notices"] if n["ref"] == east_ref)
        new = next(n for n in notices_after["notices"] if n["ref"] == new_ref)
        self.assertEqual(old["status"], "issued")
        self.assertEqual(old["placement_refs"], [m["p5"]])
        self.assertEqual(new["round"], 1)  # 点位初始 round=0，复发重发为第 1 轮
        self.assertEqual(new["placement_refs"], [m["p2"]])

    def test_recurrence_new_locator_for_withdrawn_asset(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        # 只处置东区派生件点位，使其素材达到 withdrawn
        self.client.post(f"/v1/incidents/{inc}/scope", headers=h(RISK), json={
            "placement_refs": [m["p2"]], "default_action": "takedown", "deadline_hours": 24})
        self.issue(inc)
        ev = self.submit_cb(inc, DEALER_E, "CB-Y",
                            [{"placement_ref": m["p2"], "claimed_state": "removed"}],
                            kind="manual_screenshot", source="dealer-east")
        self.verify(ev["ref"])
        # 换账号/新位置再次传播已撤回派生件
        r = self.client.post("/v1/observations", headers=h(SYSTEM), json={
            "channel_code": "dealer_shortvideo", "account_ref": "DSV-88",
            "locator": "https://video/v/88", "asset_sha256": SHA_D})
        self.assertEqual(r.json()["action"], "recurrence_new_placement")
        self.assertIn("escalation_ref", r.json())


class RbacTest(RecallTestBase):
    def test_dealer_and_talent_scoping(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        self.scope_all(inc, m)
        self.issue(inc)

        # 经销商只看自己点位（东区两个账号点位）
        r = self.client.get(f"/v1/incidents/{inc}/placements", headers=h(DEALER_E))
        self.assertEqual({p["ref"] for p in r.json()["placements"]}, {m["p2"], m["p5"]})
        r = self.client.get(f"/v1/incidents/{inc}/placements", headers=h(DEALER_W))
        self.assertEqual(r.json()["placements"], [])
        # 经销商不能圈定、不能放行、不能看管理层仪表盘
        self.assertEqual(self.client.post(f"/v1/incidents/{inc}/scope",
                                          headers=h(DEALER_E),
                                          json={"deadline_hours": 24}).status_code, 403)
        self.assertEqual(self.client.get("/v1/dashboard/coverage",
                                         headers=h(DEALER_E)).status_code, 403)
        # 经销商不能为别人点位提交凭证
        bad = self.client.post(f"/v1/incidents/{inc}/evidence", headers=h(DEALER_E), json={
            "kind": "manual_screenshot", "source_system": "dealer-east",
            "placement_ref": m["p3"],
            "coverage": [{"placement_ref": m["p3"], "claimed_state": "removed"}]})
        self.assertEqual(bad.status_code, 403)
        # 经销商不能登记平台回调/联系结果
        cb = self.client.post(f"/v1/incidents/{inc}/evidence", headers=h(DEALER_E), json={
            "kind": "platform_callback", "source_system": "x", "delivery_id": "Z",
            "coverage": [{"placement_ref": m["p2"], "claimed_state": "removed"}]})
        self.assertEqual(cb.status_code, 403)

        # 代言团队：点位受限且看不到配方与消费者材料
        r = self.client.get(f"/v1/incidents/{inc}", headers=h(TALENT))
        self.assertIsNone(r.json()["formula_revision"])
        r = self.client.get(f"/v1/incidents/{inc}/placements", headers=h(TALENT))
        p4 = next(p for p in r.json()["placements"] if p["ref"] == m["p4"])
        self.assertIsNone(p4["asset"]["consumer_material"])
        self.assertIsNone(p4["asset"]["transform_note"])
        # 指纹仍可见（处置需要核对内容）
        self.assertEqual(p4["asset"]["sha256"], SHA_D)

        # 通知列表同样受主体范围限制
        r = self.client.get(f"/v1/incidents/{inc}/notices", headers=h(TALENT))
        self.assertTrue(all(m["p4"] in n["placement_refs"] for n in r.json()["notices"]))

        # trace 时间线不得暴露其他渠道的点位/升级事件
        r = self.client.get(f"/v1/incidents/{inc}/trace", headers=h(TALENT))
        self.assertEqual([p["ref"] for p in r.json()["placements"]], [m["p4"]])
        refs = {(e["entity_type"], e["entity_ref"]) for e in r.json()["timeline"]}
        self.assertTrue(all(typ != "placement" or ref == m["p4"] for typ, ref in refs))
        self.assertTrue(all(typ != "escalation" for typ, _ in refs))
        # 但最初风险通知这一事件本身可见
        self.assertIn(("incident", inc), refs)

        # 管理层实时覆盖率
        r = self.client.get("/v1/dashboard/coverage", headers=h(EXEC))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["placements_total"], 5)

    def test_field_auditor_cannot_scope(self) -> None:
        inc = self.create_incident()
        self.full_manifest(inc)
        r = self.client.post(f"/v1/incidents/{inc}/scope", headers=h(AUDIT),
                             json={"deadline_hours": 24})
        self.assertEqual(r.status_code, 403)


class CloseAndIdempotencyTest(RecallTestBase):
    def test_close_requires_all_resolved(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        self.scope_all(inc, m)
        self.issue(inc)
        r = self.client.post(f"/v1/incidents/{inc}/close", headers=h(RISK))
        self.assertEqual(r.status_code, 409)

        self.approve(m["rep"])
        plan = [
            (LEGAL, "CB-1", m["p1"], "replaced", m["rep"], "platform_callback", "shop"),
            (DEALER_E, "SS-2", m["p2"], "removed", None, "manual_screenshot", "dealer"),
            (DEALER_E, "SS-5", m["p5"], "removed", None, "manual_screenshot", "dealer"),
            (LEGAL, "CB-3", m["p3"], "removed", None, "field_recheck", "field"),
            (TALENT, "SS-4", m["p4"], "removed", None, "manual_screenshot", "talent"),
        ]
        for token, dl, pl, state, rep, kind, src in plan:
            ev = self.submit_cb(inc, token, dl,
                                [{"placement_ref": pl, "claimed_state": state,
                                  **({"claimed_replacement_ref": rep} if rep else {})}],
                                kind=kind, source=src)
            self.assertEqual(self.verify(ev["ref"])["status"], "verified")

        r = self.client.post(f"/v1/incidents/{inc}/close", headers=h(RISK))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "monitoring")

        # 管理层覆盖率 100%
        cov = self.client.get(f"/v1/incidents/{inc}/coverage", headers=h(EXEC)).json()
        self.assertEqual(cov["coverage_rate"], 1.0)

    def test_idempotency_key_replays(self) -> None:
        payload = {"title": "t", "product_ref": "P", "risk_type": "x",
                   "source_system": "s", "source_sequence": "Q-1"}
        a = self.client.post("/v1/incidents", headers=h(RISK, **{"Idempotency-Key": "K1"}),
                             json=payload)
        b = self.client.post("/v1/incidents", headers=h(RISK, **{"Idempotency-Key": "K1"}),
                             json=payload)
        self.assertEqual(a.json()["ref"], b.json()["ref"])

    def test_expired_release_window_rejected(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        # 放行窗口已过
        self.approve(m["rep"], "RB-OLD",
                     valid_from="2026-01-01T00:00:00Z", valid_to="2026-02-01T00:00:00Z")
        self.scope_all(inc, m)
        self.issue(inc)
        ev = self.submit_cb(inc, LEGAL, "CB-E",
                            [{"placement_ref": m["p1"], "claimed_state": "replaced",
                              "claimed_replacement_ref": m["rep"]}])
        out = self.verify(ev["ref"])
        self.assertEqual(out["status"], "rejected")
        self.assertTrue(out["blocked"])


    def test_recurrence_after_close_reopens_incident(self) -> None:
        inc = self.create_incident()
        m = self.full_manifest(inc)
        self.scope_all(inc, m)
        self.issue(inc)
        self.approve(m["rep"])
        plan = [
            (LEGAL, "CB-1", m["p1"], "replaced", m["rep"], "platform_callback", "shop"),
            (DEALER_E, "SS-2", m["p2"], "removed", None, "manual_screenshot", "dealer"),
            (DEALER_E, "SS-5", m["p5"], "removed", None, "manual_screenshot", "dealer"),
            (LEGAL, "CB-3", m["p3"], "removed", None, "field_recheck", "field"),
            (TALENT, "SS-4", m["p4"], "removed", None, "manual_screenshot", "talent"),
        ]
        for token, dl, pl, state, rep, kind, src in plan:
            ev = self.submit_cb(inc, token, dl,
                                [{"placement_ref": pl, "claimed_state": state,
                                  **({"claimed_replacement_ref": rep} if rep else {})}],
                                kind=kind, source=src)
            self.verify(ev["ref"])
        closed = self.client.post(f"/v1/incidents/{inc}/close", headers=h(RISK))
        self.assertEqual(closed.json()["status"], "monitoring")

        # 监测阶段持续运行：原始件在明星团队另一账号再次出现
        r = self.client.post("/v1/observations", headers=h(SYSTEM), json={
            "channel_code": "celebrity_team", "account_ref": "TALENT-ALT",
            "locator": "https://talent/post/77", "asset_sha256": SHA_O,
            "source": "platform_feed"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["action"], "recurrence_new_placement")
        self.assertEqual(self.client.get(f"/v1/incidents/{inc}", headers=h(RISK))
                         .json()["status"], "open")
        esc = self.client.get("/v1/escalations?type=recurrence", headers=h(RISK)).json()
        self.assertTrue(any(x["incident_ref"] == inc for x in esc["escalations"]))


if __name__ == "__main__":
    unittest.main()
