"""运行基础服务与配额协调服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .errors import ConflictError
from .clock import FixedClock
from .quota import QuotaService
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行登记链与完整配额协调链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        clock = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
        service = DomainService(database, clock)
        quota = QuotaService(database, clock)
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科研机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="项目负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号创新节点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="institution_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="institution_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})

        # -- 配额协调：两个团队、两个资源、同一时段三个申请 ----------------
        quota.register_team(request_id="req-team-1", actor_id="operator-001",
                            team_id="team-001", name="模型训练一组")
        quota.register_team(request_id="req-team-2", actor_id="operator-001",
                            team_id="team-002", name="模型训练二组")
        quota.register_task(request_id="req-task-1", actor_id="operator-001",
                            task_id="task-001", team_id="team-001", name="批处理训练")
        quota.register_task(request_id="req-task-2", actor_id="operator-001",
                            task_id="task-002", team_id="team-002", name="夜间推理")
        quota.register_resource(request_id="req-res-1", actor_id="operator-001",
                                resource_id="gpu-001", pool_id="gpu-pool", name="GPU 机柜一号")
        quota.register_resource(request_id="req-res-2", actor_id="operator-001",
                                resource_id="gpu-002", pool_id="gpu-pool", name="GPU 机柜二号")
        quota.declare_window(request_id="req-win-1", actor_id="operator-001",
                             resource_id="gpu-001", start_at="2026-09-25T00:00Z",
                             end_at="2026-09-26T00:00Z", capacity=1)
        quota.declare_window(request_id="req-win-2", actor_id="operator-001",
                             resource_id="gpu-002", start_at="2026-09-25T00:00Z",
                             end_at="2026-09-26T00:00Z", capacity=1)
        common = dict(actor_id="operator-001", resource_pool="gpu-pool", amount=1,
                      duration_hours=2, earliest_at="2026-09-25T10:00Z",
                      deadline_at="2026-09-25T12:00Z", committed=False)
        quota.request_allocation(request_id="req-app-a", application_id="app-a",
                                 team_id="team-001", task_id="task-001",
                                 priority="P1", **common)
        quota.request_allocation(request_id="req-app-b", application_id="app-b",
                                 team_id="team-002", task_id="task-002",
                                 priority="P2", **common)
        quota.request_allocation(request_id="req-app-c", application_id="app-c",
                                 team_id="team-001", task_id="task-001",
                                 priority="P3", **common)
        statuses = {name: quota.get_application(name)["application"]["status"]
                    for name in ("app-a", "app-b", "app-c")}

        # 改期 app-b 到下午，app-c 应被重新计算为已确认。
        quota.reschedule_application(request_id="req-move-b", actor_id="operator-001",
                                     application_id="app-b",
                                     earliest_at="2026-09-25T14:00Z",
                                     deadline_at="2026-09-25T18:00Z")
        app_c_after_move = quota.get_application("app-c")["application"]["status"]

        # 时钟进入运行时段，恢复流程自动封冻已开始的预留。
        clock.set_to(datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc))
        recovery_plan = quota.run_recovery()
        sealed = quota.get_application("app-a")["allocation"]["status"]

        # 已开始运行不能回写。
        sealed_protected = False
        try:
            quota.reschedule_application(request_id="req-illegal", actor_id="operator-001",
                                         application_id="app-a", priority="P0")
        except ConflictError:
            sealed_protected = True

        # 故障转移：原资源置零，登记续接申请，历史不回写。
        failed_resource = quota.get_application("app-a")["allocation"]["resource_id"]
        quota.report_resource_failure(request_id="req-failure", actor_id="operator-001",
                                      resource_id=failed_resource,
                                      start_at="2026-09-25T11:00Z",
                                      end_at="2026-09-25T14:00Z")
        # 故障已上报、转移未完成时，封冻运行仍占着容量为 0 的槽位，必须显式暴露超额。
        overcommit = quota.quota_view(
            failed_resource, "2026-09-25T11:00Z", "2026-09-25T12:00Z")["slots"][0]["overcommitted"]
        quota.failover_run(request_id="req-failover", actor_id="operator-001",
                           application_id="app-a", new_application_id="app-a-cont")
        continuation = quota.get_application("app-a-cont")
        interrupted_kept = quota.get_application("app-a")["allocation"]["status"] == "interrupted"

        # 再次恢复必须幂等：指纹不变时不产生新版本。
        fingerprint = quota.latest_plan().fingerprint
        second_recovery = quota.run_recovery()

        valid, event_count = service.verify_audit()
        records = service.list_domain_data("site-001")
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed,
                  "quota_initial": statuses,
                  "quota_waitlisted_promoted": app_c_after_move,
                  "quota_recovery_sealed": recovery_plan is not None and sealed == "sealed",
                  "quota_sealed_protected": sealed_protected,
                  "quota_failover_continuation": continuation["continuation_of"],
                  "quota_failover_status": continuation["application"]["status"],
                  "quota_failover_has_alternatives":
                      bool(continuation["decision"]["alternatives"]),
                  "quota_interrupted_history_kept": interrupted_kept,
                  "quota_overcommit_visible": overcommit,
                  "quota_recovery_idempotent": second_recovery is None
                  and quota.latest_plan().fingerprint == fingerprint}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    checks = ("audit_valid", "quota_recovery_sealed", "quota_sealed_protected",
              "quota_overcommit_visible", "quota_recovery_idempotent",
              "quota_interrupted_history_kept")
    return 0 if result["status"] == "ok" and all(result[name] for name in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
