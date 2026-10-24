"""编排层：解析入库 / 查询 / 简报。"""
import datetime
import os
import re
import threading
import time

from llm import LLM
from prompts import (card_text, query_text, briefing_text, append_card_text,
                     missing_card_text, conflict_card_text, disambig_card_text,
                     HUMAN_HANDOFF, STATUS_ORDER)
from store import Store
from validator import validate_extraction, apply_change, classify_order, classify_task
from contracts import CheckStatus, CHANGE_VERBS, APPEND_INTENTS, effective_delivery_date
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
    _single_order = len(orders) == 1
    for o in orders:
        existing = store.find_active(o["customer"], o["product"])
        # C-13：批次必须在校验【之前】挂到来单上。
        # classify_order 内部用 contracts.effective_delivery_date 比对交期，而该契约只有在
        # 订单带 delivery_batches 时才取「最晚批次日」。旧逻辑只把批次最晚日回填 delivery_date
        # （且仅当来单 delivery_date 为空），来单带 delivery_date 时仍以裸字段去比台账批次最晚日
        # → 同一单被误判「数值矛盾」拦下（模块五 S6-E2 即因此失败）。直接挂 delivery_batches，
        # effective_delivery_date 自动取最晚批次日，冲突比对自然同口径。
        _batches_new = _parse_batches(o.get("source_excerpt") or "")
        if not _batches_new and _single_order:
            # 摘录被模型截断时退回对话原文；多单场景不兜底，避免把别单的批次挂错
            _batches_new = _parse_batches(chat_text or "")
        if _batches_new:
            o["delivery_batches"] = _batches_new
        # —— 模块二核心：缺失 / 冲突 / 异常 四级判定 ——
        # raw_text 传入：变更动词判定同时看原文，避免「改成/更新」被漏判为冲突
        res = classify_order(o, existing, order_date=order_date, raw_text=chat_text)
        if res.status == CheckStatus.ABNORMAL:
            cards.append("⛔ 异常拦截：" + res.message)
            continue
        # —— 追加单（用户 2026-10-23 规则③）——
        # 必须排在冲突判定【之前】：追加量必然与原单数量不同，
        # 若放任冲突分支先跑，每一笔正常追加都会被判「数值矛盾」拦掉。
        # 命中单一定未交付完成（find_active 已排除 已交付/已取消），
        # 已交付完的会走下面 existing is None 的新建路径。
        if existing is not None and _has_qty(o) and _is_append(o, chat_text):
            o["order_id"] = store.next_order_id(date)
            o["parent_order_id"] = existing["order_id"]
            o["is_append"] = True
            o.setdefault("status", "沟通中")
            o["created_at"] = now
            o["updated_at"] = now
            store.add_order(o)
            out.append(("append", o))
            continue
        if res.status == CheckStatus.CONFLICT:
            if existing is not None:
                existing.setdefault("flags", {})["conflict"] = res.message
                store.update_order(existing["order_id"], {}, None)
                # 2026-10-24 应队长需求：先调出台账真实数据展示，再询问是否以此为准
                cards.append(conflict_card_text(existing, res.message))
            else:
                cards.append("⚠️ 冲突待确认：" + res.message)
            continue
        if res.status == CheckStatus.MISSING:
            if existing is not None:
                # 台账已有这一单：只追问缺什么，**不重复建占位单**
                # （否则同客户同产品会出现两张单，台账重复计数）
                existing.setdefault("flags", {})["missing"] = res.message
                store.update_order(existing["order_id"], {}, None)
                # 2026-10-24 应队长需求：先展示当前订单已知字段，再询问是否更改
                cards.append(missing_card_text(existing, res.message, is_new=False))
                continue
            # 登记占位单，状态「待补充」，便于台账可见，待补全后正式建单
            o["order_id"] = store.next_order_id(date)
            o["status"] = "待补充"
            o.setdefault("flags", {})["missing"] = res.message
            o["created_at"] = now
            o["updated_at"] = now
            store.add_order(o)
            # 2026-10-24 应队长需求：先展示已知字段，再询问是否更改
            cards.append(missing_card_text(o, res.message, is_new=True))
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
                out.append(("update", existing))
            else:
                # BUG-07 修复：内容完全一致时不再输出"已更新订单"，标记为无变化
                out.append(("nochange", existing))
            # C-13：批次识别与「本句有无字段变更」解耦 —— 客户只补一句「分两批」时
            # 数量/交期都没变（patch 为空），旧写法会整段跳过，批次永远补不进台账。
            _apply_batches(existing, o.get("source_excerpt") or "", store)
        else:
            o["order_id"] = store.next_order_id(date)
            o["created_at"] = now
            o["updated_at"] = now
            store.add_order(o)
            # C-13：首单也要识别分批。旧逻辑只在「更新」分支写批次，
            # 客户第一次下单就说「分两批」会整段丢失，有效交期永远算不出来。
            _apply_batches(o, o.get("source_excerpt") or "", store)
            out.append(("new", o))
    new_tasks, dup_tasks = [], []
    for t in tasks:
        t["task_id"] = store.next_task_id(date)
        # 模块二·7 任务侧缺失追问（BUG-05 修复）：缺负责人/期限登记占位并追问
        cls = classify_task(t)
        if cls.status == CheckStatus.MISSING:
            t.setdefault("flags", {})["missing"] = cls.message
            cards.append("🟡 任务信息不全已占位：" + cls.message)
        if out:
            t["order_id"] = out[0][1]["order_id"]
        # BUG-13：add_task 幂等 —— 命中台账里完全相同的任务时返回原对象（不新增、不新发 TSK 号）
        stored = store.add_task(t)
        if stored is t:
            new_tasks.append(t)
        else:
            t["task_id"] = stored.get("task_id")
            dup_tasks.append(stored)
    first_new = next((i for i, (k, _) in enumerate(out) if k == "new"), None)
    for i, (kind, rec) in enumerate(out):
        if kind == "nochange":
            cards.append(f"⏸️ 无变化：{rec['customer']}的{rec['product']} 数据已一致，未重复写入。")
            continue
        if kind == "append":
            cards.append(append_card_text(rec))
            continue
        show_tasks = new_tasks if i == first_new else []
        cards.append(card_text(rec, show_tasks, kind == "new"))
    # BUG-12 修复：纯任务派发（无订单）且信息齐全时，也要给每条任务可见回执
    if not out and new_tasks:
        for t in new_tasks:
            cards.append(_task_card(t))
    # BUG-13：重复任务给一句明确回执，而不是静默丢弃（用户要知道「我说过了，系统没重复记」）
    if dup_tasks:
        cards.append("⏸️ 任务已在台账中，未重复登记：" + "；".join(
            f"{t.get('content')}（{t.get('task_id')}）" for t in dup_tasks))
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


