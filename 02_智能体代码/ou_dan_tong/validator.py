"""确定性校验 + 变更比对。模块二·7/8/9（缺失/冲突/异常）。

- validate_extraction：LLM 抽取结果的类型规整（保留，向下兼容）。
- apply_change：仅覆盖本次有值字段，并记录变更履历（保留，向下兼容）。
- classify_order：模块二核心。对一条抽取订单做 异常→冲突→缺失→正常 四级判定，
  返回契约 4 的 ValidationResult {status, message, patch}。
- 异常(abnormal)：负数数量 / 日期倒挂 / 格式错误 → 直接拦截，不写入。
- 冲突(conflict)：与在途单数值矛盾且非显式「变更动词」→ 不覆盖，待人工确认。
- 缺失(missing)：必填字段（数量/交期）为空 → 登记占位单，追问补全。
"""
import re
import datetime

from contracts import CheckStatus, ValidationResult, CHANGE_VERBS
from prompts import STATUS_RANK

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 变更动词以 contracts.CHANGE_VERBS 为单一真相源（避免本地重复定义漂移，见 BUG-02 修复）
_FIELD_CN = {"qty": "数量", "unit_price": "单价", "delivery_date": "交期", "status": "状态"}


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _meaningful(v):
    s = str(v).strip() if v is not None else ""
    return s != "" and s != "待报价"


def _parse_date(s):
    try:
        return datetime.date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _has_change_verb(text):
    t = text or ""
    return any(v in t for v in CHANGE_VERBS)


# ---------------------------------------------------------------------------
# 向下兼容：LLM 抽取结果规整 + 变更比对
# ---------------------------------------------------------------------------
def validate_extraction(raw):
    """raw: LLM 返回的 list[dict]。返回 (orders, tasks, skipped)。"""
    orders, tasks, skipped = [], [], []
    for item in raw or []:
        t = item.get("type")
        if t == "order":
            customer = (item.get("customer") or "").strip()
            product = (item.get("product") or "").strip()
            if not customer or not product:
                skipped.append(item)
                continue
            dd = item.get("delivery_date")
            if dd and not DATE_RE.match(str(dd)):
                dd = None
            orders.append({
                "customer": customer,
                "product": product,
                "spec": (item.get("spec") or "").strip(),
                "qty": _to_int(item.get("qty")),
                "unit_price": (item.get("unit_price") or "待报价").strip(),
                "delivery_date": dd,
                "status": item.get("status_hint") or "沟通中",
                "sample_flag": item.get("sample_flag") or "无",
                "source_excerpt": (item.get("source_excerpt") or "").strip()[:100],
            })
        elif t == "task":
            content = (item.get("task_content") or "").strip()
            if not content:
                skipped.append(item)
                continue
            td = item.get("task_deadline")
            if td and not DATE_RE.match(str(td)):
                td = None
            tasks.append({
                "content": content,
                "owner": (item.get("owner") or "").strip(),
                "deadline": td,
                "status": "待办",
            })
        else:
            skipped.append(item)
    return orders, tasks, skipped


def apply_change(existing: dict, extracted: dict, now: str = None):
    """返回 (patch, change|None)。仅覆盖本次有值字段，并记录变更履历。"""
    now = now or datetime.datetime.now().isoformat(timespec="seconds")
    patch, changes = {}, []
    for field in ["spec", "qty", "unit_price", "delivery_date", "sample_flag"]:
        new = extracted.get(field)
        if _meaningful(new) and str(new) != str(existing.get(field)):
            changes.append((field, existing.get(field), new))
            patch[field] = new
    # 占位单（待补充）补全：状态直接转正为抽取值，不走 rank 比较（BUG-01 修复）
    if existing.get("status") == "待补充":
        ns = extracted.get("status")
        if ns and ns in STATUS_RANK:
            changes.append(("status", existing.get("status"), ns))
            patch["status"] = ns
    else:
        old_rank = STATUS_RANK.get(existing.get("status"), 0)
        new_rank = STATUS_RANK.get(extracted.get("status"), 0)
        if new_rank > old_rank:
            changes.append(("status", existing.get("status"), extracted["status"]))
            patch["status"] = extracted["status"]
    if changes:
        change = {
            "time": now,
            "field": ",".join(f for f, _, _ in changes),
            "old": " | ".join(str(o) for _, o, _ in changes),
            "new": " | ".join(str(n) for _, _, n in changes),
            "source": extracted.get("source_excerpt", ""),
        }
        return patch, change
    return patch, None


