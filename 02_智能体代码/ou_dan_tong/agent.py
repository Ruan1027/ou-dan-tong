"""编排层：解析入库 / 查询 / 简报。"""
import datetime
import os
import re

from llm import LLM
from prompts import card_text, query_text, briefing_text, STATUS_ORDER
from store import Store
from validator import validate_extraction, apply_change, classify_order, classify_task
from contracts import CheckStatus, CHANGE_VERBS
from extractor import extract


def parse_chat(chat_text, store: Store, llm: LLM = None, mock=None, now: str = None,
               sender: str = None):
    raw = extract(chat_text, llm, mock, sender=sender)
    orders, tasks, skipped = validate_extraction(raw)
    out = []
    cards = []
    now = now or datetime.datetime.now().isoformat(timespec="seconds")
    date = now[:10].replace("-", "")
    order_date = now[:10]
    for o in orders:
        existing = store.find_active(o["customer"], o["product"])
        # —— 模块二核心：缺失 / 冲突 / 异常 四级判定 ——
        # raw_text 传入：变更动词判定同时看原文，避免「改成/更新」被漏判为冲突
        res = classify_order(o, existing, order_date=order_date, raw_text=chat_text)
        if res.status == CheckStatus.ABNORMAL:
            cards.append("⛔ 异常拦截：" + res.message)
            continue
        if res.status == CheckStatus.CONFLICT:
            if existing is not None:
                existing.setdefault("flags", {})["conflict"] = res.message
                store.update_order(existing["order_id"], {}, None)
            cards.append("⚠️ 冲突待确认：" + res.message)
            continue
        if res.status == CheckStatus.MISSING:
            if existing is not None:
                # 台账已有这一单：只追问缺什么，**不重复建占位单**
                # （否则同客户同产品会出现两张单，台账重复计数）
                existing.setdefault("flags", {})["missing"] = res.message
                store.update_order(existing["order_id"], {}, None)
                cards.append(f"🟡 信息待补充 {existing['order_id']}：" + res.message
                             + "（台账已有该单，补齐后我直接更新，不再重复建单）")
                continue
            # 登记占位单，状态「待补充」，便于台账可见，待补全后正式建单
            o["order_id"] = store.next_order_id(date)
            o["status"] = "待补充"
            o.setdefault("flags", {})["missing"] = res.message
            o["created_at"] = now
            o["updated_at"] = now
            store.add_order(o)
            # 卡片必须写清「缺什么」+「在哪能查到」，否则用户无从补充
            cards.append(f"🟡 信息不全已占位 {o['order_id']}：" + res.message
                         + "（已记入「📊 平台汇总」台账，状态=待补充）")
            continue
        # —— 正常（ok）：新建 或 主动更新 ——
        if existing is not None:
            patch, change = apply_change(existing, o, now=now)
            if patch:
                # BUG-06 修复：真实变更时同步「原文依据」到台账顶层字段，
                # 保证"结论 + 依据"同源（队友B 探针登记：数量 6000 时依据仍写 5000）
                # 只在本次原文非空时覆盖，避免变更消息没带摘录时把旧依据清空
                _src = (o.get("source_excerpt") or "").strip()
                if _src:
                    patch["source_excerpt"] = _src
                store.update_order(existing["order_id"], patch, change)
                if "flags" in existing and existing["flags"]:
                    existing["flags"].pop("conflict", None)
                    existing["flags"].pop("missing", None)
                    store.update_order(existing["order_id"], {}, None)
                # BUG-09 修复：识别分批交期写入 delivery_batches + 已交付/余量
                _batches = _parse_batches(_src)
                if _batches:
                    existing["delivery_batches"] = _batches
                    delivered = sum(b["qty"] for b in _batches)
                    existing["delivered_qty"] = delivered
                    try:
                        existing["remain_qty"] = int(existing.get("qty") or 0) - delivered
                    except (TypeError, ValueError):
                        existing["remain_qty"] = None
                    store.update_order(existing["order_id"], {}, None)
                out.append(("update", existing))
            else:
                # BUG-07 修复：内容完全一致时不再输出"已更新订单"，标记为无变化
                out.append(("nochange", existing))
        else:
            o["order_id"] = store.next_order_id(date)
            o["created_at"] = now
            o["updated_at"] = now
            store.add_order(o)
            out.append(("new", o))
    new_tasks = []
    for t in tasks:
        t["task_id"] = store.next_task_id(date)
        # 模块二·7 任务侧缺失追问（BUG-05 修复）：缺负责人/期限登记占位并追问
        cls = classify_task(t)
        if cls.status == CheckStatus.MISSING:
            t.setdefault("flags", {})["missing"] = cls.message
            cards.append("🟡 任务信息不全已占位：" + cls.message)
        if out:
            t["order_id"] = out[0][1]["order_id"]
        store.add_task(t)
        new_tasks.append(t)
    first_new = next((i for i, (k, _) in enumerate(out) if k == "new"), None)
    for i, (kind, rec) in enumerate(out):
        if kind == "nochange":
            cards.append(f"⏸️ 无变化：{rec['customer']}的{rec['product']} 数据已一致，未重复写入。")
            continue
        show_tasks = new_tasks if i == first_new else []
        cards.append(card_text(rec, show_tasks, kind == "new"))
    # BUG-12 修复：纯任务派发（无订单）且信息齐全时，也要给每条任务可见回执
    if not out and new_tasks:
        for t in new_tasks:
            cards.append(_task_card(t))
    if skipped:
        # 走到这里的只有「type 既非 order 也非 task」的条目（缺客户/缺产品的订单
        # 已改为登记待补充占位单，不再进 skipped），故文案不再提「缺客户或产品」。
        cards.append(f"⚠️ 有 {len(skipped)} 条内容既不是订单也不是任务，已忽略。"
                     "如果这是订单，请把客户、产品、数量、交期一起发我。")
    return "\n\n".join(cards), {"orders": out, "tasks": new_tasks, "skipped": len(skipped)}


