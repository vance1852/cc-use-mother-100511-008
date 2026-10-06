"""配额规划器：在给定资源快照上产生唯一、可复核的裁决结果。

规划器是纯函数：同样的申请集合、同样的资源快照、同样的当前时间，必然
得到同样的结果。服务层在申请、改期、取消、故障转移、容量变更、影响分析
与启动恢复时都调用同一个 ``plan_batch``；dry-run（影响分析）与真正落库
使用同一份代码路径，因此“替代方案是什么”与实际裁决不会出现偏差。

不可变性边界：已经开始的运行（running/completed，以及所在窗口已到开始
时间的 confirmed 预留）被视为锁定，既不参与重排，也不会被回写；规划器
只在结果中如实报告它们占用的容量。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .arbitration import (
    HOLD_TTL_SECONDS,
    arbitration_key,
    window_order_key,
)
from .models import ImpactedApplication, ResourceWindow


def parse_ts(value: str) -> datetime:
    """解析服务统一使用的 UTC 时间文本。"""

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Participant:
    """参与一次批量裁决的申请视图。"""

    application_id: str
    sequence: int
    team_id: str
    task_id: str
    units: int
    candidate_windows: tuple[str, ...]
    priority_rank: int
    status: str
    revision: int
    previous_window_id: str | None = None
    decided_at: str | None = None


@dataclass(frozen=True)
class PlanOutcome:
    """一次批量规划的完整结果。"""

    results: tuple[ImpactedApplication, ...]
    holds: dict[str, tuple[str, int]]
    expired: tuple[str, ...]
    rules_version: str
    rules_digest: str

    def for_application(self, application_id: str) -> ImpactedApplication:
        for result in self.results:
            if result.application_id == application_id:
                return result
        raise KeyError(application_id)


def _expand_preferences(candidate_windows: tuple[str, ...],
                        windows: dict[str, ResourceWindow]) -> tuple[list[str], dict[str, str]]:
    """展开候选窗口偏好序列。

    健康窗口按申请方给出的顺序保留；故障窗口在其位置插入按 ordinal 排序
    的替代窗口（故障转移）。返回 (有序窗口编号, 窗口编号 -> 采用原因)。
    """

    ordered: list[str] = []
    why: dict[str, str] = {}
    seen: set[str] = set()

    def add(window_id: str, reason: str) -> None:
        if window_id in seen or window_id not in windows:
            return
        seen.add(window_id)
        ordered.append(window_id)
        why.setdefault(window_id, reason)

    for window_id in candidate_windows:
        window = windows.get(window_id)
        if window is None:
            continue
        if window.status == "failed":
            add(window_id, "candidate_failed_kept_for_audit")
            for alternative_id in window.alternatives:
                add(alternative_id, "failover_alternative")
        else:
            add(window_id, "candidate")
    # 候选与其替代窗口之间仍按冻结的窗口排序规则决出确定顺序。
    ordered.sort(key=lambda wid: window_order_key(windows[wid]))
    return ordered, why


def plan_batch(participants: list[Participant],
               windows: dict[str, ResourceWindow],
               locked_usage: dict[str, int],
               *,
               now: datetime,
               rules_version: str,
               rules_digest: str) -> PlanOutcome:
    """对全部未开始预留重新计算可行方案。

    参数:
        participants: 系统中全部未取消/未过期的申请视图。
        windows: 窗口编号到窗口（含容量、状态、替代关系）的快照。
        locked_usage: 已开始运行在每个窗口上锁定占用的容量。
        now: 当前时间，用于判断已开始窗口与待确认持有是否过期。
    """

    remaining = {window_id: window.capacity_units for window_id, window in windows.items()}
    for window_id, units in locked_usage.items():
        if window_id in remaining:
            remaining[window_id] -= units

    results: list[ImpactedApplication] = []
    holds: dict[str, tuple[str, int]] = {}
    expired: list[str] = []
    immovable: list[Participant] = []
    arbitrable: list[Participant] = []

    for participant in participants:
        # 不可变性边界：只有运行真正开始（running/completed）后才锁定。
        # 窗口墙钟到点但运行未开始的 confirmed 仍属于“未开始的预留”，继续参与重算。
        started = participant.status in ("running", "completed")
        if started:
            immovable.append(participant)
            continue
        if participant.status == "granted" and participant.decided_at is not None:
            age = now - parse_ts(participant.decided_at)
            if age > timedelta(seconds=HOLD_TTL_SECONDS):
                expired.append(participant.application_id)
                results.append(ImpactedApplication(
                    application_id=participant.application_id,
                    team_id=participant.team_id, task_id=participant.task_id,
                    units=participant.units, priority_rank=participant.priority_rank,
                    outcome="expired", window_id=None,
                    reasons=("hold_ttl_exceeded",),
                    previous_window_id=participant.previous_window_id,
                ))
                continue
        arbitrable.append(participant)

    for participant in immovable:
        window_id = participant.previous_window_id
        results.append(ImpactedApplication(
            application_id=participant.application_id,
            team_id=participant.team_id, task_id=participant.task_id,
            units=participant.units, priority_rank=participant.priority_rank,
            outcome="immovable", window_id=window_id,
            reasons=("run_started_cannot_rewrite",),
            previous_window_id=window_id,
        ))
        # immovable 全部是 running/completed，容量已通过 locked_usage 扣减，不重复计数。
        holds[participant.application_id] = (window_id, participant.units)

    arbitrable.sort(key=lambda p: arbitration_key(p.priority_rank, p.sequence, p.application_id))

    for participant in arbitrable:
        preference, why = _expand_preferences(participant.candidate_windows, windows)
        reasons: list[str] = []
        chosen: str | None = None
        if not preference:
            reasons.append("no_known_candidate_window")
        for window_id in preference:
            window = windows[window_id]
            if parse_ts(window.end_at) <= now:
                reasons.append(f"{window_id}:window_ended")
                continue
            if window.status == "failed":
                reasons.append(f"{window_id}:window_failed")
                continue
            if remaining[window_id] < participant.units:
                reasons.append(f"{window_id}:insufficient_capacity")
                continue
            chosen = window_id
            break
        if chosen is None:
            if not reasons:
                reasons.append("no_feasible_window")
            results.append(ImpactedApplication(
                application_id=participant.application_id,
                team_id=participant.team_id, task_id=participant.task_id,
                units=participant.units, priority_rank=participant.priority_rank,
                outcome="waitlisted", window_id=None, reasons=tuple(reasons),
                previous_window_id=participant.previous_window_id,
            ))
            continue
        remaining[chosen] -= participant.units
        holds[participant.application_id] = (chosen, participant.units)
        moved = participant.previous_window_id not in (None, chosen)
        results.append(ImpactedApplication(
            application_id=participant.application_id,
            team_id=participant.team_id, task_id=participant.task_id,
            units=participant.units, priority_rank=participant.priority_rank,
            outcome="moved" if moved else "allocated", window_id=chosen,
            reasons=tuple([why.get(chosen, "candidate")] + (["window_changed"] if moved else [])),
            previous_window_id=participant.previous_window_id,
        ))

    results.sort(key=lambda r: r.application_id)
    return PlanOutcome(
        results=tuple(results),
        holds=holds,
        expired=tuple(sorted(expired)),
        rules_version=rules_version,
        rules_digest=rules_digest,
    )
