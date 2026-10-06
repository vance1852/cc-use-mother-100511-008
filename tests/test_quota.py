import unittest
from datetime import datetime, timedelta, timezone

from ai_governance_foundation.arbitration import RULES_VERSION, rules_digest
from ai_governance_foundation.clock import FixedClock
from ai_governance_foundation.errors import (
    ConflictError,
    StateConflictError,
    UnprocessableError,
    ValidationError,
)
from ai_governance_foundation.quota_service import QuotaService
from ai_governance_foundation.service import DomainService
from ai_governance_foundation.storage import Database

BASE = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def ts(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class QuotaFixture(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.clock = FixedClock(BASE)
        self.base = DomainService(self.database, self.clock)
        self.service = QuotaService(self.database, self.clock)
        self.base.register_organization(request_id="org", actor_id="bootstrap",
                                        organization_id="o1", name="算力联合机构")
        self.base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                 display_name="管理员", role="admin", organization_id="o1")
        for index in (1, 2, 3):
            self.service.register_team(request_id=f"team{index}", actor_id="a1",
                                       team_id=f"t{index}", name=f"团队{index}")
            self.service.register_task(request_id=f"task{index}", actor_id="a1",
                                       task_id=f"k{index}", team_id=f"t{index}",
                                       name=f"任务{index}", required_units=1)

    def tearDown(self):
        self.database.close()

    def window(self, request_id, window_id, *, start=BASE, end=BASE + timedelta(hours=2),
               capacity=1, tier="reserved", alternatives=None, pool="gpu-a", zone="z1"):
        return self.service.register_window(
            request_id=request_id, actor_id="a1", window_id=window_id, pool=pool, tier=tier,
            zone=zone, start_at=ts(start), end_at=ts(end), capacity_units=capacity,
            alternatives=alternatives)

    def commit(self, request_id, team_id, task_id, rank, commitment_id=None):
        return self.service.set_priority_commitment(
            request_id=request_id, actor_id="a1", commitment_id=commitment_id or f"c-{team_id}-{task_id}",
            team_id=team_id, task_id=task_id, rank=rank, note="冻结承诺")

    def apply(self, request_id, team_id, task_id, windows, *, units=1, actor="a1"):
        return self.service.apply_quota(
            request_id=request_id, actor_id=actor, team_id=team_id, task_id=task_id,
            units=units, candidate_windows=windows)


class ArbitrationTest(QuotaFixture):
    def test_lower_priority_rank_wins_contended_window(self):
        self.window("w1", "w1", capacity=1)
        self.commit("c1", "t1", "k1", rank=0)
        self.commit("c2", "t2", "k2", rank=1)
        first = self.apply("app1", "t1", "k1", ["w1"])
        second = self.apply("app2", "t2", "k2", ["w1"])
        self.assertEqual("granted", first["status"])
        self.assertEqual("w1", first["window_id"])
        self.assertEqual("waitlisted", second["status"])
        self.assertIsNone(second["window_id"])
        self.assertIn("w1:insufficient_capacity", second["reasons"])
        quota = self.service.window_quota("w1")
        self.assertEqual(1, quota["used_units"])
        self.assertEqual(0, quota["available_units"])
        self.assertEqual("k1", quota["occupied"][0]["task_id"])
        self.assertEqual(1, len(quota["waiting"]))
        self.assertEqual("k2", quota["waiting"][0]["task_id"])

    def test_equal_rank_breaks_tie_by_application_sequence(self):
        self.window("w1", "w1", capacity=1)
        self.commit("c1", "t1", "k1", rank=5)
        self.commit("c2", "t2", "k2", rank=5)
        first = self.apply("app1", "t1", "k1", ["w1"])
        second = self.apply("app2", "t2", "k2", ["w1"])
        self.assertEqual("w1", first["window_id"])
        self.assertEqual("waitlisted", second["status"])

    def test_arbitration_is_deterministic_regardless_of_call_order(self):
        # 两个容量为 1 的窗口、两个互相竞争的申请，重复重算必须得到同一结果。
        self.window("w1", "w1", capacity=1)
        self.window("w2", "w2", capacity=1, start=BASE + timedelta(hours=3),
                    end=BASE + timedelta(hours=5))
        self.commit("c1", "t1", "k1", rank=1)
        self.commit("c2", "t2", "k2", rank=0)
        self.apply("app1", "t1", "k1", ["w1", "w2"])
        self.apply("app2", "t2", "k2", ["w1", "w2"])
        snapshot = {
            app: (self.service.get_quota(app)["window_id"],
                  self.service.get_quota(app)["latest_outcome"])
            for app in self._all_applications()
        }
        # 一次不改变资源的容量更新触发全量重算。
        self.service.update_window(request_id="noop", actor_id="a1", window_id="w2",
                                   capacity_units=1)
        for app, (window_id, outcome) in snapshot.items():
            fresh = self.service.get_quota(app)
            self.assertEqual(window_id, fresh["window_id"], app)
            self.assertEqual(outcome, fresh["latest_outcome"], app)

    def _all_applications(self):
        rows = self.database.connection.execute(
            "SELECT application_id FROM quota_applications ORDER BY sequence").fetchall()
        return [row["application_id"] for row in rows]

    def test_decisions_record_frozen_rules_and_ranking_key(self):
        self.window("w1", "w1", capacity=1)
        self.commit("c1", "t1", "k1", rank=2)
        result = self.apply("app1", "t1", "k1", ["w1"])
        decision = result["decisions"][0]
        self.assertEqual(RULES_VERSION, decision["rules_version"])
        self.assertEqual(rules_digest(), decision["rules_digest"])
        self.assertEqual(2, decision["ranking_key"]["priority_rank"])
        self.assertEqual(1, decision["ranking_key"]["sequence"])


class LifecycleTest(QuotaFixture):
    def _granted(self):
        self.window("w1", "w1")
        self.commit("c1", "t1", "k1", rank=0)
        return self.apply("app1", "t1", "k1", ["w1"])

    def test_apply_confirm_start_complete_lifecycle(self):
        applied = self._granted()
        self.assertEqual("granted", applied["status"])
        self.assertEqual("held", applied["allocation"]["state"])
        confirmed = self.service.confirm_quota(request_id="confirm", actor_id="a1",
                                               application_id=applied["application_id"])
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual("scheduled", confirmed["allocation"]["state"])
        running = self.service.start_run(request_id="start", actor_id="a1",
                                        application_id=applied["application_id"])
        self.assertEqual("running", running["status"])
        quota = self.service.window_quota("w1")
        self.assertTrue(quota["occupied"][0]["locked"])
        completed = self.service.complete_run(request_id="done", actor_id="a1",
                                              application_id=applied["application_id"])
        self.assertEqual("completed", completed["status"])

    def test_apply_requires_frozen_priority_commitment(self):
        self.window("w1", "w1")
        with self.assertRaises(UnprocessableError):
            self.apply("app1", "t1", "k1", ["w1"])

    def test_idempotent_replay_returns_same_application(self):
        self.window("w1", "w1")
        self.commit("c1", "t1", "k1", rank=0)
        first = self.apply("app1", "t1", "k1", ["w1"])
        second = self.apply("app1", "t1", "k1", ["w1"])
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["application_id"], second["application_id"])

    def test_request_id_rejects_changed_payload(self):
        self.window("w1", "w1")
        self.commit("c1", "t1", "k1", rank=0)
        self.apply("app1", "t1", "k1", ["w1"])
        with self.assertRaises(ConflictError):
            self.apply("app1", "t1", "k1", ["w1"], units=2)

    def test_started_run_cannot_be_rewritten(self):
        applied = self._granted()
        app_id = applied["application_id"]
        self.service.confirm_quota(request_id="confirm", actor_id="a1", application_id=app_id)
        self.service.start_run(request_id="start", actor_id="a1", application_id=app_id)
        with self.assertRaises(StateConflictError):
            self.service.start_run(request_id="start-again", actor_id="a1", application_id=app_id)
        with self.assertRaises(StateConflictError):
            self.service.cancel_quota(request_id="cancel", actor_id="a1", application_id=app_id)
        with self.assertRaises(StateConflictError):
            self.service.reschedule_quota(request_id="move", actor_id="a1", application_id=app_id,
                                          candidate_windows=["w1"])
        with self.assertRaises(StateConflictError):
            self.service.confirm_quota(request_id="confirm-again", actor_id="a1", application_id=app_id)

    def test_cannot_start_before_window_begins(self):
        self.window("w1", "w1", start=BASE + timedelta(hours=6),
                    end=BASE + timedelta(hours=8))
        self.commit("c1", "t1", "k1", rank=0)
        applied = self.apply("app1", "t1", "k1", ["w1"])
        self.service.confirm_quota(request_id="confirm", actor_id="a1",
                                   application_id=applied["application_id"])
        with self.assertRaises(StateConflictError):
            self.service.start_run(request_id="start", actor_id="a1",
                                   application_id=applied["application_id"])


