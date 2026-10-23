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

from contracts import CheckStatus, ValidationResult, CHANGE_VERBS, VAGUE_QTY_WORDS
from prompts import STATUS_RANK

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 任务期限支持可选时刻（BUG-10 修复）：YYYY-MM-DD 或 YYYY-MM-DD HH:MM
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
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


def _vague_qty_word(o: dict, raw_text: str = None):
    """检测原文里是否用了模糊量词（「一批」「若干」…）。

    模糊量词不等于具体件数：不能猜成 1，必须按「数量缺失」交人工确认。
    返回命中的那个词（用于话术回显），没命中返回 None。
    """
    text = (o.get("source_excerpt") or "") + " " + (raw_text or "")
    for w in VAGUE_QTY_WORDS:
        if w in text:
            return w
    return None


def _missing_message(o: dict, raw_text: str = None, existing: dict = None):
    """生成「缺失信息」话术：逐项列清缺什么，不留「缺客户或产品」这种模糊话。

    规则（用户 2026-10-23 明确）：
    - 模糊量词（「一批」）按数量缺失处理，并点明是模糊量词；
    - 数量/交期一旦判缺，单价一并列入待确认（一次问清，避免反复往返）；
    - 命中在途单时，额外提示「疑似追加」，让用户知道这是往原单上补。
    返回 (message, None) 表示确实缺；返回 (None, None) 表示不缺。
    """
    miss = []
    vague = _vague_qty_word(o, raw_text)
    if not _meaningful(o.get("qty")):
        miss.append(f"数量（原文「{vague}」为模糊量词，需明确件数）" if vague else "数量")
    if not _meaningful(o.get("delivery_date")):
        miss.append("交期")
    if not miss:
        return None
    # 已判缺失 → 单价一并纳入待确认
    if not _meaningful(o.get("unit_price")):
        miss.append("单价")
    msg = "信息不全，已登记占位待补：" + "、".join(miss) + "缺失，请补充后我再正式建单。"
    if existing is not None:
        msg += (f"（疑似对 {existing['order_id']}「{existing['customer']}｜{existing['product']}」的追加，"
                "数量确认后我再挂到原单下）")
    return msg


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
    """raw: LLM 返回的 list[dict]。返回 (orders, tasks, skipped)。

    注意：缺客户 / 缺产品的 order **不再丢进 skipped**，而是照常规整后交给
    classify_order 判 MISSING，由上层登记「待补充」占位单进台账。
    旧行为直接丢弃，用户只能看到「1 条信息不全（缺客户或产品），已跳过」，
    既不知道缺的是什么，台账里也查不到这一单，无法补充 → 已按用户反馈修正。
    """
    orders, tasks, skipped = [], [], []
    for item in raw or []:
        t = item.get("type")
        if t == "order":
            customer = (item.get("customer") or "").strip()
            product = (item.get("product") or "").strip()
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
                # intent 必须透传：追加/更新/新建的分流决策全靠它（用户 2026-10-23 规则③）。
                # 早前这里漏掉该字段，导致每笔追加都被当成「更新」覆盖原单数量。
                "intent": (item.get("intent") or "").strip().lower() or None,
            })
        elif t == "task":
            content = (item.get("task_content") or "").strip()
            if not content:
                skipped.append(item)
                continue
            td = item.get("task_deadline")
            if td and not DATETIME_RE.match(str(td)):
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
def classify_order(o: dict, existing: dict = None, order_date: str = None,
                   raw_text: str = None) -> ValidationResult:
    """对一条抽取订单做四级判定，返回 ValidationResult。

    优先级：异常(abnormal) > 冲突(conflict) > 缺失(missing) > 正常(ok)。
    order_date：下单日期(YYYY-MM-DD)，用于日期倒挂判定；不传则跳过该项。
    raw_text：原始聊天文本；变更动词判定同时看 source_excerpt 与原始消息，
             避免 LLM 摘录没带上「改成」等动词时把正常更新误判为冲突（模块二）。
    """
    customer = (o.get("customer") or "").strip()
    product = (o.get("product") or "").strip()
    # 变更意图：原文或摘录里出现变更动词即视为「主动更新」
    _verb_text = (o.get("source_excerpt") or "") + " " + (raw_text or "")

    # 0. 主键缺失：无法正式建单，但必须登记占位单进台账并说清缺什么，
    #    不能只丢一句「信息不全」（用户无从补充）。
    if not customer or not product:
        miss = []
        if not customer:
            miss.append("客户信息")
        if not product:
            miss.append("产品信息")
        known = []
        if customer:
            known.append(f"客户「{customer}」")
        if product:
            known.append(f"产品「{product}」")
        msg = "缺少" + "、".join(miss) + "，已登记待补充占位单"
        if known:
            msg += "（已知" + "、".join(known) + "）"
        msg += "，请补充后我再正式建单。"
        return ValidationResult(CheckStatus.MISSING, message=msg, patch=None)

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
        # BUG-08 修复：数量暴涨/骤降（疑似多打零/笔误）优先判异常，而非降级为冲突。
        # 仅在无明确变更动词时拦截，避免正常「加到 5000」被误伤。
        if existing is not None and not _has_change_verb(_verb_text):
            eq = existing.get("qty")
            if _meaningful(eq) and _meaningful(qty):
                eqi, nqi = _to_int(eq), _to_int(qty)
                if eqi and nqi and (nqi >= 10 * eqi or eqi >= 10 * nqi):
                    return ValidationResult(
                        CheckStatus.ABNORMAL,
                        message=f"数量 {qty} 与台账 {eq} 相差超过 10 倍（疑似多打零或笔误），已拦截未写入，请确认正确数量。",
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
        verb = _has_change_verb(_verb_text)
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
        msg = _missing_message(o, raw_text)
        if msg:
            return ValidationResult(CheckStatus.MISSING, message=msg, patch=None)

    # 3.5 BUG-01' 修复：命中既有实体但数量/交期全空且无变更意图
    # → 视为补充确认追问，而非「无变化」（避免"已更新"误导）。
    if existing is not None:
        verb = _has_change_verb(_verb_text)
        if not verb and not _meaningful(o.get("qty")) and not _meaningful(o.get("delivery_date")):
            msg = _missing_message(o, raw_text, existing=existing) or \
                "这单已存在，但数量和交期还缺着——确认下数量/交期，我再正式补登？"
            return ValidationResult(CheckStatus.MISSING, message=msg, patch=None)

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
