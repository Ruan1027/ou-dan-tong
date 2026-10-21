"""编排层：解析入库 / 查询 / 简报。"""
import datetime

from llm import LLM
from prompts import card_text, query_text, briefing_text
from store import Store
from validator import validate_extraction, apply_change, classify_order, classify_task
from contracts import CheckStatus
from extractor import extract


def parse_chat(chat_text, store: Store, llm: LLM = None, mock=None, now: str = None):
    raw = extract(chat_text, llm, mock)
    orders, tasks, skipped = validate_extraction(raw)
    out = []
    cards = []
    now = now or datetime.datetime.now().isoformat(timespec="seconds")
    date = now[:10].replace("-", "")
    order_date = now[:10]
    for o in orders:
        existing = store.find_active(o["customer"], o["product"])
        # —— 模块二核心：缺失 / 冲突 / 异常 四级判定 ——
        res = classify_order(o, existing, order_date=order_date)
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
            # 登记占位单，状态「待补充」，便于台账可见，待补全后正式建单
            o["order_id"] = store.next_order_id(date)
            o["status"] = "待补充"
            o.setdefault("flags", {})["missing"] = res.message
            o["created_at"] = now
            o["updated_at"] = now
            store.add_order(o)
            cards.append("🟡 信息不全已占位：" + res.message)
            continue
        # —— 正常（ok）：新建 或 主动更新 ——
        if existing is not None:
            patch, change = apply_change(existing, o, now=now)
            if patch:
                store.update_order(existing["order_id"], patch, change)
                if "flags" in existing and existing["flags"]:
                    existing["flags"].pop("conflict", None)
                    existing["flags"].pop("missing", None)
                    store.update_order(existing["order_id"], {}, None)
            out.append(("update", existing))
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
        show_tasks = new_tasks if i == first_new else []
        cards.append(card_text(rec, show_tasks, kind == "new"))
    if skipped:
        cards.append(f"⚠️ 有 {len(skipped)} 条信息不全（缺客户或产品），已跳过，待人工确认。")
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
    customers = sorted({o["customer"] for o in store.orders}, key=len, reverse=True)
    target = next((c for c in customers if c[:2] in q or c in q), None)
    res = store.query_orders(customer=target) if target else store.orders
    if not res:
        return "台账中没有找到相关订单，可尝试换称呼或查看全部在途订单。"
    lines = []
    for o in res:
        sug = ""
        days = _days_to(o.get("delivery_date"))
        if days is not None:
            if 0 <= days <= 3:
                sug = f"距交期还有 {days} 天，建议跟进一次。"
            elif days < 0:
                sug = "已逾期，请尽快处理。"
        lines.append(query_text(o, sug))
    return "\n".join(lines)


def briefing(store: Store, llm: LLM = None):
    today = datetime.date.today().isoformat()
    due_before = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    new_orders = store.query_orders(created_on=today)
    pending = store.query_orders(status="沟通中")
    due = store.query_orders(due_before=due_before)
    overdue_tasks = store.query_tasks(overdue_before=today)

    def _urgent(o):
        d = _days_to(o.get("delivery_date"))
        return d if d is not None else 999

    most_urgent = (f"{pending[0]['customer']} 已拖 {abs(_urgent(pending[0]))} 天" if pending else "无")
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


# ---- 意图路由：自动判断用户这句话要干啥 ----
_QUERY_MARKERS = ("到哪", "状态", "查", "进度", "什么时候交", "几号交",
                 "待报价", "临期", "逾期", "交付", "什么状态", "有几")
_BRIEFING_MARKERS = ("简报", "今天情况", "今日", "整体情况", "汇总", "今天有哪些")


def handle_message(text, store: Store, llm: LLM = None, mock=None):
    """统一入口：贴群聊→提取；问句→查单；简报词→汇总。"""
    t = (text or "").strip()
    if not t:
        return {"mode": "hint",
                "reply": "把业务群聊粘贴给我，或问我「XX 那单到哪了」「出今日简报」。"}
    if any(k in t for k in _BRIEFING_MARKERS):
        return {"mode": "briefing", "reply": briefing(store, llm)}
    if any(k in t for k in _QUERY_MARKERS):
        return {"mode": "query", "reply": query(t, store, llm)}
    card, _ = parse_chat(t, store, llm=llm, mock=mock)
    return {"mode": "parse", "reply": card}
