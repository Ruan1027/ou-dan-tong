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


def _order_fields_block(order: dict) -> list:
    """订单已知字段的逐行展示（缺失/询问卡片复用）。

    每个字段独占一行，缺的标「待补充」，有的如实展示 —— 让客户一眼看到
    当前订单已有什么、缺什么，而不是只丢一句「缺XXX」。
    """
    def _val(key, default="待补充"):
        v = order.get(key)
        if v is None or v == "" or v == "?":
            return default
        return v
    return [
        f"🔖 订单号：{order.get('order_id','待生成')}",
        f"👤 客户：{_val('customer')}",
        f"📦 产品：{_val('product')} {order.get('spec','')}",
        f"💰 单价：{_val('unit_price')}",
        f"📊 数量：×{_val('qty')}",
        f"📅 交期：{effective_delivery_date(order) or '待补充'}",
        f"📌 状态：{order.get('status','待补充')}{_status_tail(order)}",
    ]


def missing_card_text(order: dict, missing_msg: str, is_new: bool = True) -> str:
    """缺失信息卡片 —— 先展示已知字段，再询问是否按需求更改（2026-10-24 应队长需求）。

    旧版只输出「🟡 信息待补充：缺XXX」，客户看不到当前订单已有什么，
    无从判断该补哪个字段。现改为：
      1. 先逐行展示订单号/客户/产品/单价/数量/交期/状态（缺的标「待补充」）；
      2. 再列缺失项询问（validator 的 missing_msg 已写清缺什么）；
      3. 提示客户：如果要改某个字段，直接说就行，我改完再发一次确认。

    is_new=True 表示这是新建占位单（无在途单）；False 表示台账已有该单（追问补全）。
    """
    head = "🟡 信息不全已占位" if is_new else "🟡 信息待补充"
    head += f" {order.get('order_id','')}"
    lines = [head]
    lines.append("── 当前订单已知信息 ──")
    lines.extend(_order_fields_block(order))
    lines.append("── 待补充 ──")
    lines.append(missing_msg)
    lines.append("💡 如果要把某个信息改成你的要求，直接告诉我，我改完再发一次确认。")
    if is_new:
        lines.append("（已记入台账，状态=待补充，补齐后自动转正）")
    else:
        lines.append("（台账已有该单，补齐后我直接更新，不再重复建单）")
    return "\n".join(lines)


# 转人工话术（2026-10-24 应队长需求）：客户不同意台账数据时的兜底回复。
HUMAN_HANDOFF = "检测到订单存在疑问，接下来将转接人工。"


def nochange_card_text(existing: dict) -> str:
    """无变化卡片 —— 展示台账现值，说明为何没改（2026-10-24 应队长需求）。

    旧版只输出「⏸️ 无变化：数据已一致，未重复写入」，客户看到会困惑：
    「我明明要求改数量，为什么说没变化？」—— 多数是台账先前已更新成该值
    （幂等去重），但客户看不到现值无从判断。现改为：
      1. 逐行展示台账中这单的当前数据（数量/交期/状态等）；
      2. 说明：您要求的数值与台账一致，所以未重复写入；
      3. 引导：如果要改成其他数值，直接告诉我目标值即可。
    """
    lines = [f"⏸️ 无变化 {existing.get('order_id','')}"]
    lines.append("── 台账当前记录 ──")
    lines.extend(_order_fields_block(existing))
    lines.append("您提供的信息与台账记录一致，未重复写入。")
    lines.append("💡 如果要改成其他数值，直接告诉我目标值即可（如「改成 100 个」）。")
    return "\n".join(lines)


_FIELD_CN = {"qty": "数量", "unit_price": "单价", "delivery_date": "交期",
             "spec": "规格", "sample_flag": "样品", "status": "状态"}