def _days_to(date_str):
    try:
        d = datetime.date.fromisoformat(date_str)
        return (d - datetime.date.today()).days
    except (ValueError, TypeError):
        return None


def query(question, store: Store, llm: LLM = None):
    q = question
    if any(k in q for k in ("简报", "今天情况", "今日", "今天有哪些", "整体")):
        return briefing(store, llm)

    # ---- 模块四·4：历史追溯分支（识别"改过/历史/变更/履历/几次"等意图）----
    _HISTORY_MARKERS = ("改过", "历史", "变更", "履历", "几次", "改了什么", "改了啥",
                        "变更记录", "变更历史", "改动", "修改记录", "怎么改的")
    is_history_query = any(k in q for k in _HISTORY_MARKERS)

    # ---- 模块四·1：查询解析增强（命中键 客户/产品/规格/状态 + 时间区间 + 相关度排序）----
    # 1. 状态关键词映射
    status_keywords = {
        "沟通中": ["沟通中", "待报价", "刚下单", "新单"],
        "已报价": ["已报价", "报过价", "报价"],
        "已确认": ["已确认", "确认了", "下单了"],
        "生产中": ["生产中", "在生产", "排产", "做货", "在做"],
        "已交付": ["已交付", "已发货", "发了", "送了", "交付"],
        "部分交付": ["部分交付", "分批", "分两批", "交了一部分", "出了一批"],
        "已取消": ["已取消", "取消了", "取消订单", "作废", "不要了"],
    }
    target_status = None
    for st, kws in status_keywords.items():
        if any(k in q for k in kws):
            target_status = st
            break

    # 2. 时间区间解析（今天/本周/上周/最近N天/近N天）→ 返回 (start, end) 作用于 created_on
    date_range = _parse_date_range(q)

    # 3. 多字段命中：客户 / 产品 / 规格；每单计算命中字段集合，按命中数排序
    candidates = []  # (order, hit_fields:set)
    for o in store.orders:
        hits = set()
        cust = o.get("customer") or ""
        prod = o.get("product") or ""
        spec = o.get("spec") or ""
        # 客户名匹配：全名 / 前2字前缀（但排除"客户/公司/厂家"等泛词前缀，避免"客户A/B/C"互相误命中）
        if cust:
            if cust in q:
                hits.add("客户")
            else:
                # 前2字前缀匹配，泛词前缀降级为前3字
                _GENERIC_PREFIXES = ("客户", "公司", "厂家", "老板", "经理")
                prefix_len = 3 if cust[:2] in _GENERIC_PREFIXES else 2
                if len(cust) >= prefix_len and cust[:prefix_len] in q:
                    hits.add("客户")
        # 产品名匹配：全名 / 任一≥2字片段（产品名常带型号，如 NMX-60）
        if prod:
            if prod in q:
                hits.add("产品")
            else:
                # 滑窗匹配产品名中长度≥3的子串（防短词误命中）
                for i in range(len(prod) - 2):
                    if prod[i:i+3] in q:
                        hits.add("产品")
                        break
        # 规格匹配：仅当规格非空且≥2字
        if spec and len(spec) >= 2 and spec in q:
            hits.add("规格")
        # 状态命中
        if target_status and o.get("status") == target_status:
            hits.add("状态")
        # 时间区间命中：created_on 落在区间内
        if date_range:
            start, end = date_range
            created = (o.get("created_at") or "")[:10]
            if start <= created <= end:
                hits.add("时间")
        if hits:
            candidates.append((o, hits))

    # 4. 无任何命中 → 兜底返回全部在途（保留原引导语义）
    if not candidates:
        in_progress = [o for o in store.orders if o.get("status") not in ("已交付", "已取消")]
        if not in_progress:
            return "台账中没有找到相关订单，可尝试换称呼、按产品问（如「NMX-60 到哪了」），或查看全部在途订单。"
        return "未命中具体客户/产品/状态，以下是全部在途订单：\n" + "\n".join(
            _highlight_query_text(o, _suggest(o)) for o in in_progress
        )

    # 5. 相关度排序：命中字段数降序 → 更新时间倒序
    candidates.sort(key=lambda x: (-len(x[1]), x[0].get("updated_at", "")), reverse=False)
    candidates.sort(key=lambda x: -len(x[1]))  # 主排序：命中数降序

    # 6. 输出：每条标注命中字段；用户问"最新"时加"最新状态"强调
    is_latest_query = any(k in q for k in ("最新", "现在", "目前", "当下", "当前状态"))
    prefix = "📌 最新状态：\n" if is_latest_query else ""

    # 模块四·4：历史追溯分支 —— 用户问"改过啥/变更历史"时输出 changes[] 履历
    if is_history_query:
        hist_lines = []
        for o, hits in candidates:
            tag = "、".join(sorted(hits))
            head = f"[命中：{tag}] {o['customer']}的{o['product']}（{o['order_id']}）"
            changes = o.get("changes") or []
            if not changes:
                hist_lines.append(head + "\n   ↳ 本单无变更记录（首次建单后未修改）")
            else:
                hist_lines.append(head + f"\n   ↳ 共 {len(changes)} 次变更记录（按时间倒序）：")
                # 时间倒序输出（changes[] 是按发生顺序追加的，最新在后，倒序展示）
                for idx, c in enumerate(reversed(changes), 1):
                    hist_lines.append(_format_change(idx, c))
        return "📜 变更履历追溯：\n" + "\n".join(hist_lines)

    # ---- P0-2 多命中澄清：并列最高分超过 1 单时，反问定位而非一次性罗列 ----
    # 单命中（top_n == 1）走下面的正常展开；历史追溯分支不受影响（履历本就展示全部）
    max_hits = len(candidates[0][1])
    top_n = sum(1 for _, hits in candidates if len(hits) == max_hits)
    if len(candidates) > 1 and top_n > 1:
        lines = [f"🤔 找到 {top_n} 单都匹配，请补充产品名或订单号精确定位："]
        for i, (o, _) in enumerate(candidates[:top_n], 1):
            lines.append(f"{i}. {o['order_id']} {o['customer']}/{o['product']}（状态{o.get('status')}）")
        return "\n".join(lines)

    lines = []
    for o, hits in candidates:
        tag = "、".join(sorted(hits))
        sug = _suggest(o)
        line = f"[命中：{tag}] " + _highlight_query_text(o, sug)
        lines.append(line)
    return prefix + "\n".join(lines)