class ReplanTest(QuotaFixture):
    def test_window_failure_moves_unstarted_hold_to_alternative(self):
        self.window("w2", "w2", capacity=1)
        self.window("w1", "w1", capacity=1, alternatives=["w2"])
        self.commit("c1", "t1", "k1", rank=0)
        applied = self.apply("app1", "t1", "k1", ["w1"])
        self.assertEqual("w1", applied["window_id"])
        updated = self.service.update_window(request_id="fail", actor_id="a1",
                                             window_id="w1", status="failed")
        self.assertEqual("failed", updated["status"])
        impacted = {item["application_id"]: item for item in updated["impacted"]}
        self.assertIn(applied["application_id"], impacted)
        self.assertEqual("moved", impacted[applied["application_id"]]["outcome"])
        fresh = self.service.get_quota(applied["application_id"])
        self.assertEqual("w2", fresh["window_id"])
        self.assertEqual("granted", fresh["status"])
        self.assertEqual("failover_alternative",
                         next(item["relation"] for item in fresh["alternatives"]
                              if item["window_id"] == "w2"))
        # 迁移后需要在新窗口重新确认。
        confirmed = self.service.confirm_quota(request_id="confirm", actor_id="a1",
                                               application_id=applied["application_id"])
        self.assertEqual("confirmed", confirmed["status"])

    def test_confirmed_but_unstarted_reservation_is_recomputed(self):
        future = BASE + timedelta(hours=3)
        self.window("w2", "w2", capacity=1)
        self.window("w1", "w1", capacity=1, start=future,
                    end=future + timedelta(hours=2), alternatives=["w2"])
        self.commit("c1", "t1", "k1", rank=0)
        applied = self.apply("app1", "t1", "k1", ["w1"])
        self.service.confirm_quota(request_id="confirm", actor_id="a1",
                                   application_id=applied["application_id"])
        self.service.update_window(request_id="fail", actor_id="a1",
                                   window_id="w1", status="failed")
        fresh = self.service.get_quota(applied["application_id"])
        self.assertEqual("w2", fresh["window_id"])
        self.assertEqual("granted", fresh["status"])

    def test_running_capacity_is_counted_once_for_new_arrivals(self):
        # 容量 2：一个运行中占用 1 单位，新的 1 单位申请仍应获批（回归重复扣减）。
        self.window("w1", "w1", capacity=2)
        self.commit("c1", "t1", "k1", rank=0)
        self.commit("c2", "t2", "k2", rank=1)
        running = self.apply("app1", "t1", "k1", ["w1"])
        self.service.confirm_quota(request_id="confirm1", actor_id="a1",
                                   application_id=running["application_id"])
        self.service.start_run(request_id="start1", actor_id="a1",
                               application_id=running["application_id"])
        later = self.apply("app2", "t2", "k2", ["w1"])
        self.assertEqual("granted", later["status"])
        self.assertEqual("w1", later["window_id"])
        quota = self.service.window_quota("w1")
        self.assertEqual(2, quota["used_units"])
        self.assertEqual(0, quota["available_units"])

    def test_confirmed_reservation_past_window_start_is_still_movable_until_run_starts(self):
        # 主窗口时间为 1:00-3:00，替代窗口 4:00-6:00。
        self.window("w2", "w2", start=BASE + timedelta(hours=4),
                    end=BASE + timedelta(hours=6), capacity=1)
        self.window("w1", "w1", start=BASE + timedelta(hours=1),
                    end=BASE + timedelta(hours=3), capacity=1, alternatives=["w2"])
        self.commit("c1", "t1", "k1", rank=0)
        applied = self.apply("app1", "t1", "k1", ["w1"])
        self.service.confirm_quota(request_id="confirm", actor_id="a1",
                                   application_id=applied["application_id"])
        # 时间推进到主窗口开始之后，但运行尚未开始：仍属于未开始预留。
        self.clock._value = BASE + timedelta(hours=1, minutes=30)
        self.service.update_window(request_id="fail", actor_id="a1",
                                   window_id="w1", status="failed")
        fresh = self.service.get_quota(applied["application_id"])
        self.assertEqual("w2", fresh["window_id"])
        self.assertEqual("granted", fresh["status"])

    def test_running_allocation_stays_on_failed_window(self):
        self.window("w1", "w1", capacity=1)
        self.commit("c1", "t1", "k1", rank=0)
        applied = self.apply("app1", "t1", "k1", ["w1"])
        app_id = applied["application_id"]
        self.service.confirm_quota(request_id="confirm", actor_id="a1", application_id=app_id)
        self.service.start_run(request_id="start", actor_id="a1", application_id=app_id)
        self.service.update_window(request_id="fail", actor_id="a1", window_id="w1",
                                   status="failed")
        fresh = self.service.get_quota(app_id)
        self.assertEqual("running", fresh["status"])
        self.assertEqual("w1", fresh["window_id"])
        self.assertEqual([], fresh["impacted"])

    def test_cancel_releases_capacity_and_admits_waitlisted(self):
        self.window("w1", "w1", capacity=1)
        self.commit("c1", "t1", "k1", rank=0)
        self.commit("c2", "t2", "k2", rank=1)
        first = self.apply("app1", "t1", "k1", ["w1"])
        second = self.apply("app2", "t2", "k2", ["w1"])
        self.assertEqual("waitlisted", second["status"])
        cancelled = self.service.cancel_quota(request_id="cancel", actor_id="a1",
                                              application_id=first["application_id"])
        admitted = next(item for item in cancelled["impacted"]
                        if item["application_id"] == second["application_id"])
        self.assertEqual("allocated", admitted["outcome"])
        self.assertEqual("w1", admitted["window_id"])
        self.assertEqual("granted", self.service.get_quota(second["application_id"])["status"])

    def test_capacity_shrink_evicts_lower_priority(self):
        self.window("w1", "w1", capacity=2)
        self.commit("c1", "t1", "k1", rank=0)
        self.commit("c2", "t2", "k2", rank=1)
        first = self.apply("app1", "t1", "k1", ["w1"])
        second = self.apply("app2", "t2", "k2", ["w1"])
        self.assertEqual("w1", second["window_id"])
        self.service.update_window(request_id="shrink", actor_id="a1", window_id="w1",
                                   capacity_units=1)
        self.assertEqual("w1", self.service.get_quota(first["application_id"])["window_id"])
        evicted = self.service.get_quota(second["application_id"])
        self.assertEqual("waitlisted", evicted["status"])
        self.assertIsNone(evicted["window_id"])

    def test_priority_change_reorders_contention(self):
        self.window("w1", "w1", capacity=1)
        self.commit("c1", "t1", "k1", rank=0)
        self.commit("c2", "t2", "k2", rank=1)
        first = self.apply("app1", "t1", "k1", ["w1"])
        self.apply("app2", "t2", "k2", ["w1"])
        self.service.set_priority_commitment(request_id="promote", actor_id="a1",
                                            commitment_id="c-t2-k2", team_id="t2",
                                            task_id="k2", rank=0)
        # rank 相同后按申请顺序，t1 仍然占住；把 t1 降到更后才发生更替。
        self.assertEqual("w1", self.service.get_quota(first["application_id"])["window_id"])
        self.service.set_priority_commitment(request_id="demote", actor_id="a1",
                                            commitment_id="c-t1-k1", team_id="t1",
                                            task_id="k1", rank=9)
        self.assertEqual("waitlisted",
                         self.service.get_quota(first["application_id"])["status"])


