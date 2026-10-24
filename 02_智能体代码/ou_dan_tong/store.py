"""本地 JSON 存储，对应订单 / 任务两张台账。模块三·状态维护（队友D）。

对应《开发任务清单》模块三 1~5：
- 三·1 台账存储模型：orders.json / tasks.json，最新状态 + change_log 历史变更（契约 3）
- 三·2 最新状态解析：apply_latest 后值覆盖前值；带冲突/异常锁的字段不自动覆盖，
      只能经 confirm_update 人工确认后写入；状态流转受状态机约束
- 三·3 持久化与读写：原子写盘、损坏文件自动备份兜底、get/update/query 增改查
- 三·4 一致性校验与修复：integrity_check + repair（重复ID/缺失字段/非法状态/孤儿引用）
- 三·5 进度状态机：基于 contracts.STATUS_ORDER 单向流转，禁止状态回退（force 需人工显式调用）
      业务口径 2026-10-24：取消「已报价」，沟通中细分 未定数/未定期/未定价（missing_info_tags），
      「已确认」= 数量/交期/单价 三项齐全（integrity_check 守卫）

铁律：契约字段以 contracts.py 为准，本文件只加实现、不改契约；既有方法签名
（add_order / update_order / find_active / query_* 等）保持向下兼容，模块二/四/五
无需改动即可继续使用。
"""
import json
import os
import datetime
import copy

from config import DATA_DIR
from contracts import STATUS_ORDER, STATUS_RANK, TERMINAL_STATUSES

# 任务清单口径（待确认→生产中→已发货→已完成）与契约口径（沟通中→…→已交付）的别名映射。
# 契约 STATUS_ORDER 是单一真相源，别名仅做归一化，不新增状态。
#
# 业务口径变更（2026-10-24，队友D 模块三）：取消「已报价」状态。
# 新流程：沟通中（细分：未定数/未定期/未定价）→ 已确认 → 生产中 → 部分交付 → 已交付 / 已取消。
# - 「已报价」历史数据自动归一化为「沟通中」，不再产生新的已报价单；
# - 「未定数/未定期/未定价」不是独立状态，是沟通中的缺失细分（见 missing_info_tags），
#   可多项并存（既没报数又没定期 → ["未定数","未定期"]），单状态字段表达不了组合缺失；
# - 契约层（contracts.py STATUS_ORDER 移除已报价）需队长/队友A 走 PR 全员确认后同步，
#   同步前 store 层先收口（next_statuses 视图过滤），契约改后过滤自动失效。
STATUS_ALIASES = {
    "待确认": "已确认",
    "已发货": "已交付",
    "已完成": "已交付",
    "已报价": "沟通中",
}

# 沟通中的三种信息缺失细分（业务口径 2026-10-24）
INFO_MISSING_TAGS = ("未定数", "未定期", "未定价")

# 旧口径中已存在、新口径已取消的状态（store 层视图统一隐藏）
RETIRED_STATUSES = ("已报价",)


def missing_info_tags(order: dict):
    """沟通中订单的信息缺失细分（模块级纯函数，供展示/追问/简报复用）。

    - 未定数：客户没报数（信息缺失）；「客户报了但商家还没确定接不接」现阶段不涉及
    - 未定期：客户没定期限（信息缺失）；商家未确认交期同理现阶段不涉及
    - 未定价：客户没报价（单价缺失或「待报价」占位）
    返回标签列表，可同时含多项；三项齐全返回 []（对应「已确认」的前提）。
    """
    tags = []
    qty = order.get("qty")
    if not (isinstance(qty, int) and not isinstance(qty, bool) and qty > 0):
        tags.append("未定数")
    if not str(order.get("delivery_date") or "").strip():
        tags.append("未定期")
    up = str(order.get("unit_price") or "").strip()
    if not up or up == "待报价":
        tags.append("未定价")
    return tags

# 冲突/异常人工确认前，禁止自动覆盖的字段（模块三·2）
LOCKABLE_FIELDS = ("qty", "unit_price", "delivery_date", "status")


def normalize_status(s):
    """状态归一化：契约合法值原样返回；别名映射到契约值；未知值返回 None（交由 integrity_check 标记）。

    已废止状态（RETIRED_STATUSES，如「已报价」）即使契约尚未移除，也走别名归一化——
    store 层先收口，保证新口径下不再产生废止状态的单。
    """
    s = (s or "").strip()
    if not s:
        return None
    if s in RETIRED_STATUSES:
        return STATUS_ALIASES.get(s)
    if s in STATUS_RANK:
        return s
    return STATUS_ALIASES.get(s)