def _suggest(o: dict) -> str:
    """根据交期生成主动建议（从 query() 抽出复用）。"""
    days = _days_to(o.get("delivery_date"))
    if days is None:
        return ""
    if 0 <= days <= 3:
        return f"距交期还有 {days} 天，建议跟进一次。"
    if days < 0:
        return "已逾期，请尽快处理。"
    return ""


def _task_card(t: dict) -> str:
    """BUG-12 修复：为单条任务生成可见回执卡片（纯任务派发也能看到反馈）。"""
    owner = (t.get("owner") or "").strip() or "待认领"
    deadline = t.get("deadline") or "待定"
    return f"✅ 已登记任务：{t.get('content')} ｜ 负责人：{owner} ｜ 期限：{deadline}"


def _parse_batches(text: str):
    """BUG-09 修复：从「分两批：11-01 出 500，11-03 出 400」抽取批次列表。无则 None。"""
    if not text or "分批" not in text:
        return None
    import re as _re
    items = []
    for m in _re.finditer(r"(\d{1,2})[-/](\d{1,2})\s*出\s*(\d+)", text):
        mo, day, qty = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = datetime.date.today().year
        items.append({"date": f"{year:04d}-{mo:02d}-{day:02d}", "qty": qty})
    return items or None


def _highlight_query_text(order: dict, suggestion: str = "") -> str:
    """模块四·2：在原 query_text 基础上给关键字段（状态/数量/交期）加 emoji 高亮，
    并在有变更履历时附加"本单有 N 次变更记录"提示。
    不改 prompts.py（队友A 文件），只在 agent.py 里包装增强。"""
    # 先拿原 query_text 的输出作为基础
    base = query_text(order, suggestion)
    # 关键字段高亮：在原文本里给状态值/数量/交期加 emoji 包围
    status = order.get("status", "")
    if status:
        # 把"当前状态XXX"替换为带高亮的版本
        status_emoji = _status_emoji(status)
        base = base.replace(f"当前状态{status}", f"当前状态{status_emoji} {status}")
    qty = order.get("qty")
    if qty is not None and f"×{qty}" in base:
        base = base.replace(f"×{qty}", f"📦 ×{qty}")
    dd = order.get("delivery_date")
    if dd and f"预计{dd}交付" in base:
        base = base.replace(f"预计{dd}交付", f"📅 预计{dd}交付")
    # 变更履历提示：有 changes[] 时告诉用户这是最新版，并标变更次数
    changes = order.get("changes") or []
    if changes:
        hint = '问"改过啥"可看详情'
        base += f"\n   ↳ 📌 本单已有 {len(changes)} 次变更记录（最新状态，{hint}）"
    return base


