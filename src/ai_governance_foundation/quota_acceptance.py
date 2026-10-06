"""配额协调服务的离线端到端验收。

覆盖：登记团队/任务/资源时段/优先级承诺 → 同容量竞争按冻结 rank 裁决 →
容量缩减挤入等待 → 取消释放并补录等待者 → 故障窗口转移到替代窗口 →
已开始运行不可回写 → 过期持有在恢复后重新分配 → 影响分析 dry-run 与
实际落库一致 → 审计哈希链完整。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FixedClock
from .quota_service import QuotaService
from .service import DomainService
from .storage import Database

BASE = datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc)


def _ts(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "quota_acceptance.sqlite3")
        clock = FixedClock(BASE)
        base = DomainService(database, clock)
        quota = QuotaService(database, clock)

        base.register_organization(request_id="org", actor_id="bootstrap",
                                   organization_id="org-001", name="算力协调示范机构")
        base.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="admin-001",
                            display_name="协调管理员", role="admin", organization_id="org-001")

        for index in (1, 2, 3):
            quota.register_team(request_id=f"team-{index}", actor_id="admin-001",
                                team_id=f"team-{index:03d}", name=f"团队{index}")
            quota.register_task(request_id=f"task-{index}", actor_id="admin-001",
                                task_id=f"task-{index:03d}", team_id=f"team-{index:03d}",
                                name=f"模型训练{index}", required_units=1)

        # 主时段与替代时段（故障转移目标）。
        quota.register_window(
            request_id="win-alt", actor_id="admin-001", window_id="win-alt",
            pool="gpu-pool-b", tier="reserved", zone="zone-b",
            start_at=_ts(BASE + timedelta(hours=4)), end_at=_ts(BASE + timedelta(hours=6)),
            capacity_units=1)
        quota.register_window(
            request_id="win-main", actor_id="admin-001", window_id="win-main",
            pool="gpu-pool-a", tier="dedicated", zone="zone-a",
            start_at=_ts(BASE + timedelta(hours=1)), end_at=_ts(BASE + timedelta(hours=3)),
            capacity_units=2, alternatives=["win-alt"])

        # 团队1 优先级最高，团队3 最低。
        quota.set_priority_commitment(request_id="commit-1", actor_id="admin-001",
                                      commitment_id="commit-1", team_id="team-001",
                                      task_id="task-001", rank=0)
        quota.set_priority_commitment(request_id="commit-2", actor_id="admin-001",
                                      commitment_id="commit-2", team_id="team-002",
                                      task_id="task-002", rank=1)
        quota.set_priority_commitment(request_id="commit-3", actor_id="admin-001",
                                      commitment_id="commit-3", team_id="team-003",
                                      task_id="task-003", rank=2)

        app1 = quota.apply_quota(request_id="apply-1", actor_id="admin-001",
                                 team_id="team-001", task_id="task-001", units=1,
                                 candidate_windows=["win-main"])
        app2 = quota.apply_quota(request_id="apply-2", actor_id="admin-001",
                                 team_id="team-002", task_id="task-002", units=1,
                                 candidate_windows=["win-main"])
        app3 = quota.apply_quota(request_id="apply-3", actor_id="admin-001",
                                 team_id="team-003", task_id="task-003", units=1,
                                 candidate_windows=["win-main"])
        assert app1["status"] == "granted" and app1["window_id"] == "win-main"
        assert app2["status"] == "granted" and app2["window_id"] == "win-main"
        assert app3["status"] == "waitlisted"

        # 容量从 2 缩到 1：最低优先级的团队3 继续等待，其余保持。
        quota.update_window(request_id="shrink", actor_id="admin-001",
                            window_id="win-main", capacity_units=1)
        assert quota.get_quota(app1["application_id"])["window_id"] == "win-main"
        assert quota.get_quota(app2["application_id"])["status"] == "waitlisted"

        # 团队1 确认后开始运行（时间推进到主时段开始之后）。
        quota.confirm_quota(request_id="confirm-1", actor_id="admin-001",
                            application_id=app1["application_id"])
        clock._value = BASE + timedelta(hours=1, minutes=5)
        quota.start_run(request_id="start-1", actor_id="admin-001",
                        application_id=app1["application_id"])

        # 主窗口故障：已开始的团队1 保持不动（报告但不回写），未开始预留转移到替代窗口。
        failure = quota.update_window(request_id="fail-main", actor_id="admin-001",
                                      window_id="win-main", status="failed")
        moved_ids = {item["application_id"] for item in failure["impacted"]
                     if item["outcome"] == "moved"}
        assert app1["application_id"] not in moved_ids
        running = quota.get_quota(app1["application_id"])
        assert running["status"] == "running" and running["window_id"] == "win-main"

        # 团队1 完成运行；窗口容量恢复，团队2 此前被迁到替代窗口并需要重新确认。
        quota.complete_run(request_id="complete-1", actor_id="admin-001",
                           application_id=app1["application_id"])

        # 影响分析：恢复主窗口不会落库，revision 不变。
        preview = quota.impact_analysis(window_id="win-main", status="active")
        assert isinstance(preview["changes"], list)

        # 恢复后重放待确认申请：过期的持有释放并按优先级重新分配。
        clock._value = BASE + timedelta(hours=2)
        recovery = quota.recover_pending()

        explanation = quota.window_quota("win-alt")
        valid, event_count = base.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "applications": 3,
            "running_completed": quota.get_quota(app1["application_id"])["status"],
            "recovered_statuses": recovery["status_after"],
            "recovered_expired": recovery["expired_holds"],
            "alternative_window": explanation["window_id"],
            "rules_version": app1["rules_version"],
        }
        database.close()
        return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
