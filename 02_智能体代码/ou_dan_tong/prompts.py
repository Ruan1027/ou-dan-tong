"""提示词骨架（平台无关，可直接复用）。"""

EXTRACTION_SYSTEM = """你是制造企业订单要素提取器。从聊天记录中提取所有订单线索与待办任务，
严格输出 JSON 数组，不编造缺失字段，缺失则填 null：
[{
 "type": "order" | "task",
 "intent": "new | update | append | null",
 "customer": "客户称谓（公司名，或消息中对客户的称呼如「王工」；结合上下文归属，完全无线索才填 null）", "product": "", "spec": "", "qty": null,
 "unit_price": "可含'待报价'", "delivery_date": "YYYY-MM-DD|null",
 "sample_flag": "打样|大货|无", "status_hint": "沟通中|已确认|生产中|部分交付|已交付|已取消|null",
 "task_content": "", "owner": "", "task_deadline": "YYYY-MM-DD|null",
 "source_excerpt": "支撑本结论的原文片段，≤100字"
}]
规则：
1. 只依据原文；两人对话注意上下文归属，把话术归到正确客户/产品。
2. 出现"改成/变更为/调到/加到"等动词，视为对该订单字段的【主动更新】，intent 填 update，如实抽取新值。
3. 同一订单出现两个互相矛盾的数量/交期（如一处说300一处说280）时，两条都抽出来，
   source_excerpt 分别标注，交由后台做矛盾检测，不要自行取舍。
4. 数量必为非负整数；交期必为不早于当天的 YYYY-MM-DD；若原文为负、为0、为乱码或交期早于今天，
   仍照原文抽（后台会拦截异常），不要擅自改成"合理"值。
5. 口语化日期（如「12月1号」「下周五」「月底」）按当前年份补全为 YYYY-MM-DD；确实无法推断的才填 null。
6. 缺数量或缺交期时，对应字段填 null，不要脑补。
7. **模糊量词不是数量**：「一批」「若干」「一些」「几件」「少量」这类词无法确定具体件数，
   qty 必须填 null，**绝不允许猜成 1 或其他数字**。这类消息 intent 仍按语义填 append/update/new。
8. **intent 判定优先级**（重要，决定建单方式）：
   - 客户说「再走一批 / 再排一批 / 再来一批 / 追加 / 再加 / 补一单 / 另加」等，
     表示在**原来那单之外再要一笔** → intent 填 **append**（后台会开一条追加单挂到原单下）；
   - 客户说「改成 / 加到 / 调整为 / 算了还是」等，表示**修改原来那单** → intent 填 **update**；
   - 首次提到的订单 → intent 填 **new**。
9. 客户提到「再…」但没有明说是改原单时，优先判 append，不要判 update。
   但有一个明确例外：**「跟上次一起发 / 并到原来那单里 / 跟原来那单一起」= 在原单上加量**，
   intent 填 update，不是追加。追加的标志是「另外排一批 / 分开走 / 单独一单」。
   例：「再排一批 300 个的，跟原来那单分开走」→ append；
       「再给我加 200 个，跟上次一起发」→ update（原单数量改成 5200）。
10. **分批交货时 qty 填总数量**（各批之和），不要把某一批的数量当成订单总数量。
   例：「900 个，分两批：11-01 出 500，11-03 出 400」→ qty=900，delivery_date=2026-11-03
   （全部交完的日期），source_excerpt 保留完整的批次原话。
11. **source_excerpt 必须精准保留变更关键词**（2026-10-24 应队长需求）：
    当客户用增减表达修改数量时，依据片段必须原样包含这些关键词，不得只留数字：
    - 百分比：「减少 10%」「增加 20%」「降 5 个百分点」「涨百分之三十」
    - 倍数/分数：「减半」「一半」「翻倍」「两倍」「三倍」「三分之一」
    - 增减动词：「减少到 / 减为 / 降低 / 增加 / 增加到 / 提升 / 调整为 / 调整到 / 调一下」
    - 折扣：「打九折 / 打八折 / 打半折」
    例：原文「发货量减少10%，即只发900公斤」→ source_excerpt 填
    「发货量减少10%，即只发900公斤」（含「减少10%」关键词 + 换算后绝对数 900 公斤），
    不要只填「只发900公斤」丢了「减少10%」。
12. **百分比/倍数必须换算成绝对数量**填入 qty，不要把「10%」「一半」直接当数量：
    - 「1吨减少10%」→ qty=900（单位公斤），source_excerpt 保留「减少10%」原话
    - 「500个减半」→ qty=250，source_excerpt 保留「减半」原话
    - 「200个翻倍」→ qty=400，source_excerpt 保留「翻倍」原话
    换算依据同时写进 source_excerpt，让台账「依据」一栏能看出是怎么算出来的。"""

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
# 交期同理：分批单的「该交付日」= 最晚批次日，统一走 contracts.effective_delivery_date
from contracts import (STATUS_ORDER, STATUS_RANK, TERMINAL_STATUSES,  # noqa: E402,F401
                       effective_delivery_date, valid_batches, missing_dims)