def _status_emoji(status: str) -> str:
    """状态对应的 emoji 高亮标记。"""
    return {
        "沟通中": "💬",
        "已报价": "💰",
        "已确认": "✅",
        "生产中": "🏭",
        "已交付": "🚚",
        "部分交付": "📦",
        "已取消": "🚫",
    }.get(status, "🔹")


def _format_change(idx: int, c: dict) -> str:
    """格式化单条变更履历为可读文本。
    changes[] 每项结构：{time, field, old, new, source}
    field 可能是逗号拼接的多字段，old/new 用 ' | ' 拼。
    """
    time_str = (c.get("time") or "")[:16].replace("T", " ")  # 截到分钟
    fields = (c.get("field") or "").split(",")
    olds = (c.get("old") or "").split(" | ")
    news = (c.get("new") or "").split(" | ")
    # 字段对齐：若 field/old/new 数量一致，逐字段展示；否则按原样整体展示
    if len(fields) == len(olds) == len(news) and len(fields) > 1:
        parts = [f"{f}: {o} → {n}" for f, o, n in zip(fields, olds, news)]
        change_str = "；".join(parts)
    else:
        change_str = f"{c.get('field','?')}: {c.get('old','?')} → {c.get('new','?')}"
    src = c.get("source", "")
    src_str = f"  📎 原文：\"{src}\"" if src else ""
    return f"   {idx}. [{time_str}] {change_str}{src_str}"