# ---- C-2：多轮指代消解（进程内会话上下文，10 分钟过期，不引外部存储）----
_SESSION_CTX = {}      # {session_key: {"order_ids": [...], "ts": time.time()}}
_CTX_TTL = 600         # 秒；过期即失效，指代词回退全表匹配
_CTX_MAX = 500         # C-2：上限，防止大量一次性 session_key 常驻内存（无主动回收路径）

# ---- 2026-10-24 应队长需求：信息缺失多轮消歧 ----
# 当客户信息缺失时，逐个拿出匹配订单问「是否是这个订单」，客户回「否」就给下一个，
# 全部问完仍「否」→转人工。需要会话级状态记录已问过的候选列表。
# 结构：{session_key: {"candidates": [order_id, ...], "asked": [order_id, ...],
#                       "customer": "...", "product": "...", "ts": time.time()}}
_DISAMBIG_CTX = {}
# C-6：补温州话常用说法（那批货 / 上一单）。刻意不加裸「那批」——「法兰那批的货」里
# 它是量词不是指代，加了会让改单话术误触发范围锁定。
_PRONOUNS = ("刚才那单", "刚才的单", "刚刚那单", "这一单", "那一单", "上一个单",
             "那批货", "这批货", "上一单", "那单", "这单")
# 泛词前缀（「客户A / 公司B」）：2 字前缀会把不同客户串在一起，故降级为 3 字
_GENERIC_PREFIXES = ("客户", "公司", "厂家", "老板", "经理")


def reset_session_ctx(session_key: str = None):
    """清空多轮上下文（测试隔离 / 会话重启用）。不传 key 清全部。"""
    if session_key is None:
        _SESSION_CTX.clear()
    else:
        _SESSION_CTX.pop(session_key, None)


def _has_pronoun(q: str) -> bool:
    """是否含指代词。「它」单独处理，避免「其它/其他」误触发。"""
    if any(k in q for k in _PRONOUNS):
        return True
    return "它" in q and "其它" not in q and "其他" not in q


def _entity_hits(q: str, order: dict) -> set:
    """单张单对这句话的实体命中（客户 / 产品 / 规格）。

    C-7：query 的候选打分与 scope 的「有没有显式实体」判定共用这一份口径。
    此前两处各写一套（前缀长度规则不一致），必然漂移——一处改了另一处不知道。
    返回新 set，调用方可安全 `add` 状态/时间等命中。
    """
    hits = set()
    cust = order.get("customer") or ""
    if cust:
        if cust in q:
            hits.add("客户")
        else:
            # 泛词前缀降级为 3 字，避免「客户A / 客户B」互相误命中
            n = 3 if cust[:2] in _GENERIC_PREFIXES else 2
            if len(cust) >= n and cust[:n] in q:
                hits.add("客户")
    prod = order.get("product") or ""
    if prod:
        hit = prod in q or any(prod[i:i + 3] in q for i in range(len(prod) - 2))
        # 反向匹配：问句里的 2~4 字片段是产品名的一部分。
        # 客户只说简称时全靠这条 —— 问「法兰那单到哪了」，全名是「304法兰盘 DN80」，
        # 只做 3 字滑窗永远命中不了。与正向滑窗同口径，只作用于本单自身产品名，不会误命中他单。
        if not hit:
            for L in (4, 3, 2):
                for i in range(len(q) - L + 1):
                    seg = q[i:i + L]
                    if seg.strip() == seg and seg in prod:
                        hit = True
                        break
                if hit:
                    break
        if hit:
            hits.add("产品")
    spec = order.get("spec") or ""
    if spec and len(spec) >= 2 and spec in q:
        hits.add("规格")
    return hits


def _has_explicit_entity(q: str, store: Store) -> bool:
    """这句里是否自带可定位实体（客户全名/前缀、产品名/3字片段、规格）。

    有实体时必须走全表匹配，不能被上一轮的范围锁死：
    例如先问「温州机电」再问「万博那单到哪了」，若走范围就会答成温州机电（串台）。
    C-7：判定口径与 query 的候选打分完全一致（_entity_hits）。
    """
    return any(_entity_hits(q, o) for o in store.orders)


