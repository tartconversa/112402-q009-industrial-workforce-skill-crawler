"""离线示例来源配置的端到端采集测试。"""

import unittest
from pathlib import Path

from parkbridge import ParkService
from parkbridge.cli import run_config_collection

ROOT = Path(__file__).resolve().parent.parent


class ExampleConfigTest(unittest.TestCase):
    def test_local_sources_config_collects_pages_and_csv(self):
        service = ParkService.in_memory()
        self.addCleanup(service.close)
        result = run_config_collection(service, str(ROOT / "examples" / "local_sources.json"))

        self.assertEqual(len(result["runs"]), 2)
        job = service.get_job("DEMO-J1")
        course = service.get_course("DEMO-C1")
        self.assertEqual(job["company"], "示范制造A")
        self.assertIn("数控调试", job["effective"]["required_skills"])
        self.assertTrue(job["source_url"].startswith("https://"))
        self.assertIsNotNone(job["raw_payload"])
        self.assertEqual(course["free_seats"], 20)
        self.assertEqual(course["start_date"], "2026-12-05")


if __name__ == "__main__":
    unittest.main()
