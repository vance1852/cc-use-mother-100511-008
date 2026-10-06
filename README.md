# 人工智能算力配额协调服务

本项目提供人工智能治理协作的服务端基础能力，用于登记主体、任务、权限、风险事件和结构化证据；并在同一套
SQLite 事务、请求幂等与哈希审计链之上，提供**算力配额协调**：记录团队、任务、资源时段与优先级承诺，
在申请、改期、取消和故障转移之间保持一致，冲突时按冻结的裁决规则产生唯一结果。

## 核心语义

- **冻结的裁决规则**（`arbitration.py`，版本 `quota-arbitration-2026-10-06`）：
  冲突时按 `(priority_rank 升序, 申请 sequence 升序, application_id 升序)` 总排序决定额度归属；
  每次裁决都持久化规则版本、规则摘要与排序键，历史结果可复核。
- **两阶段申请**：`granted`（待确认持有，带 TTL）→ `confirmed`（已排期）→ `running` → `completed`；
  无法满足时进入 `waitlisted`，容量释放后按同一规则自动补录。
- **已开始的运行不可回写**：进入 `running`/`completed`（由显式开始运行触发）的占用被锁定，
  取消、改期、优先级调整与故障转移都不会移动它们；窗口墙钟到点但尚未开始的 confirmed 预留
  仍属于未开始预留，继续参与重算。
- **未开始的预留整体重算**：容量变化、窗口故障/恢复、取消或优先级调整后，全部未开始预留通过同一个
  确定性规划器（`planner.plan_batch`）重新计算；故障窗口按其登记的替代窗口做故障转移。
- **恢复续处理**：服务启动（或 `POST /quota/recover`）时重算待确认申请并清理过期持有。
- **可解释**：每个申请返回当前裁决、替代窗口及每个窗口不可用的原因；每个资源时段说明额度被谁占用、
  谁在等待；`POST /impact-analysis` 以不落库的 dry-run 预演某次变更会影响哪些任务，且与实际落库同源。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m ai_governance_foundation.acceptance
PYTHONPATH=src python3 -m ai_governance_foundation.quota_acceptance
```

## HTTP 服务

```bash
PYTHONPATH=src python3 -m ai_governance_foundation.api --database ai_governance.sqlite3 --host 127.0.0.1 --port 8080
```

写操作通过 `X-Actor-Id` 头标识操作者，并要求携带幂等的 `request_id`；重启后 SQLite 中的状态与
审计历史继续保留，启动时会自动执行一次恢复。

### 配额相关接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/teams` `/tasks` | 登记团队、任务（含任务的最低算力要求） |
| POST | `/resource-windows` | 登记资源时段（池、层级、可用区、起止时间、容量、替代窗口） |
| POST | `/resource-windows/update` | 调整容量或标记 `failed`/`active`，随后整体重算 |
| POST | `/priority-commitments` | 冻结/调整团队对任务的优先级 rank |
| POST | `/quota-applications` | 提交申请（候选窗口列表 + 用量），立即裁决 |
| POST | `/quota-confirm` | 确认待确认持有 |
| POST | `/quota-reschedule` | 改期（更新候选窗口并重算，已开始的运行被拒绝） |
| POST | `/quota-cancel` | 取消未开始的预留并释放容量 |
| POST | `/runs/start` `/runs/complete` | 标记运行开始/完成，开始后锁定不可回写 |
| POST | `/quota/recover` | 服务恢复后续处理待确认申请 |
| POST | `/impact-analysis` | dry-run 预演容量/故障变更的影响面 |
| GET | `/quota-application?application_id=` | 单个额度：当前裁决、占用、替代方案与裁决历史 |
| GET | `/window-quota?window_id=` | 时段额度：被谁占用、谁在等待、剩余容量与替代窗口 |