def _parse_date_range(q: str):
    """解析查询中的时间区间关键词 → (start_iso, end_iso)，无识别返回 None。
    支持：今天 / 昨天 / 本周 / 上周 / 最近N天 / 近N天 / N天内
    """
    import re as _re
    today = datetime.date.today()
    # 今天
    if "今天" in q or "今日" in q:
        s = today.isoformat()
        return (s, s)
    # 昨天
    if "昨天" in q:
        y = (today - datetime.timedelta(days=1)).isoformat()
        return (y, y)
    # 本周（周一到今天）
    if "本周" in q or "这周" in q:
        monday = today - datetime.timedelta(days=today.weekday())
        return (monday.isoformat(), today.isoformat())
    # 上周（上周一到上周日）
    if "上周" in q:
        monday = today - datetime.timedelta(days=today.weekday() + 7)
        sunday = monday + datetime.timedelta(days=6)
        return (monday.isoformat(), sunday.isoformat())
    # 最近N天 / 近N天 / N天内 / N天前
    m = _re.search(r"(?:最近|近|前)\s*(\d+)\s*天", q) or _re.search(r"(\d+)\s*天(?:内|前)", q)
    if m:
        n = int(m.group(1))
        start = (today - datetime.timedelta(days=n)).isoformat()
        return (start, today.isoformat())
    return None


def briefing(store: Store, llm: LLM = None):
    today = datetime.date.today().isoformat()
    due_before = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    new_orders = store.query_orders(created_on=today)
    pending = store.query_orders(status="沟通中")
    due = store.query_orders(due_before=due_before)
    overdue_tasks = store.query_tasks(overdue_before=today)

    def _urgent(o):
        # 无交期返回 None，不再返回 999（修复"已拖 999 天"bug）
        return _days_to(o.get("delivery_date"))

    if pending:
        # 优先选有交期且 days 最小（含负数=已逾期）的单做"最急"
        with_due = [o for o in pending if _urgent(o) is not None]
        if with_due:
            most_urgent_order = min(with_due, key=_urgent)
            days = _urgent(most_urgent_order)
            tag = "已逾期" if days < 0 else f"还有 {days} 天"
            most_urgent = f"{most_urgent_order['customer']} {tag}（交期 {most_urgent_order['delivery_date']}）"
        else:
            # 待报价但都没写交期
            most_urgent = f"{pending[0]['customer']} 待定交期"
    else:
        most_urgent = "无"
    stats = {
        "date": today,
        "new": len(new_orders),
        "new_customers": "、".join(sorted({o["customer"] for o in new_orders})) or "无",
        "pending_quote": len(pending),
        "most_urgent": most_urgent,
        "due_soon": len(due),
        "due_list": "、".join(f"{o['customer']}({o['delivery_date']})" for o in due) or "无",
        "overdue": len(overdue_tasks),
        "overdue_list": "、".join(f"{t['owner'] or '待认领'}的「{t['content']}」" for t in overdue_tasks) or "无",
    }
    return briefing_text(stats)


