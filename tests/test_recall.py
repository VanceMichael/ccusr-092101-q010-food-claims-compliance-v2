"""多渠道撤回与替换后端的行为测试。"""

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app import db
from app.main import app

OWNER = {"X-Actor-Ref": "RO-1", "X-Actor-Role": "risk_owner"}
REVIEWER = {"X-Actor-Ref": "REV-1", "X-Actor-Role": "reviewer"}
MANAGEMENT = {"X-Actor-Ref": "MG-1", "X-Actor-Role": "management"}
ENDORSER = {"X-Actor-Ref": "STAR-TEAM", "X-Actor-Role": "endorsement_team"}
DEALER_1 = {"X-Actor-Ref": "DEALER-1", "X-Actor-Role": "channel_owner"}
DEALER_2 = {"X-Actor-Ref": "DEALER-2", "X-Actor-Role": "channel_owner"}
PLATFORM = {"X-Actor-Ref": "SYS-PLATFORM", "X-Actor-Role": "system"}

FUTURE = "2026-12-31T23:59:59+08:00"
PAST = "2026-09-01T00:00:00+08:00"


class RecallApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["DATABASE_PATH"] = os.path.join(self.tmp.name, "test.sqlite3")
        with db.connect() as conn:
            db.migrate(conn)
        self.client = TestClient(app)
        self.seq = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("DATABASE_PATH", None)

    # ------------------------------------------------------------ 工具

    def event(self, **extra):
        self.seq += 1
        return {"source_system": "risk-console", "source_seq": f"seq-{self.seq}", **extra}

    def post(self, path, body, headers=OWNER):
        return self.client.post(path, json=body, headers=headers)

    def get(self, path, headers=OWNER):
        return self.client.get(path, headers=headers)

    def seed_materials(self):
        self.assertEqual(self.post("/recall/materials", self.event(
            material_ref="MAT-ORIG", fingerprint_sha256="FP-ORIG")).status_code, 201)
        self.assertEqual(self.post("/recall/materials", self.event(
            material_ref="MAT-CROP", fingerprint_sha256="FP-CROP",
            parent_ref="MAT-ORIG", derivative_kind="cropped")).status_code, 201)
        self.assertEqual(self.post("/recall/materials", self.event(
            material_ref="MAT-REW", fingerprint_sha256="FP-REW",
            parent_ref="MAT-ORIG", derivative_kind="reworded")).status_code, 201)

    def seed_entries(self):
        entries = [
            dict(entry_ref="E1", material_ref="MAT-ORIG", channel_code="self_store",
                 account_ref="ACCT-SELF", channel_owner_ref="DEALER-1",
                 locations=["https://shop.example.com/p/1"]),
            dict(entry_ref="E2", material_ref="MAT-CROP", channel_code="dealer_video",
                 account_ref="ACCT-D1", channel_owner_ref="DEALER-1",
                 locations=["https://video.example.com/v/1", "https://video.example.com/v/2"]),
            dict(entry_ref="E3", material_ref="MAT-REW", channel_code="offline_screen",
                 account_ref="ACCT-SCREEN", channel_owner_ref="DEALER-2",
                 locations=["SCREEN-SH-001"]),
            dict(entry_ref="E4", material_ref="MAT-ORIG", channel_code="endorser_team",
                 account_ref="ACCT-STAR", channel_owner_ref="AGENCY-1",
                 locations=["https://weibo.example.com/post/9"]),
        ]
        for entry in entries:
            self.assertEqual(self.post("/recall/propagation-entries", self.event(**entry)).status_code, 201)

    def seed_case(self, requires_replacement=False, deadline=FUTURE, ack=FUTURE, case_ref="CASE-1"):
        response = self.post("/recall/cases", self.event(
            case_ref=case_ref, risk_notice_ref="RISK-2026-091",
            reason="喉糖宣传易使消费者误认为含药材或具药品功效",
            material_refs=["MAT-ORIG", "MAT-CROP", "MAT-REW"],
            deadline_at=deadline, ack_deadline_at=ack,
            requires_replacement=requires_replacement))
        self.assertEqual(response.status_code, 201)
        return response.json()

    def seed_notices(self, case_ref="CASE-1"):
        response = self.post(f"/recall/cases/{case_ref}/issue-notices", self.event())
        self.assertEqual(response.status_code, 201)
        return response.json()["issued"]

    def seed_all(self, requires_replacement=False, deadline=FUTURE, ack=FUTURE):
        self.seed_materials()
        self.seed_entries()
        self.seed_case(requires_replacement=requires_replacement, deadline=deadline, ack=ack)
        return self.seed_notices()

    def close_task(self, notice_no, locations):
        """以两类独立结果 + 不同提交人关闭任务（不含替代素材要求的情形）。"""
        r1 = self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="platform_callback", callback_id=f"cb-{notice_no}",
            submitted_by="SYS-PLATFORM", locations_cleared=locations), headers=PLATFORM)
        self.assertEqual(r1.status_code, 201)
        r2 = self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="field_recheck", submitted_by="RO-1",
            locations_cleared=locations, final=True))
        self.assertEqual(r2.status_code, 201)
        return r2.json()

    # ------------------------------------------------------------ 传播清单与通知签发

    def test_manifest_and_unique_notice_numbers(self):
        issued = self.seed_all()
        self.assertEqual(len(issued), 4)
        notice_numbers = [item["notice_no"] for item in issued]
        self.assertEqual(len(set(notice_numbers)), 4, "处置通知编号必须唯一")

        manifest = self.get("/recall/cases/CASE-1/manifest").json()
        self.assertEqual(manifest["risk_notice_ref"], "RISK-2026-091")
        self.assertEqual(len(manifest["entries"]), 4)
        e2 = next(e for e in manifest["entries"] if e["entry_ref"] == "E2")
        self.assertEqual(e2["derivative_kind"], "cropped")
        self.assertEqual(e2["parent_ref"], "MAT-ORIG")
        self.assertEqual(e2["locations"], ["https://video.example.com/v/1", "https://video.example.com/v/2"])
        self.assertIsNotNone(e2["notice_no"])

    def test_derivative_requires_parent(self):
        self.seed_materials()
        response = self.post("/recall/materials", self.event(
            material_ref="MAT-ORPHAN", fingerprint_sha256="FP-X", derivative_kind="cropped"))
        self.assertEqual(response.status_code, 422)

    def test_open_case_marks_materials_withdrawn(self):
        self.seed_all()
        material = self.get("/recall/materials/MAT-ORIG").json()
        self.assertEqual(material["status"], "withdrawn")

    # ------------------------------------------------------------ 关闭判定与重复回调

    def test_duplicate_callback_cannot_close_task(self):
        issued = self.seed_all()
        notice_no = issued[0]["notice_no"]
        locations = ["https://shop.example.com/p/1"]

        first = self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="platform_callback", callback_id="cb-1",
            submitted_by="SYS-PLATFORM", locations_cleared=locations), headers=PLATFORM)
        self.assertEqual(first.status_code, 201)
        self.assertFalse(first.json()["duplicate"])

        # 同一回调重复推送：留档但不计入，任务不得关闭
        dup = self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="platform_callback", callback_id="cb-1",
            submitted_by="SYS-PLATFORM", locations_cleared=locations), headers=PLATFORM)
        self.assertEqual(dup.status_code, 201)
        self.assertTrue(dup.json()["duplicate"])
        self.assertNotEqual(dup.json()["task_status"], "closed")

        # 两条同类型回调（不同 callback_id）也不满足“两类独立来源”
        second_cb = self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="platform_callback", callback_id="cb-2",
            submitted_by="SYS-PLATFORM", locations_cleared=locations), headers=PLATFORM)
        self.assertFalse(second_cb.json()["closable"])

        # 第二类来源（现场复核、不同提交人）到达后才允许关闭
        final = self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="field_recheck", submitted_by="RO-1",
            locations_cleared=locations, final=True))
        self.assertEqual(final.json()["task_status"], "closed")
        self.assertTrue(final.json()["case_closed"] is False)  # 其余任务未关闭

    def test_event_idempotency_replay(self):
        issued = self.seed_all()
        notice_no = issued[0]["notice_no"]
        body = self.event(evidence_type="platform_callback", callback_id="cb-9",
                          submitted_by="SYS-PLATFORM",
                          locations_cleared=["https://shop.example.com/p/1"])
        first = self.post(f"/recall/notices/{notice_no}/evidence", body, headers=PLATFORM)
        replay = self.post(f"/recall/notices/{notice_no}/evidence", body, headers=PLATFORM)
        self.assertFalse(first.json()["deduplicated"])
        self.assertTrue(replay.json()["deduplicated"])
        self.assertEqual(first.json()["evidence_ref"], replay.json()["evidence_ref"])

    # ------------------------------------------------------------ 替代素材与放行凭证

    def test_replacement_requires_independent_credential(self):
        issued = self.seed_all(requires_replacement=True)
        notice_no = issued[0]["notice_no"]
        locations = ["https://shop.example.com/p/1"]

        self.post("/recall/materials", self.event(
            material_ref="MAT-NEW", fingerprint_sha256="FP-NEW"))

        # 旧素材已撤回，但无放行凭证的替代素材不能关闭任务
        self.assertEqual(self.get("/recall/materials/MAT-ORIG").json()["status"], "withdrawn")
        self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="platform_callback", callback_id="cb-r1",
            submitted_by="SYS-PLATFORM", locations_cleared=locations), headers=PLATFORM)
        r2 = self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="manual_screenshot", submitted_by="DEALER-1",
            locations_cleared=locations, final=True), headers=DEALER_1)
        self.assertFalse(r2.json()["closable"], "缺少放行凭证不得关闭")
        self.assertIn("缺少替代素材或放行凭证", ";".join(r2.json()["missing"]))

        # 凭证与素材不匹配 → 拒绝
        self.post("/recall/credentials", self.event(
            credential_ref="CRED-OTHER", material_ref="MAT-ORIG"), headers=REVIEWER)
        mismatch = self.post(f"/recall/notices/{notice_no}/replacement", self.event(
            material_ref="MAT-NEW", credential_ref="CRED-OTHER"), headers=DEALER_1)
        self.assertEqual(mismatch.status_code, 422)

        # 独立审核签发匹配凭证 → 注册即关闭
        self.post("/recall/credentials", self.event(
            credential_ref="CRED-NEW", material_ref="MAT-NEW"), headers=REVIEWER)
        ok = self.post(f"/recall/notices/{notice_no}/replacement", self.event(
            material_ref="MAT-NEW", credential_ref="CRED-NEW"), headers=DEALER_1)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["task_status"], "closed")

    def test_credential_from_case_owner_is_not_independent(self):
        issued = self.seed_all(requires_replacement=True)
        notice_no = issued[0]["notice_no"]
        self.post("/recall/materials", self.event(
            material_ref="MAT-NEW", fingerprint_sha256="FP-NEW"))
        # 风险负责人自己兼任审核：凭证无效（reviewer_ref == owner_ref）
        self.post("/recall/credentials", self.event(
            credential_ref="CRED-SELF", material_ref="MAT-NEW"),
            headers={"X-Actor-Ref": "RO-1", "X-Actor-Role": "reviewer"})
        self.close_task_with_evidence(notice_no, ["https://shop.example.com/p/1"], expect_close=False)

    def close_task_with_evidence(self, notice_no, locations, expect_close):
        self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="platform_callback", callback_id=f"cb-{notice_no}",
            submitted_by="SYS-PLATFORM", locations_cleared=locations), headers=PLATFORM)
        self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="field_recheck", submitted_by="RO-1", locations_cleared=locations))
        self.post(f"/recall/notices/{notice_no}/replacement", self.event(
            material_ref="MAT-NEW", credential_ref="CRED-SELF"), headers=DEALER_1)
        task = [n for n in self.get("/recall/notices?case_ref=CASE-1").json()["notices"]
                if n["notice_no"] == notice_no][0]
        self.assertEqual(task["status"] == "closed", expect_close)

    # ------------------------------------------------------------ 四条升级路径

    def test_unreachable_escalation_via_sweep(self):
        issued = self.seed_all(ack=PAST)
        notice_no = issued[0]["notice_no"]
        sweep = self.post("/recall/cases/CASE-1/sweep", self.event())
        escalated = sweep.json()["unreachable_escalated"]
        self.assertEqual(len(escalated), 4, "全部未签收任务都应进入无法联系升级")

        # 升级未结时任务不得关闭
        result = self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="platform_callback", callback_id="cb-u1",
            submitted_by="SYS-PLATFORM",
            locations_cleared=["https://shop.example.com/p/1"]), headers=PLATFORM)
        self.assertFalse(result.json()["closable"])

        esc_ref = escalated[0]["escalation_ref"]
        self.post(f"/recall/escalations/{esc_ref}/resolve", self.event(detail="已电话确认"))
        closed = self.close_task(notice_no, ["https://shop.example.com/p/1"])
        self.assertEqual(closed["task_status"], "closed")

    def test_refused_escalation(self):
        issued = self.seed_all()
        notice_no = issued[1]["notice_no"]
        response = self.post(f"/recall/notices/{notice_no}/refuse",
                             self.event(detail="经销商拒绝下架"), headers=DEALER_1)
        self.assertEqual(response.json()["status"], "escalated")
        coverage = self.get("/recall/dashboard/coverage?case_ref=CASE-1", headers=MANAGEMENT).json()
        self.assertEqual(coverage["cases"][0]["open_escalations"], {"refused": 1})

    def test_partial_replacement_escalation(self):
        issued = self.seed_all()
        notice_no = issued[1]["notice_no"]  # E2 有两个展示位置
        self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="platform_callback", callback_id="cb-p1",
            submitted_by="SYS-PLATFORM",
            locations_cleared=["https://video.example.com/v/1"]), headers=PLATFORM)
        final = self.post(f"/recall/notices/{notice_no}/evidence", self.event(
            evidence_type="manual_screenshot", submitted_by="DEALER-1",
            locations_cleared=["https://video.example.com/v/1"], final=True), headers=DEALER_1)
        body = final.json()
        self.assertEqual(body["task_status"], "escalated")
        self.assertIn("escalation_ref", body)
        coverage = self.get("/recall/dashboard/coverage?case_ref=CASE-1", headers=MANAGEMENT).json()
        self.assertEqual(coverage["cases"][0]["open_escalations"], {"partial_replacement": 1})

    def test_reappearance_reopens_closed_task(self):
        issued = self.seed_all()
        notice_no = issued[0]["notice_no"]
        self.close_task(notice_no, ["https://shop.example.com/p/1"])

        sighting = self.post("/recall/sightings", self.event(
            fingerprint_sha256="FP-ORIG", seen_at="2026-09-20T10:00:00+08:00",
            channel_code="self_store", account_ref="ACCT-SELF",
            location_ref="https://shop.example.com/p/1"), headers=DEALER_1)
        body = sighting.json()
        self.assertTrue(body["matched"])
        self.assertIn(notice_no, body["reopened_notices"])

        task = [n for n in self.get("/recall/notices?case_ref=CASE-1").json()["notices"]
                if n["notice_no"] == notice_no][0]
        self.assertEqual(task["status"], "escalated")
        self.assertIsNone(task["closed_at"])
        coverage = self.get("/recall/dashboard/coverage?case_ref=CASE-1", headers=MANAGEMENT).json()
        self.assertEqual(coverage["cases"][0]["open_escalations"], {"reappeared": 1})

    def test_sighting_without_match_is_recorded(self):
        self.seed_all()
        sighting = self.post("/recall/sightings", self.event(
            fingerprint_sha256="FP-UNKNOWN", seen_at="2026-09-20T10:00:00+08:00"))
        self.assertFalse(sighting.json()["matched"])

    # ------------------------------------------------------------ 权限边界

    def test_dealer_sees_only_own_points(self):
        self.seed_all()
        notices = self.get("/recall/notices?case_ref=CASE-1", headers=DEALER_1).json()["notices"]
        self.assertEqual({n["assignee_ref"] for n in notices}, {"DEALER-1"})

        other_notice = [n for n in self.get("/recall/notices?case_ref=CASE-1").json()["notices"]
                        if n["assignee_ref"] == "DEALER-2"][0]
        forbidden = self.post(f"/recall/notices/{other_notice['notice_no']}/ack",
                              self.event(), headers=DEALER_1)
        self.assertEqual(forbidden.status_code, 403)

        manifest = self.get("/recall/cases/CASE-1/manifest", headers=DEALER_2).json()
        self.assertEqual({e["channel_owner_ref"] for e in manifest["entries"]}, {"DEALER-2"})

    def test_endorsement_team_cannot_see_formula_or_consumer(self):
        self.seed_all()
        self.post("/recall/materials", self.event(
            material_ref="MAT-FORMULA", fingerprint_sha256="FP-FORM", material_class="formula"))
        denied = self.get("/recall/materials/MAT-FORMULA", headers=ENDORSER)
        self.assertEqual(denied.status_code, 403)
        allowed = self.get("/recall/materials/MAT-ORIG", headers=ENDORSER)
        self.assertEqual(allowed.status_code, 200)

        # 配方类素材进入传播清单后对代言团队脱敏
        self.post("/recall/propagation-entries", self.event(
            entry_ref="E9", material_ref="MAT-FORMULA", channel_code="self_store",
            account_ref="ACCT-SELF", channel_owner_ref="DEALER-1",
            locations=["https://shop.example.com/p/formula"]))
        self.post("/recall/cases", self.event(
            case_ref="CASE-2", risk_notice_ref="RISK-2026-092", reason="配方泄露核查",
            material_refs=["MAT-FORMULA"], deadline_at=FUTURE, ack_deadline_at=FUTURE))
        manifest = self.get("/recall/cases/CASE-2/manifest", headers=ENDORSER).json()
        self.assertEqual(manifest["entries"], [])
        self.assertEqual(manifest["redacted_entries"], 1)

    def test_management_readonly_dashboard(self):
        self.seed_all(deadline=PAST, ack=PAST)
        coverage = self.get("/recall/dashboard/coverage?case_ref=CASE-1", headers=MANAGEMENT).json()
        summary = coverage["cases"][0]
        self.assertEqual(summary["tasks_total"], 4)
        self.assertEqual(summary["tasks_closed"], 0)
        self.assertEqual(summary["overdue_open"], 4)

        forbidden = self.post("/recall/cases", self.event(
            case_ref="CASE-X", risk_notice_ref="R", reason="r", material_refs=["MAT-ORIG"],
            deadline_at=FUTURE, ack_deadline_at=FUTURE), headers=MANAGEMENT)
        self.assertEqual(forbidden.status_code, 403)
        dealer_denied = self.get("/recall/dashboard/coverage", headers=DEALER_1)
        self.assertEqual(dealer_denied.status_code, 403)

    def test_missing_identity_headers_rejected(self):
        response = self.client.get("/recall/notices")
        self.assertEqual(response.status_code, 401)

    # ------------------------------------------------------------ 追溯与看板

    def test_trace_from_risk_notice_to_channel_closure(self):
        issued = self.seed_all()
        for item in issued:
            notice = [n for n in self.get("/recall/notices?case_ref=CASE-1").json()["notices"]
                      if n["notice_no"] == item["notice_no"]][0]
            self.close_task(item["notice_no"], notice["required_locations"])

        trace = self.get("/recall/cases/CASE-1/trace", headers=MANAGEMENT).json()
        self.assertEqual(trace["risk_notice_ref"], "RISK-2026-091")
        self.assertEqual(trace["status"], "closed")
        actions = [event["action"] for event in trace["timeline"]]
        self.assertEqual(actions[0], "case_opened")
        self.assertEqual(actions.count("task_closed"), 4)
        self.assertEqual(actions[-1], "case_closed")
        for task in trace["tasks"]:
            self.assertIsNotNone(task["closed_at"], "每个渠道都应有实际下架时间")

        coverage = self.get("/recall/dashboard/coverage?case_ref=CASE-1", headers=MANAGEMENT).json()
        self.assertEqual(coverage["cases"][0]["coverage"], 1.0)


if __name__ == "__main__":
    unittest.main()