def _resolve_scope(q: str, store: Store, session_key: str):
    """C-2：指代追问时返回上一轮命中的订单列表作为查询范围；否则 None（走全表）。"""
    if not _has_pronoun(q):
        return None
    ctx = _SESSION_CTX.get(session_key)
    if not ctx:
        return None
    if time.time() - ctx.get("ts", 0) > _CTX_TTL:   # 过期 → 丢弃，回退全表
        _SESSION_CTX.pop(session_key, None)
        return None
    if _has_explicit_entity(q, store):               # 显式实体优先，防跨客户串台
        return None
    ids = set(ctx.get("order_ids") or [])
    orders = [o for o in store.orders if o.get("order_id") in ids]
    return orders or None


def _remember_ctx(session_key: str, trace: dict):
    """C-2：把本轮命中的订单写进会话上下文；本轮没命中则清掉，避免污染下一句。

    淘汰策略（审查 C-2 修复）：过期条目原本只在「同一个 key 再问一次」时才删，
    大量一次性 session_key（网页每次刷新一个新 id）会常驻内存。这里在写入前先清过期，
    仍超上限则淘汰最早写入的一条（dict 3.7+ 保持插入顺序）。
    """
    ids = trace.get("order_ids") or []
    if not ids:
        _SESSION_CTX.pop(session_key, None)
        return
    if len(_SESSION_CTX) >= _CTX_MAX:
        now = time.time()
        for k in [k for k, v in _SESSION_CTX.items() if now - v.get("ts", 0) > _CTX_TTL]:
            _SESSION_CTX.pop(k, None)
        if len(_SESSION_CTX) >= _CTX_MAX:
            _SESSION_CTX.pop(next(iter(_SESSION_CTX)))
    _SESSION_CTX[session_key] = {"order_ids": list(ids), "ts": time.time()}


# ---- C-3/C-5：查询链路日志（默认关，OU_QUERY_LOG=1 开启；只用标准库 print）----
# C-5：改为「哨兵」而非 import 期求值——None 表示跟随环境变量（运行期改 env 立即生效，
# 演示时不必重启进程）；True/False 表示显式覆盖，保留给测试/演示临时置位。
_QUERY_LOG = None
_QLOG_LOCK = threading.Lock()   # 整行原子写出，避免与 ThreadingHTTPServer 的半行交错


def _query_log_on() -> bool:
    """当前是否输出链路日志（显式覆盖优先，否则读环境变量）。"""
    if _QUERY_LOG is not None:
        return bool(_QUERY_LOG)
    return os.getenv("OU_QUERY_LOG", "").lower() in ("1", "true", "yes", "on")


def _qlog_q(value, limit: int = 40) -> str:
    """问题文本的单行化：先截原串再 repr。

    顺序不能反——`repr(s)[:80]` 会把 `\\n` 这类转义序列截成半个反斜杠，
    虽然不破行但会输出坏字符；先截再 repr 一定拿到完整转义。
    """
    s = str(value or "")
    if len(s) > limit:
        s = s[:limit] + "…"
    return repr(s)


def _qlog(meta: dict):
    """输出一行可讲解的查询链路（命中键 / 命中单数 / 是否澄清 / 状态筛选 / 耗时）。"""
    q = _qlog_q(meta.get("q", ""))
    hits = meta.get("hits") or "无"
    n = meta.get("n", 0)
    status = meta.get("status") or "无"
    ms = float(meta.get("ms") or 0.0)     # None 也不炸：关日志时更不该因日志抛错
    parts = [f"[QUERY] q={q}",
             f"命中键={hits}",
             f"命中 {n} 单",
             (f"澄清=是（输出前 {n} 个候选）" if meta.get("clarify") else "澄清=否"),
             f"状态筛选={status}",
             f"耗时={ms:.1f}ms"]
    if meta.get("scope"):
        parts.insert(2, "范围=上一轮命中")
    mode = meta.get("mode")
    if mode not in (None, "orders"):
        parts.insert(1, f"分支={mode}")
    line = " | ".join(parts)
    with _QLOG_LOCK:
        print(line, flush=True)


def query(question, store: Store, llm: LLM = None, scope=None, trace=None):
    """查询入口（C-2/C-3 包装层）：计时、回填 trace、按开关输出链路日志。

    scope：限定候选范围（多轮指代消解用，传上一轮命中的订单对象列表）
    trace：出参 dict，回填 order_ids / hits / n / clarify / status / mode / ms
    """
    t0 = time.time()
    meta = {"q": question, "hits": "无", "n": 0, "clarify": False, "status": None,
            "mode": "orders", "order_ids": [], "scope": bool(scope)}
    text = _query_body(question, store, llm, scope, meta)
    meta["ms"] = (time.time() - t0) * 1000
    if trace is not None:
        trace.update(meta)
    if _query_log_on():
        _qlog(meta)
    return text