# ---- 模块四·3：口语化应答 ----
# 总开关，默认关闭：模板话术本身已是人话，再经一次 LLM 只增加 8~9s 延迟并改写固定措辞。
# 需要演示"口语播报"能力时设 OU_COLLOQUIAL=1。
_COLLOQUIAL_ON = os.getenv("OU_COLLOQUIAL", "").lower() in ("1", "true", "yes", "on")
_COLLOQUIAL_SYSTEM = (
    "你是「瓯单通」订单协同助手，负责把机器生成的查询/简报结果改写成人话口语播报。"
    "严格规则（违反即失败）："
    "1) 只能改写语气和表达方式，让它更像人说话（口语、自然、带一点温度，像同事在群里汇报）。"
    "2) 不得添加、修改、删除任何事实信息——客户名、产品名、规格、数量、日期、状态、订单号必须原样保留。"
    "3) 保留所有 📎 依据引用、emoji 标记和 [命中：xxx] 标签，不要去掉。"
    "4) 保持简洁，不超过原文长度的 1.3 倍。不要加客套话、不要加开场白、不要加'根据查询结果'之类的废话。"
    "5) 如果原文有多条订单，逐条改写，保持原有顺序和换行结构。"
    "6) 直接输出改写后的文本，不要加任何解释或前缀。"
)


def _check_facts(raw: str, rewritten: str) -> bool:
    """口语化后事实保全检查（P0-3）：数字集合 / 日期 / 状态值不得增删。

    - 数字严格等集：改写删掉数字（事实丢失）或新编数字（幻觉）都不通过；
    - 状态值（沟通中/已报价/…）必须原样保留；
    - 中文数字改写（如 5000→五千）会触发回退 → 保真优先，可接受。
    """
    import re as _re
    nums_raw = set(_re.findall(r"\d+(?:\.\d+)?", raw))
    nums_new = set(_re.findall(r"\d+(?:\.\d+)?", rewritten))
    if nums_raw != nums_new:  # 数字被删或新编
        return False
    for d in _re.findall(r"\d{4}-\d{2}-\d{2}", raw):
        if d not in rewritten:  # 日期丢失
            return False
    for st in STATUS_ORDER:
        if st in raw and st not in rewritten:  # 状态值丢失
            return False
    return True


def _colloquialize(raw_text: str, llm: LLM = None, force: bool = False) -> str:
    """模块四·3：把机器化的查询/简报输出改写成口语播报体（**默认关闭**）。

    默认走模板直出，原因（实测数据支撑）：
    - 模板本身已经是人话（card_text / query_text / briefing_text），再经一次 LLM 改写
      会多花 8~9 秒，且会把「未命中具体客户/产品/状态」这类固定话术换掉，
      破坏「问题—答案一一对应」，评审追问时对不上模块二。
    - 需要展示口语化能力时设 OU_COLLOQUIAL=1 开启，或调用 colloquialize(..., force=True)。

    force=True：无视总开关强制执行（供演示按钮/显式入口使用，C-1）。
    开启后仍有防幻觉：改写结果做事实保全校验（数字/日期/状态不得增删），失真则回退原文。
    """
    if llm is None or (not _COLLOQUIAL_ON and not force):
        return raw_text
    if not raw_text or not raw_text.strip():
        return raw_text
    try:
        result = llm.chat(_COLLOQUIAL_SYSTEM, raw_text).strip()
        # P0-3：数字/日期/状态被改丢或编造 → 回退原文（检测-修正管线，不依赖二次 LLM）
        if not _check_facts(raw_text, result):
            return raw_text
        # 安全兜底：LLM 返回空或过短（可能出错），回退原文
        if not result or len(result) < len(raw_text) * 0.3:
            return raw_text
        return result
    except Exception:
        return raw_text


