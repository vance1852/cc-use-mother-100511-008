import unittest
from datetime import datetime, timedelta, timezone

from ai_governance_foundation.api import route
from ai_governance_foundation.clock import FixedClock
from ai_governance_foundation.quota_service import QuotaService
from ai_governance_foundation.service import DomainService
from ai_governance_foundation.storage import Database

BASE = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def ts(value):
    return value.isoformat().replace("+00:00", "Z")


class QuotaApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(BASE)
        self.base = DomainService(self.database, self.clock)
        self.quota = QuotaService(self.database, self.clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="机构")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")

    def tearDown(self):
        self.database.close()

    def call(self, method, path, body=None, actor="a1"):
        return route(self.base, method, path, body or {}, {"X-Actor-Id": actor},
                     quota=self.quota)

    def seed(self):
        self.call("POST", "/teams", {"request_id": "t1", "team_id": "t1", "name": "团队一"})
        self.call("POST", "/tasks", {"request_id": "k1", "task_id": "k1", "team_id": "t1",
                                     "name": "任务一", "required_units": 1})
        status, window = self.call("POST", "/resource-windows", {
            "request_id": "w1", "window_id": "w1", "pool": "gpu", "tier": "reserved",
            "zone": "z1", "start_at": ts(BASE + timedelta(hours=1)),
            "end_at": ts(BASE + timedelta(hours=3)), "capacity_units": 1})
        self.assertEqual(201, status)
        self.call("POST", "/priority-commitments", {
            "request_id": "c1", "commitment_id": "c1", "team_id": "t1",
            "task_id": "k1", "rank": 0})

    def test_apply_to_explain_quota_flow_over_http(self):
        self.seed()
        status, applied = self.call("POST", "/quota-applications", {
            "request_id": "a1", "team_id": "t1", "task_id": "k1", "units": 1,
            "candidate_windows": ["w1"]})
        self.assertEqual(201, status)
        self.assertEqual("granted", applied["status"])
        self.assertEqual("w1", applied["window_id"])

        status, replay = self.call("POST", "/quota-applications", {
            "request_id": "a1", "team_id": "t1", "task_id": "k1", "units": 1,
            "candidate_windows": ["w1"]})
        self.assertEqual(200, status)
        self.assertTrue(replay["replayed"])

        status, confirmed = self.call("POST", "/quota-confirm", {
            "request_id": "cf1", "application_id": applied["application_id"]})
        self.assertEqual(201, status)
        self.assertEqual("confirmed", confirmed["status"])

        status, quota = self.call("GET",
                                  f"/quota-application?application_id={applied['application_id']}")
        self.assertEqual(200, status)
        self.assertEqual("scheduled", quota["allocation"]["state"])
        self.assertEqual("candidate", quota["alternatives"][0]["relation"])

        status, window = self.call("GET", "/window-quota?window_id=w1")
        self.assertEqual(200, status)
        self.assertEqual(1, window["used_units"])
        self.assertEqual("k1", window["occupied"][0]["task_id"])

    def test_impact_analysis_endpoint_does_not_require_mutation(self):
        self.seed()
        self.call("POST", "/quota-applications", {
            "request_id": "a1", "team_id": "t1", "task_id": "k1", "units": 1,
            "candidate_windows": ["w1"]})
        status, preview = self.call("POST", "/impact-analysis",
                                    {"window_id": "w1", "capacity_units": 0})
        self.assertEqual(200, status)
        self.assertEqual(1, len(preview["changes"]))
        self.assertEqual("waitlisted", preview["changes"][0]["after"]["outcome"])

    def test_recover_endpoint(self):
        self.seed()
        status, report = self.call("POST", "/quota/recover", {})
        self.assertEqual(200, status)
        self.assertEqual("ok", report["status"])
        self.assertIn("rules_version", report)

    def test_quota_route_unknown_is_404(self):
        status, payload = self.call("GET", "/quota-nope", {})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_actor_is_rejected(self):
        self.seed()
        status, payload = self.call("POST", "/quota-applications", {
            "request_id": "a1", "team_id": "t1", "task_id": "k1", "units": 1,
            "candidate_windows": ["w1"]}, actor="")
        self.assertEqual(404, status)

    def test_failed_window_moves_hold_over_http(self):
        self.seed()
        status, _alt = self.call("POST", "/resource-windows", {
            "request_id": "w2", "window_id": "w2", "pool": "gpu2", "tier": "reserved",
            "zone": "z2", "start_at": ts(BASE + timedelta(hours=4)),
            "end_at": ts(BASE + timedelta(hours=6)), "capacity_units": 1})
        self.assertEqual(201, status)
        status, _w3 = self.call("POST", "/resource-windows", {
            "request_id": "w3", "window_id": "w3", "pool": "gpu3", "tier": "reserved",
            "zone": "z3", "start_at": ts(BASE + timedelta(hours=1)),
            "end_at": ts(BASE + timedelta(hours=3)), "capacity_units": 1,
            "alternatives": ["w2"]})
        self.assertEqual(201, status)
        status, applied = self.call("POST", "/quota-applications", {
            "request_id": "a2", "team_id": "t1", "task_id": "k1", "units": 1,
            "candidate_windows": ["w3"]})
        self.assertEqual(201, status)
        self.assertEqual("w3", applied["window_id"])
        status, failed = self.call("POST", "/resource-windows/update", {
            "request_id": "f3", "window_id": "w3", "status": "failed"})
        self.assertEqual(201, status)
        moved = failed["impacted"][0]
        self.assertEqual("w2", moved["window_id"])
        self.assertEqual("moved", moved["outcome"])


if __name__ == "__main__":
    unittest.main()