def _query_body(question, store: Store, llm: LLM = None, scope=None, meta=None):
    meta = meta if meta is not None else {}
    q = question
    # C-2：有 scope 时只在上一轮命中范围内找（不扫全表）
    orders = list(scope) if scope else store.orders
    if any(k in q for k in ("简报", "今天情况", "今日", "今天有哪些", "整体")):
        meta["mode"] = "briefing"
        return briefing(store, llm)

    # ---- 模块四·4：历史追溯分支（识别"改过/历史/变更/履历/几次"等意图）----
    # C-4：词表已提到模块级 _HISTORY_MARKERS（handle_message 也要用同一份，避免两处漂移）
    is_history_query = any(k in q for k in _HISTORY_MARKERS)

    # ---- 模块四·1：查询解析增强（命中键 客户/产品/规格/状态 + 时间区间 + 相关度排序）----
    # 1. 状态关键词映射（C-9：词表提到模块级，可被测试直接断言，也不再来回漂移）
    target_status = None
    for st, kws in _STATUS_KEYWORDS.items():
        if any(k in q for k in kws):
            target_status = st
            break

    # 问的是「交期这个字段」而不是「已交付这个状态」：避免「那单的交付日期呢」被当成
    # 状态筛选=已交付，导致指代追问落空（同时让 C-3 的链路日志不再误导）。
    if any(k in q for k in _DATE_FIELD_MARKERS):
        target_status = None

    # 2. 时间区间解析（今天/本周/上周/最近N天/近N天）→ 返回 (start, end) 作用于 created_on
    date_range = _parse_date_range(q)

    # 3. 多字段命中：客户 / 产品 / 规格；每单计算命中字段集合，按命中数排序
    # C-7：实体部分统一走 _entity_hits（与 scope 判定同一口径），此处只补状态/时间
    candidates = []  # (order, hit_fields:set)
    for o in orders:
        hits = _entity_hits(q, o)
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
    # C-2 例外：指代追问（scope 非空）但这句话没带任何可匹配字段（如「那单的交付日期呢」），
    # 语义是「就刚才那几单，换个字段再说一遍」，应把上一轮的单原样带出，而不是落兜底。
    # C-8：scope + 状态追问先给是/否结论（「那单取消了吗」不能只甩一张卡片让用户自己反推）。
    # 判据用 orders（=scope）而非 candidates，与候选流解耦，澄清/正常两条出口都能带上。
    status_verdict = _status_verdict(target_status, orders) if scope else ""
    if not candidates and scope:
        candidates = [(o, {"上一轮"}) for o in orders]
    if not candidates:
        meta["mode"] = "fallback"
        meta["status"] = target_status
        in_progress = [o for o in orders if o.get("status") not in ("已交付", "已取消")]
        if not in_progress:
            return "台账中没有找到相关订单，可尝试换称呼、按产品问（如「NMX-60 到哪了」），或查看全部在途订单。"
        return "未命中具体客户/产品/状态，以下是全部在途订单：\n" + "\n".join(
            _highlight_query_text(o, _suggest(o)) for o in in_progress
        )

    # 5. 相关度排序：命中字段数降序 → 更新时间倒序
    candidates.sort(key=lambda x: (-len(x[1]), x[0].get("updated_at", "")), reverse=False)
    candidates.sort(key=lambda x: -len(x[1]))  # 主排序：命中数降序

    # C-3：链路元信息（供日志与多轮上下文使用）
    meta["n"] = len(candidates)
    meta["hits"] = "、".join(sorted(candidates[0][1])) if candidates else "无"
    meta["status"] = target_status
    meta["order_ids"] = [o["order_id"] for o, _ in candidates]

    # 6. 输出：每条标注命中字段；用户问"最新"时加"最新状态"强调
    is_latest_query = any(k in q for k in ("最新", "现在", "目前", "当下", "当前状态"))
    prefix = "📌 最新状态：\n" if is_latest_query else ""

    # 模块四·4：历史追溯分支 —— 用户问"改过啥/变更历史"时输出 changes[] 履历
    if is_history_query:
        meta["mode"] = "history"
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
        meta["clarify"] = True
        meta["n"] = top_n
        meta["order_ids"] = [o["order_id"] for o, _ in candidates[:top_n]]
        # 审查 C-3：已经是 scope（上一轮命中的那几单）时再问一次「请补充产品名」等于原地打转，
        # 用户说「那单」本身就是不想指认。改成「就是这几单，您说的是哪一单」并补状态/交期差异，
        # 让用户按差异指认，而不是重复同一句反问。
        if scope:
            lines = [f"🤔 就是这 {top_n} 单，您说的是哪一单？（回单号或产品名都行）"]
        else:
            lines = [f"🤔 找到 {top_n} 单都匹配，请补充产品名或订单号精确定位："]
        for i, (o, _) in enumerate(candidates[:top_n], 1):
            lines.append(f"{i}. {o['order_id']} {o['customer']}/{o['product']}"
                         f"（状态{o.get('status')}，交期{effective_delivery_date(o) or '待定'}）")
        return status_verdict + "\n".join(lines)

    lines = []
    for o, hits in candidates:
        tag = "、".join(sorted(hits))
        sug = _suggest(o)
        line = f"[命中：{tag}] " + _highlight_query_text(o, sug)
        lines.append(line)
    return prefix + status_verdict + "\n".join(lines)


