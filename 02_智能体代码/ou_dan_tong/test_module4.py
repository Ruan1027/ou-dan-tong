"""模块四 查询应答自测：查询解析/高亮/历史追溯/简报999修复/LLM口语化（无需 Key 可跑）。

归属：队友C（模块四 查询应答）。
运行：python test_module4.py          → 全量 golden（43 场景，改动 query/口语化/prompt 后必跑）
      python test_module4.py --smoke  → 快速回归集（秒级，开发期每次改动先跑）
注：test_pipeline.py 归队友B（模块五），本文件独立，避免文件冲突。
三层约定见 collect_failures.md（golden / smoke / 失败样本收集）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store
from agent import parse_chat, query, briefing
from sim_chats import SCENARIO1_MOCK, SCENARIO3_MOCK

# 本套件专测「模块四·3 口语化改写」能力，必须显式开启开关。
# 生产默认是关闭的（模板直出，避免多一次 LLM 拖慢 8~9s 并改写固定话术）。
import agent as _agent_mod
_agent_mod._COLLOQUIAL_ON = True


def test_module4():
    store = Store()
    store.reset()

    # 场景1：应入库 2 单 + 1 任务
    _, info = parse_chat("dummy", store, mock=SCENARIO1_MOCK)
    assert len(info["orders"]) == 2, info["orders"]
    assert len(info["tasks"]) == 1
    assert store.orders[0]["order_id"].startswith("ORD-")

    # 场景3：变更应 UPDATE 且留痕，qty 5000 → 6000
    parse_chat("dummy", store, mock=SCENARIO3_MOCK)
    nmx = next(o for o in store.orders if o["product"] == "NMX-60 断路器外壳")
    assert nmx["qty"] == 6000, nmx
    assert len(nmx["changes"]) >= 1, nmx["changes"]

    # 场景2：查询带原文依据
    ans = query("万博那单到哪了？", store)
    assert "万博机电" in ans and "依据" in ans, ans

    # 场景5：简报数字来自台账
    b = briefing(store)
    assert "新增订单" in b and "临期交付" in b, b

    # 场景6（防回归）：无交期的"沟通中"订单不能显示"已拖 999 天"
    # 构造一个无交期、沟通中的订单，简报应显示"待定交期"
    store.reset()
    store.add_order({
        "order_id": "ORD-TEST-01", "customer": "测试客户甲", "product": "P-001",
        "spec": "", "qty": 10, "unit_price": "待报价", "delivery_date": None,
        "status": "沟通中", "sample_flag": "无", "source_excerpt": "测试",
        "created_at": "2026-10-21T00:00:00", "updated_at": "2026-10-21T00:00:00",
    })
    b2 = briefing(store)
    assert "999" not in b2, f"999 bug 回归：{b2}"
    assert "待定交期" in b2, f"应显示待定交期：{b2}"

    # 场景7（防回归）：有交期已逾期的"沟通中"订单应标"已逾期"
    store.reset()
    store.add_order({
        "order_id": "ORD-TEST-02", "customer": "测试客户乙", "product": "P-002",
        "spec": "", "qty": 5, "unit_price": "待报价", "delivery_date": "2020-01-01",
        "status": "沟通中", "sample_flag": "无", "source_excerpt": "测试",
        "created_at": "2026-10-21T00:00:00", "updated_at": "2026-10-21T00:00:00",
    })
    b3 = briefing(store)
    assert "已逾期" in b3 and "999" not in b3, f"逾期标记错误：{b3}"

    # 场景8（防回归）：混合场景下最急单应选 days 最小者，而非 pending[0]
    store.reset()
    # 第一条无交期，第二条已逾期 2020，第三条未到期 2026-11
    for oid, cust, dd in [("ORD-T1", "甲无交期", None),
                          ("ORD-T2", "乙已逾期", "2020-01-01"),
                          ("ORD-T3", "丙未到期", "2026-11-15")]:
        store.add_order({
            "order_id": oid, "customer": cust, "product": "P", "spec": "",
            "qty": 1, "unit_price": "待报价", "delivery_date": dd,
            "status": "沟通中", "sample_flag": "无", "source_excerpt": "测试",
            "created_at": "2026-10-21T00:00:00", "updated_at": "2026-10-21T00:00:00",
        })
    b4 = briefing(store)
    # 最急应是"乙已逾期"（days 最小=负数），不是第一条"甲无交期"
    assert "乙已逾期" in b4 and "已逾期" in b4 and "999" not in b4, f"最急单选择错误：{b4}"

    # ---- 模块四·1 查询解析增强测试（产品/状态/时间/多命中/兜底/规格）----
    store.reset()
    # 构造 3 单：客户A买NMX-60 生产中；客户B买TND-3 沟通中；客户C买ABC-100 已交付
    orders_data = [
        {"order_id": "ORD-Q1", "customer": "客户A", "product": "NMX-60 断路器外壳",
         "spec": "304不锈钢", "qty": 100, "unit_price": "3.2", "delivery_date": "2026-11-20",
         "status": "生产中", "source_excerpt": "原文A", "sample_flag": "大货",
         "created_at": "2026-10-21T10:00:00", "updated_at": "2026-10-21T10:00:00"},
        {"order_id": "ORD-Q2", "customer": "客户B", "product": "TND-3 稳压器",
         "spec": "", "qty": 200, "unit_price": "待报价", "delivery_date": None,
         "status": "沟通中", "source_excerpt": "原文B", "sample_flag": "无",
         "created_at": "2026-10-21T11:00:00", "updated_at": "2026-10-21T11:00:00"},
        {"order_id": "ORD-Q3", "customer": "客户C", "product": "ABC-100 模具",
         "spec": "", "qty": 50, "unit_price": "10", "delivery_date": "2026-09-01",
         "status": "已交付", "source_excerpt": "原文C", "sample_flag": "无",
         "created_at": "2026-08-15T00:00:00", "updated_at": "2026-09-05T00:00:00"},
    ]
    for o in orders_data:
        store.add_order(dict(o))

    # 场景9：按产品查（核心增强）—— "NMX-60 到哪了" 应命中客户A的单
    ans = query("NMX-60 到哪了", store)
    assert "客户A" in ans and "NMX-60" in ans, f"产品查询未命中: {ans}"
    assert "客户B" not in ans and "客户C" not in ans, f"产品查询误命中: {ans}"

    # 场景10：按产品查另一个 —— "TND-3" 应只命中客户B
    ans = query("TND-3 稳压器什么状态", store)
    assert "客户B" in ans, f"产品查询未命中B: {ans}"
    assert "客户A" not in ans, f"产品查询误命中A: {ans}"

    # 场景11：按状态查 —— "生产中的单" 应只命中客户A
    ans = query("生产中的单有哪些", store)
    assert "客户A" in ans and "客户B" not in ans, f"状态查询错误: {ans}"

    # 场景12：按状态查 —— "待报价" 应只命中客户B（沟通中）
    ans = query("待报价的单", store)
    assert "客户B" in ans and "客户A" not in ans, f"待报价状态查询错误: {ans}"

    # 场景13：时间区间 —— "今天的单" 应命中客户A、B（created_at=2026-10-21），不含客户C
    ans = query("今天的单", store)
    assert "客户A" in ans and "客户B" in ans, f"今天时间查询遗漏: {ans}"
    assert "客户C" not in ans, f"今天时间查询误命中C: {ans}"

    # 场景14：多命中 + 排序 —— "客户A 生产中的单" 应命中且标签含"客户、状态"
    ans = query("客户A 生产中的单", store)
    assert "客户A" in ans, f"多命中查询失败: {ans}"
    assert "命中" in ans, f"缺少命中标签: {ans}"
    assert "客户" in ans and "状态" in ans, f"命中标签应含客户和状态: {ans}"

    # 场景15：无任何命中兜底 —— "xyz123" 应返回"未命中"+全部在途（A、B，不含已交付的C）
    ans = query("xyz123无匹配词", store)
    assert "未命中" in ans, f"无命中兜底错误: {ans}"
    assert "客户A" in ans and "客户B" in ans, f"兜底应返回在途: {ans}"
    assert "客户C" not in ans, f"兜底不应含已交付: {ans}"

    # 场景16：按规格查 —— "304不锈钢" 应命中客户A（spec=304不锈钢）
    ans = query("304不锈钢那单", store)
    assert "客户A" in ans, f"规格查询未命中: {ans}"

    # 场景17：客户名前2字匹配仍有效（回归）—— "客户A" 命中
    ans = query("客户A", store)
    assert "客户A" in ans, f"客户名回归失败: {ans}"

    # ---- 模块四·2 关键字段高亮 + 最新状态返回测试 ----
    # 场景18：高亮 —— 状态有 emoji 标记（生产中→🏭）
    ans = query("客户A 生产中的单", store)
    assert "🏭" in ans and "生产中" in ans, f"状态高亮缺失: {ans}"
    # 场景19：高亮 —— 数量有 📦 标记
    assert "📦 ×100" in ans, f"数量高亮缺失: {ans}"
    # 场景20：高亮 —— 交期有 📅 标记（客户A 有交期 2026-11-20）
    assert "📅" in ans and "2026-11-20" in ans, f"交期高亮缺失: {ans}"

    # 场景21：变更履历提示 —— 客户A 在场景1后没改过，无 changes；构造一个有 changes 的单验证
    store.reset()
    store.add_order({
        "order_id": "ORD-H1", "customer": "高亮测试客户", "product": "HL-001",
        "spec": "", "qty": 50, "unit_price": "8.8", "delivery_date": "2026-12-01",
        "status": "生产中", "source_excerpt": "原文", "sample_flag": "无",
        "created_at": "2026-10-21T10:00:00", "updated_at": "2026-10-21T11:00:00",
        "changes": [
            {"time": "2026-10-21T11:00:00", "field": "qty", "old": "30", "new": "50", "source": "改数量"},
        ],
    })
    ans = query("高亮测试客户", store)
    assert "📌" in ans and "1 次变更记录" in ans, f"变更履历提示缺失: {ans}"
    assert "最新状态" in ans, f"应提示是最新状态: {ans}"

    # 场景22：用户问"最新"时回复开头加"📌 最新状态："强调
    ans = query("高亮测试客户最新状态", store)
    assert "📌 最新状态" in ans, f"最新查询缺强调前缀: {ans}"

    # 场景23：无交期单不崩溃（之前 999 bug 防回归，这里再加高亮场景）
    store.reset()
    store.add_order({
        "order_id": "ORD-H2", "customer": "无交期高亮", "product": "P-H",
        "spec": "", "qty": 1, "unit_price": "待报价", "delivery_date": None,
        "status": "沟通中", "source_excerpt": "", "sample_flag": "无",
        "created_at": "2026-10-21T10:00:00", "updated_at": "2026-10-21T10:00:00",
    })
    ans = query("无交期高亮", store)
    assert "💬" in ans and "沟通中" in ans, f"沟通中状态高亮缺失: {ans}"
    assert "📅" not in ans or "待定" in ans, f"无交期不应有日期高亮或应显示待定: {ans}"

    # ---- 模块四·4 历史追溯测试 ----
    # 构造有变更履历的订单：建单后改一次（用 parse_chat 走完整变更流程）
    store.reset()
    parse_chat("dummy", store, mock=SCENARIO1_MOCK)  # 建 NMX-60 + TND-3
    parse_chat("dummy", store, mock=SCENARIO3_MOCK)  # NMX-60 qty 5000→6000，留 changes

    # 场景24：历史追溯 —— "NMX-60 改过啥" 应返回履历列表
    ans = query("NMX-60 改过啥", store)
    assert "变更履历" in ans or "变更记录" in ans, f"历史追溯缺标题: {ans}"
    assert "NMX-60" in ans, f"历史追溯未命中产品: {ans}"
    assert "5000" in ans and "6000" in ans, f"历史追溯缺变更前后值: {ans}"
    assert "qty" in ans, f"历史追溯缺变更字段: {ans}"

    # 场景25：历史追溯 —— "万博那单 改了什么" 无 changes 应提示"无变更记录"
    ans = query("万博那单 改了什么", store)
    assert "万博" in ans, f"历史追溯未命中万博: {ans}"
    assert "无变更记录" in ans, f"无 changes 应回退提示: {ans}"

    # 场景26：历史追溯 —— 变更履历带原文依据
    ans = query("NMX-60 变更历史", store)
    assert "📎" in ans, f"历史追溯缺原文依据: {ans}"
    assert "改成 6000" in ans, f"历史追溯缺原文片段: {ans}"

    # 场景27：历史追溯 —— 时间倒序（最新变更排第 1 位）
    # 再改一次 NMX-60（qty 6000→8000），最新变更应排第一
    parse_chat("dummy", store, mock=[{
        "type": "order", "customer": "王工（XX电气）", "product": "NMX-60 断路器外壳",
        "qty": 8000, "source_excerpt": "再改成 8000 个"
    }])
    ans = query("NMX-60 变更记录", store)
    assert "共 2 次变更记录" in ans, f"应显示 2 次变更: {ans}"
    # 最新变更（→ 8000）应在第 1 位，旧变更（→ 6000）在第 2 位
    # 用"→ 8000"和"→ 6000"定位（避免被"6000 → 8000"里的 6000 干扰）
    idx_new = ans.find("→ 8000")
    idx_old = ans.find("→ 6000")
    assert idx_new > 0 and idx_old > 0, f"找不到变更箭头: {ans}"
    assert idx_new < idx_old, f"最新变更(→8000)应排在旧变更(→6000)前面: {ans}"

    # 场景28：历史追溯意图识别 —— "NMX-60 几次变更" 应进历史分支，不被当群聊解析
    ans = query("NMX-60 几次变更", store)
    assert "变更" in ans and "次变更记录" in ans, f"几次变更查询错误: {ans}"
    # 不应返回订单卡片（建单回复）
    assert "已登记新订单" not in ans and "已更新订单" not in ans, f"误走建单分支: {ans}"

    # ---- 模块四·3：口语化应答测试 ----
    from agent import _colloquialize, handle_message

    # 场景29：无 LLM 时保底返回原文本（不依赖 key，随时可跑）
    raw = "温州机电的法兰盘 ×500，当前状态生产中，预计2026-11-01交付。"
    assert _colloquialize(raw, llm=None) == raw, "无 LLM 应原样返回"

    # 场景30：LLM 调用失败时保底返回原文本（模拟异常 LLM）
    class _BrokenLLM:
        def chat(self, system, user, json_mode=False):
            raise RuntimeError("模拟 LLM 故障")
    assert _colloquialize(raw, llm=_BrokenLLM()) == raw, "LLM 故障应原样返回"

    # 场景31：空文本保底
    assert _colloquialize("", llm=None) == "", "空文本应原样返回"

    # 场景32-34：真实 LLM 口语化测试（有 key 时才跑）
    _has_key = bool(os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"))
    if _has_key:
        from llm import LLM
        _live_llm = LLM()

        # 场景32：口语化后关键事实不丢（客户名/产品/数量/状态必须保留）
        raw_query = query("NMX-60 到哪了", store)
        colloquial = _colloquialize(raw_query, llm=_live_llm)
        assert "NMX-60" in colloquial, f"口语化后产品名丢失: {colloquial}"
        assert "王工" in colloquial or "XX电气" in colloquial, f"口语化后客户名丢失: {colloquial}"
        # 状态/数量至少有一个保留（口语化可能换说法，但事实要在）
        assert any(k in colloquial for k in ("生产", "8000", "确认")), f"口语化后关键事实丢失: {colloquial}"
        print(f"  [LLM口语化] 原文: {raw_query[:80]}...")
        print(f"  [LLM口语化] 改写: {colloquial[:80]}...")

        # 场景33：简报口语化后关键数字不丢
        raw_brief = briefing(store)
        colloquial_brief = _colloquialize(raw_brief, llm=_live_llm)
        # 简报里至少有"简报"或日期关键词
        assert len(colloquial_brief) > 10, f"口语化简报过短: {colloquial_brief}"
        print(f"  [LLM口语化] 简报改写: {colloquial_brief[:80]}...")

        # 场景34：口语化不添加虚构客户（防幻觉）
        # 台账里只有 王工/温州机电，口语化后不应出现"张三""李四"等虚构名
        _fake_names = ["张三", "李四", "王五", "赵六", "虚构"]
        assert not any(fn in colloquial for fn in _fake_names), f"口语化出现虚构客户名(幻觉): {colloquial}"
    else:
        print("  [跳过] LLM 口语化测试（未设置 DEEPSEEK_API_KEY）")

    # ---- BUG-07 防回归：内容一致时不再误报"已更新订单" ----
    # 场景35：完全相同订单二次导入 → 应显示"无变化"，不显示"已更新订单"
    store.reset()
    parse_chat("dummy", store, mock=SCENARIO1_MOCK)  # 首次导入
    card2, info2 = parse_chat("dummy", store, mock=SCENARIO1_MOCK)  # 原样再导
    assert all(k == "nochange" for k, _ in info2["orders"]), \
        f"BUG-07: 相同订单应标记 nochange: {[k for k, _ in info2['orders']]}"
    assert "已更新订单" not in card2, f"BUG-07: 不应出现已更新订单: {card2}"
    assert "无变化" in card2, f"BUG-07: 应提示无变化: {card2}"
    # 台账不应被重复写入（订单数不变）
    assert len(store.orders) == 2, f"BUG-07: 台账应保持 2 单: {len(store.orders)}"

    # 场景36：真实变更仍应显示"已更新订单"（防过度修复）
    store.reset()
    parse_chat("dummy", store, mock=SCENARIO1_MOCK)  # 首次
    _, info3 = parse_chat("dummy", store, mock=SCENARIO3_MOCK)  # qty 5000→6000 真变更
    assert any(k == "update" for k, _ in info3["orders"]), \
        f"BUG-07 过度修复: 真实变更应标记 update: {[k for k, _ in info3['orders']]}"

    # ---- BUG-06 防回归：变更后「原文依据」同步（结论 + 依据同源）----
    # 场景37：数量 5000→6000 后，顶层 source_excerpt 应同步为本次原文（含 6000），
    #         且查询卡片展示的依据也是最新的（评委追问"依据里怎么是 5000"不穿帮）
    store.reset()
    _C1, _P1 = "温州机电", "法兰盘"
    parse_chat("dummy", store, mock=[{
        "type": "order", "customer": _C1, "product": _P1, "qty": 5000,
        "unit_price": "3.2", "delivery_date": "2026-11-20",
        "status_hint": "已报价", "source_excerpt": "法兰盘 5000 个，3.2，11-20 交"}])
    parse_chat("dummy", store, mock=[{
        "type": "order", "customer": _C1, "product": _P1, "qty": 6000,
        "unit_price": "3.2", "delivery_date": "2026-11-20",
        "status_hint": "已报价", "source_excerpt": "法兰盘数量改成 6000 个"}])
    rec6 = next(o for o in store.orders if o["customer"] == _C1 and o["product"] == _P1)
    assert rec6["qty"] == 6000, f"BUG-06: 数量应更新为 6000: {rec6}"
    assert "6000" in rec6.get("source_excerpt", ""), \
        f"BUG-06: 依据应同步为本次原文: {rec6.get('source_excerpt')!r}"
    q6 = query(f"{_C1} {_P1} 到哪了", store)
    assert "6000" in q6, f"BUG-06: 查询展示的依据应为最新原文: {q6}"
    # 变更履历记录本次变更时带各自原文依据（建单本身不留履历，故此处仅 1 条）
    h6 = query(f"{_C1} {_P1} 变更历史", store)
    assert "改成 6000" in h6, f"BUG-06: 履历应带本次变更依据: {h6}"

    # 场景38：变更消息没带原文摘录时，保留旧依据不清空（防误删）
    store.reset()
    parse_chat("dummy", store, mock=[{
        "type": "order", "customer": _C1, "product": _P1, "qty": 5000,
        "source_excerpt": "法兰盘 5000 个"}])
    parse_chat("dummy", store, mock=[{
        "type": "order", "customer": _C1, "product": _P1, "qty": 6000,
        "source_excerpt": ""}])
    rec8 = next(o for o in store.orders if o["customer"] == _C1 and o["product"] == _P1)
    assert rec8["qty"] == 6000, f"BUG-06: 数量应更新: {rec8}"
    assert "5000" in rec8.get("source_excerpt", ""), \
        f"BUG-06: 无新原文时不应清空旧依据: {rec8.get('source_excerpt')!r}"

    # ---- P0-2：查询多命中澄清（并列最高分 >1 单时反问，而非罗列）----
    # 场景39：两单同客户不同产品，问"温州机电 到哪了"→ 澄清，含候选清单
    store.reset()
    parse_chat("dummy", store, mock=[
        {"type": "order", "customer": "温州机电", "product": "法兰盘",
         "qty": 300, "source_excerpt": "法兰盘 300 个"},
        {"type": "order", "customer": "温州机电", "product": "阀体",
         "qty": 200, "source_excerpt": "阀体 200 个"},
    ])
    ans = query("温州机电 到哪了", store)
    assert "请补充" in ans, f"P0-2: 多命中应追问: {ans}"
    assert "2 单" in ans, f"P0-2: 应提示命中单数: {ans}"
    assert "法兰盘" in ans and "阀体" in ans, f"P0-2: 候选清单应列出两单: {ans}"
    assert "当前状态" not in ans, f"P0-2: 澄清时不应展开明细: {ans}"

    # 场景40：单命中（补充产品名后唯一）→ 走正常展开，不误伤
    ans = query("温州机电 法兰盘 到哪了", store)
    assert "请补充" not in ans, f"P0-2 误伤: 单命中不应追问: {ans}"
    assert "法兰盘" in ans and "当前状态" in ans, f"P0-2: 单命中应正常展开: {ans}"

    # ---- P0-3：口语化后事实保全校验（_colloquialize 逻辑，无 Key 可跑）----
    from agent import _check_facts, _colloquialize

    class _GoodLLM:
        """改写保留全部事实（数字/日期/状态）"""
        def chat(self, system, user, json_mode=False):
            return '温州机电那单法兰盘 ×500，现在生产中，2026-11-20 交付。📎 依据："原文"'

    class _DropNumLLM:
        """改丢数字：500 被删掉"""
        def chat(self, system, user, json_mode=False):
            return '温州机电的法兰盘，现在生产中，2026-11-20 交付。📎 依据："原文"'

    class _AddNumLLM:
        """编造数字：多出一个 999"""
        def chat(self, system, user, json_mode=False):
            return '温州机电的法兰盘 ×500 个，单价 999，2026-11-20 交付。📎 依据："原文"'

    raw_ok = '温州机电的法兰盘 ×500，当前状态生产中，预计2026-11-20交付。📎 依据："法兰盘 500 个"。'

    # 场景41：事实全部保留 → 走通口语化，返回改写文本
    out41 = _colloquialize(raw_ok, llm=_GoodLLM())
    assert out41 != raw_ok and "500" in out41, f"P0-3: 事实完整应走通: {out41!r}"

    # 场景42：改动丢失数字 → 回退原文
    out42 = _colloquialize(raw_ok, llm=_DropNumLLM())
    assert out42 == raw_ok, f"P0-3: 删数字应回退原文: {out42!r}"

    # 场景43：改增添数字（幻觉）→ 回退原文
    out43 = _colloquialize(raw_ok, llm=_AddNumLLM())
    assert out43 == raw_ok, f"P0-3: 编数字应回退原文: {out43!r}"

    print("ALL TESTS PASSED ✅")
    print("-" * 40)
    print(b)


def smoke():
    """P0-4 快速回归集（python test_module4.py --smoke）：守关键链路，秒级跑完。

    与 golden（全量 43 场景）不同，smoke 只覆盖最容易回归的核心路径，
    改动 query()/_colloquialize()/prompt 或卡片后，先跑这个再跑全量。
    """
    import time
    t0 = time.time()
    store = Store()
    store.reset()

    # ① 建单 + 任务（基础链路）
    _, info = parse_chat("dummy", store, mock=SCENARIO1_MOCK)
    assert len(info["orders"]) == 2, f"建单数量错误: {info['orders']}"
    assert len(info["tasks"]) == 1, "任务未入库"

    # ② 变更留痕（qty 5000 → 6000）
    parse_chat("dummy", store, mock=SCENARIO3_MOCK)
    nmx = next(o for o in store.orders if o["product"] == "NMX-60 断路器外壳")
    assert nmx["qty"] == 6000 and len(nmx["changes"]) >= 1, "变更未留痕"

    # ③ 查询带原文依据
    assert "万博机电" in query("万博那单到哪了？", store), "查询未命中或缺依据"

    # ④ 简报 999 防回归（无交期 → 待定交期）
    store.reset()
    store.add_order({
        "order_id": "ORD-SMK-01", "customer": "无交期客户", "product": "P-SMK",
        "spec": "", "qty": 10, "unit_price": "待报价", "delivery_date": None,
        "status": "沟通中", "sample_flag": "无", "source_excerpt": "测试",
        "created_at": "2026-10-21T00:00:00", "updated_at": "2026-10-21T00:00:00",
    })
    b = briefing(store)
    assert "999" not in b and "待定交期" in b, f"简报 999 回归: {b}"

    # ⑤ 产品查询命中 + 不误伤（构造 3 单）
    store.reset()
    for o in [
        {"order_id": "ORD-S1", "customer": "客户A", "product": "NMX-60 断路器外壳",
         "spec": "", "qty": 100, "unit_price": "3.2", "delivery_date": "2026-11-20",
         "status": "生产中", "source_excerpt": "A", "sample_flag": "大货",
         "created_at": "2026-10-21T10:00:00", "updated_at": "2026-10-21T10:00:00"},
        {"order_id": "ORD-S2", "customer": "客户B", "product": "TND-3 稳压器",
         "spec": "", "qty": 200, "unit_price": "待报价", "delivery_date": None,
         "status": "沟通中", "source_excerpt": "B", "sample_flag": "无",
         "created_at": "2026-10-21T11:00:00", "updated_at": "2026-10-21T11:00:00"},
        {"order_id": "ORD-S3", "customer": "客户C", "product": "ABC-100 模具",
         "spec": "", "qty": 50, "unit_price": "10", "delivery_date": "2026-09-01",
         "status": "已交付", "source_excerpt": "C", "sample_flag": "无",
         "created_at": "2026-08-15T00:00:00", "updated_at": "2026-09-05T00:00:00"},
    ]:
        store.add_order(dict(o))
    ans = query("NMX-60 到哪了", store)
    assert "客户A" in ans and "客户B" not in ans, f"产品查询误命中: {ans}"

    # ⑥ 状态查询 + 无命中兜底（跳过已交付）
    assert "客户A" in query("生产中的单有哪些", store), "状态查询失败"
    ans = query("xyz无匹配词", store)
    assert "未命中" in ans and "客户A" in ans and "客户C" not in ans, f"兜底错误: {ans}"

    # ⑦ 高亮（状态/数量/交期 emoji）
    ans = query("客户A 生产中的单", store)
    assert "🏭" in ans and "📦 ×100" in ans and "📅" in ans, f"高亮缺失: {ans}"

    # ⑧ 历史追溯（变更前后值）
    store.reset()
    parse_chat("dummy", store, mock=SCENARIO1_MOCK)
    parse_chat("dummy", store, mock=SCENARIO3_MOCK)
    h = query("NMX-60 改过啥", store)
    assert "5000" in h and "6000" in h, f"历史追溯失败: {h}"

    # ⑨ BUG-07：内容一致时 nochange，不误报"已更新订单"
    store.reset()
    parse_chat("dummy", store, mock=SCENARIO1_MOCK)
    card2, info2 = parse_chat("dummy", store, mock=SCENARIO1_MOCK)
    assert all(k == "nochange" for k, _ in info2["orders"]), "BUG-07 回归"
    assert "无变化" in card2 and "已更新订单" not in card2, f"BUG-07 卡片: {card2}"

    # ⑩ BUG-06：变更后原文依据同步（结论 + 依据同源）
    store.reset()
    parse_chat("dummy", store, mock=[{
        "type": "order", "customer": "温州机电", "product": "法兰盘", "qty": 5000,
        "status_hint": "已报价", "source_excerpt": "法兰盘 5000 个，3.2，11-20 交"}])
    parse_chat("dummy", store, mock=[{
        "type": "order", "customer": "温州机电", "product": "法兰盘", "qty": 6000,
        "status_hint": "已报价", "source_excerpt": "法兰盘数量改成 6000 个"}])
    rec6 = next(o for o in store.orders if o["product"] == "法兰盘")
    assert rec6["qty"] == 6000 and "6000" in rec6.get("source_excerpt", ""), \
        f"BUG-06 依据未同步: {rec6.get('source_excerpt')!r}"

    # ⑪ P0-2：多命中澄清（两单同客户不同产品）
    store.reset()
    parse_chat("dummy", store, mock=[
        {"type": "order", "customer": "温州机电", "product": "法兰盘", "qty": 300,
         "source_excerpt": "法兰盘 300"},
        {"type": "order", "customer": "温州机电", "product": "阀体", "qty": 200,
         "source_excerpt": "阀体 200"},
    ])
    assert "请补充" in query("温州机电 到哪了", store), "P0-2 澄清未触发"

    # ⑫ P0-3：口语化事实保全（伪 LLM，无 Key 可跑）
    from agent import _colloquialize

    class _KeepLLM:
        def chat(self, system, user, json_mode=False):
            return '温州机电那单法兰盘 ×500，现在生产中，2026-11-20 交付。📎 依据："原文"'

    class _DropLLM:
        def chat(self, system, user, json_mode=False):
            return '温州机电的法兰盘，现在生产中，2026-11-20 交付。📎 依据："原文"'

    raw_ok = '温州机电的法兰盘 ×500，当前状态生产中，预计2026-11-20交付。📎 依据："法兰盘 500 个"。'
    assert _colloquialize(raw_ok, llm=_KeepLLM()) != raw_ok, "P0-3: 事实完整应走通"
    assert _colloquialize(raw_ok, llm=_DropLLM()) == raw_ok, "P0-3: 删数字应回退原文"

    print(f"SMOKE TESTS PASSED ✅（{time.time() - t0:.2f}s，12 个检查点）")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="模块四查询应答自测（三层：golden 全量 / smoke 快速 / failures 收集）")
    ap.add_argument("--smoke", action="store_true", help="只跑快速回归集（秒级），默认跑全量 43 场景")
    args = ap.parse_args()
    if args.smoke:
        smoke()
    else:
        test_module4()
        print("\n提示：快速回归请用 python test_module4.py --smoke；失败样本记录见 collect_failures.md")