def colloquialize(raw_text: str, force: bool = False, llm: LLM = None) -> str:
    """显式口语化入口（C-1）：给演示/前端一个可对比「开 / 关」的调用点。

    - force=False：受 OU_COLLOQUIAL 总开关约束，默认原样返回，**不产生任何 LLM 调用**；
    - force=True：无视总开关强制执行；未注入 llm 时自行构造 LLM()；
    - 事实失真 / LLM 异常 / 未配 Key 一律回退原文，本函数**不会抛错**（演示不能被打断）。
    """
    if not force and not _COLLOQUIAL_ON:
        return raw_text
    if not raw_text or not raw_text.strip():
        return raw_text
    if llm is None:
        llm = LLM()  # 未配 Key 时 chat() 会抛错，由 _colloquialize 兜住并回退原文
    return _colloquialize(raw_text, llm=llm, force=True)


# ---- 意图路由：自动判断用户这句话要干啥 ----
_QUERY_MARKERS = ("到哪", "状态", "查", "进度", "什么时候交", "几号交",
                 "待报价", "临期", "逾期", "交付", "什么状态", "有几",
                 # 模块四·1 增强：补状态/产品/规格/时间类查询意图词，避免被误判为群聊去解析
                 "生产中", "沟通中", "已报价", "已确认", "已发货", "在做", "做货", "排产",
                 "的单", "那单", "有哪些", "什么时候", "几号", "本周", "上周", "最近",
                 "今天", "昨天", "近几天", "天内",
                 # 模块四·4 增强：历史追溯关键词
                 "改过", "历史", "变更", "履历", "几次", "改了什么", "改了啥",
                 "变更记录", "变更历史", "改动", "修改记录")
_BRIEFING_MARKERS = ("简报", "今天情况", "今日", "整体情况", "汇总", "今天有哪些")


# 订单/改单意图优先判定：含变更动词或明确的数量/价格数值主张时，即便夹带
# 「那单/的单」等查询词，也按群聊指令解析，而非当成查单问句。
# 解决模块一真实话术（如「法兰 DN80 那单单价你按 18 算」）被误判为查询的问题。
_ORDER_QTY_RE = re.compile(r"\d+\s*(个|台|件|双|米|批|套|只|根|张|箱|副|把)")
_ORDER_PRICE_RE = re.compile(r"(单价|价格|报价|单价按|元|块)\D*\d")


def _looks_like_order(t: str) -> bool:
    if any(v in t for v in CHANGE_VERBS):
        return True
    if _ORDER_QTY_RE.search(t):
        return True
    if _ORDER_PRICE_RE.search(t):
        return True
    return False


def handle_message(text, store: Store, llm: LLM = None, mock=None, sender: str = None,
                   force_colloquial: bool = False):
    """统一入口：贴群聊→提取；问句→查单；简报词→汇总。
    sender：发这条消息的客户方称谓（真实微信群的发言人昵称），缺了它客户自报类
            消息会被抽成「无客户」而丢弃，故网页端必须一并传入。
    force_colloquial：C-1 显式演示开关，True 时无视 OU_COLLOQUIAL 强制执行口语化改写
            （前端「口语化 开/关」按钮透传这个参数即可，默认模板直出）。
    模块四·3：query/briefing 的输出经 _colloquialize() 口语化（需显式开启，默认模板直出）。
    """
    t = (text or "").strip()
    if not t:
        return {"mode": "hint",
                "reply": "把业务群聊粘贴给我，或问我「XX 那单到哪了」「出今日简报」。"}
    if any(k in t for k in _BRIEFING_MARKERS):
        return {"mode": "briefing",
                "reply": _colloquialize(briefing(store, llm), llm, force=force_colloquial)}
    # 订单/改单意图优先：含变更动词或数量/价格数值主张时，即使带「那单」也按群聊解析
    if _looks_like_order(t):
        card, _ = parse_chat(t, store, llm=llm, mock=mock, sender=sender)
        return {"mode": "parse", "reply": card}
    if any(k in t for k in _QUERY_MARKERS):
        return {"mode": "query",
                "reply": _colloquialize(query(t, store, llm), llm, force=force_colloquial)}
    card, _ = parse_chat(t, store, llm=llm, mock=mock, sender=sender)
    return {"mode": "parse", "reply": card}