def _status_verdict(target_status, order_list) -> str:
    """C-8：scope 内被问某个状态、但没一单是该状态时，先给一句明确结论。

    背景：「那单取消了吗」在上一轮范围里筛不到「已取消」，原先只甩一张卡片，
    要用户自己从状态标签反推「没取消」，答非所问。命中时不输出——卡片里的状态
    本身就是答案，同一结论说两遍反而啰嗦。
    措辞刻意避开「当前状态」四字：澄清路径有「不展开明细」的断言拿它当排除词。
    """
    if not target_status or not order_list:
        return ""
    if any((o.get("status") or "") == target_status for o in order_list):
        return ""
    if len(order_list) == 1:
        o = order_list[0]
        return (f"🔎 不是「{target_status}」——{o.get('customer')}的{o.get('product')}"
                f"现在是「{o.get('status') or '未知'}」。\n")
    curs = "、".join(dict.fromkeys(f"「{o.get('status') or '未知'}」" for o in order_list))
    return f"🔎 这 {len(order_list)} 单都不是「{target_status}」，现在分别是{curs}。\n"


def _suggest(o: dict) -> str:
    """根据交期生成主动建议（从 query() 抽出复用）。"""
    # C-10：分批单 delivery_date 常为 null，走契约的有效交期才不会静默丢建议
    days = _days_to(effective_delivery_date(o))
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


_BATCH_ITEM_RE = re.compile(r"(\d{1,2})[-/](\d{1,2})\s*[出交发]\s*(\d+)")
# 分批关键词：「分批」「分两批」「分 3 批」「两批」都要认（C-13）。
# 原来只判字面「分批」，客户说「分两批」直接不触发 —— 真实话术里这才是主流说法。
_BATCH_HINT_RE = re.compile(r"分批|分\s*[两二三四五六七八九\d]+\s*批|[两二三四五六七八九\d]\s*批")


def _parse_batches(text: str):
    """从「分两批：11-01 出 500，11-03 出 400」抽取批次列表。无则 None。

    C-13 修复：识别口径从「必须出现字面『分批』」放宽为
    「有分批字样 **或** 至少两条『日期+出+数量』」，覆盖口语主流说法。
    单条且无分批字样仍返回 None —— 「11-01 出 500」可能只是交期+数量，不能当分批。
    """
    if not text:
        return None
    items = []
    year = datetime.date.today().year
    for m in _BATCH_ITEM_RE.finditer(text):
        mo, day, qty = int(m.group(1)), int(m.group(2)), int(m.group(3))
        items.append({"date": f"{year:04d}-{mo:02d}-{day:02d}", "qty": qty})
    if not items:
        return None
    if not (_BATCH_HINT_RE.search(text) or len(items) >= 2):
        return None
    items.sort(key=lambda b: b["date"])
    return items


def _apply_batches(order: dict, text: str, store: Store) -> bool:
    """识别并写入分批交期 + 已交付/余量（BUG-09 / C-13）。返回是否发生变更。

    三个设计要点：
    - **与「本句有没有字段变更」解耦**：客户常常只补一句「分两批：11-01 出 500…」，
      数量和交期都没变。旧逻辑把这段放在 `if patch:` 里面，整段被跳过，批次永远补不进去。
    - **幂等**：内容与台账一致时直接返回，不重复写盘、不制造假的变更痕迹。
    - **护栏**：批次和 ≠ 订单总量时（真实模式下 LLM 常把「某一批的量」抽成总量），
      绝不写负的余量，改为打标记交人工确认 —— 台账里出现 −400 这种数比不显示更糟。
    """
    batches = _parse_batches(text or "")
    if not batches:
        return False
    # 幂等判定放在「累计」之前：内容与台账一致时不再写盘、不制造假变更痕迹，
    # 但 delivered_qty/remain_qty 仍要算（parse_chat 已在校验前把 delivery_batches 挂到 o 上，
    # 若这里直接早退，新建分批单的已交付量会一直是 None）。
    changed = order.get("delivery_batches") != batches
    order["delivery_batches"] = batches
    # 注意：不在这里把 delivery_date 改写成最晚批次日。
    # 单值 delivery_date 保留「客户所述日期」，真正的有效交期一律由
    # contracts.effective_delivery_date（取 delivery_batches 最晚日）计算，
    # 避免字段与有效交期混用。显示/冲突比对都走 effective_delivery_date，故无需回写。
    delivered = sum(b["qty"] for b in batches)
    qty = order.get("qty")
    if isinstance(qty, int) and not isinstance(qty, bool) and qty > 0 and delivered != qty:
        order.setdefault("flags", {})["batch_inconsistent"] = (
            f"批次数量之和 {delivered} ≠ 订单数量 {qty}，请确认总数量后再核对余量")
        order["delivered_qty"] = delivered
        order["remain_qty"] = None
    else:
        order["delivered_qty"] = delivered
        try:
            order["remain_qty"] = int(order.get("qty") or 0) - delivered
        except (TypeError, ValueError):
            order["remain_qty"] = None
    if changed:
        store.update_order(order["order_id"], {}, None)
    return changed


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
    # C-10：替换目标串 `预计{dd}交付` 由 prompts.query_text 产出，而那边已用契约的有效交期；
    # 这里若还读裸字段，分批单 dd 为 None → 匹配不中 → 📅 高亮静默丢失。
    dd = effective_delivery_date(order)
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
    # C-10：临期口径改走「有效交期」（分批单 = 最晚批次日）。
    # store.query_orders(due_before=) 读的是单值 delivery_date（store.py），
    # 分批单会按旧日期误报临期、或直接漏报，故在 agent 内按有效交期重算，不改 store.py（模块三资产）。
    # 过滤条件与原实现逐字对齐：有交期（dd 非空）+ dd <= 阈值 + 排除「已交付」。
    due_pairs = []
    for o in store.orders:
        dd = effective_delivery_date(o)
        if dd and dd <= due_before and o.get("status") != "已交付":
            due_pairs.append((o, dd))
    due = [o for o, _ in due_pairs]
    overdue_tasks = store.query_tasks(overdue_before=today)

    def _urgent(o):
        # 无交期返回 None，不再返回 999（修复"已拖 999 天"bug）
        # C-10：分批单走最晚批次日，否则永远判成「无交期」而选不进最急
        return _days_to(effective_delivery_date(o))

    if pending:
        # 优先选有交期且 days 最小（含负数=已逾期）的单做"最急"
        with_due = [o for o in pending if _urgent(o) is not None]
        if with_due:
            most_urgent_order = min(with_due, key=_urgent)
            days = _urgent(most_urgent_order)
            tag = "已逾期" if days < 0 else f"还有 {days} 天"
            most_urgent = (f"{most_urgent_order['customer']} {tag}"
                           f"（交期 {effective_delivery_date(most_urgent_order)}）")
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
        "due_list": "、".join(f"{o['customer']}({dd})" for o, dd in due_pairs) or "无",
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