PLACEHOLDER_STATUS = "待补充"  # 模块二缺失分支登记的占位单状态（非契约流程状态）


def can_transition(old_status, new_status):
    """状态机判定：仅允许沿 STATUS_ORDER 向前流转（同值视为无操作，返回 True）。

    占位单「待补充」是缺失信息时的临时态，补全后可进入任意合法状态。
    BUG-11 修复：新增「已取消」终态——可从任一非「已交付」活跃态进入，
    进入后不可再流转；「已交付」后不可取消。
    """
    o, n = normalize_status(old_status), normalize_status(new_status)
    if n is None:
        return False
    if (old_status or "").strip() == PLACEHOLDER_STATUS:
        return True
    if o is None:
        return False
    # 终态（已取消）进入后不可再流转
    if o in TERMINAL_STATUSES:
        return o == n
    # 已取消可从任一活跃态进入，但已交付后不可取消
    if n == "已取消":
        return o != "已交付"
    return STATUS_RANK[n] >= STATUS_RANK[o]


def next_statuses(status):
    """给定状态的合法后续状态列表（不含自身）。

    新口径下视图统一隐藏「已报价」；契约层移除已报价后此过滤自动变空操作。
    """
    cur = normalize_status(status)
    if cur is None:
        return [s for s in STATUS_ORDER if s not in RETIRED_STATUSES]
    return [s for s in STATUS_ORDER[STATUS_RANK[cur] + 1:]
            if s not in RETIRED_STATUSES]


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _longest_common_substring(a: str, b: str) -> str:
    """返回 a、b 的最长公共子串（用于口语化简称/语序颠倒的模糊匹配）。
    客户名/产品名都很短，O(n*m) DP 足够。"""
    if not a or not b:
        return ""
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    best, bi = 0, 0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
                if dp[i][j] > best:
                    best, bi = dp[i][j], i
    return a[bi - best:bi] if best else ""


