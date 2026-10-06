import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_governance_foundation.clock import FixedClock
from ai_governance_foundation.errors import ConflictError, PermissionDenied
from ai_governance_foundation.quota import QuotaService
from ai_governance_foundation.service import DomainService
from ai_governance_foundation.storage import Database

BASE = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)


class QuotaServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(BASE)
        self.gov = DomainService(self.database, self.clock)
        self.service = QuotaService(self.database, self.clock)
        self.gov.register_organization(request_id="org", actor_id="bootstrap",
                                       organization_id="o1", name="机构")
        self.gov.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                display_name="管理员", role="admin", organization_id="o1")
        self.gov.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                                display_name="操作员", role="operator", organization_id="o1")
        self.gov.register_actor(request_id="au", actor_id="a1", new_actor_id="au1",
                                display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_team(request_id="t-a", actor_id="op1", team_id="team-a", name="甲团队")
        self.service.register_team(request_id="t-b", actor_id="op1", team_id="team-b", name="乙团队")
        self.service.register_task(request_id="k-a", actor_id="op1", task_id="task-a",
                                   team_id="team-a", name="任务甲")
        self.service.register_task(request_id="k-b", actor_id="op1", task_id="task-b",
                                   team_id="team-b", name="任务乙")
        self.service.register_resource(request_id="r-1", actor_id="op1", resource_id="gpu-1",
                                       pool_id="gpu", name="机柜一")
        self.service.register_resource(request_id="r-2", actor_id="op1", resource_id="gpu-2",
                                       pool_id="gpu", name="机柜二")
        self.service.declare_window(request_id="w-1", actor_id="op1", resource_id="gpu-1",
                                    start_at="2026-10-06T00:00Z",
                                    end_at="2026-10-07T00:00Z", capacity=1)
        self.service.declare_window(request_id="w-2", actor_id="op1", resource_id="gpu-2",
                                    start_at="2026-10-06T00:00Z",
                                    end_at="2026-10-07T00:00Z", capacity=1)

    def tearDown(self):
        self.database.close()

    def _request(self, request_id, application_id, team="team-a", task="task-a",
                 priority="P2", committed=False, earliest="2026-10-06T02:00Z",
                 deadline="2026-10-06T06:00Z", duration=3):
        return self.service.request_allocation(
            request_id=request_id, actor_id="op1", application_id=application_id,
            team_id=team, task_id=task, resource_pool="gpu", amount=1,
            duration_hours=duration, earliest_at=earliest, deadline_at=deadline,
            priority=priority, committed=committed)

    def test_priority_conflict_produces_single_deterministic_result(self):
        self._request("ap1", "app-1", priority="P2")
        self._request("ap2", "app-2", team="team-b", task="task-b", priority="P1")
        self._request("ap3", "app-3", priority="P2")
        statuses = {app: self.service.get_application(app)["application"]["status"]
                    for app in ("app-1", "app-2", "app-3")}
        self.assertEqual("confirmed", statuses["app-1"])
        self.assertEqual("confirmed", statuses["app-2"])
        self.assertEqual("waitlisted", statuses["app-3"])
        plan = self.service.latest_plan()
        self.assertEqual("quota-arbitration-2026-10-v1", plan.rules_version)
        self.assertEqual(2, plan.confirmed)
        self.assertEqual(1, plan.waitlisted)

    def test_request_is_idempotent(self):
        first = self._request("ap1", "app-1")
        second = self._request("ap1", "app-1")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)

    def test_auditor_cannot_request_quota(self):
        with self.assertRaises(PermissionDenied):
            self.service.request_allocation(
                request_id="apx", actor_id="au1", application_id="app-x",
                team_id="team-a", task_id="task-a", resource_pool="gpu", amount=1,
                duration_hours=1, earliest_at="2026-10-06T02:00Z",
                deadline_at="2026-10-06T04:00Z", priority="P2", committed=False)

    def test_reschedule_frees_capacity_and_recomputes_others(self):
        self._request("ap1", "app-1")
        self._request("ap2", "app-2", team="team-b", task="task-b", priority="P1")
        self._request("ap3", "app-3")
        self.assertEqual("waitlisted",
                         self.service.get_application("app-3")["application"]["status"])
        self.service.reschedule_application(
            request_id="rs1", actor_id="op1", application_id="app-1",
            earliest_at="2026-10-06T08:00Z", deadline_at="2026-10-06T12:00Z")
        self.assertEqual("confirmed",
                         self.service.get_application("app-3")["application"]["status"])

    def test_cancel_releases_reservation_for_waitlisted_application(self):
        self._request("ap1", "app-1")
        self._request("ap2", "app-2", team="team-b", task="task-b", priority="P1")
        self._request("ap3", "app-3")
        self.service.cancel_application(request_id="cx", actor_id="op1",
                                        application_id="app-1", reason="可延后")
        self.assertEqual("cancelled",
                         self.service.get_application("app-1")["application"]["status"])
        self.assertEqual("confirmed",
                         self.service.get_application("app-3")["application"]["status"])

    def test_started_run_is_sealed_and_rejects_rewrite(self):
        self._request("ap2", "app-2", team="team-b", task="task-b", priority="P1")
        self.clock._value = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)
        self.service.run_recovery()
        allocation = self.service.get_application("app-2")["allocation"]
        self.assertEqual("sealed", allocation["status"])
        with self.assertRaises(ConflictError):
            self.service.reschedule_application(
                request_id="rs", actor_id="op1", application_id="app-2", priority="P0")
        with self.assertRaises(ConflictError):
            self.service.cancel_application(request_id="cc", actor_id="op1",
                                            application_id="app-2")

    def test_failover_keeps_history_and_plans_continuation(self):
        self._request("ap2", "app-2", team="team-b", task="task-b", priority="P1")
        self._request("ap1", "app-1", priority="P2")
        self.clock._value = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)
        self.service.run_recovery()
        sealed = self.service.get_application("app-2")
        resource_id = sealed["allocation"]["resource_id"]
        sealed_id = sealed["allocation"]["allocation_id"]
        self.service.report_resource_failure(
            request_id="fail", actor_id="op1", resource_id=resource_id,
            start_at="2026-10-06T03:00Z", end_at="2026-10-06T06:00Z")
        self.service.failover_run(request_id="fo", actor_id="op1",
                                  application_id="app-2", new_application_id="app-2b")
        original = self.service.get_application("app-2")
        self.assertEqual("interrupted", original["application"]["status"])
        self.assertEqual(sealed_id, original["allocation"]["allocation_id"])
        continuation = self.service.get_application("app-2b")
        self.assertEqual("app-2", continuation["continuation_of"])
        self.assertIn(continuation["application"]["status"], {"confirmed", "waitlisted"})
        self.assertEqual(2, continuation["application"]["duration_slots"])

    def test_capacity_change_recomputes_unstarted_only(self):
        self._request("ap1", "app-1")
        self._request("ap2", "app-2", team="team-b", task="task-b", priority="P1")
        self.clock._value = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)
        self.service.run_recovery()
        sealed_resource = self.service.get_application("app-2")["allocation"]["resource_id"]
        sealed_start = self.service.get_application("app-2")["allocation"]["start_slot"]
        # 故障资源让未开始申请迁出；封冻运行保持原放置。
        self.service.report_resource_failure(
            request_id="fail2", actor_id="op1", resource_id=sealed_resource,
            start_at="2026-10-06T03:00Z", end_at="2026-10-06T06:00Z")
        sealed_after = self.service.get_application("app-2")["allocation"]
        self.assertEqual(sealed_start, sealed_after["start_slot"])
        self.assertEqual("sealed", sealed_after["status"])
        app1 = self.service.get_application("app-1")["allocation"]
        self.assertNotEqual(sealed_resource, app1["resource_id"])

    def test_quota_view_names_holders_and_overcommit(self):
        self._request("ap1", "app-1")
        view = self.service.quota_view("gpu-1", "2026-10-06T02:00Z",
                                       "2026-10-06T03:00Z")
        slot = view["slots"][0]
        self.assertEqual(1, slot["capacity"])
        holders = {h["application_id"]: h for h in slot["holders"]}
        self.assertIn("app-1", holders)

    def test_preview_window_change_lists_affected_applications(self):
        self._request("ap1", "app-1")
        self._request("ap2", "app-2", team="team-b", task="task-b", priority="P1")
        self._request("ap3", "app-3")
        preview = self.service.preview_window_change(
            resource_id="gpu-1", start_at="2026-10-06T00:00Z",
            end_at="2026-10-07T00:00Z", capacity=2)
        affected = {item["application_id"]: item for item in preview["affected"]}
        self.assertIn("app-3", affected)
        self.assertEqual("waitlisted", affected["app-3"]["before"]["outcome"])
        self.assertEqual("confirmed", affected["app-3"]["after"]["outcome"])
        # 预览不落库，app-3 仍是等待。
        self.assertEqual("waitlisted",
                         self.service.get_application("app-3")["application"]["status"])

    def test_recovery_after_restart_continues_pending_applications(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quota.sqlite3"
            database = Database(path)
            gov = DomainService(database, self.clock)
            service = QuotaService(database, self.clock)
            gov.register_organization(request_id="org", actor_id="bootstrap",
                                      organization_id="o1", name="机构")
            gov.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                               display_name="管理员", role="admin", organization_id="o1")
            gov.register_actor(request_id="op", actor_id="a1", new_actor_id="op1",
                               display_name="操作员", role="operator", organization_id="o1")
            service.register_team(request_id="t-a", actor_id="op1", team_id="team-a", name="甲")
            service.register_task(request_id="k-a", actor_id="op1", task_id="task-a",
                                  team_id="team-a", name="任务")
            service.register_resource(request_id="r-1", actor_id="op1", resource_id="gpu-1",
                                      pool_id="gpu", name="机柜")
            service.declare_window(
                request_id="w-rec", actor_id="op1", resource_id="gpu-1",
                start_at="2026-10-06T00:00Z", end_at="2026-10-07T00:00Z", capacity=1)
            # 绕过规划直接落一条待确认申请，模拟宕机时“已提交未裁决”的状态。
            from ai_governance_foundation.planner import slot_of
            earliest_slot = slot_of(
                datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc))
            deadline_slot = slot_of(
                datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc))
            database.connection.execute(
                "INSERT INTO applications(application_id,organization_id,team_id,task_id,"
                "resource_pool,amount,duration_slots,earliest_slot,deadline_slot,priority,"
                "committed,preferred_resource_id,status,sequence,request_id,created_by,"
                "created_at,updated_at) VALUES('app-pending','o1','team-a','task-a','gpu',"
                "1,1,?,?, 'P2',0,NULL,'pending',1,'old','op1','2026-10-06T00:00Z','2026-10-06T00:00Z')",
                (earliest_slot, deadline_slot),
            )
            self.assertIsNone(service.latest_plan())
            database.close()

            database = Database(path)
            recovered = QuotaService(database, self.clock)
            plan = recovered.run_recovery()
            self.assertIsNotNone(plan)
            self.assertTrue(plan.recovered)
            self.assertEqual("confirmed",
                             recovered.get_application("app-pending")["application"]["status"])
            # 再次恢复是幂等的：指纹不变，不产生新版本。
            self.assertIsNone(recovered.run_recovery())
            database.close()

    def test_failover_after_run_window_ends_is_rejected(self):
        self._request("ap2", "app-2", team="team-b", task="task-b", priority="P1")
        self.clock._value = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)
        self.service.run_recovery()
        self.clock._value = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)
        self.service.run_recovery()
        with self.assertRaises(ConflictError):
            self.service.failover_run(request_id="fo-late", actor_id="op1",
                                      application_id="app-2", new_application_id="app-late")

    def test_unstarted_reservation_expires_and_releases_quota(self):
        self._request("ap1", "app-1", earliest="2026-10-06T02:00Z",
                      deadline="2026-10-06T04:00Z", duration=2)
        self.assertEqual("confirmed",
                         self.service.get_application("app-1")["application"]["status"])
        # 越过最后可行起跑点，申请过期且不再占用额度。
        self.clock._value = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)
        self.service.run_recovery()
        self.assertEqual("expired",
                         self.service.get_application("app-1")["application"]["status"])
        view = self.service.quota_view("gpu-1", "2026-10-06T02:00Z",
                                       "2026-10-06T03:00Z")
        self.assertEqual(0, view["slots"][0]["allocated"])

    def test_reschedule_partial_update_keeps_other_fields(self):
        self._request("ap1", "app-1", priority="P1", committed=True)
        self.service.reschedule_application(
            request_id="rs-partial", actor_id="op1", application_id="app-1",
            priority="P0")
        row = self.service.get_application("app-1")["application"]
        self.assertEqual("P0", row["priority"])
        self.assertTrue(row["committed"])
        self.assertEqual(3, row["duration_slots"])
        self.assertEqual("2026-10-06T02:00:00Z", row["earliest_at"])

    def test_explicit_start_seals_allocation(self):
        self._request("ap1", "app-1", earliest="2026-10-06T02:00Z",
                      deadline="2026-10-06T05:00Z", duration=3)
        self.clock._value = datetime(2026, 10, 6, 2, 30, tzinfo=timezone.utc)
        receipt = self.service.mark_started(request_id="start-1", actor_id="op1",
                                            application_id="app-1")
        self.assertFalse(receipt.replayed)
        self.assertEqual("sealed",
                         self.service.get_application("app-1")["allocation"]["status"])
        # 重复 start 因已封冻而被拒绝（不会产生第二个封冻分配）。
        with self.assertRaises(ConflictError):
            self.service.mark_started(request_id="start-2", actor_id="op1",
                                      application_id="app-1")

    def test_partial_window_override_keeps_outside_capacity(self):
        # gpu-1 全天容量 1；仅在 03:00-04:00 置零，区间外容量应保留。
        self.service.declare_window(
            request_id="w-partial", actor_id="op1", resource_id="gpu-1",
            start_at="2026-10-06T03:00Z", end_at="2026-10-06T04:00Z", capacity=0)
        view = self.service.quota_view("gpu-1", "2026-10-06T02:00Z",
                                       "2026-10-06T05:00Z")
        caps = {slot["at"]: slot["capacity"] for slot in view["slots"]}
        self.assertEqual(1, caps["2026-10-06T02:00:00Z"])
        self.assertEqual(0, caps["2026-10-06T03:00:00Z"])
        self.assertEqual(1, caps["2026-10-06T04:00:00Z"])
        # 再次以全天容量 1 覆盖，可恢复全部槽位。
        self.service.declare_window(
            request_id="w-restore", actor_id="op1", resource_id="gpu-1",
            start_at="2026-10-06T00:00Z", end_at="2026-10-07T00:00Z", capacity=1)
        view = self.service.quota_view("gpu-1", "2026-10-06T03:00Z",
                                       "2026-10-06T04:00Z")
        self.assertEqual(1, view["slots"][0]["capacity"])

    def test_preview_matches_actual_partial_override(self):
        self._request("ap1", "app-1", earliest="2026-10-06T02:00Z",
                      deadline="2026-10-06T05:00Z", duration=1)
        preview = self.service.preview_window_change(
            resource_id="gpu-1", start_at="2026-10-06T02:00Z",
            end_at="2026-10-06T03:00Z", capacity=0)
        projected = {a["application_id"]: a["after"]["outcome"]
                     for a in preview["affected"]}
        self.service.report_resource_failure(
            request_id="f-partial", actor_id="op1", resource_id="gpu-1",
            start_at="2026-10-06T02:00Z", end_at="2026-10-06T03:00Z")
        actual = self.service.get_application("app-1")["application"]["status"]
        self.assertEqual(actual, projected.get("app-1"))

    def test_audit_chain_remains_valid(self):
        self._request("ap1", "app-1")
        self.service.cancel_application(request_id="cx", actor_id="op1",
                                        application_id="app-1")
        valid, count = self.gov.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