def _safe_llm():
    """C-1：force 通道专用——调用方没注入 llm 时自建一个，构造失败也不抛。

    背景：`handle_message(..., force_colloquial=True)` 在无 Key 场景下 llm 为 None，
    `_colloquialize` 第一行就原样返回，与 `colloquialize()` 自建 LLM 的行为不一致，
    演示时按「口语化 开」等于没反应。这里统一成「先尝试、失败由 _colloquialize 回退」。
    """
    try:
        return LLM()
    except Exception:
        return None


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
# 审查 C-4：历史追随意图词表提到模块级——handle_message 需要在 `_looks_like_order` 之前
# 先按历史意图分流，否则「改过啥」里的「改」会被 contracts.CHANGE_VERBS 判成改单指令。
# 仅保留这一份 _HISTORY_MARKERS（C-13 清理：原 _HISTORY_QUERY_MARKERS 与之重复，
# 两份并存时改一处忘一处必然漂移，且其为死代码无任何引用，已删除）。
_HISTORY_MARKERS = ("改过", "历史", "变更", "履历", "几次", "改了什么", "改了啥",
                    "变更记录", "变更历史", "改动", "修改记录", "怎么改的")

# ---- 2026-10-24 应队长需求：冲突确认 / 转人工 ----
# 客户看到冲突卡片后可能回复同意或拒绝：
#   · 同意 → 以台账数据为准（不改动，下一条消息若无变更动词走 nochange）
#   · 拒绝 → 转人工（HUMAN_HANDOFF）
# 词表放模块级以便测试断言；放在 handle_message 局部变量会导致改了忘同步。
_REJECT_MARKERS = ("不是", "不对", "不同意", "不确认", "不认", "错了", "搞错",
                   "弄错", "不对吧", "不是这", "不是这单", "不一定是", "存疑",
                   "有疑问", "有疑问", "质疑")
_CONFIRM_MARKERS = ("是的", "确认", "对的", "没错", "是这样", "是这个", "是这单",
                    "以此为准", "以你们为准", "以台账为准", "好的", "可以", "嗯")

# ---- C-9：查询词表统一放模块级（原先散在 _query_body 里当局部变量，测试断不到，
# 也无法和 _QUERY_MARKERS 一起维护）----
_STATUS_KEYWORDS = {
    # 「已报价」状态已废弃：报价类话术归到「沟通中」（未定价维度），与状态机口径一致
    "沟通中": ["沟通中", "待报价", "刚下单", "新单", "已报价", "报过价", "报价"],
    "已确认": ["已确认", "确认了", "下单了"],
    "生产中": ["生产中", "在生产", "排产", "做货", "在做"],
    "已交付": ["已交付", "已发货", "发了", "送了", "交付"],
    "部分交付": ["部分交付", "分批", "分两批", "交了一部分", "出了一批"],
    # C-8：补问句形态。「取消了吗」这类追问必须能识别成「问已取消」，
    # 否则 scope 追问进不了结论分支；但**不加裸「取消」**——CHANGE_VERBS 不含「取消」，
    # 「那单先取消吧」会走查询分支，加了会把它答成「不是已取消」的错结论。
    "已取消": ["已取消", "取消了", "取消订单", "作废", "不要了",
               "取消了吗", "取消没", "取消没有", "取消了没", "是不是取消"],
}

# 问的是「交期这个字段」而不是「已交付这个状态」：避免「那单的交付日期呢」被当成
# 状态筛选=已交付，导致指代追问落空（同时让 C-3 的链路日志不再误导）。
# C-9 补漏：交付时间（含「交付」二字，真的会被误判成已交付）、货期、几时交、
# 什么时候发 / 什么时候送到；冗余的「交期是」已被「交期」覆盖，删掉。
_DATE_FIELD_MARKERS = ("交付日期", "交货日期", "交付时间", "交期",
                       "什么时候交", "几时交", "几号交", "哪天交", "何时交",
                       "什么时候到", "什么时候发", "什么时候送到", "货期")


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


