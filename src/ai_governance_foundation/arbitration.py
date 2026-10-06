"""冻结的配额裁决规则。

规则内容一经发布只能通过提升 RULES_VERSION 替换；规则摘要（digest）会
随每一次裁决与分配持久化，使任何一次历史结果都可以对照当时的规则复核。

裁决总排序（决定同一份算力被谁占用）：
  1. 优先级承诺 rank 数值更小者优先（rank=0 为最高优先）；
  2. rank 相同时，申请进入系统的先后顺序（application sequence）更小者优先；
  3. 仍相同时，application_id 字典序更小者优先，保证跨进程结果唯一。

窗口选择（决定一次占用落在哪个资源时段）：
  对申请给出的候选窗口，按 (开始时间, 层级权重, 窗口编号) 取可容纳的第一个；
  申请方通过候选列表顺序表达改期偏好，时间更早者天然排在前面。
"""

from __future__ import annotations

from .audit import canonical_json, digest

# 规则变更必须显式提升版本号，历史裁决保留旧版本号与摘要。
RULES_VERSION = "quota-arbitration-2026-10-06"

# 资源层级权重：同一起始时间下，专用资源优先于共享资源。
TIER_WEIGHT = {"dedicated": 0, "reserved": 1, "shared": 2}

# 待确认持有的有效期。granted 后超过该时长未确认即过期，释放其持有的额度。
HOLD_TTL_SECONDS = 900


def rules_digest() -> str:
    """返回当前冻结规则文本的稳定摘要。"""

    return digest({
        "version": RULES_VERSION,
        "tier_weight": TIER_WEIGHT,
        "hold_ttl_seconds": HOLD_TTL_SECONDS,
        "ordering": [
            "priority_rank asc",
            "application_sequence asc",
            "application_id asc",
        ],
        "window_ordering": [
            "start_at asc",
            "tier_weight asc",
            "window_id asc",
        ],
    })


def arbitration_key(priority_rank: int, sequence: int, application_id: str) -> tuple[int, int, str]:
    """构造裁决总排序键。该键是冲突时产生唯一结果的唯一依据。"""

    return (priority_rank, sequence, application_id)


def ranking_token(priority_rank: int, sequence: int, application_id: str) -> str:
    """生成可持久化、可复核的排序键文本。"""

    return canonical_json({
        "priority_rank": priority_rank,
        "sequence": sequence,
        "application_id": application_id,
    })


def window_order_key(window) -> tuple[str, int, str]:
    """候选窗口的确定性排序键。"""

    return (window.start_at, TIER_WEIGHT.get(window.tier, len(TIER_WEIGHT)), window.window_id)