def pending_update_card_text(existing: dict, patch: dict) -> str:
    """变更待确认卡片 —— 关键字段（数量/单价/交期）改动先确认再落库
    （2026-10-24 应队长需求：修改订单先发一遍订单信息，客户确认后再更改）。

    1. 先逐行展示订单当前信息（订单号/客户/产品/单价/数量/交期/状态）；
    2. 列出拟改动清单（字段：旧值 → 新值），让客户一眼看到要改什么；
    3. 空一行后请客户回复 是/否；确认后才落库，取消则台账保持原样。
    """
    lines = [f"🟡 变更待确认 {existing.get('order_id','')}"]
    lines.append("── 订单当前信息 ──")
    lines.extend(_order_fields_block(existing))
    lines.append("── 拟改动 ──")
    for k, v in patch.items():
        if k == "source_excerpt":
            continue
        cn = _FIELD_CN.get(k, k)
        old_v = effective_delivery_date(existing) if k == "delivery_date" else existing.get(k)
        old_v = old_v if old_v not in (None, "") else "（空）"
        lines.append(f"· {cn}：{old_v} → {v}")
    lines.append("")
    lines.append("· 回复「是」→ 确认修改，我改完再发一次订单信息；")
    lines.append("· 回复「否」→ 取消本次修改，台账保持原样。")
    lines.append("请回复 是/否")
    return "\n".join(lines)


def disambig_card_text(order: dict, index: int, total: int) -> str:
    """消歧卡片 —— 从候选订单中拿出一个问客户「是否是这个订单」。

    2026-10-24 应队长需求：信息缺失时逐个拿匹配订单询问，客户说「是」给更新后订单，
    说「否」给下一个，全部问完仍「否」→转人工。

    index/total：当前是第几个候选 / 共几个（让客户知道还有几轮）。
    """
    lines = [f"🟡 信息缺失，请确认：以下是否是您要查询/修改的订单？（第 {index}/{total} 个匹配）"]
    lines.append("── 订单数据 ──")
    lines.extend(_order_fields_block(order))
    lines.append("")
    lines.append(f"· 回复「是」→ 以此订单为准，我给您更新后的信息；")
    lines.append(f"· 回复「否」→ {'继续查找其他匹配订单' if index < total else '已无更多匹配订单，将转接人工'}")
    lines.append("请回复 是/否")
    return "\n".join(lines)


def conflict_card_text(existing: dict, conflict_msg: str) -> str:
    """冲突卡片 —— 先调出台账真实数据，再询问是否以此订单为准（2026-10-24 应队长需求）。

    旧版只输出「⚠️ 冲突待确认：数值矛盾…」，客户看不到台账里到底记的是什么，
    也无法直接确认或拒绝。现改为：
      1. 先逐行展示台账中这单的真实数据（订单号/客户/产品/单价/数量/交期/状态）；
      2. 列出矛盾点（validator 的 conflict_msg 已写清哪个字段不一致）；
      3. 询问客户：是否就是这个订单，数据以我们台账为准？
      4. 提示：如果不同意，将转接人工处理。

    流程上：客户回复同意 → 下一条消息带变更动词走正常更新或 nochange；
    客户回复不同意/拒绝 → 下一条消息仍不带变更动词命中冲突 → 触发转人工。
    """
    lines = [f"⚠️ 冲突待确认 {existing.get('order_id','')}"]
    lines.append("── 我们台账中记录的订单数据 ──")
    lines.extend(_order_fields_block(existing))
    lines.append("── 矛盾点 ──")
    lines.append(conflict_msg)
    lines.append("❓ 请确认：是否就是这个订单？数据以我们台账记录的为准。")
    lines.append("")
    lines.append("· 回复「是的 / 确认」→ 以台账数据为准，不做更改；")
    lines.append("· 回复「不是 / 不同意」→ " + HUMAN_HANDOFF)
    return "\n".join(lines)


def briefing_text(stats: dict) -> str:
    today = stats.get("date", "")
    lines = [f"📋 {today} 订单协同简报",
             f"🆕 新增订单：{stats['new']} 笔（{stats.get('new_customers','')}）",
             f"💬 沟通中（未定价）：{stats['pending_quote']} 笔（最急：{stats.get('most_urgent','无')}）",
             f"⏰ 临期交付：{stats['due_soon']} 笔（{stats.get('due_list','')}）",
             f"❗ 逾期任务：{stats['overdue']} 项（{stats.get('overdue_list','')}）"]
    return "\n".join(lines)
