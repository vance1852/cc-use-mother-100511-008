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
    """申请算力额度的团队。"""

    team_id: str
    organization_id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class Task:
    """团队名下可被申请引用的任务。"""

    task_id: str
    team_id: str
    name: str
    active: bool
    created_at: str


@dataclass(frozen=True)
class Resource:
    """可被纳入同一资源池互为替代的算力资源。"""

    resource_id: str
    organization_id: str
    pool_id: str
    name: str
    active: bool
    created_at: str


@dataclass(frozen=True)
class Application:
    """一条算力额度申请及其优先级承诺。"""

    application_id: str
    organization_id: str
    team_id: str
    task_id: str
    resource_pool: str
    amount: int
    duration_slots: int
    earliest_slot: int
    deadline_slot: int
    priority: str
    committed: bool
    preferred_resource_id: str | None
    status: str
    sequence: int
    continuation_of: str | None
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Allocation:
    """计划为申请产生的具体额度放置。"""

    allocation_id: str
    application_id: str
    plan_id: str
    resource_id: str
    start_slot: int
    end_slot: int
    amount: int
    status: str
    sealed_at: str | None
    created_at: str


@dataclass(frozen=True)
class PlanSummary:
    """一版冻结裁决计划的摘要。"""

    plan_id: str
    fingerprint: str
    rules_version: str
    current_slot: int
    confirmed: int
    waitlisted: int
    recovered: bool
    created_at: str
