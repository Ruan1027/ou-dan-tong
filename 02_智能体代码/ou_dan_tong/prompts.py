"""提示词骨架（平台无关，可直接复用）。"""

EXTRACTION_SYSTEM = """你是制造企业订单要素提取器。从聊天记录中提取所有订单线索与待办任务，
严格输出 JSON 数组，不编造缺失字段，缺失则填 null：
[{
 "type": "order" | "task",
 "customer": "客户称谓（公司名，或消息中对客户的称呼如「王工」；结合上下文归属，完全无线索才填 null）", "product": "", "spec": "", "qty": null,
 "unit_price": "可含'待报价'", "delivery_date": "YYYY-MM-DD|null",
 "sample_flag": "打样|大货|无", "status_hint": "沟通中|已报价|已确认|生产中|部分交付|已交付|已取消|null",
 "task_content": "", "owner": "", "task_deadline": "YYYY-MM-DD|null",
 "source_excerpt": "支撑本结论的原文片段，≤100字"
}]
规则：
1. 只依据原文；两人对话注意上下文归属，把话术归到正确客户/产品。
2. 出现"改成/变更为/调到/加到"等动词，视为对该订单字段的【主动更新】，如实抽取新值。
3. 同一订单出现两个互相矛盾的数量/交期（如一处说300一处说280）时，两条都抽出来，
   source_excerpt 分别标注，交由后台做矛盾检测，不要自行取舍。
4. 数量必为非负整数；交期必为不早于当天的 YYYY-MM-DD；若原文为负、为0、为乱码或交期早于今天，
   仍照原文抽（后台会拦截异常），不要擅自改成"合理"值。
5. 口语化日期（如「12月1号」「下周五」「月底」）按当前年份补全为 YYYY-MM-DD；确实无法推断的才填 null。
6. 缺数量或缺交期时，对应字段填 null，不要脑补。"""

# 发送者身份规则：真实微信群每条消息都带发言人昵称，网页端必须一并传入。
# 缺了它，像「小陈，DZ47 那个加到 80 个」这种客户自报场景会被判成「无客户」而整条丢弃。
SENDER_RULE = """
7. 输入消息前可能带 [发送者:xxx] 前缀，表示发这条消息的客户方人员称谓。
   此时 customer 必须填该发送者（如「王总」「李姐」「阿珍」），**不得填 null**；
   若正文里同时出现了公司名（如「永嘉精密机械」），customer 写成「王总（永嘉精密机械）」。
   前缀里的名字是被客户@的本方业务员（如「小陈」「老周」）时，不要把他当客户。"""

QUERY_SYSTEM = """根据查询结果用中文口语回答，每条结论后必须附「依据：{source_excerpt}」。
只陈述数据表中存在的事实，不得补充表外信息。多条结果用列表，先说客户和产品，
再说状态、数量、交期，最后给一句主动建议（如临期提醒）。"""

BRIEFING_SYSTEM = """把以下聚合数据汇总成一份简洁的今日订单协同简报，中文，分条列出，不要编造数字。"""

# 状态机单一真相源：直接复用 contracts.py（避免两份定义漂移，见 BUG-11 修复）
from contracts import STATUS_ORDER, STATUS_RANK, TERMINAL_STATUSES  # noqa: E402,F401


def card_text(order: dict, tasks: list, is_new: bool) -> str:
    tag = "已登记新订单" if is_new else "已更新订单"
    lines = [
        f"✅ {tag} {order['order_id']}",
        f"📦 客户：{order['customer']}｜产品：{order['product']} {order.get('spec','')} ×{order.get('qty','?')}",
        f"💰 单价 {order.get('unit_price','待报价')} ｜ 📅 交期 {order.get('delivery_date') or '待定'} ｜ 状态：{order.get('status')}",
    ]
    if tasks:
        t = "；".join(f"{x['content']}" + (f"（{x['owner']}）" if x.get('owner') else "（待认领）") for x in tasks)
        lines.append(f"🔬 自动记下任务：{t}")
    if order.get("source_excerpt"):
        lines.append(f"📎 依据：\"{order['source_excerpt']}\"")
    return "\n".join(lines)


def query_text(order: dict, suggestion: str = "") -> str:
    base = (f"{order['customer']}的{order['product']} {order.get('spec','')} ×{order.get('qty','?')}，"
            f"当前状态{order.get('status')}，预计{order.get('delivery_date') or '待定'}交付。")
    ev = f"📎 依据：\"{order.get('source_excerpt','')}\"。" if order.get("source_excerpt") else ""
    return base + ev + (suggestion and f" {suggestion}")


def briefing_text(stats: dict) -> str:
    today = stats.get("date", "")
    lines = [f"📋 {today} 订单协同简报",
             f"🆕 新增订单：{stats['new']} 笔（{stats.get('new_customers','')}）",
             f"💰 待报价：{stats['pending_quote']} 笔（最急：{stats.get('most_urgent','无')}）",
             f"⏰ 临期交付：{stats['due_soon']} 笔（{stats.get('due_list','')}）",
             f"❗ 逾期任务：{stats['overdue']} 项（{stats.get('overdue_list','')}）"]
    return "\n".join(lines)