def _has_amount_claim(t: str) -> bool:
    """C-4：这句里有没有数量/价格主张（真正的「下单/改单」才有的东西）。

    用它而不是 `_looks_like_order` 来区分历史追问：CHANGE_VERBS 含单字「改」，
    「法兰盘改过啥」也会被 `_looks_like_order` 判成改单，导致历史查询走不到查询分支、
    无 Key 时直接抛 RuntimeError。
    """
    return bool(_ORDER_QTY_RE.search(t) or _ORDER_PRICE_RE.search(t))


def _is_append(o: dict, raw_text: str = None) -> bool:
    """判定是否为「追加单」意图（用户 2026-10-23 规则③）。

    追加 vs 更新：
    - 追加 = 在原单之外再要一笔（「再走一批 / 再排一批 / 追加 / 再加」）→ 新开一条挂到原单下；
    - 更新 = 改原单本身（「改成 / 加到 / 调整为」）→ 覆盖原单字段。

    判定顺序：LLM 抽的 intent 是权威，明确表态就照办；只有 intent 缺失时才用关键词兜底。
    兜底只查 APPEND_INTENTS，**不查 CHANGE_VERBS** —— 「加」同时出现在两边
    （「再加 200 个」是追加，「数量加到 6000」是更新），靠「先排除变更动词」区分会把
    追加误杀成更新，实测已踩过。
    """
    intent = (o.get("intent") or "").strip().lower()
    if intent == "append":
        return True
    if intent in ("update", "new"):
        return False
    text = (o.get("source_excerpt") or "") + " " + (raw_text or "")
    return any(k in text for k in APPEND_INTENTS)


def _has_qty(o: dict) -> bool:
    try:
        return int(o.get("qty")) is not None
    except (TypeError, ValueError):
        return False


def _query_focus(trace: dict) -> dict:
    """把 query() 的链路元信息转成网页端可直接执行的定位指令（2026-10-23 交互改版）。

    网页端不再渲染「命中」结果栏，而是按这里给的 order_ids 跳到订单台账对应行、
    浅蓝高亮；kind=history 时连变更履历子单一起展开。

    kind 取值（与前端 applyQuery 一一对应）：
      orders    命中订单 → 跳转高亮
      history   变更履历查询 → 跳转高亮 + 自动展开子单
      clarify   多单并列匹配 → 全部高亮，提示补充产品名/单号
      fallback  未精确命中 → 高亮全部在途单，并按文本说明
      briefing  问的是简报 → 结果落「今日简报」面板，不占查询区
    文本答案一律保留：命令行、测试、微信侧都直接用它，网页端只是多了一条更直观的路。
    """
    if not trace:
        return {"kind": "orders", "order_ids": []}
    kind = trace.get("mode") or "orders"
    if trace.get("clarify"):
        kind = "clarify"          # clarify 优先于 mode=orders，否则前端会当成单命中高亮
    return {"kind": kind, "order_ids": list(trace.get("order_ids") or [])}


