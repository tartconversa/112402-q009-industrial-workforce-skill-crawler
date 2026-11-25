"""容量、学员唯一确认、变更复核和顾问查询测试。"""

import unittest

from parkbridge import ParkService
from parkbridge.errors import (
    CapacityExceededError,
    DuplicateLearnerConfirmationError,
    ReviewRequiredError,
)
from parkbridge.sample import import_samples


class ReservationWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.service = ParkService.in_memory()
        self.addCleanup(self.service.close)
        import_samples(self.service)

    def test_reservation_cannot_exceed_course_capacity(self):
        # C3 容量 10，样例 R3 已确认占用 1。
        for index in range(9):
            rid = f"H{index}"
            self.service.reserve_seat(
                referral_id=rid,
                job_id="J1",
                course_id="C3",
                person=f"U{index}",
                override_unverified=True,
                override_reason="容量边界测试",
            )
        occupancy = self.service.course_occupancy("C3")
        self.assertEqual(occupancy["occupied_seats"], 10)
        self.assertEqual(occupancy["free_seats"], 0)

        with self.assertRaises(CapacityExceededError):
            self.service.reserve_seat(
                referral_id="TOO_MANY",
                job_id="J1",
                course_id="C3",
                person="OVER",
                override_unverified=True,
                override_reason="容量边界测试",
            )

    def test_same_confirmed_learner_cannot_occupy_another_provider(self):
        self.service.reserve_seat(referral_id="A1", job_id="J1", course_id="C1", person="U9")
        self.service.confirm_referral("A1")

        self.service.reserve_seat(referral_id="A2", job_id="J2", course_id="C2", person="U9")
        with self.assertRaises(DuplicateLearnerConfirmationError):
            self.service.confirm_referral("A2")

        # 未确认的第二个预留仍可释放，释放后容量归还。
        self.service.release_referral("A2", reason="学员已选择 T-1")
        self.assertEqual(self.service.get_referral("A2")["state"], "released")

    def test_confirmed_learner_can_confirm_another_course_at_same_provider(self):
        # C1 provider T-1；再建一门 T-1 课程。
        self.service.add_course("C9", provider="T-1", skills=["PLC", "diagnostics"], seats=3, start="2026-12-10")
        self.service.reserve_seat(referral_id="A1", job_id="J1", course_id="C1", person="U9")
        self.service.confirm_referral("A1")
        self.service.reserve_seat(referral_id="A2", job_id="J1", course_id="C9", person="U9")
        confirmed = self.service.confirm_referral("A2")
        self.assertEqual(confirmed["state"], "confirmed")

    def test_job_withdrawal_rechecks_unconfirmed_but_preserves_confirmed_basis(self):
        # R1 held 未确认；R3 是另一个已确认样例。为同一岗位构造 confirmed。
        self.service.reserve_seat(referral_id="MINE", job_id="J1", course_id="C1", person="U10")
        self.service.confirm_referral("MINE")
        snapshot_before = self.service.get_referral("MINE")["basis_snapshot"]
        self.assertIsNotNone(snapshot_before)

        self.service.confirm_job("J1", is_hiring=False, confirmer="liaison-li")

        r1 = self.service.get_referral("R1")
        self.assertEqual(r1["state"], "needs_review")
        self.assertIn("job_withdrawn", r1["review_reasons"])
        self.assertEqual(r1["seat_held"], 1)  # 重新核对前不自动释放，避免席位凭空消失。

        mine = self.service.get_referral("MINE")
        self.assertEqual(mine["state"], "confirmed")
        self.assertEqual(mine["basis_snapshot"], snapshot_before)
        self.assertTrue(mine["contact_learner"])
        self.assertFalse(mine["contact_company"])  # 企业联络员已确认撤回。

        with self.assertRaises(ReviewRequiredError):
            self.service.confirm_referral("R1", person="U-1")

        resolved = self.service.resolve_review("R1", action="release", note="岗位撤回，释放席位")
        self.assertEqual(resolved["state"], "released")
        self.assertEqual(self.service.course_occupancy("C1")["occupied_seats"], 1)  # 只剩 MINE

    def test_job_skill_change_triggers_match_recheck(self):
        self.service.confirm_job("J1", required_skills=["PLC", "calibration", "safety"])
        r1 = self.service.get_referral("R1")
        self.assertEqual(r1["state"], "needs_review")
        self.assertIn("job_skills_changed", r1["review_reasons"])
        assessment = r1["auto_assessment"]
        self.assertIn("match", assessment)
        self.assertFalse(assessment["still_satisfied"])

        rechecked = self.service.recheck_referral("R1")
        self.assertIn("match", rechecked["auto_assessment"])

    def test_course_reschedule_flags_unconfirmed_and_confirmed(self):
        self.service.confirm_course("C1", start="2027-01-20", confirmer="owner-wang")
        self.service.confirm_course("C3", start="2027-02-10", confirmer="owner-zhao")

        r1 = self.service.get_referral("R1")
        self.assertEqual(r1["state"], "needs_review")
        self.assertIn("course_rescheduled", r1["review_reasons"])

        # 已确认 R3 保留旧日期快照，但提示联系学员。
        r3 = self.service.get_referral("R3")
        self.assertEqual(r3["state"], "confirmed")
        self.assertEqual(r3["basis_snapshot"]["course_basis"]["start_date"], "2026-11-15")
        self.assertTrue(r3["contact_learner"])

    def test_advisor_query_contains_reason_free_seats_pending_facts_and_history(self):
        report = self.service.advisor_report(company="C-1", skill="diagnostics")
        self.assertGreaterEqual(report["count"], 1)
        item = report["recommendations"][0]
        for key in (
            "recommendation_reason",
            "free_seats",
            "pending_facts",
            "history",
        ):
            self.assertIn(key, item)
        self.assertTrue(item["history"])

        by_period = self.service.query_recommendations(start_from="2026-11-01", start_to="2026-11-10")
        self.assertTrue(any(r["course_id"] in {"C1", "C2"} for r in by_period))

    def test_terminal_referral_is_not_resurrected_by_later_change(self):
        self.service.release_referral("R2", reason="已通知但学员放弃")
        self.assertEqual(self.service.get_referral("R2")["state"], "released")

        self.service.confirm_job("J2", required_skills=["PLC", "calibration", "safety"])
        self.assertEqual(self.service.get_referral("R2")["state"], "released")

        self.service.confirm_course("C2", start="2027-03-01")
        self.assertEqual(self.service.get_referral("R2")["state"], "released")

    def test_cancel_referral_uses_cancelled_state(self):
        cancelled = self.service.cancel_referral("R2", reason="顾问取消")
        self.assertEqual(cancelled["state"], "cancelled")

    def test_confirmed_capacity_remains_authoritative_after_source_edit(self):
        self.service.confirm_course("C2", seats=2)
        self.assertEqual(self.service.course_occupancy("C2")["capacity"], 2)
        self.service.db.execute("UPDATE courses SET seats=100 WHERE id='C2'")
        self.service.db.commit()
        occupancy = self.service.course_occupancy("C2")
        self.assertEqual(occupancy["capacity"], 2)
        self.assertEqual(occupancy["free_seats"], 2)

    def test_change_history_accepts_person_and_state_filters(self):
        events = self.service.change_history(person="U-1", state="held")
        self.assertTrue(events)
        self.assertTrue(all(event["aggregate_type"] == "referral" for event in events))

    def test_pending_facts_after_source_edit_require_confirmation(self):
        self.service.add_job("JX", company="CX", skills=["PLC"], location="East", source="platform")
        pending = self.service.db.query(
            "SELECT field FROM fact_confirmations WHERE subject_id='JX' AND status='pending'"
        )
        self.assertIn("required_skills", [r["field"] for r in pending])
        self.service.confirm_job("JX", required_skills=["PLC", "safety"], location="South")
        job = self.service.get_job("JX")
        self.assertEqual(job["effective"]["required_skills"], ["PLC", "safety"])
        self.assertEqual(job["location"], "South")


if __name__ == "__main__":
    unittest.main()
