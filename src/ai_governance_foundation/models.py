"""定义基础服务在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Actor:
    """表示具有明确角色的后台操作者。"""

    actor_id: str
    display_name: str
    role: str
    organization_id: str
    active: bool


@dataclass(frozen=True)
class Site:
    """表示科研创新机构下的业务场所。"""

    site_id: str
    organization_id: str
    name: str
    timezone_name: str
    version: int


@dataclass(frozen=True)
class DomainRecord:
    """表示已经持久化的领域资料记录。"""

    record_id: str
    site_id: str
    category: str
    external_key: str
    payload: dict[str, Any]
    created_by: str
    created_at: str


@dataclass(frozen=True)
class WriteReceipt:
    """描述一次幂等写入的稳定结果。"""

    request_id: str
    resource_type: str
    resource_id: str
    replayed: bool


@dataclass(frozen=True)
class Team:
    """记录申请算力额度的团队。"""

    team_id: str
    organization_id: str
    name: str


@dataclass(frozen=True)
class Task:
    """记录团队提交给自动化流程运行的任务。"""

    task_id: str
    team_id: str
    name: str
    required_units: int


@dataclass(frozen=True)
class ResourceWindow:
    """描述一段高算力资源时段及其容量与状态。"""

    window_id: str
    organization_id: str
    pool: str
    tier: str
    zone: str
    start_at: str
    end_at: str
    capacity_units: int
    status: str
    version: int
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class PriorityCommitment:
    """记录团队对任务给出的、被冻结进裁决的优先级承诺。"""

    commitment_id: str
    team_id: str
    task_id: str
    rank: int
    note: str


@dataclass(frozen=True)
class QuotaApplication:
    """描述一次额度申请及其裁决状态。"""

    application_id: str
    request_id: str
    team_id: str
    task_id: str
    units: int
    candidate_windows: tuple[str, ...]
    commitment_id: str
    priority_rank: int
    status: str
    revision: int
    sequence: int
    created_at: str


@dataclass(frozen=True)
class QuotaAllocation:
    """描述申请在某资源时段上的实际占用。"""

    allocation_id: str
    application_id: str
    window_id: str
    units: int
    state: str
    revision: int
    rules_version: str
    rules_digest: str
    decided_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class ImpactedApplication:
    """描述一次规划中受影响申请的裁决结果与原因。"""

    application_id: str
    team_id: str
    task_id: str
    units: int
    priority_rank: int
    outcome: str
    window_id: str | None
    reasons: tuple[str, ...]
    previous_window_id: str | None = None