# ---------------------------------------------------------------------------
# 模块二核心：缺失 / 冲突 / 异常 判定（契约 4）
# ---------------------------------------------------------------------------
def classify_order(o: dict, existing: dict = None, order_date: str = None) -> ValidationResult:
    """对一条抽取订单做四级判定，返回 ValidationResult。

    优先级：异常(abnormal) > 冲突(conflict) > 缺失(missing) > 正常(ok)。
    order_date：下单日期(YYYY-MM-DD)，用于日期倒挂判定；不传则跳过该项。
    """
    customer = (o.get("customer") or "").strip()
    product = (o.get("product") or "").strip()

    # 0. 主键缺失：无法建单
    if not customer or not product:
        return ValidationResult(
            CheckStatus.MISSING,
            message=f"缺客户或产品名，无法建单，已跳过，请补充「{product or '产品'}」的归属客户。",
            patch=None,
        )

    # 1. 异常拦截（最高优先级，直接 block，绝不写入）
    qty = o.get("qty")
    if qty is not None:
        qi = _to_int(qty)
        if qi is None:
            return ValidationResult(
                CheckStatus.ABNORMAL,
                message=f"数量「{qty}」无法识别为整数，格式异常，已拦截未写入，请确认。",
                patch=None,
            )
        if qi < 0:
            return ValidationResult(
                CheckStatus.ABNORMAL,
                message=f"数量 {qty} 为负数，疑似笔误，已拦截未写入，请确认正确数量。",
                patch=None,
            )
    dd = o.get("delivery_date")
    if dd and not DATE_RE.match(str(dd)):
        return ValidationResult(
            CheckStatus.ABNORMAL,
            message=f"交期「{dd}」格式异常（应为 YYYY-MM-DD），已拦截未写入，请确认。",
            patch=None,
        )
    if dd and order_date:
        pd, od = _parse_date(str(dd)), _parse_date(order_date)
        if pd and od and pd < od:
            return ValidationResult(
                CheckStatus.ABNORMAL,
                message=f"交期 {dd} 早于下单日 {order_date}，日期倒挂，已拦截未写入，请确认。",
                patch=None,
            )

    # 2. 冲突检测（与在途单数值矛盾，且非显式「变更动词」→ 不覆盖，待确认）
    # 修复 BUG-01：占位单（status=待补充）补全时直接转正，不进冲突比对；
    # 冲突比对收敛为数值类字段（移除 status，状态流转走状态机而非冲突判定）。
    if existing and existing.get("status") != "待补充":
        verb = _has_change_verb(o.get("source_excerpt", "") or o.get("raw", ""))
        contradictions = []
        for f in ("qty", "unit_price", "delivery_date"):
            ev = existing.get(f)
            nv = o.get(f)
            if _meaningful(ev) and _meaningful(nv) and str(ev) != str(nv) and not verb:
                contradictions.append(f"{_FIELD_CN.get(f, f)}：台账 {ev} ↔ 来单 {nv}")
        if contradictions:
            return ValidationResult(
                CheckStatus.CONFLICT,
                message="检测到与在途单数值矛盾（" + "；".join(contradictions)
                        + "），未自动覆盖，请人工确认以哪份为准。",
                patch=None,
            )

    # 3. 缺失信息：仅当【新建订单】(无在途单) 才算缺失；更新场景缺字段只表示"该项不变更"。
    if existing is None:
        miss = []
        if not _meaningful(o.get("qty")):
            miss.append("数量")
        if not _meaningful(o.get("delivery_date")):
            miss.append("交期")
        if miss:
            return ValidationResult(
                CheckStatus.MISSING,
                message="信息不全，已登记占位待补：" + "、".join(miss) + "缺失，请补充后我再正式建单。",
                patch=None,
            )

    # 4. 正常：可写入。patch 取本次有意义的新值（缺字段视为"不变更"，不报错）。
    patch = {f: o[f] for f in ("spec", "qty", "unit_price", "delivery_date", "sample_flag", "status")
             if f in o and _meaningful(o.get(f))}
    return ValidationResult(CheckStatus.OK, message="", patch=patch)


def classify_task(t: dict) -> ValidationResult:
    """模块二·7 任务侧缺失判定（BUG-05 修复）：缺负责人/期限则登记占位并追问，
    与订单 missing 对称。返回契约 4 的 ValidationResult。"""
    miss = []
    if not (t.get("owner") or "").strip():
        miss.append("负责人")
    if not (t.get("deadline") or "").strip():
        miss.append("期限")
    if miss:
        content = (t.get("content") or "").strip()
        return ValidationResult(
            CheckStatus.MISSING,
            message="任务信息不全，已登记占位待补：" + "、".join(miss) + "缺失（" + content
                    + "），请补全后我再正式排期。",
            patch=None,
        )
    return ValidationResult(CheckStatus.OK, message="", patch=None)
