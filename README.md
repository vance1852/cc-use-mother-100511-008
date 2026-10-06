# 人工智能算力配额协调服务

本项目在人工智能治理协作基础能力（主体、场所、资料登记、角色权限、请求幂等、SQLite
事务与哈希审计链）之上，提供**算力配额协调服务**：记录团队、任务、资源时段与优先级
承诺，在申请、改期、取消与故障转移之间保持一致，并对冲突给出唯一、可离线复核的裁决。

## 设计要点

- **冻结的裁决规则**：`planner.py` 是不依赖时钟与数据库的纯函数内核，规则版本固定为
  `quota-arbitration-2026-10-v1`。相同的资源、容量窗口、已开始运行与申请集合，必然产生
  相同的计划指纹（`plan_versions.fingerprint`）。冲突时排序键为
  `优先级(P0>P1>P2>P3) → 承诺(committed) → 申请序号 → 团队 → 任务 → 申请ID`，
  再按“最早可行槽位 × 首选资源优先”放置。
- **已开始的运行不能被回写**：分配一旦开始即 `sealed`，改期、取消、容量变化、资源故障
  都不能移动它；只能通过**故障转移**登记一条 `continuation_of` 续接申请，原放置保留为
  `interrupted` 历史事实。
- **未开始的预留在资源变化后重新计算**：容量窗口声明、故障上报（容量置零）、改期、取消
  都会触发整盘重算；旧的未开始分配标记为 `superseded` 并保留历史。
- **恢复续处理**：待确认申请持久化在 SQLite 中；服务重启后调用恢复（`POST /recovery`
  或 `QuotaService.run_recovery()`）继续裁决。计划指纹不变时不产生新版本，恢复幂等。
- **可解释**：每个额度视图逐槽位说明容量、剩余、占用者（团队/任务/申请、是否封冻）与
  超额；申请详情给出当前放置与替代方案；变更前可 dry-run 预览受影响任务。

时间按 1 小时栅格对齐，所有时间使用带时区的 ISO 8601（建议 `...Z`）。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/teams` `/tasks` `/resources` | 登记团队、任务、资源（资源按 `pool_id` 互为替代） |
| POST | `/resource-windows` | 声明/覆盖某资源时段容量，触发重算 |
| POST | `/resource-failures` | 上报资源某时段故障（容量置零），触发重算 |
| POST | `/applications` | 提交额度申请（数量、时长、最早/截止时刻、P0–P3、committed） |
| POST | `/applications/reschedule` | 改期未开始申请（保持原排队序号） |
| POST | `/applications/cancel` | 取消未开始申请并释放预留 |
| POST | `/applications/start` | 显式确认运行开始（到点也会在恢复时自动封冻） |
| POST | `/applications/failover` | 中断已开始运行并创建续接申请 |
| POST | `/recovery` | 服务恢复后继续处理待确认申请 |
| POST | `/preview/window-change` `/preview/cancel` | 不落库的变更影响预演 |
| GET | `/applications` `/applications/{id}` | 申请列表 / 详情（当前占用 + 替代方案） |
| GET | `/quota-view?resource_id=&start_at=&end_at=` | 逐槽位额度占用视图 |
| GET | `/resources` `/plans/latest` `/audit-events` | 资源、最新计划、审计链 |

写操作均通过 `X-Actor-Id` 头携带操作者，并要求请求体带幂等 `request_id`；
`operator/admin` 可写，`auditor/reviewer` 只读。

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
```

验收覆盖：同场争用的唯一裁决、改期重算、到点封冻、封冻拒绝回写、故障超额可见、
故障转移续接、恢复幂等与审计链有效。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m ai_governance_foundation.api --database ai_governance.sqlite3 --host 127.0.0.1 --port 8080
```

服务提供健康检查和带操作者身份的业务请求，重启后 SQLite 中的状态与审计历史继续保留。