class RecoveryAndExplainTest(QuotaFixture):
    def test_expired_hold_is_released_and_capacity_reassigned_on_recovery(self):
        self.window("w1", "w1", capacity=1)
        self.commit("c1", "t1", "k1", rank=0)
        self.commit("c2", "t2", "k2", rank=1)
        first = self.apply("app1", "t1", "k1", ["w1"])
        second = self.apply("app2", "t2", "k2", ["w1"])
        self.assertEqual("waitlisted", second["status"])
        # 时间推进超过持有有效期，再恢复服务。
        self.clock._value = BASE + timedelta(seconds=901)
        report = self.service.recover_pending()
        self.assertIn(first["application_id"], report["expired_holds"])
        self.assertEqual("expired", self.service.get_quota(first["application_id"])["status"])
        self.assertEqual("granted", self.service.get_quota(second["application_id"])["status"])
        self.assertEqual("w1", self.service.get_quota(second["application_id"])["window_id"])

    def test_recover_pending_is_safe_when_idle(self):
        report = self.service.recover_pending()
        self.assertEqual("ok", report["status"])
        self.assertEqual(0, report["examined"])

    def test_impact_analysis_previews_changes_without_writing(self):
        self.window("w1", "w1", capacity=2)
        self.commit("c1", "t1", "k1", rank=0)
        self.commit("c2", "t2", "k2", rank=1)
        first = self.apply("app1", "t1", "k1", ["w1"])
        second = self.apply("app2", "t2", "k2", ["w1"])
        revision_before = self.database.connection.execute(
            "SELECT revision FROM quota_applications WHERE application_id=?",
            (second["application_id"],)).fetchone()["revision"]
        preview = self.service.impact_analysis(window_id="w1", capacity_units=1)
        change_apps = {item["application_id"] for item in preview["changes"]}
        self.assertIn(second["application_id"], change_apps)
        self.assertNotIn(first["application_id"], change_apps)
        revision_after = self.database.connection.execute(
            "SELECT revision FROM quota_applications WHERE application_id=?",
            (second["application_id"],)).fetchone()["revision"]
        self.assertEqual(revision_before, revision_after)
        # dry-run 结果与真正落库一致。
        self.service.update_window(request_id="shrink", actor_id="a1", window_id="w1",
                                   capacity_units=1)
        self.assertEqual("waitlisted",
                         self.service.get_quota(second["application_id"])["status"])

    def test_impact_analysis_previews_failover(self):
        self.window("w2", "w2", capacity=1)
        self.window("w1", "w1", capacity=1, alternatives=["w2"])
        self.commit("c1", "t1", "k1", rank=0)
        applied = self.apply("app1", "t1", "k1", ["w1"])
        preview = self.service.impact_analysis(window_id="w1", status="failed")
        change = next(item for item in preview["changes"]
                      if item["application_id"] == applied["application_id"])
        self.assertEqual("w1", change["before"]["window_id"])
        self.assertEqual("w2", change["after"]["window_id"])
        self.assertEqual("moved", change["after"]["outcome"])

    def test_get_quota_explains_alternatives(self):
        self.window("w2", "w2", capacity=1, start=BASE + timedelta(hours=3),
                    end=BASE + timedelta(hours=5))
        self.window("w1", "w1", capacity=1, alternatives=["w2"])
        self.commit("c1", "t1", "k1", rank=0)
        applied = self.apply("app1", "t1", "k1", ["w1"])
        quota = self.service.get_quota(applied["application_id"])
        chosen = next(item for item in quota["alternatives"] if item["usable"] == "chosen")
        self.assertEqual("w1", chosen["window_id"])
        self.assertEqual(RULES_VERSION, quota["rules_version"])

    def test_window_quota_reports_capacity_and_alternatives(self):
        self.window("w2", "w2", capacity=1)
        self.window("w1", "w1", capacity=1, alternatives=["w2"])
        quota = self.service.window_quota("w1")
        self.assertEqual(["w2"], quota["alternatives"])
        self.assertEqual(1, quota["capacity_units"])
        self.assertEqual(0, quota["used_units"])


class ValidationTest(QuotaFixture):
    def test_window_rejects_unknown_alternative(self):
        with self.assertRaises(Exception):
            self.window("w1", "w1", alternatives=["nope"])

    def test_window_rejects_end_before_start(self):
        with self.assertRaises(ValidationError):
            self.window("w1", "w1", start=BASE + timedelta(hours=3), end=BASE)

    def test_application_rejects_unknown_window(self):
        self.commit("c1", "t1", "k1", rank=0)
        with self.assertRaises(Exception):
            self.apply("app1", "t1", "k1", ["nope"])

    def test_application_rejects_units_below_task_requirement(self):
        self.window("w1", "w1", capacity=4)
        self.service.register_task(request_id="big", actor_id="a1", task_id="big",
                                   team_id="t1", name="大任务", required_units=4)
        self.commit("cb", "t1", "big", rank=0)
        with self.assertRaises(ValidationError):
            self.apply("appbig", "t1", "big", ["w1"], units=2)


if __name__ == "__main__":
    unittest.main()
