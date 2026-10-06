"""配额协调服务。

在既有“权限校验 + 幂等回执 + 即时事务 + 哈希审计链”的基础上，增加团队、
任务、资源时段、优先级承诺与额度申请的协调能力。所有可能改变占用结果的
动作（申请、确认、改期、取消、故障转移、容量调整、启动恢复）都在同一个
即时事务内调用纯函数规划器 ``plan_batch``，因此：

* 冲突时按冻结规则（见 ``arbitration.py``）产生唯一结果；
* 已经开始的运行在规划器中即不可移动，服务层也拒绝回写；
* 未开始的预留在任何资源变化后都会整体重新计算；
* dry-run 影响分析与真正落库走同一份规划代码，替代方案不会失真。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Callable

from . import arbitration
from .audit import append_event, canonical_json, digest
from .clock import Clock, SystemClock
from .errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    StateConflictError,
    UnprocessableError,
    ValidationError,
)
from .models import ImpactedApplication, QuotaApplication, ResourceWindow
from .planner import Participant, plan_batch, parse_ts
from .service import IDENTIFIER, DomainService
from .storage import Database

# 仍参与裁决的申请状态；cancelled/expired 已退出生命周期。
ACTIVE_APPLICATION_STATUSES = frozenset(
    {"pending", "granted", "confirmed", "running", "completed", "waitlisted"}
)
UNSTARTED_STATUSES = frozenset({"pending", "granted", "confirmed", "waitlisted"})


class QuotaService:
    """提供配额登记、裁决、生命周期与解释能力。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self._base = DomainService(database, self.clock)

    # ------------------------------------------------------------------ 基础工具

    def _now_dt(self) -> datetime:
        return self.clock.now()

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _id(self, value: Any, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _integer(self, value: Any, field: str, minimum: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValidationError(f"{field} 必须是不小于 {minimum} 的整数")
        return value

    def _timestamp(self, value: Any, field: str) -> str:
        try:
            parsed = parse_ts(str(value).strip())
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{field} 必须是带时区的 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.isoformat().replace("+00:00", "Z")

    def _actor(self, connection, actor_id: str):
        return self._base._actor(connection, actor_id)

    def _require(self, actor, *roles: str) -> None:
        self._base._require(actor, *roles)

    def _same_org(self, actor, organization_id: str) -> None:
        if actor.organization_id != organization_id and actor.role != "admin":
            raise PermissionDenied("不能操作其他组织的配额对象")

    def _idem(self, connection, *, request_id: str, action: str, payload: dict[str, Any],
              create: Callable[[], tuple[str, str, dict[str, Any]]]) -> dict[str, Any]:
        request_id = self._id(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)
        ).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return {**json.loads(row["response_json"]), "replayed": True}
        _resource_type, _resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,"
            "response_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, _resource_type, _resource_id,
             canonical_json(response), self._now()),
        )
        return {**response, "replayed": False}

    # ------------------------------------------------------------------ 快照与规划

    def _snapshot(self):
        connection = self.database.connection
        windows: dict[str, ResourceWindow] = {}
        for row in connection.execute("SELECT * FROM resource_windows"):
            alternatives = tuple(
                item["alternative_window_id"]
                for item in connection.execute(
                    "SELECT alternative_window_id FROM window_alternatives "
                    "WHERE window_id=? ORDER BY ordinal, alternative_window_id",
                    (row["window_id"],),
                )
            )
            windows[row["window_id"]] = ResourceWindow(
                window_id=row["window_id"], organization_id=row["organization_id"],
                pool=row["pool"], tier=row["tier"], zone=row["zone"],
                start_at=row["start_at"], end_at=row["end_at"],
                capacity_units=row["capacity_units"], status=row["status"],
                version=row["version"], alternatives=alternatives,
            )
        applications: dict[str, QuotaApplication] = {}
        for row in connection.execute("SELECT * FROM quota_applications ORDER BY sequence"):
            applications[row["application_id"]] = QuotaApplication(
                application_id=row["application_id"], request_id=row["request_id"],
                team_id=row["team_id"], task_id=row["task_id"], units=row["units"],
                candidate_windows=tuple(json.loads(row["candidate_windows_json"])),
                commitment_id=row["commitment_id"], priority_rank=row["priority_rank"],
                status=row["status"], revision=row["revision"],
                sequence=row["sequence"], created_at=row["created_at"],
            )
        allocations = {
            row["application_id"]: row
            for row in connection.execute("SELECT * FROM quota_allocations WHERE state!='released'")
        }
        return windows, applications, allocations

    def _participants(self, applications, allocations) -> list[Participant]:
        participants: list[Participant] = []
        for app in applications.values():
            if app.status not in ACTIVE_APPLICATION_STATUSES:
                continue
            alloc = allocations.get(app.application_id)
            participants.append(Participant(
                application_id=app.application_id, sequence=app.sequence,
                team_id=app.team_id, task_id=app.task_id, units=app.units,
                candidate_windows=app.candidate_windows, priority_rank=app.priority_rank,
                status=app.status, revision=app.revision,
                previous_window_id=alloc["window_id"] if alloc else None,
                decided_at=alloc["decided_at"] if alloc is not None and alloc["state"] == "held" else None,
            ))
        return participants

    def _locked_usage(self, allocations) -> dict[str, int]:
        """已开始运行（running/completed）锁定的容量。"""

        usage: dict[str, int] = {}
        for alloc in allocations.values():
            if alloc["state"] in ("running", "completed"):
                usage[alloc["window_id"]] = usage.get(alloc["window_id"], 0) + alloc["units"]
        return usage

    def _plan(self, windows, applications, allocations):
        return plan_batch(
            self._participants(applications, allocations), windows,
            self._locked_usage(allocations), now=self._now_dt(),
            rules_version=arbitration.RULES_VERSION, rules_digest=arbitration.rules_digest(),
        )

    def _persist_decision(self, connection, *, app: QuotaApplication, result: ImpactedApplication,
                          outcome, attempt: int, batch_id: str, now_text: str) -> None:
        connection.execute(
            "INSERT INTO arbitration_decisions(decision_id,application_id,revision,attempt,"
            "rules_version,rules_digest,ranking_key,outcome,chosen_window_id,detail_json,"
            "batch_id,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, app.application_id, app.revision, attempt,
             outcome.rules_version, outcome.rules_digest,
             arbitration.ranking_token(app.priority_rank, app.sequence, app.application_id),
             result.outcome, result.window_id,
             canonical_json({"reasons": list(result.reasons),
                             "previous_window_id": result.previous_window_id}),
             batch_id, now_text),
        )

    def _set_application_status(self, connection, application_id: str, status: str) -> None:
        connection.execute(
            "UPDATE quota_applications SET status=?, revision=revision+1 WHERE application_id=?",
            (status, application_id),
        )

    def _release_allocation(self, connection, application_id: str) -> bool:
        cursor = connection.execute(
            "UPDATE quota_allocations SET state='released', revision=revision+1 "
            "WHERE application_id=? AND state!='released'", (application_id,),
        )
        return cursor.rowcount > 0

    def _hold_allocation(self, connection, application_id: str, result: ImpactedApplication,
                         now_text: str) -> None:
        """在裁决窗口上建立或迁移一个待确认持有（含从等待状态恢复）。"""

        existing = connection.execute(
            "SELECT allocation_id FROM quota_allocations WHERE application_id=?",
            (application_id,),
        ).fetchone()
        if existing:
            connection.execute(
                "UPDATE quota_allocations SET window_id=?, units=?, state='held', revision=revision+1, "
                "rules_version=?, rules_digest=?, decided_at=?, started_at=NULL, completed_at=NULL "
                "WHERE application_id=?",
                (result.window_id, result.units,
                 arbitration.RULES_VERSION, arbitration.rules_digest(), now_text, application_id),
            )
        else:
            connection.execute(
                "INSERT INTO quota_allocations(allocation_id,application_id,window_id,units,state,"
                "revision,rules_version,rules_digest,decided_at,started_at,completed_at) "
                "VALUES(?,?,?,?,?,1,?,?,?,NULL,NULL)",
                (uuid.uuid4().hex, application_id, result.window_id, result.units, "held",
                 arbitration.RULES_VERSION, arbitration.rules_digest(), now_text),
            )

    def _replan(self, connection, *, trigger: str):
        """整体重算并落库，返回 (规划结果, 实际发生变化的申请清单)。

        调用方必须持有即时事务。变化清单按重算前后的状态与占用窗口比对
        得出，因此既包含被迁走/等待/过期者，也包含被补录的等待者。
        """

        windows, applications, allocations = self._snapshot()
        before = {
            app_id: (app.status,
                     allocations[app_id]["window_id"] if app_id in allocations else None)
            for app_id, app in applications.items()
        }
        outcome = self._plan(windows, applications, allocations)
        now_text = self._now()
        batch_id = uuid.uuid4().hex
        summaries: list[dict[str, Any]] = []

        for participant in self._participants(applications, allocations):
            app = applications[participant.application_id]
            result = outcome.for_application(participant.application_id)
            attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt),0) AS attempt FROM arbitration_decisions "
                "WHERE application_id=?", (app.application_id,),
            ).fetchone()["attempt"] + 1
            self._persist_decision(connection, app=app, result=result, outcome=outcome,
                                   attempt=attempt, batch_id=batch_id, now_text=now_text)

            if result.outcome == "expired":
                if app.status != "expired":
                    self._set_application_status(connection, app.application_id, "expired")
                    self._release_allocation(connection, app.application_id)
                    append_event(connection, actor_id="system", action="quota.hold_expired",
                                 resource_type="quota_application", resource_id=app.application_id,
                                 detail={"previous_window_id": result.previous_window_id},
                                 occurred_at=now_text)
            elif result.outcome == "immovable":
                # 已开始的运行（running/completed）保持原样，不做任何回写。
                pass
            elif result.outcome == "waitlisted":
                if app.status != "waitlisted":
                    self._set_application_status(connection, app.application_id, "waitlisted")
                    self._release_allocation(connection, app.application_id)
                    append_event(connection, actor_id="system", action="quota.waitlisted",
                                 resource_type="quota_application", resource_id=app.application_id,
                                 detail={"reasons": list(result.reasons),
                                         "previous_window_id": result.previous_window_id},
                                 occurred_at=now_text)
            else:  # allocated / moved
                chosen = result.window_id
                if result.previous_window_id != chosen:
                    # 新裁决或窗口迁移：统一回到待确认持有，要求团队重新确认新窗口。
                    self._set_application_status(connection, app.application_id, "granted")
                    self._hold_allocation(connection, app.application_id, result, now_text)
                    append_event(connection, actor_id="system",
                                 action="quota.moved" if result.previous_window_id else "quota.granted",
                                 resource_type="quota_application", resource_id=app.application_id,
                                 detail={"window_id": chosen,
                                         "previous_window_id": result.previous_window_id,
                                         "reasons": list(result.reasons)},
                                 occurred_at=now_text)
                elif app.status in ("pending", "waitlisted"):
                    self._set_application_status(connection, app.application_id, "granted")
                    self._hold_allocation(connection, app.application_id, result, now_text)
                    append_event(connection, actor_id="system", action="quota.granted",
                                 resource_type="quota_application", resource_id=app.application_id,
                                 detail={"window_id": chosen}, occurred_at=now_text)
                # granted/confirmed 且窗口不变：持有与确认继续有效，不刷新 TTL。

            summaries.append({
                "application_id": app.application_id, "outcome": result.outcome,
                "window_id": result.window_id, "previous_window_id": result.previous_window_id,
                "task_id": app.task_id, "team_id": app.team_id,
            })

        append_event(connection, actor_id="system", action="quota.replanned",
                     resource_type="quota_batch", resource_id=batch_id,
                     detail={"trigger": trigger, "rules_version": outcome.rules_version,
                             "rules_digest": outcome.rules_digest,
                             "expired": list(outcome.expired), "outcomes": summaries},
                     occurred_at=now_text)

        changed: list[dict[str, Any]] = []
        for result in outcome.results:
            app_id = result.application_id
            after_row = connection.execute(
                "SELECT a.status AS status, q.window_id AS window_id FROM quota_applications a "
                "LEFT JOIN quota_allocations q ON q.application_id=a.application_id AND q.state!='released' "
                "WHERE a.application_id=?", (app_id,),
            ).fetchone()
            before_status, before_window = before[app_id]
            if after_row["status"] != before_status or after_row["window_id"] != before_window:
                item = self._result_dict(result)
                item["before"] = {"status": before_status, "window_id": before_window}
                item["after"] = {"status": after_row["status"], "window_id": after_row["window_id"]}
                changed.append(item)
        changed.sort(key=lambda item: (item["priority_rank"], item["application_id"]))
        return outcome, changed

    # ------------------------------------------------------------------ 登记：团队与任务

    def register_team(self, *, request_id: str, actor_id: str, team_id: str,
                      name: str, organization_id: str | None = None) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "team_id": team_id, "name": name,
                   "organization_id": organization_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            team_id = self._id(team_id, "team_id")
            name = self._text(name, "name")
            organization_id = organization_id or actor.organization_id
            self._same_org(actor, organization_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO teams(team_id,organization_id,name,created_at) VALUES(?,?,?,?)",
                        (team_id, organization_id, name, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("团队编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="team.registered",
                             resource_type="team", resource_id=team_id,
                             detail={"name": name, "organization_id": organization_id},
                             occurred_at=self._now())
                return "team", team_id, {"team_id": team_id, "name": name,
                                         "organization_id": organization_id}

            return self._idem(connection, request_id=request_id, action="register_team",
                              payload=payload, create=create)

    def register_task(self, *, request_id: str, actor_id: str, task_id: str, team_id: str,
                      name: str, required_units: int) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "task_id": task_id, "team_id": team_id,
                   "name": name, "required_units": required_units}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            task_id = self._id(task_id, "task_id")
            team_id = self._id(team_id, "team_id")
            name = self._text(name, "name")
            required_units = self._integer(required_units, "required_units", 1)
            team = connection.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
            if team is None:
                raise NotFoundError("团队不存在")
            self._same_org(actor, team["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO tasks(task_id,team_id,name,required_units,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (task_id, team_id, name, required_units, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("任务编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="task.registered",
                             resource_type="task", resource_id=task_id,
                             detail={"team_id": team_id, "name": name,
                                     "required_units": required_units},
                             occurred_at=self._now())
                return "task", task_id, {"task_id": task_id, "team_id": team_id,
                                         "required_units": required_units}

            return self._idem(connection, request_id=request_id, action="register_task",
                              payload=payload, create=create)

    # ---------------------------------------------------------- 登记：资源时段与替代

    def register_window(self, *, request_id: str, actor_id: str, window_id: str, pool: str,
                        tier: str, zone: str, start_at: str, end_at: str,
                        capacity_units: int, alternatives: list[str] | None = None,
                        organization_id: str | None = None) -> dict[str, Any]:
        alternatives = alternatives or []
        payload = {"actor_id": actor_id, "window_id": window_id, "pool": pool, "tier": tier,
                   "zone": zone, "start_at": start_at, "end_at": end_at,
                   "capacity_units": capacity_units, "alternatives": alternatives,
                   "organization_id": organization_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            window_id = self._id(window_id, "window_id")
            pool = self._text(pool, "pool", 80)
            zone = self._text(zone, "zone", 80)
            tier = self._text(tier, "tier", 40)
            if tier not in arbitration.TIER_WEIGHT:
                raise ValidationError("tier 必须是 dedicated、reserved 或 shared")
            start_text = self._timestamp(start_at, "start_at")
            end_text = self._timestamp(end_at, "end_at")
            if parse_ts(end_text) <= parse_ts(start_text):
                raise ValidationError("end_at 必须晚于 start_at")
            capacity_units = self._integer(capacity_units, "capacity_units", 0)
            organization_id = organization_id or actor.organization_id
            self._same_org(actor, organization_id)
            alt_ids: list[str] = []
            for alternative_id in alternatives:
                alternative_id = self._id(alternative_id, "alternatives[]")
                row = connection.execute(
                    "SELECT organization_id FROM resource_windows WHERE window_id=?",
                    (alternative_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"替代窗口 {alternative_id} 不存在")
                self._same_org(actor, row["organization_id"])
                if alternative_id == window_id or alternative_id in alt_ids:
                    raise ValidationError("替代窗口不能重复且不能指向自身")
                alt_ids.append(alternative_id)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO resource_windows(window_id,organization_id,pool,tier,zone,"
                        "start_at,end_at,capacity_units,status,version,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,'active',1,?)",
                        (window_id, organization_id, pool, tier, zone, start_text, end_text,
                         capacity_units, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("资源时段编号已经存在") from exc
                for ordinal, alternative_id in enumerate(alt_ids):
                    connection.execute(
                        "INSERT INTO window_alternatives(window_id,alternative_window_id,ordinal) "
                        "VALUES(?,?,?)", (window_id, alternative_id, ordinal),
                    )
                append_event(connection, actor_id=actor_id, action="window.registered",
                             resource_type="resource_window", resource_id=window_id,
                             detail={"pool": pool, "tier": tier, "zone": zone,
                                     "start_at": start_text, "end_at": end_text,
                                     "capacity_units": capacity_units,
                                     "alternatives": alt_ids},
                             occurred_at=self._now())
                outcome, changed = self._replan(connection, trigger="window_registered")
                return "resource_window", window_id, {
                    "window_id": window_id, "capacity_units": capacity_units,
                    "alternatives": alt_ids, "impacted": changed,
                }

            return self._idem(connection, request_id=request_id, action="register_window",
                              payload=payload, create=create)

    def update_window(self, *, request_id: str, actor_id: str, window_id: str,
                      capacity_units: int | None = None,
                      status: str | None = None) -> dict[str, Any]:
        """调整容量或标记故障；随后未开始的预留整体重新计算。"""

        payload = {"actor_id": actor_id, "window_id": window_id,
                   "capacity_units": capacity_units, "status": status}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            window_id = self._id(window_id, "window_id")
            row = connection.execute(
                "SELECT * FROM resource_windows WHERE window_id=?", (window_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("资源时段不存在")
            self._same_org(actor, row["organization_id"])
            if status is not None and status not in ("active", "failed"):
                raise ValidationError("status 只能是 active 或 failed")
            new_capacity = row["capacity_units"]
            if capacity_units is not None:
                new_capacity = self._integer(capacity_units, "capacity_units", 0)
            if capacity_units is None and status is None:
                raise ValidationError("必须提供 capacity_units 或 status")
            new_status = status or row["status"]

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE resource_windows SET capacity_units=?, status=?, version=version+1 "
                    "WHERE window_id=?",
                    (new_capacity, new_status, window_id),
                )
                if status == "failed" and row["status"] != "failed":
                    trigger = "window_failed"
                elif status == "active" and row["status"] != "active":
                    trigger = "window_recovered"
                else:
                    trigger = "window_changed"
                append_event(connection, actor_id=actor_id, action="window.updated",
                             resource_type="resource_window", resource_id=window_id,
                             detail={"previous_capacity_units": row["capacity_units"],
                                     "capacity_units": new_capacity,
                                     "previous_status": row["status"], "status": new_status,
                                     "new_version": row["version"] + 1},
                             occurred_at=self._now())
                outcome, changed = self._replan(connection, trigger=trigger)
                return "resource_window", window_id, {
                    "window_id": window_id, "capacity_units": new_capacity,
                    "status": new_status, "version": row["version"] + 1,
                    "impacted": changed,
                }

            return self._idem(connection, request_id=request_id, action="update_window",
                              payload=payload, create=create)

    # -------------------------------------------------------------- 优先级承诺

    def set_priority_commitment(self, *, request_id: str, actor_id: str, commitment_id: str,
                                team_id: str, task_id: str, rank: int,
                                note: str = "") -> dict[str, Any]:
        payload = {"actor_id": actor_id, "commitment_id": commitment_id, "team_id": team_id,
                   "task_id": task_id, "rank": rank, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            commitment_id = self._id(commitment_id, "commitment_id")
            team_id = self._id(team_id, "team_id")
            task_id = self._id(task_id, "task_id")
            rank = self._integer(rank, "rank", 0)
            note = str(note).strip()
            if len(note) > 500:
                raise ValidationError("note 不能超过 500 个字符")
            team = connection.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
            if team is None:
                raise NotFoundError("团队不存在")
            task = connection.execute("SELECT * FROM tasks WHERE task_id=? AND team_id=?",
                                      (task_id, team_id)).fetchone()
            if task is None:
                raise NotFoundError("任务不存在或不属于该团队")
            self._same_org(actor, team["organization_id"])
            existing = connection.execute(
                "SELECT * FROM priority_commitments WHERE team_id=? AND task_id=?",
                (team_id, task_id),
            ).fetchone()
            if existing is not None and existing["commitment_id"] != commitment_id:
                raise ConflictError("该任务已有不同编号的优先级承诺")
            locked = connection.execute(
                "SELECT 1 FROM quota_applications a JOIN quota_allocations q "
                "ON q.application_id=a.application_id "
                "WHERE a.task_id=? AND q.state IN ('running','completed') LIMIT 1",
                (task_id,),
            ).fetchone()
            if existing is not None and existing["rank"] != rank and locked:
                raise StateConflictError("任务已开始运行，优先级承诺不能再调整")

            def create() -> tuple[str, str, dict[str, Any]]:
                if existing is None:
                    action_name = "priority.committed"
                    connection.execute(
                        "INSERT INTO priority_commitments(commitment_id,team_id,task_id,rank,note,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (commitment_id, team_id, task_id, rank, note, self._now()),
                    )
                else:
                    action_name = "priority.adjusted"
                    connection.execute(
                        "UPDATE priority_commitments SET rank=?, note=? WHERE commitment_id=?",
                        (rank, note, commitment_id),
                    )
                # 未开始申请采用新承诺；已开始申请保留裁决时刻冻结的 rank。
                connection.execute(
                    "UPDATE quota_applications SET priority_rank=?, revision=revision+1 "
                    "WHERE task_id=? AND status IN ('pending','granted','confirmed','waitlisted')",
                    (rank, task_id),
                )
                append_event(connection, actor_id=actor_id, action=action_name,
                             resource_type="priority_commitment", resource_id=commitment_id,
                             detail={"team_id": team_id, "task_id": task_id, "rank": rank,
                                     "note": note},
                             occurred_at=self._now())
                outcome, changed = self._replan(connection, trigger="priority_adjusted")
                return "priority_commitment", commitment_id, {
                    "commitment_id": commitment_id, "team_id": team_id, "task_id": task_id,
                    "rank": rank, "impacted": changed,
                }

            return self._idem(connection, request_id=request_id, action="set_priority_commitment",
                              payload=payload, create=create)

    # ------------------------------------------------------------------ 申请与生命周期

    def _validate_application_inputs(self, connection, actor, team_id: str, task_id: str,
                                     units: Any, candidate_windows: Any):
        team_id = self._id(team_id, "team_id")
        task_id = self._id(task_id, "task_id")
        units = self._integer(units, "units", 1)
        if not isinstance(candidate_windows, list) or not candidate_windows:
            raise ValidationError("candidate_windows 必须是非空数组")
        candidate_ids: list[str] = []
        for candidate in candidate_windows:
            candidate = self._id(candidate, "candidate_windows[]")
            if candidate in candidate_ids:
                continue
            window = connection.execute(
                "SELECT organization_id FROM resource_windows WHERE window_id=?", (candidate,)
            ).fetchone()
            if window is None:
                raise NotFoundError(f"资源时段 {candidate} 不存在")
            self._same_org(actor, window["organization_id"])
            candidate_ids.append(candidate)
        team = connection.execute("SELECT * FROM teams WHERE team_id=?", (team_id,)).fetchone()
        if team is None:
            raise NotFoundError("团队不存在")
        task = connection.execute(
            "SELECT * FROM tasks WHERE task_id=? AND team_id=?", (task_id, team_id)
        ).fetchone()
        if task is None:
            raise NotFoundError("任务不存在或不属于该团队")
        if units < task["required_units"]:
            raise ValidationError(f"units 不能少于任务要求的 {task['required_units']}")
        commitment = connection.execute(
            "SELECT * FROM priority_commitments WHERE team_id=? AND task_id=?",
            (team_id, task_id),
        ).fetchone()
        if commitment is None:
            raise UnprocessableError("申请前必须先冻结优先级承诺")
        return team, task, commitment, candidate_ids

    def apply_quota(self, *, request_id: str, actor_id: str, team_id: str, task_id: str,
                    units: int, candidate_windows: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "team_id": team_id, "task_id": task_id,
                   "units": units, "candidate_windows": candidate_windows}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            team, _task, commitment, candidate_ids = self._validate_application_inputs(
                connection, actor, team_id, task_id, units, candidate_windows)
            self._same_org(actor, team["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                application_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO quota_applications(application_id,request_id,team_id,task_id,units,"
                    "candidate_windows_json,commitment_id,priority_rank,status,revision,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'pending',1,?)",
                    (application_id, request_id, team_id, task_id, units,
                     canonical_json(candidate_ids), commitment["commitment_id"],
                     commitment["rank"], self._now()),
                )
                append_event(connection, actor_id=actor_id, action="quota.applied",
                             resource_type="quota_application", resource_id=application_id,
                             detail={"team_id": team_id, "task_id": task_id, "units": units,
                                     "candidate_windows": candidate_ids,
                                     "commitment_id": commitment["commitment_id"],
                                     "priority_rank": commitment["rank"]},
                             occurred_at=self._now())
                outcome, changed = self._replan(connection, trigger="quota_applied")
                result = outcome.for_application(application_id)
                return "quota_application", application_id, self._application_payload(
                    connection, application_id, outcome, result, changed=changed)

            return self._idem(connection, request_id=request_id, action="apply_quota",
                              payload=payload, create=create)

    def _application_for_actor(self, connection, application_id: str, actor):
        row = connection.execute(
            "SELECT a.*, t.organization_id AS team_organization_id FROM quota_applications a "
            "JOIN teams t ON t.team_id=a.team_id WHERE a.application_id=?",
            (application_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("申请不存在")
        self._same_org(actor, row["team_organization_id"])
        return row

    def confirm_quota(self, *, request_id: str, actor_id: str, application_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            application_id = self._id(application_id, "application_id")
            row = self._application_for_actor(connection, application_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "confirmed":
                    outcome = self._plan_from_live(connection)
                    return "quota_application", application_id, self._application_payload(
                        connection, application_id, outcome, outcome.for_application(application_id))
                if row["status"] in ("running", "completed"):
                    raise StateConflictError("运行已经开始，确认不再有效")
                if row["status"] != "granted":
                    raise StateConflictError("当前没有待确认的额度持有")
                # 只读校验：持有可能已过期，或因资源变化被迁走/列入等待。
                outcome = self._plan_from_live(connection)
                result = outcome.for_application(application_id)
                held_window = _current_window_id(connection, application_id)
                if result.outcome == "expired":
                    raise StateConflictError("待确认持有已超过有效期，请重新申请")
                if result.outcome == "waitlisted":
                    raise StateConflictError("持有已因资源变化失效，申请处于等待队列")
                if result.window_id != held_window:
                    raise StateConflictError("裁决窗口已迁移，请按新结果重新确认")
                connection.execute(
                    "UPDATE quota_applications SET status='confirmed', revision=revision+1 "
                    "WHERE application_id=?", (application_id,))
                connection.execute(
                    "UPDATE quota_allocations SET state='scheduled' WHERE application_id=? "
                    "AND state='held'", (application_id,))
                append_event(connection, actor_id=actor_id, action="quota.confirmed",
                             resource_type="quota_application", resource_id=application_id,
                             detail={"window_id": result.window_id}, occurred_at=self._now())
                outcome = self._plan_from_live(connection)
                return "quota_application", application_id, self._application_payload(
                    connection, application_id, outcome, outcome.for_application(application_id))

            return self._idem(connection, request_id=request_id, action="confirm_quota",
                              payload=payload, create=create)

    def reschedule_quota(self, *, request_id: str, actor_id: str, application_id: str,
                         candidate_windows: list[str]) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id,
                   "candidate_windows": candidate_windows}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            application_id = self._id(application_id, "application_id")
            row = self._application_for_actor(connection, application_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] in ("running", "completed"):
                    raise StateConflictError("已经开始的运行不能改期")
                _team, _task, _commitment, candidate_ids = self._validate_application_inputs(
                    connection, actor, row["team_id"], row["task_id"], row["units"],
                    candidate_windows)
                previous_window_id = _current_window_id(connection, application_id)
                connection.execute(
                    "UPDATE quota_applications SET candidate_windows_json=?, revision=revision+1 "
                    "WHERE application_id=?",
                    (canonical_json(candidate_ids), application_id),
                )
                append_event(connection, actor_id=actor_id, action="quota.rescheduled",
                             resource_type="quota_application", resource_id=application_id,
                             detail={"candidate_windows": candidate_ids,
                                     "previous_window_id": previous_window_id},
                             occurred_at=self._now())
                outcome, changed = self._replan(connection, trigger="quota_rescheduled")
                result = outcome.for_application(application_id)
                return "quota_application", application_id, self._application_payload(
                    connection, application_id, outcome, result, changed=changed)

            return self._idem(connection, request_id=request_id, action="reschedule_quota",
                              payload=payload, create=create)

    def cancel_quota(self, *, request_id: str, actor_id: str, application_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            application_id = self._id(application_id, "application_id")
            row = self._application_for_actor(connection, application_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] in ("running", "completed"):
                    raise StateConflictError("已经开始的运行不能取消或回写")
                if row["status"] != "cancelled":
                    connection.execute(
                        "UPDATE quota_applications SET status='cancelled', revision=revision+1 "
                        "WHERE application_id=?", (application_id,))
                    self._release_allocation(connection, application_id)
                    append_event(connection, actor_id=actor_id, action="quota.cancelled",
                                 resource_type="quota_application", resource_id=application_id,
                                 detail={"previous_window_id": _current_window_id(connection, application_id)},
                                 occurred_at=self._now())
                    outcome, changed = self._replan(connection, trigger="quota_cancelled")
                    impacted = [item for item in changed
                                if item["application_id"] != application_id]
                else:
                    impacted = []
                return "quota_application", application_id, {
                    "application_id": application_id, "status": "cancelled",
                    "impacted": impacted,
                }

            return self._idem(connection, request_id=request_id, action="cancel_quota",
                              payload=payload, create=create)

    def start_run(self, *, request_id: str, actor_id: str, application_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            application_id = self._id(application_id, "application_id")
            row = self._application_for_actor(connection, application_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] in ("running", "completed"):
                    raise StateConflictError("运行已经开始，状态不可回写")
                if row["status"] != "confirmed":
                    raise StateConflictError("只有已确认的预留可以开始运行")
                alloc = connection.execute(
                    "SELECT * FROM quota_allocations WHERE application_id=? AND state='scheduled'",
                    (application_id,),
                ).fetchone()
                window = connection.execute(
                    "SELECT * FROM resource_windows WHERE window_id=?", (alloc["window_id"],)
                ).fetchone()
                if parse_ts(window["start_at"]) > self._now_dt():
                    raise StateConflictError("资源时段尚未开始")
                now_text = self._now()
                connection.execute(
                    "UPDATE quota_applications SET status='running', revision=revision+1 "
                    "WHERE application_id=?", (application_id,))
                connection.execute(
                    "UPDATE quota_allocations SET state='running', started_at=?, revision=revision+1 "
                    "WHERE application_id=?", (now_text, application_id))
                append_event(connection, actor_id=actor_id, action="quota.run_started",
                             resource_type="quota_application", resource_id=application_id,
                             detail={"window_id": alloc["window_id"]}, occurred_at=now_text)
                return "quota_application", application_id, {
                    "application_id": application_id, "status": "running",
                    "window_id": alloc["window_id"], "started_at": now_text,
                }

            return self._idem(connection, request_id=request_id, action="start_run",
                              payload=payload, create=create)

    def complete_run(self, *, request_id: str, actor_id: str, application_id: str) -> dict[str, Any]:
        payload = {"actor_id": actor_id, "application_id": application_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            application_id = self._id(application_id, "application_id")
            row = self._application_for_actor(connection, application_id, actor)

            def create() -> tuple[str, str, dict[str, Any]]:
                if row["status"] == "completed":
                    return "quota_application", application_id, {
                        "application_id": application_id, "status": "completed"}
                if row["status"] != "running":
                    raise StateConflictError("只有运行中的任务可以完成")
                now_text = self._now()
                connection.execute(
                    "UPDATE quota_applications SET status='completed', revision=revision+1 "
                    "WHERE application_id=?", (application_id,))
                connection.execute(
                    "UPDATE quota_allocations SET state='completed', completed_at=?, "
                    "revision=revision+1 WHERE application_id=?",
                    (now_text, application_id))
                append_event(connection, actor_id=actor_id, action="quota.run_completed",
                             resource_type="quota_application", resource_id=application_id,
                             detail={}, occurred_at=now_text)
                return "quota_application", application_id, {
                    "application_id": application_id, "status": "completed",
                    "completed_at": now_text,
                }

            return self._idem(connection, request_id=request_id, action="complete_run",
                              payload=payload, create=create)

    # ------------------------------------------------------------------ 恢复与解释

    def recover_pending(self) -> dict[str, Any]:
        """服务恢复后继续处理待确认/待裁决申请并清理过期持有。"""

        with self.database.transaction(immediate=True) as connection:
            pending_before = connection.execute(
                "SELECT COUNT(*) AS count FROM quota_applications "
                "WHERE status IN ('pending','granted','waitlisted')"
            ).fetchone()["count"]
            outcome, changed = self._replan(connection, trigger="service_recovery")
            after = connection.execute(
                "SELECT status, COUNT(*) AS count FROM quota_applications "
                "WHERE status IN ('pending','granted','waitlisted','expired') GROUP BY status"
            ).fetchall()
            return {"status": "ok", "examined": len(outcome.results),
                    "unresolved_before": pending_before,
                    "status_after": {row["status"]: row["count"] for row in after},
                    "expired_holds": list(outcome.expired),
                    "impacted": changed,
                    "rules_version": outcome.rules_version,
                    "rules_digest": outcome.rules_digest}

    def get_quota(self, application_id: str) -> dict[str, Any]:
        application_id = self._id(application_id, "application_id")
        with self.database.lock:
            return self._get_quota_locked(application_id)

    def _get_quota_locked(self, application_id: str) -> dict[str, Any]:
        connection = self.database.connection
        row = connection.execute(
            "SELECT * FROM quota_applications WHERE application_id=?", (application_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("申请不存在")
        windows, applications, allocations = self._snapshot()
        outcome = self._plan(windows, applications, allocations)
        if application_id in {item.application_id for item in outcome.results}:
            result = outcome.for_application(application_id)
        else:
            # 已取消/已过期申请退出裁决，按终态组装解释信息。
            last = connection.execute(
                "SELECT window_id FROM quota_allocations WHERE application_id=? "
                "ORDER BY revision DESC LIMIT 1", (application_id,),
            ).fetchone()
            result = ImpactedApplication(
                application_id=application_id, team_id=row["team_id"],
                task_id=row["task_id"], units=row["units"],
                priority_rank=row["priority_rank"], outcome=row["status"],
                window_id=None, reasons=(),
                previous_window_id=last["window_id"] if last else None,
            )
        return self._application_payload(connection, application_id, outcome, result)

    def window_quota(self, window_id: str) -> dict[str, Any]:
        """说明某个资源时段的额度当前被谁占用，以及谁在等待它。"""

        window_id = self._id(window_id, "window_id")
        with self.database.lock:
            return self._window_quota_locked(window_id)

    def _window_quota_locked(self, window_id: str) -> dict[str, Any]:
        connection = self.database.connection
        row = connection.execute(
            "SELECT * FROM resource_windows WHERE window_id=?", (window_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("资源时段不存在")
        windows, applications, allocations = self._snapshot()
        outcome = self._plan(windows, applications, allocations)
        occupied: list[dict[str, Any]] = []
        waiting: list[dict[str, Any]] = []
        used = 0
        for result in outcome.results:
            if result.window_id == window_id:
                app = applications[result.application_id]
                occupied.append({
                    "application_id": result.application_id, "team_id": result.team_id,
                    "task_id": result.task_id, "units": result.units,
                    "priority_rank": result.priority_rank,
                    "status": app.status, "outcome": result.outcome,
                    "locked": result.outcome == "immovable",
                })
                if result.outcome in ("allocated", "moved", "immovable"):
                    used += result.units
            elif result.outcome == "waitlisted" and any(
                reason == f"{window_id}:insufficient_capacity" or
                reason == f"{window_id}:window_failed"
                for reason in result.reasons
            ):
                waiting.append({
                    "application_id": result.application_id, "team_id": result.team_id,
                    "task_id": result.task_id, "units": result.units,
                    "priority_rank": result.priority_rank,
                })
        occupied.sort(key=lambda item: (-item["units"], item["application_id"]))
        waiting.sort(key=lambda item: (item["priority_rank"], item["application_id"]))
        return {
            "window_id": window_id, "pool": row["pool"], "tier": row["tier"],
            "zone": row["zone"], "start_at": row["start_at"], "end_at": row["end_at"],
            "capacity_units": row["capacity_units"], "status": row["status"],
            "version": row["version"], "used_units": used,
            "available_units": max(row["capacity_units"] - used, 0),
            "alternatives": list(windows[window_id].alternatives),
            "occupied": occupied, "waiting": waiting,
        }

    def impact_analysis(self, *, window_id: str, capacity_units: int | None = None,
                        status: str | None = None) -> dict[str, Any]:
        """在不落库的前提下预演资源变化，给出受影响任务与替代方案。"""

        window_id = self._id(window_id, "window_id")
        if status is not None and status not in ("active", "failed"):
            raise ValidationError("status 只能是 active 或 failed")
        with self.database.lock:
            return self._impact_analysis_locked(window_id, capacity_units, status)

    def _impact_analysis_locked(self, window_id: str, capacity_units: int | None,
                                status: str | None) -> dict[str, Any]:
        windows, applications, allocations = self._snapshot()
        if window_id not in windows:
            raise NotFoundError("资源时段不存在")
        target = windows[window_id]
        capacity = self._integer(capacity_units if capacity_units is not None
                                 else target.capacity_units, "capacity_units", 0)
        new_status = status or target.status
        baseline = self._plan(windows, applications, allocations)
        projected_windows = dict(windows)
        projected_windows[window_id] = ResourceWindow(
            window_id=target.window_id, organization_id=target.organization_id,
            pool=target.pool, tier=target.tier, zone=target.zone,
            start_at=target.start_at, end_at=target.end_at,
            capacity_units=capacity, status=new_status, version=target.version + 1,
            alternatives=target.alternatives,
        )
        projected = plan_batch(
            self._participants(applications, allocations), projected_windows,
            self._locked_usage(allocations), now=self._now_dt(),
            rules_version=arbitration.RULES_VERSION, rules_digest=arbitration.rules_digest(),
        )
        base_by_id = {item.application_id: item for item in baseline.results}
        changes = []
        for item in projected.results:
            previous = base_by_id.get(item.application_id)
            if previous is None:
                continue
            if previous.outcome != item.outcome or previous.window_id != item.window_id:
                changes.append({
                    "application_id": item.application_id, "team_id": item.team_id,
                    "task_id": item.task_id, "units": item.units,
                    "priority_rank": item.priority_rank,
                    "before": {"outcome": previous.outcome, "window_id": previous.window_id},
                    "after": {"outcome": item.outcome, "window_id": item.window_id},
                    "reasons": list(item.reasons),
                })
        changes.sort(key=lambda item: (item["priority_rank"], item["application_id"]))
        return {
            "mutation": {"window_id": window_id, "capacity_units": capacity,
                         "status": new_status},
            "rules_version": projected.rules_version, "rules_digest": projected.rules_digest,
            "changes": changes,
        }

    # ------------------------------------------------------------------ 组装辅助

    def _plan_from_live(self, connection):
        windows, applications, allocations = self._snapshot()
        return self._plan(windows, applications, allocations)

    def _application_payload(self, connection, application_id: str, outcome, result,
                             changed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM quota_applications WHERE application_id=?", (application_id,)
        ).fetchone()
        _windows, applications, _allocations = self._snapshot()
        payload: dict[str, Any] = {
            "application_id": application_id,
            "team_id": row["team_id"], "task_id": row["task_id"], "units": row["units"],
            "status": row["status"], "revision": row["revision"],
            "priority_rank": row["priority_rank"],
            "commitment_id": row["commitment_id"],
            "candidate_windows": list(json.loads(row["candidate_windows_json"])),
            "latest_outcome": result.outcome, "window_id": result.window_id,
            "previous_window_id": result.previous_window_id,
            "reasons": list(result.reasons),
            "rules_version": outcome.rules_version, "rules_digest": outcome.rules_digest,
            "alternatives": self._alternatives(applications[application_id], _windows, result),
            "impacted": ([item for item in changed if item["application_id"] != application_id]
                         if changed is not None
                         else self._impacted(outcome, ignore=application_id)),
        }
        alloc = connection.execute(
            "SELECT * FROM quota_allocations WHERE application_id=? AND state!='released' "
            "ORDER BY revision DESC", (application_id,),
        ).fetchone()
        if alloc:
            payload["allocation"] = {
                "window_id": alloc["window_id"], "units": alloc["units"],
                "state": alloc["state"], "revision": alloc["revision"],
                "decided_at": alloc["decided_at"], "started_at": alloc["started_at"],
                "completed_at": alloc["completed_at"],
            }
        decisions = connection.execute(
            "SELECT attempt,rules_version,rules_digest,ranking_key,outcome,chosen_window_id,"
            "detail_json,decided_at FROM arbitration_decisions WHERE application_id=? "
            "ORDER BY attempt DESC, rowid DESC", (application_id,),
        ).fetchall()
        payload["decisions"] = [{
            "attempt": item["attempt"], "rules_version": item["rules_version"],
            "rules_digest": item["rules_digest"], "ranking_key": json.loads(item["ranking_key"]),
            "outcome": item["outcome"], "chosen_window_id": item["chosen_window_id"],
            "detail": json.loads(item["detail_json"]), "decided_at": item["decided_at"],
        } for item in decisions]
        return payload

    def _alternatives(self, app: QuotaApplication, windows, result) -> list[dict[str, Any]]:
        from .planner import _expand_preferences

        ordered, why = _expand_preferences(app.candidate_windows, windows)
        blocked = {}
        for reason in result.reasons:
            if ":" in reason:
                wid, code = reason.split(":", 1)
                blocked[wid] = code
        alternatives = []
        for window_id in ordered:
            window = windows[window_id]
            if result.window_id == window_id:
                usable = "chosen"
            elif window_id in blocked:
                usable = blocked[window_id]
            else:
                usable = "available_alternative"
            alternatives.append({
                "window_id": window_id, "start_at": window.start_at, "end_at": window.end_at,
                "tier": window.tier, "status": window.status,
                "capacity_units": window.capacity_units,
                "relation": why.get(window_id, "candidate"), "usable": usable,
            })
        return alternatives

    def _impacted(self, outcome, *, ignore: str | None = None) -> list[dict[str, Any]]:
        items = []
        for result in outcome.results:
            if result.application_id == ignore:
                continue
            if result.outcome in ("moved", "waitlisted", "expired"):
                items.append(self._result_dict(result))
        items.sort(key=lambda item: (item["priority_rank"], item["application_id"]))
        return items

    def _result_dict(self, result: ImpactedApplication) -> dict[str, Any]:
        return {
            "application_id": result.application_id, "team_id": result.team_id,
            "task_id": result.task_id, "units": result.units,
            "priority_rank": result.priority_rank, "outcome": result.outcome,
            "window_id": result.window_id, "previous_window_id": result.previous_window_id,
            "reasons": list(result.reasons),
        }


def _current_window_id(connection, application_id: str) -> str | None:
    row = connection.execute(
        "SELECT window_id FROM quota_allocations WHERE application_id=? AND state!='released'",
        (application_id,),
    ).fetchone()
    return row["window_id"] if row else None
