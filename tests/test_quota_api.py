import unittest
from datetime import datetime, timezone

from ai_governance_foundation.api import route
from ai_governance_foundation.clock import FixedClock
from ai_governance_foundation.service import DomainService
from ai_governance_foundation.storage import Database

DAY = {"start_at": "2026-10-06T00:00Z", "end_at": "2026-10-07T00:00Z"}


class QuotaApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 6, tzinfo=timezone.utc))
        self.service = DomainService(self.database, clock)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "机构"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "a1",
               "display_name": "管理员", "role": "admin", "organization_id": "o1"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "op", "new_actor_id": "op1",
               "display_name": "操作员", "role": "operator", "organization_id": "o1"},
              {"X-Actor-Id": "a1"})
        self.headers = {"X-Actor-Id": "op1"}
        route(self.service, "POST", "/teams",
              {"request_id": "tm", "team_id": "team-a", "name": "甲"}, self.headers)
        route(self.service, "POST", "/tasks",
              {"request_id": "tk", "task_id": "task-a", "team_id": "team-a", "name": "任务"},
              self.headers)
        route(self.service, "POST", "/resources",
              {"request_id": "rc", "resource_id": "gpu-1", "pool_id": "gpu", "name": "机柜"},
              self.headers)
        route(self.service, "POST", "/resource-windows",
              {"request_id": "wn", "resource_id": "gpu-1", "capacity": 1, **DAY},
              self.headers)

    def tearDown(self):
        self.database.close()

    def _application(self, application_id, priority="P2"):
        return route(self.service, "POST", "/applications", {
            "request_id": application_id, "application_id": application_id,
            "team_id": "team-a", "task_id": "task-a", "resource_pool": "gpu",
            "amount": 1, "duration_hours": 2,
            "earliest_at": "2026-10-06T02:00Z", "deadline_at": "2026-10-06T05:00Z",
            "priority": priority, "committed": False,
        }, self.headers)

    def test_application_lifecycle_and_quota_view(self):
        status, payload = self._application("app-1")
        self.assertEqual(201, status)
        self.assertEqual("application", payload["resource_type"])
        status, payload = route(self.service, "GET", "/applications/app-1", None)
        self.assertEqual(200, status)
        self.assertEqual("confirmed", payload["application"]["status"])
        self.assertEqual("gpu-1", payload["allocation"]["resource_id"])
        self.assertIn("decision", payload)
        status, payload = route(
            self.service, "GET",
            "/quota-view?resource_id=gpu-1&start_at=2026-10-06T02:00Z&end_at=2026-10-06T03:00Z",
            None)
        self.assertEqual(200, status)
        self.assertEqual("app-1", payload["slots"][0]["holders"][0]["application_id"])

    def test_idempotent_replay_returns_200(self):
        first, _ = self._application("app-1")
        second, _ = self._application("app-1")
        self.assertEqual(201, first)
        self.assertEqual(200, second)

    def test_conflict_when_capacity_exhausted(self):
        self._application("app-1")
        status, payload = route(self.service, "POST", "/applications", {
            "request_id": "app-2", "application_id": "app-2",
            "team_id": "team-a", "task_id": "task-a", "resource_pool": "gpu",
            "amount": 1, "duration_hours": 2,
            "earliest_at": "2026-10-06T02:00Z", "deadline_at": "2026-10-06T04:00Z",
            "priority": "P2", "committed": False,
        }, self.headers)
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/applications/app-2", None)
        self.assertEqual("waitlisted", payload["application"]["status"])
        self.assertTrue(payload["decision"]["alternatives"])

    def test_recovery_endpoint(self):
        status, payload = route(self.service, "POST", "/recovery", {}, self.headers)
        self.assertEqual(200, status)
        self.assertIn("plan", payload)

    def test_preview_window_change_is_non_mutating(self):
        self._application("app-1")
        status, payload = route(self.service, "POST", "/preview/window-change", {
            "resource_id": "gpu-1", "capacity": 0, **DAY}, self.headers)
        self.assertEqual(200, status)
        self.assertTrue(payload["affected"])
        status, payload = route(self.service, "GET", "/applications/app-1", None)
        self.assertEqual("confirmed", payload["application"]["status"])

    def test_quota_view_requires_parameters(self):
        status, payload = route(self.service, "GET", "/quota-view?resource_id=gpu-1", None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_missing_actor_is_rejected_for_writes(self):
        status, payload = route(self.service, "POST", "/teams",
                                {"request_id": "xx", "team_id": "t", "name": "n"}, {})
        self.assertEqual(404, status)

    def test_list_resources_and_applications(self):
        self._application("app-1")
        status, payload = route(self.service, "GET", "/resources?pool_id=gpu", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        status, payload = route(self.service, "GET", "/applications?team_id=team-a", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))


if __name__ == "__main__":
    unittest.main()