def _fuzzy_eq(a: str, b: str) -> bool:
    """口语化等价：互为子串，或共享 ≥4 字核心片段。

    覆盖三类真实口语变形（关联到在途单时不再误判缺失/冲突）：
    - 简称： 「王工」↔「王工（XX电气）」
    - 后缀包裹：「NMX-60 那批」↔「NMX-60 断路器外壳」
    - 语序颠倒：「XX电气的王工」↔「王工（XX电气）」
    至少 2 字，防单字误命中。
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    short = a if len(a) <= len(b) else b
    long = b if short is a else a
    # 1) 互为子串（原逻辑，覆盖简称场景）
    if len(short) >= 2 and short in long:
        return True
    # 2) 共享 ≥4 字核心片段（覆盖口语包裹 / 语序颠倒）
    # 快筛：两串无任何公共字符必然无公共子串，直接短路，省掉 O(n*m) DP
    if not (set(a) & set(b)):
        return False
    common = _longest_common_substring(a, b)
    if len(common) >= 4:
        return True
    return False


# ----------------------------------------------------------------------
# D-1 批次交期有效性解析（分批交期语义的单一解释口径）
# D-3：提为模块级纯函数（不依赖 Store 实例），模块二 validator / 模块五查询
#      简报可直接 from store import effective_delivery_date, _valid_batches 复用
# ----------------------------------------------------------------------
def _parse_batch(b):
    """单条批次合法性校验。返回 (规范化条目, None) 或 (None, 原因)。

    D-5：date 用 datetime.date.fromisoformat 真实解析（能拦住 2026-13-01
    这类「格式像但日历上不存在」的日期），而非只查字符串形状。
    """
    if not isinstance(b, dict):
        return None, "条目不是对象"
    q, dt = b.get("qty"), b.get("date")
    qi = q if isinstance(q, int) and not isinstance(q, bool) else None
    if qi is None or qi < 0:
        return None, f"qty「{q}」不是非负整数"
    if not isinstance(dt, str) or len(dt) != 10:
        return None, f"date「{dt}」不是 YYYY-MM-DD 字符串"
    try:
        datetime.date.fromisoformat(dt)
    except ValueError:
        return None, f"date「{dt}」不是真实存在的日期"
    return {"qty": qi, "date": dt}, None


def _valid_batches(order: dict):
    """取批次列表里的合法条目（qty 为非负整数、date 为真实存在的 YYYY-MM-DD）。"""
    batches = order.get("delivery_batches") or []
    out = []
    if isinstance(batches, list):
        for b in batches:
            clean, _ = _parse_batch(b)
            if clean is not None:
                out.append(clean)
    return out


def _invalid_batches(order: dict):
    """D-4 诊断口径：返回被跳过的非法批次条目 [(索引, 原始条目, 原因)]。"""
    batches = order.get("delivery_batches") or []
    bad = []
    if isinstance(batches, list):
        for i, b in enumerate(batches):
            _, reason = _parse_batch(b)
            if reason:
                bad.append((i, b, reason))
    return bad


def effective_delivery_date(order: dict):
    """返回该单真正该交付的日期（D-3 模块级纯函数，模块二冲突比对请用此口径）。

    - 有 delivery_batches：返回所有批次里最晚的日期（全部批次交完才算交完）
    - 无批次（或批次全部非法）：回退 order["delivery_date"]
    - 都没有：返回 None
    """
    batches = _valid_batches(order)
    if batches:
        return max(b["date"] for b in batches)
    return order.get("delivery_date") or None


class Store:
    def __init__(self, data_dir: str = DATA_DIR):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.orders_file = os.path.join(data_dir, "orders.json")
        self.tasks_file = os.path.join(data_dir, "tasks.json")
        # 模块三·3：损坏文件兜底记录（load 时备份并重建，供运维查看）
        self.load_warnings = []
        self.orders = self._load(self.orders_file)
        self.tasks = self._load(self.tasks_file)
        self._normalize_all()

    # ------------------------------------------------------------------
    # 模块三·3 持久化：容错加载 + 原子写盘
    # ------------------------------------------------------------------
    def _load(self, path):
        if not os.path.exists(path):
            return []
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            # 损坏文件不静默丢弃：备份留证后重建空台账
            backup = f"{path}.corrupt-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"
            try:
                os.replace(path, backup)
                self.load_warnings.append(f"{os.path.basename(path)} 已损坏，已备份至 {backup}：{e}")
            except OSError:
                self.load_warnings.append(f"{os.path.basename(path)} 已损坏且备份失败：{e}")
            return []

    def _save_one(self, path, data):
        """原子写盘：先写临时文件再 os.replace，避免写一半断电/并发导致台账损坏。"""
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def save(self):
        self._save_one(self.orders_file, self.orders)
        self._save_one(self.tasks_file, self.tasks)

    def reset(self):
        self.orders, self.tasks = [], []
        self.save()

    # ------------------------------------------------------------------
    # 模块三·1 台账存储模型：记录归一化（最新状态 + 历史变更）
    # ------------------------------------------------------------------
    @staticmethod
    def _ensure_history(rec):
        """change_log（契约 3）与 changes（既有运行时字段）互为镜像，指向同一列表。"""
        hist = rec.get("change_log") or rec.get("changes") or []
        rec["change_log"] = hist
        rec["changes"] = hist  # 同一列表对象，两键始终一致
        return rec

    @classmethod
    def _norm_order(cls, o):
        o.setdefault("order_id", "")
        o.setdefault("customer", "")
        o.setdefault("product", "")
        o.setdefault("spec", "")
        o.setdefault("qty", None)
        o.setdefault("unit_price", "待报价")
        o.setdefault("delivery_date", None)
        raw_status = o.get("status")
        st = normalize_status(raw_status)
        if raw_status == PLACEHOLDER_STATUS:
            o["status"] = PLACEHOLDER_STATUS  # 占位单保留，待补全后走状态机
        elif st is None:
            o["status"] = "沟通中"
            if raw_status:
                o.setdefault("flags", {}).setdefault("invalid_status", raw_status)
        else:
            o["status"] = st
        o.setdefault("sample_flag", "无")
        o.setdefault("source_excerpt", "")
        # BUG-11 修复：部分交付 / 分批交期 支撑字段（仅加不改，缺省 None）
        o.setdefault("delivered_qty", None)
        o.setdefault("remain_qty", None)
        o.setdefault("delivery_batches", None)
        o.setdefault("created_at", o.get("updated_at") or _now())
        o.setdefault("updated_at", _now())
        o.setdefault("flags", {})
        cls._ensure_history(o)
        return o

    @classmethod
    def _norm_task(cls, t):
        t.setdefault("task_id", "")
        t.setdefault("content", "")
        t.setdefault("owner", "")
        t.setdefault("deadline", None)
        t.setdefault("status", "待办")
        t.setdefault("order_id", None)
        t.setdefault("created_at", t.get("updated_at") or _now())
        t.setdefault("updated_at", _now())
        cls._ensure_history(t)
        return t

    def _normalize_all(self):
        for o in self.orders:
            self._norm_order(o)
        for t in self.tasks:
            self._norm_task(t)

    # ------------------------------------------------------------------
    # 编号 / 查找（向下兼容）
    # ------------------------------------------------------------------
    def next_order_id(self, date: str = None):
        date = date or datetime.date.today().strftime("%Y%m%d")
        n = sum(1 for o in self.orders if date in o.get("order_id", "")) + 1
        return f"ORD-{date}-{n:02d}"

    def next_task_id(self, date: str = None):
        date = date or datetime.date.today().strftime("%Y%m%d")
        n = sum(1 for t in self.tasks if date in t.get("task_id", "")) + 1
        return f"TSK-{date}-{n:02d}"

    def get_order(self, order_id: str):
        """按单号精确取单（模块三·3 查）。"""
        return next((o for o in self.orders if o.get("order_id") == order_id), None)

    def get_task(self, task_id: str):
        return next((t for t in self.tasks if t.get("task_id") == task_id), None)

    def find_active(self, customer: str, product: str):
        # 主键为空时不做任何匹配：否则多条「缺客户」占位单会互相撞车，
        # 后一条缺客户消息会把前一条占位单当成「同一单」去更新（数据串台）。
        if not (customer or "").strip() or not (product or "").strip():
            return None
        # 1) 精确匹配（mock / 正式名称场景，行为与原版完全一致）
        for o in self.orders:
            if o.get("customer") == customer and o.get("product") == product \
                    and o.get("status") not in ("已交付",):
                return o
        # 2) 简称模糊匹配（真实模式口语常只说简称：
        #    「王工」↔「王工（XX电气）」、「NMX-60」↔「NMX-60 断路器外壳」）
        if customer and product:
            for o in self.orders:
                if o.get("status") in ("已交付",):
                    continue
                oc, op = (o.get("customer") or "").strip(), (o.get("product") or "").strip()
                if _fuzzy_eq(oc, customer) and _fuzzy_eq(op, product):
                    return o
        return None

    # ------------------------------------------------------------------
    # D-1 批次交期有效性解析（分批交期语义的单一解释口径）
    # ------------------------------------------------------------------
    def effective_delivery_date(self, order: dict):
        """委托模块级纯函数（D-3）：保留方法形式，向下兼容既有调用点。"""
        return effective_delivery_date(order)

    def batch_summary(self, order: dict, as_of: str = None) -> dict:
        """批次汇总（D-2 语义拆分：区分「计划排满」与「真的交完了」）。

        - planned：批次计划总量之和；shipped：日期已到（<= as_of）的批次量之和
        - plan_complete：计划量排满 qty（只看计划，与时间无关）
        - done：计划排满 且 最晚批次日期已到 —— 修复旧版「未来批次也算交完」
        - last_date / next_date：最晚批次日 / 下一个未到期批次日（无则 None）
        - as_of：统计基准日，默认今天（测试与回放场景可显式传入）
        """
        today = as_of or datetime.date.today().strftime("%Y-%m-%d")
        qty = order.get("qty")
        total = qty if isinstance(qty, int) and not isinstance(qty, bool) else 0
        batches = _valid_batches(order)
        if not batches:
            return {"has_batches": False, "planned": 0, "shipped": 0, "total": total,
                    "plan_complete": False, "done": False,
                    "last_date": None, "next_date": None}
        planned = sum(b["qty"] for b in batches)
        dates = [b["date"] for b in batches]
        last_date = max(dates)
        plan_complete = bool(total and planned >= total)
        return {"has_batches": True,
                "planned": planned,
                "shipped": sum(b["qty"] for b in batches if b["date"] <= today),
                "total": total,
                "plan_complete": plan_complete,
                "done": bool(plan_complete and last_date <= today),
                "last_date": last_date,
                "next_date": min((d for d in dates if d > today), default=None)}

    def _batch_consistency_msgs(self, order: dict):
        """D-2 批次一致性检查——integrity_check 与 repair 共用的唯一口径（防两处漂移）。

        返回 (level, 消息) 列表；空列表 = 批次数据自洽。
        """
        raw = order.get("delivery_batches")
        if not (isinstance(raw, list) and raw):
            if raw not in (None, []):
                return [("error", f"delivery_batches「{raw}」不是批次列表")]
            return []
        msgs = []
        # D-4：先报告被跳过的非法批次条目（可诊断，不再静默吞掉）
        for i, raw_b, reason in _invalid_batches(order):
            msgs.append(("warn", f"delivery_batches 第 {i + 1} 条（{raw_b}）非法已跳过：{reason}"))
        dq, rq = order.get("delivered_qty"), order.get("remain_qty")
        q = order.get("qty")
        batch_sum = sum(b["qty"] for b in _valid_batches(order))
        # 规则1：批次数量之和必须等于 delivered_qty
        # D-6：仅「缺 delivered_qty」降为 warn（可能是分批起步还没人记账），
        #       值对不上仍是 error（数据本身矛盾）
        if dq is None:
            msgs.append(("warn", f"有 delivery_batches（合计 {batch_sum}）但缺 delivered_qty，批次总量未记账"))
        elif batch_sum != dq:
            msgs.append(("error", f"批次数量之和 {batch_sum} ≠ delivered_qty {dq}"))
        # 规则2：delivered_qty + remain_qty 必须等于 qty
        if dq is not None and rq is not None \
                and isinstance(q, int) and not isinstance(q, bool) and dq + rq != q:
            msgs.append(("error", f"delivered_qty {dq} + remain_qty {rq} ≠ qty {q}"))
        elif dq is not None and rq is None \
                and isinstance(q, int) and not isinstance(q, bool):
            msgs.append(("warn", "有 delivered_qty 但缺 remain_qty，无法核对 delivered+remain=qty"))
        # 规则3：待补充占位单不应有批次（未确认的东西不可能有交期）
        if order.get("status") == "待补充":
            msgs.append(("error", "待补充占位单不应有 delivery_batches（信息未确认不可能定交期）"))
        return msgs

    # ------------------------------------------------------------------
    # 模块三·2 最新状态解析（后值覆盖前值；冲突/异常不自动覆盖）
    # ------------------------------------------------------------------
    def apply_latest(self, order_id: str, patch: dict, source: str = "",
                     speaker: str = "", time: str = None, force: bool = False,
                     record_each: bool = True):
        """后值覆盖前值：仅覆盖本次有值字段（None/空串不覆盖）。

        保护规则（模块三·2）：
        1. flags.locked_fields 中的字段（冲突/异常待人工确认）不自动覆盖，
           除非 force=True（人工确认路径 confirm_update）；
        2. 状态回退（沿 STATUS_ORDER 向后）一律拒绝，除非 force=True；
        3. 每个被覆盖字段写入 change_log 留痕（record_each=False 时由调用方
           自行写入聚合变更，避免双份留痕）。

        返回 {"updated": [字段...], "blocked": {字段: 原因}, "order": 记录|None}。
        """
        rec = self.get_order(order_id)
        if rec is None:
            return {"updated": [], "blocked": {}, "order": None}
        time = time or _now()
        locked = set(rec.get("flags", {}).get("locked_fields") or [])
        updated, blocked, changes = [], {}, []
        for f, new in (patch or {}).items():
            if new is None or str(new).strip() == "":
                continue  # 后值覆盖前值：无值不覆盖
            old = rec.get(f)
            if str(new) == str(old):
                continue
            if f == "status":
                new_st = normalize_status(new)
                if new_st is None:
                    blocked[f] = f"非法状态值「{new}」"
                    continue
                if not can_transition(old, new_st) and not force:
                    blocked[f] = f"状态不允许从「{old}」回退到「{new_st}」"
                    continue
                new = new_st
            elif f in locked and not force:
                blocked[f] = "该字段存在冲突/异常待人工确认，已锁定"
                continue
            changes.append({"field": f, "old": old, "new": new})
            updated.append(f)
        for c in changes:
            rec[c["field"]] = c["new"]
            if record_each:
                self._ensure_history(rec)["change_log"].append({
                    "time": time,
                    "speaker": speaker,
                    "field": c["field"],
                    "old": c["old"],
                    "new": c["new"],
                    "raw": source,
                })
        if updated:
            rec["updated_at"] = time
        self.save()
        return {"updated": updated, "blocked": blocked, "order": rec}

    def update_order(self, order_id: str, patch: dict, change: dict = None,
                     force: bool = False):
        """向下兼容入口：模块二 agent 调用的 update_order 语义不变。

        change 为模块二 apply_change 产出的留痕 dict（time/field/old/new/source），
        原样追加进 change_log（此时 apply_latest 不再逐字段留痕，避免双份）；
        patch 走 apply_latest 的后值覆盖 + 保护规则。
        """
        res = self.apply_latest(order_id, patch or {}, force=force,
                                record_each=(change is None))
        rec = res["order"]
        if rec is not None and change:
            hist = self._ensure_history(rec)["change_log"]
            entry = dict(change)
            hist.append(entry)
            rec["updated_at"] = entry.get("time") or _now()
            self.save()
        return rec

    def confirm_update(self, order_id: str, patch: dict, confirmed_by: str = "",
                       note: str = "", time: str = None):
        """人工确认后的强制覆盖（冲突/异常字段的唯一合法写入入口）。

        覆盖指定字段、清除对应字段锁与 conflict/abnormal 标记，并留痕来源=人工确认。
        """
        rec = self.get_order(order_id)
        if rec is None:
            return {"updated": [], "blocked": {}, "order": None}
        locked = set(rec.get("flags", {}).get("locked_fields") or [])
        flags = rec.get("flags", {})
        res = self.apply_latest(order_id, patch or {}, source=f"人工确认({confirmed_by}){('：' + note) if note else ''}",
                                speaker=confirmed_by, time=time, force=True)
        for f in res["updated"]:
            locked.discard(f)
        if locked:
            flags["locked_fields"] = sorted(locked)
        else:
            flags.pop("locked_fields", None)
        if not any(k in flags for k in ("conflict", "abnormal")):
            flags.pop("conflict", None)
            flags.pop("abnormal", None)
        if rec.get("flags") is not flags:
            rec["flags"] = flags
        self.save()
        return res

    def lock_fields(self, order_id: str, fields, reason: str = ""):
        """冲突/异常发生时锁定字段（配合模块二的 conflict/abnormal 判定使用）。"""
        rec = self.get_order(order_id)
        if rec is None:
            return None
        flags = rec.setdefault("flags", {})
        cur = set(flags.get("locked_fields") or [])
        cur |= {f for f in fields if f in LOCKABLE_FIELDS}
        flags["locked_fields"] = sorted(cur)
        if reason:
            flags["lock_reason"] = reason
        self.save()
        return rec

    def set_order_status(self, order_id: str, new_status: str, speaker: str = "",
                         raw: str = "", time: str = None, force: bool = False):
        """模块三·5 状态机推进入口：非法流转拒绝并说明原因。"""
        new_st = normalize_status(new_status)
        if new_st is None:
            return {"ok": False, "reason": f"非法状态值「{new_status}」", "order": None}
        res = self.apply_latest(order_id, {"status": new_st}, source=raw,
                                speaker=speaker, time=time, force=force)
        if res["order"] is None:
            return {"ok": False, "reason": "订单不存在", "order": None}
        if "status" in res["blocked"]:
            return {"ok": False, "reason": res["blocked"]["status"], "order": res["order"]}
        return {"ok": True, "reason": "", "order": res["order"]}

    # ------------------------------------------------------------------
    # 增改查（向下兼容 + 补全任务侧 CRUD）
    # ------------------------------------------------------------------
    def add_order(self, order: dict):
        self._norm_order(order)
        self.orders.append(order)
        self.save()
        return order

    def add_task(self, task: dict):
        self._norm_task(task)
        self.tasks.append(task)
        self.save()
        return task

    def update_task(self, task_id: str, patch: dict, speaker: str = "",
                    time: str = None):
        """任务增改查补全：状态限 待办/进行中/已完成。"""
        t = self.get_task(task_id)
        if t is None:
            return None
        time = time or _now()
        for k, v in (patch or {}).items():
            if v is None:
                continue
            if k == "status" and v not in ("待办", "进行中", "已完成"):
                continue
            if str(v) == str(t.get(k)):
                continue
            self._ensure_history(t)["change_log"].append(
                {"time": time, "speaker": speaker, "field": k,
                 "old": t.get(k), "new": v, "raw": ""})
            t[k] = v
        t["updated_at"] = time
        self.save()
        return t

    def query_orders(self, customer=None, status=None, due_before=None, created_on=None,
                     order_id=None):
        res = self.orders
        if order_id:
            res = [o for o in res if o.get("order_id") == order_id]
        if customer:
            res = [o for o in res if customer in o.get("customer", "")]
        if status:
            res = [o for o in res if o.get("status") == status]
        if due_before:
            res = [o for o in res if o.get("delivery_date")
                   and o["delivery_date"] <= due_before and o.get("status") != "已交付"]
        if created_on:
            res = [o for o in res if o.get("created_at", "").startswith(created_on)]
        return res

    def query_tasks(self, status: str = "待办", overdue_before=None, all_status: bool = False):
        res = self.tasks if all_status else [t for t in self.tasks if t.get("status") == status]
        if overdue_before:
            res = [t for t in res if t.get("deadline") and t["deadline"] < overdue_before]
        return res

    # ------------------------------------------------------------------
    # 模块三·4 一致性校验与修复
    # ------------------------------------------------------------------
    def integrity_check(self):
        """扫描台账一致性问题，返回问题清单（不改动数据）。

        检查项：重复ID、必填字段缺失、状态非法、数量非法（负数/非整数）、
        交期格式、任务孤儿引用、变更记录缺时间；
        D-2 批次规则：①批次数量之和=delivered_qty ②delivered_qty+remain_qty=qty
        ③待补充占位单不得带 delivery_batches。
        """
        issues = []

        def _issue(level, entity, eid, msg):
            issues.append({"level": level, "entity": entity, "id": eid, "issue": msg})

        seen_o, seen_t = {}, {}
        for o in self.orders:
            oid = o.get("order_id") or "(无单号)"
            if oid in seen_o:
                _issue("error", "order", oid, f"与 {seen_o[oid]} 单号重复")
            else:
                seen_o[oid] = oid
            if not o.get("customer") or not o.get("product"):
                _issue("error", "order", oid, "缺客户或产品（主键字段）")
            st = normalize_status(o.get("status"))
            if st is None and o.get("status") != "待补充":
                _issue("error", "order", oid, f"非法状态「{o.get('status')}」")
            # 新口径守卫：「已确认」= 数量/交期/单价三项齐全；缺项说明确认过早
            if o.get("status") == "已确认":
                miss = missing_info_tags(o)
                if miss:
                    _issue("warn", "order", oid,
                           f"已确认单仍缺「{'、'.join(miss)}」，建议核实或退回沟通中")
            q = o.get("qty")
            if q is not None:
                qi = q if isinstance(q, int) and not isinstance(q, bool) else None
                if qi is None:
                    _issue("error", "order", oid, f"数量「{q}」非整数")
                elif qi < 0:
                    _issue("error", "order", oid, f"数量 {qi} 为负数")
            dd = o.get("delivery_date")
            if dd and not (isinstance(dd, str) and len(dd) == 10 and dd[4] == dd[7] == "-"):
                _issue("error", "order", oid, f"交期「{dd}」非 YYYY-MM-DD 格式")
            # D-2 批次字段一致性（与 repair 共用同一口径）
            for level, msg in self._batch_consistency_msgs(o):
                _issue(level, "order", oid, msg)
            if not o.get("created_at"):
                _issue("warn", "order", oid, "缺 created_at")
            for c in o.get("change_log", []):
                if not c.get("time"):
                    _issue("warn", "order", oid, "存在缺 time 的变更记录")
                    break

        order_ids = {o.get("order_id") for o in self.orders}
        for t in self.tasks:
            tid = t.get("task_id") or "(无任务号)"
            if tid in seen_t:
                _issue("error", "task", tid, f"与 {seen_t[tid]} 任务号重复")
            else:
                seen_t[tid] = tid
            if not t.get("content"):
                _issue("error", "task", tid, "缺任务内容")
            ref = t.get("order_id")
            if ref and ref not in order_ids:
                _issue("warn", "task", tid, f"关联订单 {ref} 不存在（孤儿引用）")
        return issues

    def repair(self, apply: bool = True):
        """修复可自动修复的问题，返回 (修复动作列表, 仍需人工处理的问题清单)。

        自动修复：重复ID重新编号、非法状态归一化（别名映射，仍非法→沟通中并标记）、
        缺 created_at/updated_at 补当前时间、任务孤儿引用清除、变更记录补时间。
        不自动改值：负数/非整数数量、非法交期 → 标记 flags 待人工确认（不覆盖原值）；
        D-2 批次不一致（批次和≠delivered_qty / delivered+remain≠qty / 占位单带批次）
        → 仅打 flags.batch_inconsistent 标记进 remaining，绝不静默篡改批次数据。
        """
        actions, remaining = [], []

        def _act(msg):
            actions.append(msg)

        # 订单：重复单号
        seen = set()
        for o in self.orders:
            oid = o.get("order_id")
            if not oid or oid in seen:
                new_id = self.next_order_id()
                _act(f"订单「{oid or '(空)'}」单号重复/缺失，重新编号为 {new_id}")
                o["order_id"] = new_id
            seen.add(o["order_id"])
        for o in self.orders:
            oid = o["order_id"]
            # 非法状态
            st = normalize_status(o.get("status"))
            if st is None:
                if o.get("status") == "待补充":
                    pass  # 占位单合法状态，保留
                else:
                    bad = o.get("status")
                    o["status"] = "沟通中"
                    o.setdefault("flags", {})["invalid_status"] = bad
                    _act(f"订单 {oid} 非法状态「{bad}」归一化为「沟通中」并标记待核")
            # 时间戳
            if not o.get("created_at"):
                o["created_at"] = _now()
                _act(f"订单 {oid} 补 created_at")
            if not o.get("updated_at"):
                o["updated_at"] = o["created_at"]
                _act(f"订单 {oid} 补 updated_at")
            # 变更记录补时间
            hist = self._ensure_history(o)["change_log"]
            for c in hist:
                if not c.get("time"):
                    c["time"] = o["updated_at"]
                    _act(f"订单 {oid} 变更记录补 time")
            # 数量/交期异常：只标记不覆盖（模块三·2 原则）
            flags = o.setdefault("flags", {})
            q = o.get("qty")
            if q is not None:
                ok_int = isinstance(q, int) and not isinstance(q, bool) and q >= 0
                if not ok_int:
                    flags["abnormal"] = f"数量「{q}」异常，待人工确认"
                    flags["locked_fields"] = sorted(
                        set(flags.get("locked_fields") or []) | {"qty"})
                    remaining.append(f"订单 {oid} 数量异常（{q}），已锁定待人工确认")
            dd = o.get("delivery_date")
            if dd and not (isinstance(dd, str) and len(dd) == 10 and dd[4] == dd[7] == "-"):
                flags["abnormal"] = f"交期「{dd}」格式异常，待人工确认"
                flags["locked_fields"] = sorted(
                    set(flags.get("locked_fields") or []) | {"delivery_date"})
                remaining.append(f"订单 {oid} 交期格式异常（{dd}），已锁定待人工确认")
            # D-2 批次字段不一致：只标记「需人工确认」，不得静默篡改批次/数量数据
            # （口径与 integrity_check 共用 _batch_consistency_msgs，防两处漂移）
            batch_msgs = self._batch_consistency_msgs(o)
            if batch_msgs:
                msg = "；".join(m for _, m in batch_msgs)
                flags["batch_inconsistent"] = f"{msg}，需人工确认（未自动修改）"
                remaining.append(f"订单 {oid} 批次数据不一致（{msg}），需人工确认")
                # D-1 修复：必须计入 actions，否则 `if apply and actions: self.save()`
                # 不触发，batch_inconsistent 标记只存在于内存，重启即丢
                _act(f"订单 {oid} 批次数据不一致，已标记待人工确认（未改数据）")

        # 任务：重复号 / 孤儿引用 / 时间戳
        seen_t = set()
        order_ids = {o["order_id"] for o in self.orders}
        for t in self.tasks:
            tid = t.get("task_id")
            if not tid or tid in seen_t:
                new_id = self.next_task_id()
                _act(f"任务「{tid or '(空)'}」任务号重复/缺失，重新编号为 {new_id}")
                t["task_id"] = new_id
            seen_t.add(t["task_id"])
            if not t.get("created_at"):
                t["created_at"] = _now()
                _act(f"任务 {t['task_id']} 补 created_at")
            if not t.get("updated_at"):
                t["updated_at"] = t["created_at"]
                _act(f"任务 {t['task_id']} 补 updated_at")
            if t.get("order_id") and t["order_id"] not in order_ids:
                _act(f"任务 {t['task_id']} 孤儿引用 {t['order_id']} 已清除")
                t["order_id"] = None
            self._norm_task(t)

        if apply and actions:
            self.save()
        return actions, remaining

    def snapshot(self):
        """深拷贝当前台账（给查询/展示层，防止外部误改内存数据）。"""
        return {"orders": copy.deepcopy(self.orders), "tasks": copy.deepcopy(self.tasks)}