def handle_message(text, store: Store, llm: LLM = None, mock=None, sender: str = None,
                   force_colloquial: bool = False, session_key: str = "default"):
    """统一入口：贴群聊→提取；问句→查单；简报词→汇总。
    sender：发这条消息的客户方称谓（真实微信群的发言人昵称），缺了它客户自报类
            消息会被抽成「无客户」而丢弃，故网页端必须一并传入。
    force_colloquial：C-1 显式演示开关，True 时无视 OU_COLLOQUIAL 强制执行口语化改写
            （前端「口语化 开/关」按钮透传这个参数即可，默认模板直出）。
    session_key：C-2 多轮上下文隔离键，默认单会话；多用户场景传各自 id 即可。
    模块四·3：query/briefing 的输出经 _colloquialize() 口语化（需显式开启，默认模板直出）。
    """
    t = (text or "").strip()
    if not t:
        return {"mode": "hint",
                "reply": "把业务群聊粘贴给我，或问我「XX 那单到哪了」「出今日简报」。"}
    # 2026-10-24 应队长需求：冲突确认 / 消歧环节的「是 / 否」识别。
    # 消歧状态优先于通用 handoff/confirm：若 session 正在消歧，客户回「是」给更新后订单、
    # 回「否」给下一个候选或转人工。非消歧状态才走通用 handoff/confirm。
    _t_low = t.lower()
    _in_disambig = session_key in _DISAMBIG_CTX
    _is_reject = any(k in t for k in _REJECT_MARKERS) and not _looks_like_order(t) \
            and not any(k in t for k in _QUERY_MARKERS + _BRIEFING_MARKERS)
    _is_confirm = any(k in t for k in _CONFIRM_MARKERS) and not _looks_like_order(t) \
            and not any(k in t for k in _QUERY_MARKERS + _BRIEFING_MARKERS)

    if _in_disambig and (_is_reject or _is_confirm):
        _ctx = _DISAMBIG_CTX[session_key]
        _asked = _ctx.get("asked", [])
        _customer = _ctx.get("customer")
        _product = _ctx.get("product")
        if _is_confirm:
            # 客户说「是」→ 取出当前询问的订单，给更新后的订单卡片
            _cur_id = _asked[-1] if _asked else None
            _cur = store.get_order(_cur_id) if _cur_id else None
            _DISAMBIG_CTX.pop(session_key, None)
            if _cur:
                _card = card_text(_cur, [], is_new=False)
                return {"mode": "disambig_confirm", "reply": "好的，以下是您确认的订单信息：\n\n" + _card}
            return {"mode": "confirm", "reply": "好的，已确认以台账记录的数据为准，不做更改。"}
        else:
            # 客户说「否」→ 找下一个未问过的匹配订单
            _candidates = store.find_candidates(customer=_customer, product=_product,
                                                 exclude_ids=_asked)
            if _candidates:
                _next = _candidates[0]
                _ctx["asked"].append(_next["order_id"])
                _total = len(_ctx.get("all_ids", []))
                _idx = len(_ctx["asked"])
                _card = disambig_card_text(_next, _idx, _total)
                return {"mode": "disambig_next", "reply": _card}
            else:
                # 全部问完仍「否」→ 转人工
                _DISAMBIG_CTX.pop(session_key, None)
                return {"mode": "handoff", "reply": HUMAN_HANDOFF}

    # 非消歧状态的通用 handoff/confirm（冲突确认环节）
    if _is_reject:
        return {"mode": "handoff", "reply": HUMAN_HANDOFF}
    if _is_confirm:
        return {"mode": "confirm", "reply": "好的，已确认以台账记录的数据为准，不做更改。"}
    if any(k in t for k in _BRIEFING_MARKERS):
        if force_colloquial and llm is None:
            llm = _safe_llm()          # C-1：无 Key 也要尝试改写，失败由 _colloquialize 回退
        ans = briefing(store, llm)
        reply = _colloquialize(ans, llm, force=force_colloquial)
        out = {"mode": "briefing", "reply": reply,
               "focus": {"kind": "briefing", "order_ids": []}}
        if force_colloquial and reply is ans:
            out["colloquial_fallback"] = True   # 供前端提示「口语化未生效」
        return out
    # 审查 C-4：历史追溯优先于改单意图——「改过啥/变更历史」本身带「改」字，会被
    # `_looks_like_order` 判成改单去走 parse_chat（无 Key 直接抛错），必须在这之前分流。
    # 守卫用「无数量/价格主张」而不是 `not _looks_like_order`：后者恒为假（「改」必命中）。
    if any(k in t for k in _HISTORY_MARKERS) and not _has_amount_claim(t):
        if force_colloquial and llm is None:
            llm = _safe_llm()
        scope = _resolve_scope(t, store, session_key)
        trace = {}
        ans = query(t, store, llm, scope=scope, trace=trace)
        _remember_ctx(session_key, trace)
        reply = _colloquialize(ans, llm, force=force_colloquial)
        out = {"mode": "query", "reply": reply, "focus": _query_focus(trace)}
        if force_colloquial and reply is ans:
            out["colloquial_fallback"] = True
        return out
    # 订单/改单意图优先：含变更动词或数量/价格数值主张时，即使带「那单」也按群聊解析
    if _looks_like_order(t):
        card, _ = parse_chat(t, store, llm=llm, mock=mock, sender=sender)
        # 兜底：抽不出任何订单/任务时必须给可见反馈，不能返回空字符串
        # （空响应在界面上表现为「点了没反应」，用户会判定功能坏了）
        if not (card or "").strip():
            return {"mode": "hint",
                    "reply": "这句话我没能识别出订单要素——请补上客户、产品、数量和交期，"
                             "或者换个问法，例如「法兰那单到哪了？」「法兰那单改过几次」。"}
        # 2026-10-24 应队长需求：信息缺失时触发多轮消歧。
        # 当 parse_chat 返回的卡片含缺失标记（"信息不全"或"信息待补充"），
        # 且客户消息里至少带了一个可匹配维度（sender 或正文里的客户/产品名）时，
        # 在台账中找匹配候选逐个询问，而非只建占位单等客户补。
        if ("信息不全" in card or "信息待补充" in card) and sender:
            _cands = store.find_candidates(customer=sender, product=None)
            if _cands:
                _DISAMBIG_CTX[session_key] = {
                    "all_ids": [c["order_id"] for c in _cands],
                    "asked": [_cands[0]["order_id"]],
                    "customer": sender, "product": None,
                    "ts": time.time(),
                }
                _dcard = disambig_card_text(_cands[0], 1, len(_cands))
                return {"mode": "disambig_start", "reply": _dcard}
        return {"mode": "parse", "reply": card}
    if any(k in t for k in _QUERY_MARKERS):
        if force_colloquial and llm is None:
            llm = _safe_llm()          # C-1：无 Key 也要尝试改写，失败由 _colloquialize 回退
        # C-2：指代追问优先在上一轮命中范围内解析
        scope = _resolve_scope(t, store, session_key)
        trace = {}
        ans = query(t, store, llm, scope=scope, trace=trace)
        _remember_ctx(session_key, trace)
        reply = _colloquialize(ans, llm, force=force_colloquial)
        out = {"mode": "query", "reply": reply, "focus": _query_focus(trace)}
        if force_colloquial and reply is ans:
            out["colloquial_fallback"] = True   # 无 Key / 事实失真 / 异常时都会走这里
        return out
    # 兜底分支也必须带 sender —— 早前这里漏传，导致「小陈，法兰那批再帮我排上一批」
    # 这类既不含变更动词也不含数量的消息走进来后丢掉发信人，被误判「缺少客户信息」。
    card, _ = parse_chat(t, store, llm=llm, mock=mock, sender=sender)
    if not (card or "").strip():
        return {"mode": "hint",
                "reply": "没识别出订单或任务。可问我「XX 那单到哪了」「出今日简报」，"
                         "或直接粘贴客户的原话群聊。"}
    return {"mode": "parse", "reply": card}
