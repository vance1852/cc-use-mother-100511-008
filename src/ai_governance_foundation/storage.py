"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
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
    required_units INTEGER NOT NULL CHECK(required_units >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resource_windows (
    window_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    pool TEXT NOT NULL,
    tier TEXT NOT NULL,
    zone TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    capacity_units INTEGER NOT NULL CHECK(capacity_units >= 0),
    status TEXT NOT NULL CHECK(status IN ('active', 'failed')),
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS window_alternatives (
    window_id TEXT NOT NULL REFERENCES resource_windows(window_id),
    alternative_window_id TEXT NOT NULL REFERENCES resource_windows(window_id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    PRIMARY KEY(window_id, alternative_window_id)
);
CREATE TABLE IF NOT EXISTS priority_commitments (
    commitment_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    rank INTEGER NOT NULL CHECK(rank >= 0),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(team_id, task_id)
);
CREATE TABLE IF NOT EXISTS quota_applications (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL UNIQUE,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    units INTEGER NOT NULL CHECK(units >= 1),
    candidate_windows_json TEXT NOT NULL,
    commitment_id TEXT NOT NULL REFERENCES priority_commitments(commitment_id),
    priority_rank INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'granted', 'confirmed', 'running', 'completed',
        'waitlisted', 'cancelled', 'expired'
    )),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quota_allocations (
    allocation_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES quota_applications(application_id),
    window_id TEXT NOT NULL REFERENCES resource_windows(window_id),
    units INTEGER NOT NULL CHECK(units >= 1),
    state TEXT NOT NULL CHECK(state IN ('held', 'scheduled', 'running', 'completed', 'released')),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    rules_version TEXT NOT NULL,
    rules_digest TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS arbitration_decisions (
    decision_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES quota_applications(application_id),
    revision INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    rules_version TEXT NOT NULL,
    rules_digest TEXT NOT NULL,
    ranking_key TEXT NOT NULL,
    outcome TEXT NOT NULL,
    chosen_window_id TEXT,
    detail_json TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        # 服务使用单个底层连接（ThreadingHTTPServer 下可能被多个线程共享），
        # 用可重入锁串行化事务边界，避免并发写交错破坏 BEGIN/COMMIT 配对。
        self.lock = RLock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。整个事务期间持有连接锁。"""

        with self.lock:
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
