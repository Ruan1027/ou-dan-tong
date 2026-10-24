"""瓯单通 · 单一真相源契约（契约 1 / 2 / 3 / 4）。模块二·1。

所有模块统一 import 本文件；不得在本文件之外自行定义同义结构。
向后兼容原则：只加字段、不改既有字段名/类型。任何改动必须走 PR 全员确认。

本文件归 队友A 主笔；队友D 的 store、队友C 的 query 一律引用此处定义。
契约 3（订单/任务记录）当前字段与 store.py 运行时形状对齐，避免漂移。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 契约 1：聊天输入（模块一 ↔ 模块二）
# ---------------------------------------------------------------------------
@dataclass
class Message:
    group: str          # 群名
    speaker: str        # 发言人
    time: str           # "2026-10-20 14:30"
    text: str           # 原始口语化聊天内容

    def to_dict(self) -> Dict[str, str]:
        return {"group": self.group, "speaker": self.speaker,
                "time": self.time, "text": self.text}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Message":
        return cls(group=d.get("group", ""), speaker=d.get("speaker", ""),
                   time=d.get("time", ""), text=d.get("text", ""))


# ---------------------------------------------------------------------------
# 契约 2：变更片段（extractor 输出，模块二定）
# ---------------------------------------------------------------------------
@dataclass
class ChangeSegment:
    entity_type: str                    # "order" | "task"
    entity_id: Optional[str] = None    # 客户名或单号，允许暂缺
    field: str = ""                    # 数量 / 交期 / 状态 / 单价 ...
    old_value: Optional[Any] = None    # 旧值（新建时 null）
    new_value: Optional[Any] = None    # 新值
    confidence: float = 1.0            # 0.0~1.0
    raw: str = ""                      # 命中该变更的原文片段
    # 是否显式"变更动词"（改成/变更为/调到/更新为）。用于区分「正常更新」与「矛盾断言」。
    change_verb: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "confidence": self.confidence,
            "raw": self.raw,
            "change_verb": self.change_verb,
        }


# ---------------------------------------------------------------------------
# 契约 4：校验结果（validator 输出，模块二定）
# ---------------------------------------------------------------------------
class CheckStatus(str, Enum):
    OK = "ok"              # 正常，可写入
    MISSING = "missing"    # 信息缺失，需追问
    CONFLICT = "conflict"  # 数值矛盾，需人工确认
    ABNORMAL = "abnormal"  # 明显异常，直接拦截


@dataclass
class ValidationResult:
    status: CheckStatus
    message: str = ""              # 给用户的话术
    patch: Optional[Dict[str, Any]] = None  # ok 时非空，可应用变更

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status.value, "message": self.message,
                "patch": self.patch}


# ---------------------------------------------------------------------------
# 契约 3：订单 / 任务记录（与 store.py 运行时形状对齐）
# ---------------------------------------------------------------------------
# OrderRecord 字段：id, customer, product, spec, qty, unit_price,
#                   delivery_date, status, sample_flag, source_excerpt,
#                   change_log, created_at, updated_at, flags
# TaskRecord 字段：id, content(=title), owner, deadline(=due), status, order_id,
#                  created_at, updated_at
#
# 下面用 TypedDict 描述，避免与 store.py 的 dict 用法冲突（store 直接读写 dict）。

ORDER_REQUIRED_FIELDS = ("customer", "product", "qty", "delivery_date")
TASK_REQUIRED_FIELDS = ("content", "owner", "deadline")


# ---------------------------------------------------------------------------
# 公共常量
# ---------------------------------------------------------------------------
# 状态机（模块三·5，单一真相源）：单向流转 + 终态「已取消」。
# 部分交付介于生产中与已交付之间；已取消可从任一非「已交付」活跃态进入、且为终态（不可再流转）。
STATUS_ORDER = ["沟通中", "已确认", "生产中", "部分交付", "已交付", "已取消"]
STATUS_RANK = {s: i for i, s in enumerate(STATUS_ORDER)}
# 终态：进入后不可再流转（BUG-11 修复）
TERMINAL_STATUSES = ("已取消",)

# 变更动词：出现则视为「主动更新」，不与既有值判为冲突
# 涵盖口语增量（加到/加/增到/增至/提到/补到）与口语减量（减到/降到/少到/减成/调回/退回），
# 避免口语增量/减量被误判冲突（BUG-02 / BUG-02' 修复）
# 2026-10-24 应队长需求增补「减少/调整/增加」系动词 + 百分比/倍数量词触发词
# （BUG-14：「发货量减少10%」因缺「减少」被判冲突拦截；「调整为/调整到」同理）
CHANGE_VERBS = ("改", "变更为", "调成", "调到", "更新为", "变为", "改成", "调整到",
               "加到", "加", "增到", "增至", "提到", "补到",
               "减到", "降到", "少到", "减成", "调回", "退回",
               # 口语修正表达：客户改主意后重新给数（如「算了还是 65 个」）
               "算了还是", "算了", "改主意",
               # 2026-10-24 新增：百分比/倍数变更动词（BUG-14）
               "减少", "减少到", "减为", "降低", "降低到",
               "增加", "增加到", "增为", "提升", "提升到",
               "调整", "调整为", "调整一下", "调一下")

# 百分比/倍数量词触发词：抽取层见这些词时，source_excerpt 必须原样保留，
# 并把百分比/倍数换算成绝对数量再下发（1吨×90%=900kg）。
# 2026-10-24 应队长需求：让「依据」精准呈现增减关键词。
RATIO_KEYWORDS = ("百分之", "百分点", "%", "一半", "两倍", "三倍", "几倍", "双倍",
                  "翻倍", "翻番", "打九折", "打八折", "打七折", "打半折")

# 追加语义：客户在**已有订单**基础上「再要一批」，应新开一条追加单挂到原单下，
# 而不是覆盖原单数量。与 CHANGE_VERBS（修改原单）互斥，判定时**先判追加**。
APPEND_INTENTS = ("再走", "再排", "再来", "再订", "再要", "再给", "再发", "再送",
                  "追加", "另加", "再加", "补一批", "加一批", "补一单", "再来一批")

# 模糊量词：不是具体数量，必须按「数量缺失」处理，交由人工确认
# （「一批」不能被猜成 1，否则会建成一张数量为 1 的假单）
VAGUE_QTY_WORDS = ("一批", "若干", "一些", "一点", "几件", "几个", "少量", "大量", "若干批")
# 异常拦截相关的关键词（演示说明用）
ABNORMAL_HINTS = ("负数", "倒挂", "笔误", "格式错误")


# ---------------------------------------------------------------------------
# 沟通中的三个维度（产品口径，2026-10-23 用户定义）
#
# 「沟通中」= 数 / 期 / 价 三个维度**至少一个没定**：
#   未定数 —— 客户没报数量；报了但商家还没确定接不接（现阶段不涉及）
#   未定期 —— 客户没定期限；定了但商家还没确定行不行（现阶段不涉及）
#   未定价 —— 客户没报价；报了但商家还没确定行不行（现阶段不涉及）
# 「已确认」= 数 / 期 / 价 全部确定。
# 取消 / 交付 / 生产 是原文里带明确证据时才进入的后续状态。
# 「已报价」状态已废弃（PR 审查与用户确认）：报价只是把价定了，
#   不构成独立状态，体现为「未定价」消失。
# ---------------------------------------------------------------------------
def missing_dims(order: dict) -> List[str]:
    """返回这张单还没定下来的维度：「未定数 / 未定期 / 未定价」的子集。

    判定只用已落库字段（validator 与 prompts 共用同一口径，防漂移）：
    - 未定数：qty 缺失或非正整数
    - 未定期：有效交期缺失（无单值交期且无合法批次）
    - 未定价：unit_price 缺失或为「待报价」
    """
    qty = (order or {}).get("qty")
    has_qty = isinstance(qty, int) and not isinstance(qty, bool) and qty > 0
    has_date = bool(effective_delivery_date(order))
    up = str((order or {}).get("unit_price") or "").strip()
    has_price = bool(up) and up != "待报价"
    dims = []
    if not has_qty:
        dims.append("未定数")
    if not has_date:
        dims.append("未定期")
    if not has_price:
        dims.append("未定价")
    return dims


def is_fully_confirmed(order: dict) -> bool:
    """数 / 期 / 价 三个维度全部确定（= 沟通中的终点，可进入已确认）。"""
    return not missing_dims(order)


# ---------------------------------------------------------------------------
# 批次交期口径（单一真相源）
#
# 背景（PR #14 审查结论）：批次 `delivery_batches` 是**交付计划**，一张单可能分多批交。
# 判断「这单什么时候该交」「临期/逾期」「交期冲突」时，必须用「最晚批次日」，
# 不能再用裸字段 delivery_date（分批单该字段常为 null，直接比对会得出「待定」）。
#
# 模块三 store / 模块二 validator / 模块二 prompts 一律调用本文件的实现，
# 禁止在各自模块里另写一份（同一语义两套实现必然漂移）。
# ---------------------------------------------------------------------------
def valid_batches(order: dict) -> List[Dict[str, Any]]:
    """取批次列表里的合法条目：qty 为非负整数、date 为可解析的 YYYY-MM-DD。

    非法条目一律跳过（不猜、不修），由模块三 integrity_check 负责报出来。
    """
    raw = (order or {}).get("delivery_batches") or []
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for b in raw:
        if not isinstance(b, dict):
            continue
        q, dt = b.get("qty"), b.get("date")
        if not (isinstance(q, int) and not isinstance(q, bool) and q >= 0):
            continue
        if not isinstance(dt, str):
            continue
        try:
            datetime.date.fromisoformat(dt)
        except ValueError:
            continue
        out.append({"qty": q, "date": dt})
    return out


def effective_delivery_date(order: dict) -> Optional[str]:
    """该单真正该交付的日期。

    - 有合法批次 → 所有批次里最晚的日期（全部批次交完才算交完）
    - 无批次 / 批次全非法 → 回退 order["delivery_date"]
    - 都没有 → None
    """
    batches = valid_batches(order)
    if batches:
        return max(b["date"] for b in batches)
    return (order or {}).get("delivery_date") or None
