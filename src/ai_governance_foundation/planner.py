"""配额协调服务的纯函数规划内核。

裁决规则在本模块冻结（见 ``RULES_VERSION``）：相同的资源、容量窗口、已开始
运行与申请集合，必须产生逐字节稳定的唯一计划。所有涉及时钟与持久化的逻辑
都放在服务层，本模块只接收普通数据对象，便于离线复算与单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .audit import digest
from .errors import ValidationError

#: 裁决规则版本。规则发生变化时必须提升版本，历史计划保留当时的版本号。
RULES_VERSION = "quota-arbitration-2026-10-v1"

#: 时间轴按此时长切槽，所有时刻与时长必须与其对齐。
SLOT_GRID_MINUTES = 60

#: 每个申请附带的候选替代方案上限。
MAX_ALTERNATIVES = 3

#: 数字越小优先级越高。
PRIORITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

GRID_SECONDS = SLOT_GRID_MINUTES * 60


@dataclass(frozen=True)
class Window:
    """资源在半开区间 ``[start_slot, end_slot)`` 内的有效容量。"""

    resource_id: str
    start_slot: int
    end_slot: int
    capacity: int


@dataclass(frozen=True)
class ReservationInput:
    """参与规划的一条未开始申请。"""

    application_id: str
    team_id: str
    task_id: str
    resource_pool: str
    amount: int
    duration_slots: int
    earliest_slot: int
    deadline_slot: int
    priority_rank: int
    committed: bool
    sequence: int
    preferred_resource_id: str | None = None


@dataclass(frozen=True)
class FixedAllocation:
    """已开始运行对未来槽位形成的不可移动占用。"""

    allocation_id: str
    application_id: str
    resource_id: str
    start_slot: int
    end_slot: int
    amount: int


@dataclass(frozen=True)
class Placement:
    """一个申请在某资源上的具体放置结果。"""

    application_id: str
    resource_id: str
    start_slot: int
    end_slot: int
    amount: int


@dataclass(frozen=True)
class Alternative:
    """可供团队选择的替代放置建议。"""

    resource_id: str
    start_slot: int
    end_slot: int
    beyond_deadline: bool


@dataclass(frozen=True)
class Decision:
    """规划器对单条申请的裁决。"""

    application_id: str
    outcome: str  # confirmed | waitlisted
    placement: Placement | None
    alternatives: tuple[Alternative, ...]
    reason: str | None


@dataclass(frozen=True)
class PlanResult:
    """一次完整规划的全部裁决与指纹。"""

    decisions: tuple[Decision, ...]
    fingerprint: str
    confirmed: int
    waitlisted: int


def parse_instant(value: Any, field: str) -> datetime:
    """解析并校验一个对齐时间栅格的 UTC 时刻。"""

    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 8601 时间字符串")
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法的 ISO 8601 时间") from exc
    if moment.tzinfo is None:
        raise ValidationError(f"{field} 必须携带时区")
    moment = moment.astimezone(timezone.utc)
    if moment.microsecond or moment.second or moment.minute % SLOT_GRID_MINUTES:
        raise ValidationError(f"{field} 必须对齐 {SLOT_GRID_MINUTES} 分钟时间栅格")
    return moment


def slot_of(moment: datetime) -> int:
    """把 UTC 时刻换算为从 epoch 起的槽位序号。"""

    return int(moment.astimezone(timezone.utc).timestamp() // GRID_SECONDS)


def slot_to_iso(slot: int) -> str:
    """把槽位序号还原为 ISO 8601 的整点时刻。"""

    moment = datetime.fromtimestamp(slot * GRID_SECONDS, tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def arbitration_key(reservation: ReservationInput) -> tuple[int, int, int, str, str, str]:
    """冻结的裁决排序键，越靠前越优先获得容量。"""

    return (
        reservation.priority_rank,
        0 if reservation.committed else 1,
        reservation.sequence,
        reservation.team_id,
        reservation.task_id,
        reservation.application_id,
    )


def _pool_candidates(pool: str, resources: dict[str, str],
                     preferred: str | None) -> list[str]:
    candidates = sorted(
        resource_id
        for resource_id, resource_pool in resources.items()
        if resource_pool == pool
    )
    if preferred:
        candidates.sort(key=lambda resource_id: (resource_id != preferred, resource_id))
    return candidates


def _fits(resource_id: str, start_slot: int, duration: int, amount: int,
          caps: dict[str, dict[int, int]], used: dict[tuple[str, int], int]) -> bool:
    resource_caps = caps.get(resource_id)
    if not resource_caps:
        return False
    for offset in range(duration):
        slot = start_slot + offset
        capacity = resource_caps.get(slot)
        if capacity is None or used.get((resource_id, slot), 0) + amount > capacity:
            return False
    return True


def _relaxed_alternatives(reservation: ReservationInput, candidates: list[str],
                          start_lo: int, horizon_end: int,
                          caps: dict[str, dict[int, int]],
                          relaxed_used: dict[tuple[str, int], int]) -> tuple[Alternative, ...]:
    """只考虑已开始运行与资源容量时，给出截止期之后的最早可行建议。"""

    alternatives: list[Alternative] = []
    for start_slot in range(start_lo, horizon_end - reservation.duration_slots + 1):
        if start_slot <= reservation.deadline_slot - reservation.duration_slots:
            continue
        for resource_id in candidates:
            if not _fits(resource_id, start_slot, reservation.duration_slots,
                         reservation.amount, caps, relaxed_used):
                continue
            alternatives.append(Alternative(
                resource_id=resource_id,
                start_slot=start_slot,
                end_slot=start_slot + reservation.duration_slots,
                beyond_deadline=True,
            ))
            if len(alternatives) >= MAX_ALTERNATIVES:
                return tuple(alternatives)
    return tuple(alternatives)


def plan_allocations(*, resources: dict[str, str], windows: list[Window],
                     fixed: list[FixedAllocation],
                     reservations: list[ReservationInput],
                     current_slot: int) -> PlanResult:
    """依据冻结规则对全部未开始申请给出唯一可行计划。

    算法是确定的：按裁决键排序后依次做“最早可行槽位 × 首选资源优先”的
    贪心放置；已开始运行作为固定负载，任何申请都不能挤占。
    """

    caps: dict[str, dict[int, int]] = {}
    horizon_end = current_slot
    for window in windows:
        resource_caps = caps.setdefault(window.resource_id, {})
        for slot in range(window.start_slot, window.end_slot):
            # 同一资源的窗口由服务层保证互不重叠。
            resource_caps[slot] = window.capacity
        horizon_end = max(horizon_end, window.end_slot)

    used: dict[tuple[str, int], int] = _fixed_only_used(fixed, current_slot)

    decisions: list[Decision] = []
    ordered = sorted(reservations, key=arbitration_key)
    for reservation in ordered:
        candidates = _pool_candidates(reservation.resource_pool, resources,
                                      reservation.preferred_resource_id)
        start_lo = max(reservation.earliest_slot, current_slot)
        start_hi = reservation.deadline_slot - reservation.duration_slots
        chosen: Placement | None = None
        alternatives: list[Alternative] = []
        if candidates and start_hi >= start_lo:
            for start_slot in range(start_lo, start_hi + 1):
                for resource_id in candidates:
                    if not _fits(resource_id, start_slot, reservation.duration_slots,
                                 reservation.amount, caps, used):
                        continue
                    if chosen is None:
                        chosen = Placement(
                            application_id=reservation.application_id,
                            resource_id=resource_id,
                            start_slot=start_slot,
                            end_slot=start_slot + reservation.duration_slots,
                            amount=reservation.amount,
                        )
                        for offset in range(reservation.duration_slots):
                            key = (resource_id, start_slot + offset)
                            used[key] = used.get(key, 0) + reservation.amount
                    elif len(alternatives) < MAX_ALTERNATIVES:
                        alternatives.append(Alternative(
                            resource_id=resource_id,
                            start_slot=start_slot,
                            end_slot=start_slot + reservation.duration_slots,
                            beyond_deadline=False,
                        ))
        if chosen is not None:
            outcome = Decision(reservation.application_id, "confirmed", chosen,
                               tuple(alternatives[:MAX_ALTERNATIVES]), None)
        else:
            if not candidates:
                reason = "resource_pool_unavailable"
            else:
                reason = "no_capacity_before_deadline"
            # 替代方案只避让已开始运行，不避让其它未开始申请，因此它表达的是
            # “资源本身可用的最早时点”，不会与主裁决相互污染。
            relaxed = _relaxed_alternatives(
                reservation, candidates, start_lo, horizon_end, caps,
                _fixed_only_used(fixed, current_slot),
            )
            outcome = Decision(reservation.application_id, "waitlisted", None,
                               relaxed, reason)
        decisions.append(outcome)

    fingerprint = _fingerprint(resources, windows, fixed, reservations,
                               current_slot, decisions)
    confirmed = sum(1 for decision in decisions if decision.outcome == "confirmed")
    return PlanResult(tuple(decisions), fingerprint, confirmed,
                      len(decisions) - confirmed)


def _fixed_only_used(fixed: list[FixedAllocation], current_slot: int) -> dict[tuple[str, int], int]:
    relaxed_used: dict[tuple[str, int], int] = {}
    for allocation in fixed:
        start = max(allocation.start_slot, current_slot)
        for slot in range(start, allocation.end_slot):
            key = (allocation.resource_id, slot)
            relaxed_used[key] = relaxed_used.get(key, 0) + allocation.amount
    return relaxed_used


def _fingerprint(resources: dict[str, str], windows: list[Window],
                 fixed: list[FixedAllocation],
                 reservations: list[ReservationInput], current_slot: int,
                 decisions: list[Decision]) -> str:
    basis: dict[str, Any] = {
        "rules_version": RULES_VERSION,
        "current_slot": current_slot,
        "resources": sorted(resources.items()),
        "windows": sorted((w.resource_id, w.start_slot, w.end_slot, w.capacity)
                          for w in windows),
        "fixed": sorted((a.allocation_id, a.resource_id, a.start_slot,
                         a.end_slot, a.amount) for a in fixed),
        "reservations": sorted(
            (r.application_id, r.resource_pool, r.amount, r.duration_slots,
             r.earliest_slot, r.deadline_slot, r.priority_rank, r.committed,
             r.sequence, r.preferred_resource_id or "")
            for r in reservations
        ),
        "decisions": sorted(
            (d.application_id, d.outcome,
             (d.placement.resource_id, d.placement.start_slot,
              d.placement.end_slot, d.placement.amount) if d.placement else None)
            for d in decisions
        ),
    }
    return digest(basis)
