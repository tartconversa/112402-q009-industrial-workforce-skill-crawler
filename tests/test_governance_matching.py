"""技能治理与推荐匹配规则测试。"""

import unittest

from parkbridge import ParkService
from parkbridge.errors import ValidationError


class GovernanceMatchingTest(unittest.TestCase):
    def setUp(self):
        self.service = ParkService.in_memory()
        self.addCleanup(self.service.close)

    def test_identical_normalized_terms_match_without_similarity_judgment(self):
        result = self.service.governance.match_course(
            ["PLC|diagnostics"], ["PLC", "diagnostics"], ["PLC-X"]
        )
        # 注意：竖线未拆分时是一个短语，不会命中；调用方应拆分。
        self.assertFalse(result["satisfies"])

        result = self.service.governance.match_course(
            ["PLC", "diagnostics"], ["PLC", "diagnostics"], ["PLC-X"]
        )
        self.assertTrue(result["satisfies"])
        self.assertEqual(result["lines"][0]["matches"][0]["via"], "exact-normalized-text")

    def test_text_similarity_alone_is_not_enough(self):
        self.service.add_job("J1", skills=["数控调试"])
        self.service.add_course("C1", skills=["数控设备调试"], seats=5, start="2026-12-01")

        report = self.service.recommend_for_job("J1")
        candidate = report["recommendations"][0]
        self.assertFalse(candidate["can_satisfy"])
        self.assertTrue(candidate["match"]["lines"][0]["text_similarity_hint"])

        with self.assertRaises(ValidationError):
            self.service.create_recommendation("J1", "C1", person="U1", referral_id="R1")

    def test_confirmed_synonym_and_partial_coverage_have_different_effects(self):
        g = self.service.governance
        g.add_synonym("数控调试", "cnc-debug")
        g.add_synonym("CNC调试", "cnc-debug")
        g.add_term("parameter-maintenance")
        g.add_partial_coverage("cnc-debug", "parameter-maintenance")

        # 同义表达可完整命中。
        full = g.match_course(["数控调试"], ["CNC调试"])
        self.assertTrue(full["satisfies"])

        # 部分覆盖只能标记 partial，不能判定满足。
        partial = g.match_course(["cnc-debug"], ["parameter-maintenance"])
        self.assertFalse(partial["satisfies"])
        self.assertEqual(partial["partial_count"], 1)
        self.assertEqual(partial["lines"][0]["status"], "partial")

        self.service.add_job("J1", skills=["cnc-debug"])
        self.service.add_course("C1", skills=["parameter-maintenance"], seats=3)
        rec = self.service.create_recommendation("J1", "C1", referral_id="R1")
        self.assertEqual(rec["state"], "proposed")
        self.assertIn("coverage_not_full", rec["reason"]["coverage_warnings"])
        # 初始部分覆盖是推荐理由/缺口，不是岗位或课程变化导致的待复核。
        self.assertEqual(rec["review_status"], "ok")
        held = self.service.hold_reservation("R1")
        self.assertEqual(held["state"], "held")
        confirmed = self.service.confirm_referral("R1", person="U-PARTIAL")
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertFalse(confirmed["reason"]["match"]["satisfies"])

    def test_full_coverage_rule_can_establish_satisfaction(self):
        g = self.service.governance
        g.add_full_coverage("数控调试", "PLC诊断实操")
        result = g.match_course(["数控调试"], ["PLC诊断实操"])
        self.assertTrue(result["satisfies"])
        self.assertEqual(result["lines"][0]["matches"][-1]["via"], "confirmed-coverage-rule")


if __name__ == "__main__":
    unittest.main()