def batch_brief(order: dict) -> str:
    """分批交期的人类可读摘要（C-13）：「分 2 批：11-01×500 / 11-03×400」。

    台账 / 卡片 / 查询都必须带上这句 —— 只显示总数和最终日期，
    等于客户说的「分两批」根本没被记录，屏上完全看不出交付节奏。
    """
    bs = valid_batches(order)
    if not bs:
        return ""
    return "分 %d 批：%s" % (len(bs), " / ".join(f"{b['date'][5:]}×{b['qty']}" for b in bs))


def _status_tail(order: dict) -> str:
    """状态后缀（C-13 产品口径）：沟通中必须带出「还差哪个维度没定」。

    「沟通中」= 数/期/价至少一个没定 —— 只显示「沟通中」三个字，
    等于把「缺什么」藏起来了，用户没法据此补信息。
    """
    if order.get("status") != "沟通中":
        return ""
    dims = missing_dims(order)
    return ("（" + "、".join(dims) + "）") if dims else ""


def card_text(order: dict, tasks: list, is_new: bool) -> str:
    """订单卡片 —— 每个字段独占一行，清晰明了（2026-10-24 应队长需求）。

    旧版把「客户｜产品」「单价｜交期｜状态」挤在一行用 ｜ 分隔，移动端窄屏
    会折行错位。现拆成每项一行：客户 / 产品 / 数量 / 单价 / 交期 / 状态 / 依据。
    """
    tag = "已登记新订单" if is_new else "已更新订单"
    _note = batch_brief(order)
    lines = [
        f"✅ {tag} {order['order_id']}",
        f"👤 客户：{order['customer']}",
        f"📦 产品：{order['product']} {order.get('spec','')}",
        f"📊 数量：×{order.get('qty','?')}",
        f"💰 单价：{order.get('unit_price','待报价')}",
        f"📅 交期：{effective_delivery_date(order) or '待定'}"
        + (f"（{_note}）" if _note else ""),
        f"📌 状态：{order.get('status')}{_status_tail(order)}",
    ]
    if tasks:
        t = "；".join(f"{x['content']}" + (f"（{x['owner']}）" if x.get('owner') else "（待认领）") for x in tasks)
        lines.append(f"🔬 自动记下任务：{t}")
    if order.get("source_excerpt"):
        lines.append(f"📎 依据：\"{order['source_excerpt']}\"")
    return "\n".join(lines)


def append_card_text(order: dict) -> str:
    """追加单卡片：必须一眼看得出「这是加在原单后面的新单，原单没被改」。

    与 card_text 的区别只在于标题行和 parent 标注，其余字段口径完全一致，
    避免同一张台账出现两套话术。每项独占一行（2026-10-24 应队长需求）。
    """
    pid = order.get("parent_order_id") or ""
    head = f"📥 已登记追加单 {order['order_id']}"
    head += f"（追加自 {pid}，原单不改动）" if pid else ""
    _note = batch_brief(order)
    lines = [
        head,
        f"👤 客户：{order['customer']}",
        f"📦 产品：{order['product']} {order.get('spec','')}",
        f"📊 数量：×{order.get('qty','?')}",
        f"💰 单价：{order.get('unit_price','待报价')}",
        f"📅 交期：{effective_delivery_date(order) or '待定'}"
        + (f"（{_note}）" if _note else ""),
        f"📌 状态：{order.get('status')}{_status_tail(order)}",
    ]
    if order.get("source_excerpt"):
        lines.append(f"📎 依据：\"{order['source_excerpt']}\"")
    return "\n".join(lines)


def query_text(order: dict, suggestion: str = "") -> str:
    """查询回复 —— 每项独占一行，与卡片口径一致（2026-10-24 应队长需求）。"""
    _note = batch_brief(order)
    lines = [
        f"📦 产品：{order['customer']}的{order['product']} {order.get('spec','')}",
        f"📊 数量：×{order.get('qty','?')}",
        f"📌 状态：{order.get('status')}{_status_tail(order)}",
        f"📅 交期：预计{effective_delivery_date(order) or '待定'}交付"
        + (f"（{_note}）" if _note else ""),
    ]
    if order.get("source_excerpt"):
        lines.append(f"📎 依据：\"{order['source_excerpt']}\"。")
    if suggestion:
        lines.append(suggestion)
    return "\n".join(lines)


def briefing_text(stats: dict) -> str:
    today = stats.get("date", "")
    lines = [f"📋 {today} 订单协同简报",
             f"🆕 新增订单：{stats['new']} 笔（{stats.get('new_customers','')}）",
             f"💬 沟通中（未定价）：{stats['pending_quote']} 笔（{stats.get('pending_list','无')}）",
             f"⏰ 临期交付：{stats['due_soon']} 笔（{stats.get('due_list','')}）",
             f"❗ 逾期任务：{stats['overdue']} 项（{stats.get('overdue_list','')}）"]
    return "\n".join(lines)
