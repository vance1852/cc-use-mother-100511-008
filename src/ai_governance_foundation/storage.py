"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
    resource_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    pool_id TEXT NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_windows (
    window_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    start_slot INTEGER NOT NULL,
    end_slot INTEGER NOT NULL,
    capacity INTEGER NOT NULL CHECK(capacity >= 0),
    version INTEGER NOT NULL CHECK(version >= 1),
    superseded INTEGER NOT NULL CHECK(superseded IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    resource_pool TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 1),
    duration_slots INTEGER NOT NULL CHECK(duration_slots >= 1),
    earliest_slot INTEGER NOT NULL,
    deadline_slot INTEGER NOT NULL,
    priority TEXT NOT NULL,
    committed INTEGER NOT NULL CHECK(committed IN (0, 1)),
    preferred_resource_id TEXT,
    status TEXT NOT NULL,
    sequence INTEGER NOT NULL UNIQUE,
    continuation_of TEXT,
    request_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(deadline_slot - earliest_slot >= duration_slots)
);
CREATE TABLE IF NOT EXISTS plan_versions (
    plan_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    current_slot INTEGER NOT NULL,
    decisions_json TEXT NOT NULL,
    confirmed INTEGER NOT NULL,
    waitlisted INTEGER NOT NULL,
    recovered INTEGER NOT NULL CHECK(recovered IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    plan_id TEXT NOT NULL REFERENCES plan_versions(plan_id),
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    start_slot INTEGER NOT NULL,
    end_slot INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 1),
    status TEXT NOT NULL,
    sealed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS allocations_one_active
    ON allocations(application_id) WHERE status != 'superseded';
CREATE INDEX IF NOT EXISTS allocations_resource_time
    ON allocations(resource_id, start_slot, end_slot);
CREATE INDEX IF NOT EXISTS windows_resource_lookup
    ON resource_windows(resource_id, superseded, start_slot, end_slot);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
