"""配额协调服务：在申请、改期、取消与故障转移之间保持一致。

服务层负责权限、幂等、事务、审计与生命周期；具体的额度裁决全部委托给
:mod:`ai_governance_foundation.planner` 中冻结的纯函数规则，因此任何一次
重算都可以离线复核，并对相同输入给出唯一结果。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

from . import planner
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import (
    Actor,
    PlanSummary,
    Resource,
    Task,
    Team,
    WriteReceipt,
)
from .planner import (
    Alternative,
    Decision,
    FixedAllocation,
    Placement,
    ReservationInput,
    RULES_VERSION,
    Window,
    parse_instant,
    slot_of,
    slot_to_iso,
)
from .storage import Database

WRITE_ROLES = ("admin", "operator")
TERMINAL_STATUSES = frozenset({"cancelled", "expired", "completed", "interrupted"})
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")


def merge_window(windows: list[Window], resource_id: str, start_slot: int,
                 end_slot: int, capacity: int) -> list[Window]:
    """用 ``[start_slot, end_slot)`` 的容量覆盖该资源的既有窗口。

    与真实写入 :meth:`QuotaService._apply_window` 使用相同的切分规则，因此
    影响预演与实际变更对容量的刻画完全一致。
    """

    result: list[Window] = []
    for window in windows:
        if window.resource_id == resource_id \
                and window.start_slot < end_slot and window.end_slot > start_slot:
            if window.start_slot < start_slot:
                result.append(Window(resource_id, window.start_slot, start_slot,
                                     window.capacity))
            if end_slot < window.end_slot:
                result.append(Window(resource_id, end_slot, window.end_slot,
                                     window.capacity))
        else:
            result.append(window)
    result.append(Window(resource_id, start_slot, end_slot, capacity))
    return result


class QuotaService:
    """记录团队、任务、资源时段与优先级承诺并产生一致裁决。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # -- 基础工具 -------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _current_slot(self) -> int:
        return slot_of(self.clock.now())

    def _identifier(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require_write(self, actor: Actor, organization_id: str | None = None) -> None:
        if actor.role not in WRITE_ROLES:
            raise PermissionDenied("当前角色不能执行该动作")
        if organization_id is not None and actor.organization_id != organization_id \
                and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的配额对象")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]]):
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,"
            "resource_id,response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id,
             canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    # -- 团队、任务、资源登记 -------------------------------------------

    def register_team(self, *, request_id: str, actor_id: str,
                      team_id: str, name: str):
        payload = {"actor_id": actor_id, "team_id": team_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            team_id = self._identifier(team_id, "team_id")
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO teams(team_id,organization_id,name,created_at) VALUES(?,?,?,?)",
                        (team_id, actor.organization_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("团队编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="team.registered",
                             resource_type="team", resource_id=team_id,
                             detail={"name": name}, occurred_at=self._now())
                return "team", team_id, {"team_id": team_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_team", payload=payload, create=create)

    def register_task(self, *, request_id: str, actor_id: str,
                      task_id: str, team_id: str, name: str):
        payload = {"actor_id": actor_id, "task_id": task_id, "team_id": team_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            team = self._team_row(connection, team_id)
            self._require_write(actor, team["organization_id"])
            task_id = self._identifier(task_id, "task_id")
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO tasks(task_id,team_id,name,active,created_at) VALUES(?,?,?,1,?)",
                        (task_id, team_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("任务编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="task.registered",
                             resource_type="task", resource_id=task_id,
                             detail={"team_id": team_id, "name": name},
                             occurred_at=self._now())
                return "task", task_id, {"task_id": task_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_task", payload=payload, create=create)

    def register_resource(self, *, request_id: str, actor_id: str,
                          resource_id: str, pool_id: str, name: str):
        payload = {"actor_id": actor_id, "resource_id": resource_id,
                   "pool_id": pool_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            resource_id = self._identifier(resource_id, "resource_id")
            pool_id = self._identifier(pool_id, "pool_id")
            name = str(name).strip()
            if not name:
                raise ValidationError("name 不能为空")

            def create():
                try:
                    connection.execute(
                        "INSERT INTO resources(resource_id,organization_id,pool_id,name,"
                        "active,created_at) VALUES(?,?,?,?,1,?)",
                        (resource_id, actor.organization_id, pool_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资源编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="resource.registered",
                             resource_type="resource", resource_id=resource_id,
                             detail={"pool_id": pool_id, "name": name},
                             occurred_at=self._now())
                return "resource", resource_id, {"resource_id": resource_id, "pool_id": pool_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_resource", payload=payload, create=create)

    # -- 资源时段（容量窗口） -------------------------------------------

    def declare_window(self, *, request_id: str, actor_id: str, resource_id: str,
                       start_at: str, end_at: str, capacity: int):
        """登记或覆盖资源在某时段的容量，并立即重新计算全部未开始预留。

        返回幂等回执；新计划版本可通过 ``latest_plan`` 查询。
        """

        payload = {"actor_id": actor_id, "resource_id": resource_id,
                   "start_at": start_at, "end_at": end_at, "capacity": capacity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            resource = self._resource_row(connection, resource_id)
            self._require_write(actor, resource["organization_id"])
            start_slot = slot_of(parse_instant(start_at, "start_at"))
            end_slot = slot_of(parse_instant(end_at, "end_at"))
            if end_slot <= start_slot:
                raise ValidationError("end_at 必须晚于 start_at")
            if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0:
                raise ValidationError("capacity 必须是非负整数")

            def create():
                window_ids = self._apply_window(connection, resource_id, start_slot,
                                                end_slot, capacity, actor_id=actor_id,
                                                action="window.declared")
                plan = self._replan(connection, reason="window_changed")
                return "resource_window", window_ids[0], {
                    "window_ids": window_ids,
                    "plan_id": plan.plan_id if plan else None,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="declare_window", payload=payload,
                                    create=create)

    def report_resource_failure(self, *, request_id: str, actor_id: str,
                                resource_id: str, start_at: str, end_at: str):
        """把资源在某时段的容量置零以表达故障，随后重算可行方案。

        已开始运行不会被回写：它们继续作为固定负载占用，配额视图会把容量
        不足的槽位标为 overcommitted；未开始的预留则迁移到池内替代资源。
        """

        payload = {"actor_id": actor_id, "resource_id": resource_id,
                   "start_at": start_at, "end_at": end_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            resource = self._resource_row(connection, resource_id)
            self._require_write(actor, resource["organization_id"])
            start_slot = slot_of(parse_instant(start_at, "start_at"))
            end_slot = slot_of(parse_instant(end_at, "end_at"))
            if end_slot <= start_slot:
                raise ValidationError("end_at 必须晚于 start_at")

            def create():
                window_ids = self._apply_window(connection, resource_id, start_slot,
                                                end_slot, 0, actor_id=actor_id,
                                                action="resource.failure_reported")
                plan = self._replan(connection, reason="resource_failure")
                return "resource_failure", resource_id, {
                    "resource_id": resource_id,
                    "window_ids": window_ids,
                    "plan_id": plan.plan_id if plan else None,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="report_resource_failure", payload=payload,
                                    create=create)

    def _apply_window(self, connection, resource_id: str, start_slot: int,
                      end_slot: int, capacity: int, *, actor_id: str,
                      action: str) -> list[str]:
        """用新区间容量覆盖旧窗口；旧窗口在区间外的部分按原容量切分保留。"""

        created: list[str] = []
        next_version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM resource_windows "
            "WHERE resource_id=?", (resource_id,)).fetchone()["version"]
        old_rows = connection.execute(
            "SELECT * FROM resource_windows WHERE resource_id=? AND superseded=0 "
            "AND start_slot < ? AND end_slot > ? ORDER BY start_slot",
            (resource_id, end_slot, start_slot),
        ).fetchall()
        overlapping = [Window(resource_id, row["start_slot"], row["end_slot"],
                              row["capacity"]) for row in old_rows]
        segments = merge_window(overlapping, resource_id, start_slot, end_slot, capacity)
        for old in old_rows:
            connection.execute(
                "UPDATE resource_windows SET superseded=1 WHERE window_id=?",
                (old["window_id"],),
            )
        for segment in segments:
            created.append(self._insert_window(
                connection, resource_id, segment.start_slot, segment.end_slot,
                segment.capacity, next_version))
            next_version += 1
        append_event(connection, actor_id=actor_id, action=action,
                     resource_type="resource", resource_id=resource_id,
                     detail={"start_slot": start_slot, "end_slot": end_slot,
                             "capacity": capacity, "window_ids": created},
                     occurred_at=self._now())
        return created

    def _insert_window(self, connection, resource_id, start_slot, end_slot,
                       capacity, version) -> str:
        window_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO resource_windows(window_id,resource_id,start_slot,end_slot,"
            "capacity,version,superseded,created_by,created_at) VALUES(?,?,?,?,?,?,0,?,?)",
            (window_id, resource_id, start_slot, end_slot, capacity, version,
             "system", self._now()),
        )
        return window_id

    # -- 申请 -----------------------------------------------------------

    def request_allocation(self, *, request_id: str, actor_id: str,
                           application_id: str, team_id: str, task_id: str,
                           resource_pool: str, amount: int, duration_hours: int,
                           earliest_at: str, deadline_at: str, priority: str,
                           committed: bool, preferred_resource_id: str | None = None):
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "team_id": team_id, "task_id": task_id, "resource_pool": resource_pool,
                   "amount": amount, "duration_hours": duration_hours,
                   "earliest_at": earliest_at, "deadline_at": deadline_at,
                   "priority": priority, "committed": committed,
                   "preferred_resource_id": preferred_resource_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            application_id = self._identifier(application_id, "application_id")
            team = self._team_row(connection, team_id)
            self._require_write(actor, team["organization_id"])
            task = connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None or task["team_id"] != team_id or not task["active"]:
                raise NotFoundError("任务不存在或不属于该团队")
            values = self._validate_request_fields(
                connection, resource_pool=resource_pool, amount=amount,
                duration_hours=duration_hours, earliest_at=earliest_at,
                deadline_at=deadline_at, priority=priority, committed=committed,
                preferred_resource_id=preferred_resource_id)

            def create():
                existing = connection.execute(
                    "SELECT 1 FROM applications WHERE application_id=?",
                    (application_id,)).fetchone()
                if existing:
                    raise ConflictError("申请编号已经存在")
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM applications"
                ).fetchone()["next"]
                connection.execute(
                    "INSERT INTO applications(application_id,organization_id,team_id,"
                    "task_id,resource_pool,amount,duration_slots,earliest_slot,"
                    "deadline_slot,priority,committed,preferred_resource_id,status,"
                    "sequence,continuation_of,request_id,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'pending', ?, NULL, ?, ?, ?, ?)",
                    (application_id, team["organization_id"], team_id, task_id,
                     resource_pool, values["amount"], values["duration_slots"],
                     values["earliest_slot"], values["deadline_slot"], values["priority"],
                     1 if values["committed"] else 0, values["preferred_resource_id"],
                     sequence, request_id, actor_id, self._now(), self._now()),
                )
                append_event(connection, actor_id=actor_id, action="application.submitted",
                             resource_type="application", resource_id=application_id,
                             detail={"team_id": team_id, "task_id": task_id,
                                     "resource_pool": resource_pool,
                                     "priority": values["priority"],
                                     "committed": values["committed"],
                                     "sequence": sequence},
                             occurred_at=self._now())
                plan = self._replan(connection, reason="application_submitted")
                return "application", application_id, {
                    "application_id": application_id,
                    "plan_id": plan.plan_id if plan else None,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="request_allocation", payload=payload,
                                    create=create)

    def _validate_request_fields(self, connection, *, resource_pool: str, amount: int,
                                 duration_hours: int, earliest_at: str,
                                 deadline_at: str, priority: str, committed: bool,
                                 preferred_resource_id: str | None) -> dict[str, Any]:
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
            raise ValidationError("amount 必须是不小于 1 的整数")
        if not isinstance(duration_hours, int) or isinstance(duration_hours, bool) \
                or duration_hours < 1:
            raise ValidationError("duration_hours 必须是不小于 1 的整数小时")
        if planner.SLOT_GRID_MINUTES != 60:
            raise ValidationError("时间栅格变化后需要显式换算时长")
        if priority not in planner.PRIORITY_RANK:
            raise ValidationError("priority 必须是 P0/P1/P2/P3 之一")
        if not isinstance(committed, bool):
            raise ValidationError("committed 必须是布尔值")
        earliest_slot = slot_of(parse_instant(earliest_at, "earliest_at"))
        deadline_slot = slot_of(parse_instant(deadline_at, "deadline_at"))
        duration_slots = duration_hours
        if deadline_slot < earliest_slot + duration_slots:
            raise ValidationError("可行窗口长度不足以容纳该时长")
        pool_resources = connection.execute(
            "SELECT resource_id FROM resources WHERE active=1 AND pool_id=?",
            (resource_pool,)).fetchall()
        if not pool_resources:
            raise ValidationError("资源池不存在或没有可用资源")
        if preferred_resource_id is not None:
            preferred = connection.execute(
                "SELECT 1 FROM resources WHERE resource_id=? AND active=1 AND pool_id=?",
                (preferred_resource_id, resource_pool)).fetchone()
            if preferred is None:
                raise ValidationError("首选资源不在该资源池中")
        return {
            "amount": amount, "duration_slots": duration_slots,
            "earliest_slot": earliest_slot, "deadline_slot": deadline_slot,
            "priority": priority, "committed": committed,
            "preferred_resource_id": preferred_resource_id,
        }

    def reschedule_application(self, *, request_id: str, actor_id: str,
                               application_id: str, earliest_at: str | None = None,
                               deadline_at: str | None = None, amount: int | None = None,
                               duration_hours: int | None = None,
                               priority: str | None = None,
                               committed: bool | None = None,
                               preferred_resource_id: str | None = None):
        """改期一条尚未开始的申请并立即重算。

        申请的排队序号保持不变，因此改期不会通过“重新排队”获得裁决优势；
        已开始运行对应的申请拒绝改期。
        """

        payload = {"actor_id": actor_id, "application_id": application_id,
                   "earliest_at": earliest_at, "deadline_at": deadline_at,
                   "amount": amount, "duration_hours": duration_hours,
                   "priority": priority, "committed": committed,
                   "preferred_resource_id": preferred_resource_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            row = self._application_row(connection, application_id)
            self._require_write(actor, row["organization_id"])
            self._ensure_mutable(connection, row)

            def create():
                earliest_slot = row["earliest_slot"] if earliest_at is None \
                    else slot_of(parse_instant(earliest_at, "earliest_at"))
                deadline_slot = row["deadline_slot"] if deadline_at is None \
                    else slot_of(parse_instant(deadline_at, "deadline_at"))
                new_amount = row["amount"] if amount is None else amount
                new_duration = row["duration_slots"] if duration_hours is None \
                    else duration_hours
                new_priority = row["priority"] if priority is None else priority
                new_committed = bool(row["committed"]) if committed is None \
                    else committed
                new_preferred = row["preferred_resource_id"] \
                    if preferred_resource_id is None else preferred_resource_id
                if not isinstance(new_amount, int) or new_amount < 1:
                    raise ValidationError("amount 必须是不小于 1 的整数")
                if not isinstance(new_duration, int) or new_duration < 1:
                    raise ValidationError("duration_hours 必须是不小于 1 的整数小时")
                if new_priority not in planner.PRIORITY_RANK:
                    raise ValidationError("priority 必须是 P0/P1/P2/P3 之一")
                if deadline_slot < earliest_slot + new_duration:
                    raise ValidationError("可行窗口长度不足以容纳该时长")
                if new_preferred is not None and connection.execute(
                        "SELECT 1 FROM resources WHERE resource_id=? AND active=1 AND pool_id=?",
                        (new_preferred, row["resource_pool"])).fetchone() is None:
                    raise ValidationError("首选资源不在该资源池中")
                connection.execute(
                    "UPDATE applications SET amount=?,duration_slots=?,earliest_slot=?,"
                    "deadline_slot=?,priority=?,committed=?,preferred_resource_id=?,"
                    "status='pending',updated_at=? WHERE application_id=?",
                    (new_amount, new_duration, earliest_slot, deadline_slot,
                     new_priority, 1 if new_committed else 0, new_preferred,
                     self._now(), application_id))
                append_event(connection, actor_id=actor_id,
                             action="application.rescheduled",
                             resource_type="application", resource_id=application_id,
                             detail={"earliest_slot": earliest_slot,
                                     "deadline_slot": deadline_slot,
                                     "amount": new_amount,
                                     "duration_slots": new_duration,
                                     "priority": new_priority,
                                     "committed": new_committed},
                             occurred_at=self._now())
                plan = self._replan(connection, reason="application_rescheduled")
                return "application", application_id, {
                    "application_id": application_id,
                    "plan_id": plan.plan_id if plan else None,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="reschedule_application", payload=payload,
                                    create=create)

    def cancel_application(self, *, request_id: str, actor_id: str,
                           application_id: str, reason: str = ""):
        """取消未开始的申请并释放其预留，随后重算其它申请。"""

        payload = {"actor_id": actor_id, "application_id": application_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            row = self._application_row(connection, application_id)
            self._require_write(actor, row["organization_id"])
            self._ensure_mutable(connection, row)

            def create():
                connection.execute(
                    "UPDATE applications SET status='cancelled',updated_at=? "
                    "WHERE application_id=?", (self._now(), application_id))
                connection.execute(
                    "UPDATE allocations SET status='superseded' WHERE application_id=? "
                    "AND status='confirmed'", (application_id,))
                append_event(connection, actor_id=actor_id, action="application.cancelled",
                             resource_type="application", resource_id=application_id,
                             detail={"reason": reason}, occurred_at=self._now())
                plan = self._replan(connection, reason="application_cancelled")
                return "application", application_id, {
                    "application_id": application_id,
                    "plan_id": plan.plan_id if plan else None,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="cancel_application", payload=payload,
                                    create=create)

    def mark_started(self, *, request_id: str, actor_id: str, application_id: str):
        """显式确认一次运行已经开始；开始后其额度放置被永久封冻。"""

        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            row = self._application_row(connection, application_id)
            self._ensure_mutable(connection, row)

            def create():
                allocation = connection.execute(
                    "SELECT * FROM allocations WHERE application_id=? AND status='confirmed'",
                    (application_id,)).fetchone()
                if allocation is None:
                    raise ConflictError("该申请当前没有已确认的预留，无法开始运行")
                current_slot = self._current_slot()
                if current_slot < allocation["start_slot"]:
                    raise ConflictError("尚未到达预留开始时间")
                if current_slot >= allocation["end_slot"]:
                    raise ConflictError("预留时段已经结束")
                connection.execute(
                    "UPDATE allocations SET status='sealed', sealed_at=? WHERE allocation_id=?",
                    (self._now(), allocation["allocation_id"]))
                connection.execute(
                    "UPDATE applications SET status='running',updated_at=? WHERE application_id=?",
                    (self._now(), application_id))
                append_event(connection, actor_id=actor_id, action="run.started",
                             resource_type="allocation",
                             resource_id=allocation["allocation_id"],
                             detail={"application_id": application_id,
                                     "resource_id": allocation["resource_id"],
                                     "start_slot": allocation["start_slot"],
                                     "end_slot": allocation["end_slot"]},
                             occurred_at=self._now())
                return "allocation", allocation["allocation_id"], {
                    "allocation_id": allocation["allocation_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="mark_started", payload=payload, create=create)

    def failover_run(self, *, request_id: str, actor_id: str, application_id: str,
                     new_application_id: str, reason: str = "",
                     deadline_at: str | None = None):
        """中断已开始的运行并登记续接申请，由冻结规则重新裁决剩余需求。

        旧放置不被回写：原分配保留为 ``interrupted`` 事实，新申请通过
        ``continuation_of`` 与原申请关联，优先级与数量默认沿用原申请。
        """

        payload = {"actor_id": actor_id, "application_id": application_id,
                   "new_application_id": new_application_id, "reason": reason,
                   "deadline_at": deadline_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require_write(actor)
            new_application_id = self._identifier(new_application_id, "new_application_id")
            row = self._application_row(connection, application_id)
            self._require_write(actor, row["organization_id"])
            allocation = connection.execute(
                "SELECT * FROM allocations WHERE application_id=? AND status='sealed'",
                (application_id,)).fetchone()
            if allocation is None:
                raise ConflictError("只有已经开始且尚未转移的运行才能登记故障转移")
            if self._current_slot() >= allocation["end_slot"]:
                raise ConflictError("运行时段已经结束，不能再故障转移")

            def create():
                connection.execute(
                    "UPDATE allocations SET status='interrupted' WHERE allocation_id=?",
                    (allocation["allocation_id"],))
                connection.execute(
                    "UPDATE applications SET status='interrupted',updated_at=? "
                    "WHERE application_id=?", (self._now(), application_id))
                append_event(connection, actor_id=actor_id, action="run.interrupted",
                             resource_type="allocation",
                             resource_id=allocation["allocation_id"],
                             detail={"application_id": application_id, "reason": reason},
                             occurred_at=self._now())
                if connection.execute("SELECT 1 FROM applications WHERE application_id=?",
                                      (new_application_id,)).fetchone():
                    raise ConflictError("续接申请编号已经存在")
                current_slot = self._current_slot()
                # 续接只覆盖尚未消耗的槽位；原运行已封冻的历史保持不变。
                remaining = max(1, allocation["end_slot"] - current_slot)
                new_deadline = max(row["deadline_slot"], current_slot + remaining) \
                    if deadline_at is None else slot_of(parse_instant(deadline_at, "deadline_at"))
                if new_deadline < current_slot + remaining:
                    raise ValidationError("续接截止时间无法容纳剩余时长")
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM applications"
                ).fetchone()["next"]
                connection.execute(
                    "INSERT INTO applications(application_id,organization_id,team_id,"
                    "task_id,resource_pool,amount,duration_slots,earliest_slot,"
                    "deadline_slot,priority,committed,preferred_resource_id,status,"
                    "sequence,continuation_of,request_id,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?)",
                    (new_application_id, row["organization_id"], row["team_id"],
                     row["task_id"], row["resource_pool"], row["amount"],
                     remaining, current_slot, new_deadline, row["priority"],
                     row["committed"], None, sequence, application_id, request_id,
                     actor_id, self._now(), self._now()))
                append_event(connection, actor_id=actor_id,
                             action="application.continued",
                             resource_type="application",
                             resource_id=new_application_id,
                             detail={"continuation_of": application_id, "reason": reason},
                             occurred_at=self._now())
                plan = self._replan(connection, reason="failover")
                return "application", new_application_id, {
                    "application_id": new_application_id,
                    "plan_id": plan.plan_id if plan else None,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="failover_run", payload=payload, create=create)

    # -- 规划与恢复 -----------------------------------------------------

    def run_recovery(self) -> PlanSummary | None:
        """服务恢复后继续处理待确认申请。

        只有当前输入算出的指纹与最近一版计划不一致时才落新版本，因此重复
        恢复是幂等的，不会制造空计划版本。
        """

        with self.database.transaction(immediate=True) as connection:
            return self._replan(connection, reason="startup_recovery", recovered=True)

    def _replan(self, connection, *, reason: str, recovered: bool = False):
        current_slot = self._current_slot()
        self._reconcile_lifecycle(connection, current_slot)
        inputs = self._build_inputs(connection, current_slot)
        if inputs is None:
            return None
        resources, windows, fixed, reservations = inputs
        result = planner.plan_allocations(
            resources=resources, windows=windows, fixed=fixed,
            reservations=reservations, current_slot=current_slot)
        latest = connection.execute(
            "SELECT fingerprint FROM plan_versions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if latest is not None and latest["fingerprint"] == result.fingerprint:
            return None
        return self._persist_plan(connection, result, current_slot, recovered=recovered,
                                  reason=reason)

    def _reconcile_lifecycle(self, connection, current_slot: int) -> None:
        """按墙钟推进分配与申请的生命周期；封冻动作只追加，不回写。"""

        active_rows = connection.execute(
            "SELECT * FROM allocations WHERE status IN ('confirmed','sealed')"
        ).fetchall()
        for row in active_rows:
            if row["status"] == "sealed" and row["end_slot"] <= current_slot:
                connection.execute(
                    "UPDATE allocations SET status='completed' WHERE allocation_id=?",
                    (row["allocation_id"],))
                connection.execute(
                    "UPDATE applications SET status='completed',updated_at=? "
                    "WHERE application_id=?",
                    (self._now(), row["application_id"]))
            elif row["status"] == "confirmed" and row["start_slot"] <= current_slot \
                    and current_slot < row["end_slot"]:
                connection.execute(
                    "UPDATE allocations SET status='sealed', sealed_at=? WHERE allocation_id=?",
                    (self._now(), row["allocation_id"]))
                connection.execute(
                    "UPDATE applications SET status='running',updated_at=? WHERE application_id=?",
                    (self._now(), row["application_id"]))
                append_event(connection, actor_id="system", action="run.auto_sealed",
                             resource_type="allocation",
                             resource_id=row["allocation_id"],
                             detail={"application_id": row["application_id"]},
                             occurred_at=self._now())
        for row in connection.execute(
                "SELECT * FROM applications WHERE status IN ('pending','confirmed','waitlisted')"):
            active_allocation = connection.execute(
                "SELECT * FROM allocations WHERE application_id=? AND status != 'superseded'",
                (row["application_id"],)).fetchone()
            # 已封冻说明运行确实开始，由上面的分支负责完成，不算过期。
            if active_allocation is not None and active_allocation["status"] == "sealed":
                continue
            last_start = row["deadline_slot"] - row["duration_slots"]
            placed_end_passed = active_allocation is not None \
                and active_allocation["status"] == "confirmed" \
                and current_slot >= active_allocation["end_slot"]
            if last_start < current_slot or placed_end_passed:
                # 从未开始却已越过最后可行起跑点：释放其未开始预留并标记过期。
                if active_allocation is not None \
                        and active_allocation["status"] == "confirmed":
                    connection.execute(
                        "UPDATE allocations SET status='superseded' WHERE allocation_id=?",
                        (active_allocation["allocation_id"],))
                    append_event(connection, actor_id="system",
                                 action="allocation.revoked",
                                 resource_type="allocation",
                                 resource_id=active_allocation["allocation_id"],
                                 detail={"application_id": row["application_id"],
                                         "reason": "deadline_passed_without_start"},
                                 occurred_at=self._now())
                connection.execute(
                    "UPDATE applications SET status='expired',updated_at=? WHERE application_id=?",
                    (self._now(), row["application_id"]))

    def _build_inputs(self, connection, current_slot: int,
                      *, window_override: tuple[str, int, int, int] | None = None,
                      drop_application_id: str | None = None):
        resource_rows = connection.execute(
            "SELECT * FROM resources WHERE active=1").fetchall()
        if not resource_rows:
            return None
        resources = {row["resource_id"]: row["pool_id"] for row in resource_rows}
        window_rows = connection.execute(
            "SELECT * FROM resource_windows WHERE superseded=0").fetchall()
        windows: list[Window] = []
        for row in window_rows:
            windows.append(Window(row["resource_id"], row["start_slot"],
                                  row["end_slot"], row["capacity"]))
        if window_override is not None:
            resource_id, start_slot, end_slot, capacity = window_override
            windows = merge_window(windows, resource_id, start_slot, end_slot, capacity)
        fixed: list[FixedAllocation] = []
        # 已封冻的运行不可移动；已到起跑时刻的 confirmed 分配在下一次真实重算时
        # 会先被自动封冻，预演中同样按固定负载处理，保持与落库结果一致。
        started_application_ids: set[str] = set()
        fixed_rows = connection.execute(
            "SELECT a.* FROM allocations a WHERE a.status='sealed' OR "
            "(a.status='confirmed' AND a.start_slot <= ? AND a.end_slot > ?)",
            (current_slot, current_slot)).fetchall()
        for row in fixed_rows:
            fixed.append(FixedAllocation(row["allocation_id"], row["application_id"],
                                         row["resource_id"], row["start_slot"],
                                         row["end_slot"], row["amount"]))
            started_application_ids.add(row["application_id"])
        reservations: list[ReservationInput] = []
        rows = connection.execute(
            "SELECT * FROM applications WHERE status IN ('pending','confirmed','waitlisted') "
            "ORDER BY sequence").fetchall()
        for row in rows:
            if row["application_id"] in started_application_ids:
                continue
            if row["application_id"] == drop_application_id:
                continue
            if row["deadline_slot"] - row["duration_slots"] < current_slot:
                continue
            reservations.append(ReservationInput(
                application_id=row["application_id"], team_id=row["team_id"],
                task_id=row["task_id"], resource_pool=row["resource_pool"],
                amount=row["amount"], duration_slots=row["duration_slots"],
                earliest_slot=row["earliest_slot"], deadline_slot=row["deadline_slot"],
                priority_rank=planner.PRIORITY_RANK[row["priority"]],
                committed=bool(row["committed"]), sequence=row["sequence"],
                preferred_resource_id=row["preferred_resource_id"]))
        if not reservations and window_override is None and drop_application_id is None:
            # 没有待裁决申请时不落计划版本。
            return None
        return resources, windows, fixed, reservations

    def _persist_plan(self, connection, result, current_slot: int, *,
                      recovered: bool, reason: str) -> PlanSummary:
        plan_id = uuid.uuid4().hex
        decisions_payload = [
            {"application_id": d.application_id, "outcome": d.outcome, "reason": d.reason,
             "placement": None if d.placement is None else d.placement.__dict__,
             "alternatives": [alternative.__dict__ for alternative in d.alternatives]}
            for d in result.decisions
        ]
        connection.execute(
            "INSERT INTO plan_versions(plan_id,fingerprint,rules_version,current_slot,"
            "decisions_json,confirmed,waitlisted,recovered,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (plan_id, result.fingerprint, RULES_VERSION, current_slot,
             canonical_json(decisions_payload), result.confirmed, result.waitlisted,
             1 if recovered else 0, self._now()))

        decisions = {d.application_id: d for d in result.decisions}
        for application_id, decision in decisions.items():
            previous = connection.execute(
                "SELECT * FROM allocations WHERE application_id=? AND status='confirmed'",
                (application_id,)).fetchone()
            if decision.outcome == "confirmed":
                placement = decision.placement
                same = previous is not None and all((
                    previous["resource_id"] == placement.resource_id,
                    previous["start_slot"] == placement.start_slot,
                    previous["end_slot"] == placement.end_slot,
                    previous["amount"] == placement.amount,
                ))
                if not same:
                    if previous is not None:
                        connection.execute(
                            "UPDATE allocations SET status='superseded' WHERE allocation_id=?",
                            (previous["allocation_id"],))
                        append_event(connection, actor_id="system",
                                     action="allocation.revoked",
                                     resource_type="allocation",
                                     resource_id=previous["allocation_id"],
                                     detail={"application_id": application_id,
                                             "plan_id": plan_id, "reason": reason},
                                     occurred_at=self._now())
                    allocation_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO allocations(allocation_id,application_id,plan_id,"
                        "resource_id,start_slot,end_slot,amount,status,sealed_at,created_at) "
                        "VALUES(?,?,?,?,?,?,?, 'confirmed', NULL, ?)",
                        (allocation_id, application_id, plan_id, placement.resource_id,
                         placement.start_slot, placement.end_slot, placement.amount,
                         self._now()))
                    append_event(connection, actor_id="system",
                                 action="allocation.proposed",
                                 resource_type="allocation", resource_id=allocation_id,
                                 detail={"application_id": application_id,
                                         "plan_id": plan_id,
                                         "resource_id": placement.resource_id,
                                         "start_slot": placement.start_slot,
                                         "end_slot": placement.end_slot},
                                 occurred_at=self._now())
                connection.execute(
                    "UPDATE applications SET status='confirmed',updated_at=? WHERE application_id=?",
                    (self._now(), application_id))
            else:
                if previous is not None:
                    connection.execute(
                        "UPDATE allocations SET status='superseded' WHERE allocation_id=?",
                        (previous["allocation_id"],))
                    append_event(connection, actor_id="system",
                                 action="allocation.revoked",
                                 resource_type="allocation",
                                 resource_id=previous["allocation_id"],
                                 detail={"application_id": application_id,
                                         "plan_id": plan_id, "reason": reason},
                                 occurred_at=self._now())
                connection.execute(
                    "UPDATE applications SET status='waitlisted',updated_at=? WHERE application_id=?",
                    (self._now(), application_id))
        append_event(connection, actor_id="system", action="plan.generated",
                     resource_type="plan", resource_id=plan_id,
                     detail={"fingerprint": result.fingerprint,
                             "rules_version": RULES_VERSION,
                             "confirmed": result.confirmed,
                             "waitlisted": result.waitlisted,
                             "recovered": recovered, "reason": reason},
                     occurred_at=self._now())
        return PlanSummary(plan_id, result.fingerprint, RULES_VERSION, current_slot,
                           result.confirmed, result.waitlisted, recovered, self._now())

    # -- 查询与解释 -----------------------------------------------------

    def get_team(self, team_id: str) -> Team:
        row = self._team_row(self.database.connection, team_id)
        return Team(row["team_id"], row["organization_id"], row["name"], row["created_at"])

    def get_task(self, task_id: str) -> Task:
        row = self.database.connection.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务不存在")
        return Task(row["task_id"], row["team_id"], row["name"], bool(row["active"]),
                    row["created_at"])

    def get_resource(self, resource_id: str) -> Resource:
        row = self._resource_row(self.database.connection, resource_id)
        return Resource(row["resource_id"], row["organization_id"], row["pool_id"],
                        row["name"], bool(row["active"]), row["created_at"])

    def list_resources(self, pool_id: str | None = None) -> list[Resource]:
        if pool_id is None:
            rows = self.database.connection.execute(
                "SELECT * FROM resources ORDER BY pool_id, resource_id").fetchall()
        else:
            rows = self.database.connection.execute(
                "SELECT * FROM resources WHERE pool_id=? ORDER BY resource_id",
                (pool_id,)).fetchall()
        return [Resource(row["resource_id"], row["organization_id"], row["pool_id"],
                         row["name"], bool(row["active"]), row["created_at"]) for row in rows]

    def get_application(self, application_id: str) -> dict[str, Any]:
        """说明申请的当前状态、占用它的额度以及替代方案。"""

        connection = self.database.connection
        row = self._application_row(connection, application_id)
        allocation_row = connection.execute(
            "SELECT * FROM allocations WHERE application_id=? AND status!='superseded' "
            "ORDER BY rowid DESC LIMIT 1", (application_id,)).fetchone()
        decision = self._latest_decision(connection, application_id)
        return {
            "application": self._application_dict(row),
            "allocation": None if allocation_row is None else self._allocation_dict(allocation_row),
            "decision": None if decision is None else self._decision_dict(decision),
            "continuation_of": row["continuation_of"],
        }

    def list_applications(self, team_id: str | None = None,
                          status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM applications WHERE 1=1"
        parameters: list[Any] = []
        if team_id is not None:
            query += " AND team_id=?"
            parameters.append(team_id)
        if status is not None:
            query += " AND status=?"
            parameters.append(status)
        query += " ORDER BY sequence"
        return [self._application_dict(row)
                for row in self.database.connection.execute(query, parameters)]

    def quota_view(self, resource_id: str, start_at: str, end_at: str) -> dict[str, Any]:
        """逐槽位说明额度当前被谁占用、还剩多少、是否因故障超额。"""

        connection = self.database.connection
        self._resource_row(connection, resource_id)
        start_slot = slot_of(parse_instant(start_at, "start_at"))
        end_slot = slot_of(parse_instant(end_at, "end_at"))
        if end_slot <= start_slot:
            raise ValidationError("end_at 必须晚于 start_at")
        windows = {row["start_slot"]: row for row in connection.execute(
            "SELECT * FROM resource_windows WHERE resource_id=? AND superseded=0 "
            "AND start_slot < ? AND end_slot > ? ORDER BY start_slot",
            (resource_id, end_slot, start_slot))}
        allocations = connection.execute(
            "SELECT al.*, ap.team_id, ap.task_id FROM allocations al "
            "JOIN applications ap ON ap.application_id=al.application_id "
            "WHERE al.resource_id=? AND al.status IN ('confirmed','sealed') "
            "AND al.start_slot < ? AND al.end_slot > ?",
            (resource_id, end_slot, start_slot)).fetchall()
        slots: list[dict[str, Any]] = []
        for slot in range(start_slot, end_slot):
            capacity = None
            for row in windows.values():
                if row["start_slot"] <= slot < row["end_slot"]:
                    capacity = row["capacity"]
                    break
            holders = []
            used = 0
            for row in allocations:
                if row["start_slot"] <= slot < row["end_slot"]:
                    holders.append({
                        "application_id": row["application_id"],
                        "team_id": row["team_id"], "task_id": row["task_id"],
                        "amount": row["amount"],
                        "state": row["status"],
                        "sealed": row["status"] == "sealed",
                    })
                    used += row["amount"]
            slots.append({
                "slot": slot, "at": slot_to_iso(slot),
                "capacity": capacity, "allocated": used,
                "remaining": None if capacity is None else capacity - used,
                "overcommitted": capacity is not None and used > capacity,
                "holders": sorted(holders, key=lambda item: item["application_id"]),
            })
        return {"resource_id": resource_id, "rules_version": RULES_VERSION,
                "slots": slots}

    def preview_window_change(self, *, resource_id: str, start_at: str,
                              end_at: str, capacity: int) -> dict[str, Any]:
        """预演一次容量变化，返回受影响申请与它们的新放置。"""

        connection = self.database.connection
        self._resource_row(connection, resource_id)
        start_slot = slot_of(parse_instant(start_at, "start_at"))
        end_slot = slot_of(parse_instant(end_at, "end_at"))
        if end_slot <= start_slot:
            raise ValidationError("end_at 必须晚于 start_at")
        if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity < 0:
            raise ValidationError("capacity 必须是非负整数")
        return self._preview(connection, window_override=(resource_id, start_slot,
                                                          end_slot, capacity))

    def preview_cancel(self, application_id: str) -> dict[str, Any]:
        """预演取消某申请会把哪些等待中的申请提前确认。"""

        connection = self.database.connection
        self._application_row(connection, application_id)
        return self._preview(connection, drop_application_id=application_id)

    def _preview(self, connection, *, window_override=None,
                 drop_application_id=None) -> dict[str, Any]:
        current_slot = self._current_slot()
        baseline_inputs = self._build_inputs(connection, current_slot)
        if baseline_inputs is None:
            return {"affected": [], "projected": []}
        projected_inputs = self._build_inputs(
            connection, current_slot, window_override=window_override,
            drop_application_id=drop_application_id)
        baseline = planner.plan_allocations(
            resources=baseline_inputs[0], windows=baseline_inputs[1],
            fixed=baseline_inputs[2], reservations=baseline_inputs[3],
            current_slot=current_slot)
        if projected_inputs is None:
            projected_decisions: dict[str, Decision] = {}
            projected_fingerprint = None
        else:
            projected = planner.plan_allocations(
                resources=projected_inputs[0], windows=projected_inputs[1],
                fixed=projected_inputs[2], reservations=projected_inputs[3],
                current_slot=current_slot)
            projected_decisions = {d.application_id: d for d in projected.decisions}
            projected_fingerprint = projected.fingerprint
        baseline_decisions = {d.application_id: d for d in baseline.decisions}
        affected = []
        all_ids = sorted(set(baseline_decisions) | set(projected_decisions))
        for application_id in all_ids:
            before = baseline_decisions.get(application_id)
            after = projected_decisions.get(application_id)
            if self._placement_tuple(before) == self._placement_tuple(after) \
                    and (before is not None and before.outcome) == \
                    (after is not None and after.outcome):
                continue
            affected.append({
                "application_id": application_id,
                "before": None if before is None else self._decision_dict(before),
                "after": None if after is None else self._decision_dict(after),
            })
        return {"projected_fingerprint": projected_fingerprint,
                "affected": affected}

    def latest_plan(self) -> PlanSummary | None:
        row = self.database.connection.execute(
            "SELECT * FROM plan_versions ORDER BY rowid DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return PlanSummary(row["plan_id"], row["fingerprint"], row["rules_version"],
                           row["current_slot"], row["confirmed"], row["waitlisted"],
                           bool(row["recovered"]), row["created_at"])

    # -- 行与序列化辅助 -------------------------------------------------

    def _team_row(self, connection, team_id: str):
        row = connection.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
        if row is None:
            raise NotFoundError("团队不存在")
        return row

    def _resource_row(self, connection, resource_id: str):
        row = connection.execute("SELECT * FROM resources WHERE resource_id=?",
                                 (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return row

    def _application_row(self, connection, application_id: str):
        row = connection.execute("SELECT * FROM applications WHERE application_id=?",
                                 (application_id,)).fetchone()
        if row is None:
            raise NotFoundError("申请不存在")
        return row

    def _ensure_mutable(self, connection, row) -> None:
        if row["status"] in TERMINAL_STATUSES:
            raise ConflictError(f"申请处于 {row['status']} 状态，不能再变更")
        sealed = connection.execute(
            "SELECT 1 FROM allocations WHERE application_id=? AND status='sealed'",
            (row["application_id"],)).fetchone()
        if sealed is not None:
            raise ConflictError("运行已经开始，预留不能回写；如需调整请使用故障转移")

    def _latest_decision(self, connection, application_id: str):
        row = connection.execute(
            "SELECT decisions_json FROM plan_versions ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        for item in json.loads(row["decisions_json"]):
            if item["application_id"] == application_id:
                return item
        return None

    def _application_dict(self, row) -> dict[str, Any]:
        return {
            "application_id": row["application_id"],
            "organization_id": row["organization_id"],
            "team_id": row["team_id"], "task_id": row["task_id"],
            "resource_pool": row["resource_pool"], "amount": row["amount"],
            "duration_slots": row["duration_slots"],
            "earliest_slot": row["earliest_slot"],
            "earliest_at": slot_to_iso(row["earliest_slot"]),
            "deadline_slot": row["deadline_slot"],
            "deadline_at": slot_to_iso(row["deadline_slot"]),
            "priority": row["priority"], "committed": bool(row["committed"]),
            "preferred_resource_id": row["preferred_resource_id"],
            "status": row["status"], "sequence": row["sequence"],
            "continuation_of": row["continuation_of"],
            "created_by": row["created_by"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _allocation_dict(self, row) -> dict[str, Any]:
        return {
            "allocation_id": row["allocation_id"],
            "application_id": row["application_id"], "plan_id": row["plan_id"],
            "resource_id": row["resource_id"], "start_slot": row["start_slot"],
            "start_at": slot_to_iso(row["start_slot"]),
            "end_slot": row["end_slot"], "end_at": slot_to_iso(row["end_slot"]),
            "amount": row["amount"], "status": row["status"],
            "sealed_at": row["sealed_at"], "created_at": row["created_at"],
        }

    def _decision_dict(self, decision) -> dict[str, Any]:
        if isinstance(decision, Decision):
            placement = decision.placement
            alternatives = decision.alternatives
            outcome = decision.outcome
            reason = decision.reason
            application_id = decision.application_id
        else:
            application_id = decision["application_id"]
            outcome = decision["outcome"]
            reason = decision["reason"]
            placement = None if decision["placement"] is None else Placement(
                **decision["placement"])
            alternatives = tuple(Alternative(**item) for item in decision["alternatives"])
        return {
            "application_id": application_id, "outcome": outcome, "reason": reason,
            "placement": None if placement is None else {
                "resource_id": placement.resource_id,
                "start_slot": placement.start_slot, "start_at": slot_to_iso(placement.start_slot),
                "end_slot": placement.end_slot, "end_at": slot_to_iso(placement.end_slot),
                "amount": placement.amount,
            },
            "alternatives": [{
                "resource_id": item.resource_id, "start_slot": item.start_slot,
                "start_at": slot_to_iso(item.start_slot),
                "end_slot": item.end_slot, "end_at": slot_to_iso(item.end_slot),
                "beyond_deadline": item.beyond_deadline,
            } for item in alternatives],
        }

    def _placement_tuple(self, decision: Decision | None):
        if decision is None or decision.placement is None:
            return None
        placement = decision.placement
        return (decision.outcome, placement.resource_id, placement.start_slot,
                placement.end_slot, placement.amount)
