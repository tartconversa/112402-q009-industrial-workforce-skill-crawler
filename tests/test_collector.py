"""采集器：分页、重复、版本、删除对账和临时中断测试。"""

import json
import unittest

from parkbridge import ParkService
from parkbridge.collectors import (
    AccessPolicy,
    DictTransport,
    HttpFetcher,
    JsonPagedAdapter,
)
from parkbridge.errors import CollectionInterruptedError


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.service = ParkService.in_memory()
        self.addCleanup(self.service.close)
        self.url = "https://jobs.example.test/api"
        self.page2_url = "https://jobs.example.test/api?page=2"
        self.policy = AccessPolicy(
            respect_robots_txt=False,
            min_interval_seconds=0,
            backoff_base_seconds=0,
            allowed_domains=["jobs.example.test"],
        )

    def adapter(self):
        return JsonPagedAdapter(
            "corp",
            "job",
            self.url,
            next_url_path="next",
            policy=self.policy,
        )

    def fetcher(self, routes):
        transport = DictTransport(routes)
        return HttpFetcher(self.policy, transport, sleep=lambda _seconds: None), transport

    def test_pagination_repeated_record_and_provenance(self):
        first = {
            "items": [{"id": "J1", "company": "C1", "skills": "PLC|diagnostics", "state": "open"}],
            "next": self.page2_url,
        }
        second = {
            "items": [
                {"id": "J1", "company": "C1", "skills": "PLC|diagnostics", "state": "open"},
                {"id": "J2", "company": "C2", "skills": "robot-arm", "state": "open"},
            ]
        }
        fetcher, transport = self.fetcher(
            {self.url: json.dumps(first), self.page2_url: json.dumps(second)}
        )
        self.service.collector.fetcher = fetcher

        result = self.service.collector.collect(self.adapter())

        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["seen_external_ids"], ["J1", "J2"])
        self.assertEqual(len(transport.requests), 2)
        j1 = self.service.get_job("J1")
        self.assertTrue(j1["source_url"].startswith("https://jobs.example.test/"))
        self.assertIsNotNone(j1["current_version_id"])
        versions = self.service.db.query(
            "SELECT * FROM record_versions WHERE subject_id='J1' ORDER BY version_no"
        )
        self.assertEqual(len(versions), 1)  # 跨页重复且内容相同，不产生新版本。
        self.assertTrue(versions[0]["raw_json"])
        self.assertTrue(versions[0]["source_url"])
        self.assertIsNotNone(versions[0]["fetched_at"])

    def test_edit_creates_new_raw_version_and_change_event(self):
        routes = {
            self.url: json.dumps({"items": [{"id": "J1", "title": "old", "skills": "PLC"}]}),
        }
        fetcher, transport = self.fetcher(routes)
        self.service.collector.fetcher = fetcher
        self.service.collector.collect(self.adapter())

        transport.routes[self.url] = json.dumps(
            {"items": [{"id": "J1", "title": "new", "skills": "PLC|calibration"}]}
        )
        self.service.collector.collect(self.adapter())

        versions = self.service.db.query(
            "SELECT version_no, raw_json FROM record_versions WHERE subject_id='J1' ORDER BY version_no"
        )
        self.assertEqual([row["version_no"] for row in versions], [1, 2])
        self.assertIn("calibration", versions[1]["raw_json"])
        events = self.service.db.query(
            "SELECT event_type FROM change_events WHERE aggregate_id='J1' ORDER BY rowid"
        )
        self.assertIn("source_edited", [row["event_type"] for row in events])

    def test_complete_sync_marks_missing_record_deleted_but_keeps_history(self):
        routes = {
            self.url: json.dumps(
                {"items": [{"id": "J1"}, {"id": "J2"}], "next": self.page2_url}
            ),
            self.page2_url: json.dumps({"items": [{"id": "J2"}]}),
        }
        fetcher, transport = self.fetcher(routes)
        self.service.collector.fetcher = fetcher
        self.service.collector.collect(self.adapter())

        transport.routes[self.url] = json.dumps({"items": [{"id": "J2"}]})
        result = self.service.collector.collect(self.adapter())

        self.assertEqual(result["marked_deleted"], ["J1"])
        self.assertEqual(self.service.get_job("J1")["status"], "source_deleted")
        self.assertGreaterEqual(
            len(self.service.history_for("job", "J1")), 2
        )  # discovered + deleted

    def test_temporary_interruption_does_not_reconcile_deletions(self):
        self.service.add_job("J9", source="corp")
        routes = {
            self.url: json.dumps({"items": [{"id": "J1"}], "next": self.page2_url}),
            self.page2_url: ConnectionError("temporary reset"),
        }
        fetcher, _ = self.fetcher(routes)
        self.service.collector.fetcher = fetcher

        with self.assertRaises(CollectionInterruptedError):
            self.service.collector.collect(self.adapter())

        self.assertEqual(self.service.get_job("J9")["status"], "active")
        run = self.service.db.query_one(
            "SELECT status, complete FROM fetch_runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
        )
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["complete"], 0)

    def test_deleted_record_reappearing_with_same_payload_restores(self):
        routes = {self.url: json.dumps({"items": [{"id": "J1"}]})}
        fetcher, transport = self.fetcher(routes)
        self.service.collector.fetcher = fetcher
        self.service.collector.collect(self.adapter())

        transport.routes[self.url] = json.dumps({"items": []})
        self.service.collector.collect(self.adapter())
        self.assertEqual(self.service.get_job("J1")["status"], "source_deleted")

        transport.routes[self.url] = json.dumps({"items": [{"id": "J1"}]})
        result = self.service.collector.collect(self.adapter())
        self.assertEqual(result["marked_deleted"], [])
        self.assertEqual(self.service.get_job("J1")["status"], "active")

    def test_reverting_raw_payload_reuses_historical_version(self):
        fetcher, transport = self.fetcher({})
        self.service.collector.fetcher = fetcher
        payloads = [
            {"items": [{"id": "J1", "title": "A"}]},
            {"items": [{"id": "J1", "title": "B"}]},
            {"items": [{"id": "J1", "title": "A"}]},
        ]
        for payload in payloads:
            transport.routes[self.url] = json.dumps(payload)
            self.service.collector.collect(self.adapter())

        versions = self.service.record_versions("job", "J1")
        self.assertEqual([v["version_no"] for v in versions], [1, 2])
        current = self.service.get_job("J1")
        self.assertEqual(current["current_version_id"], versions[0]["id"])

    def test_resume_keeps_previous_page_ids_for_deletion_reconciliation(self):
        page2_url = self.page2_url
        routes = {
            self.url: json.dumps({"items": [{"id": "J1"}], "next": page2_url}),
            page2_url: ConnectionError("reset"),
        }
        fetcher, transport = self.fetcher(routes)
        self.service.collector.fetcher = fetcher
        with self.assertRaises(CollectionInterruptedError):
            self.service.collector.collect(self.adapter())

        transport.routes[page2_url] = json.dumps({"items": [{"id": "J2"}]})
        result = self.service.collector.collect(self.adapter(), resume=True)

        self.assertEqual(set(result["seen_external_ids"]), {"J1", "J2"})
        self.assertEqual(result["marked_deleted"], [])
        self.assertEqual(self.service.get_job("J1")["status"], "active")
        self.assertEqual(self.service.get_job("J2")["status"], "active")

    def test_retry_recovers_from_http_503(self):
        calls = {"count": 0}

        def flaky(_url):
            calls["count"] += 1
            if calls["count"] == 1:
                from parkbridge.collectors import TransportResponse

                return TransportResponse(self.url, 503, "busy", {})
            return json.dumps({"items": [{"id": "J1"}]})

        transport = DictTransport({self.url: flaky})
        fetcher = HttpFetcher(self.policy, transport, sleep=lambda _s: None)
        self.service.collector.fetcher = fetcher

        result = self.service.collector.collect(self.adapter())
        self.assertEqual(result["records_seen"], 1)
        self.assertEqual(calls["count"], 2)

    def test_domain_access_rule_blocks_other_host(self):
        transport = DictTransport({})
        fetcher = HttpFetcher(self.policy, transport, sleep=lambda _s: None)
        with self.assertRaises(Exception):
            fetcher.get("https://evil.example.test/data")


if __name__ == "__main__":
    unittest.main()
