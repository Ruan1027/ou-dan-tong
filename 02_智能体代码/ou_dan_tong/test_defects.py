"""模块五·3 已知缺陷登记与归责看板（队友B）。

运行：python test_defects.py

定位：模块五只做「发现—定位—归责—建议修复—回归验证」，**不越界改他人模块文件**
（《接口与模块对接约定》铁律：每人只改自己模块的文件）。因此每项缺陷都：
  1. 给出可复现探针（probe），跑一遍就能看到当前是否仍存在；
  2. 标注归属模块与责任人，附可直接套用的修复建议；
  3. 修复后探针自动转绿，无需改本文件。

本脚本为「信息性看板」，无论是否存在未修复缺陷都以 0 退出，不阻断其他测试；
但其结论会写进 `模块五_测试报告.md`，作为队长验收与分派修复的依据。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store
from agent import parse_chat, briefing, handle_message
from sim_chats import SEED_MOCK, SCENARIO1_MOCK

NOW = "2026-10-21T10:00:00"
C_WZ, P_FLANGE = "温州机电", "法兰盘"
C_LW, P_GEAR = "龙湾机械", "齿轮箱"


def fresh():
    s = Store()
    s.reset()
    return s


def _order(customer, product, qty=None, price="待报价", date=None,
           status="沟通中", spec="", excerpt=""):
    return {"type": "order", "customer": customer, "product": product, "spec": spec,
            "qty": qty, "unit_price": price, "delivery_date": date,
            "status_hint": status, "sample_flag": "无", "source_excerpt": excerpt}


def _rec(s, customer, product=None):
    for o in s.orders:
        if o["customer"] == customer and (product is None or o["product"] == product):
            return o
    return None


# ---------------------------------------------------------------------------
# 缺陷探针：fixed=True 表示行为已正确
# ---------------------------------------------------------------------------
def probe_bug01():
    """占位单补全被误判为「冲突」，补单永远无法转正。"""
    s = fresh()
    parse_chat("x", s, mock=[_order(C_LW, P_GEAR, None, "待报价", None,
                                    "沟通中", excerpt="齿轮箱再走一批")], now=NOW)
    card, _ = parse_chat("x", s, mock=[_order(C_LW, P_GEAR, 80, "待报价",
                                              "2026-11-30", "沟通中",
                                              excerpt="齿轮箱 80 个，11-30 交")], now=NOW)
    r = _rec(s, C_LW)
    fixed = bool(r) and r.get("qty") == 80 and r.get("status") != "待补充"
    return fixed, f"补全后台账：数量={r.get('qty')} 状态={r.get('status')} ｜ 卡片：{card.splitlines()[0]}"


def probe_bug02():
    """变更动词表缺「加到」，口语增量被判冲突（prompts 抽取规则里却列了「加到」）。"""
    s = fresh()
    parse_chat("x", s, mock=[_order(C_WZ, P_FLANGE, 50, "26", "2026-10-27",
                                    "已确认", excerpt="法兰盘 50 个，10-27 交")], now=NOW)
    card, _ = parse_chat("x", s, mock=[_order(C_WZ, P_FLANGE, 80, "26", "2026-10-27",
                                              "已确认", excerpt="法兰那个加到 80 个")], now=NOW)
    r = _rec(s, C_WZ)
    fixed = bool(r) and r.get("qty") == 80
    return fixed, f"增量后数量={r.get('qty')}（期望 80）｜ 卡片：{card.splitlines()[0]}"


def probe_bug03():
    """简报把「未来交期」用 abs() 说成「已拖 X 天」。"""
    s = fresh()
    handle_message("__seed__", s, llm=None, mock=SEED_MOCK)
    b = briefing(s)
    return ("已拖" not in b), b.splitlines()[2] if len(b.splitlines()) > 2 else b


def probe_bug04():
    """简报在订单无交期时输出 magic number「已拖 999 天」。"""
    s = fresh()
    s.add_order({"order_id": "ORD-TEST", "customer": C_WZ, "product": P_FLANGE, "spec": "",
                 "qty": 10, "unit_price": "待报价", "delivery_date": None,
                 "status": "沟通中", "sample_flag": "无", "source_excerpt": "",
                 "created_at": NOW, "updated_at": NOW})
    b = briefing(s)
    return ("999" not in b), b.splitlines()[2] if len(b.splitlines()) > 2 else b


def probe_bug05():
    """任务缺 owner / 缺 deadline 时静默入库，无追问话术（路线梳理明确要求）。"""
    s = fresh()
    card, _ = parse_chat("x", s, mock=[
        _order("瑞安精工", "轴承座", 120, "5.5", "2026-12-01", "沟通中",
               spec="UC205", excerpt="轴承座120个12月1号交"),
        {"type": "task", "task_content": "安排打样", "owner": "", "task_deadline": None},
    ], now=NOW)
    # 注意：「（待认领）」只是占位展示，不算追问话术，故关键词不含「认领」
    asked = any(k in card for k in ("负责人", "谁负责", "期限", "追问", "什么时候"))
    return asked, f"卡片含追问话术={asked} ｜ 末行：{card.splitlines()[-1]}"


def probe_bug06():
    """变更后原文依据未更新：数量已 6000，依据仍显示 5000（评委追问会穿帮）。"""
    s = fresh()
    parse_chat("x", s, mock=[_order(C_WZ, P_FLANGE, 5000, "3.2", "2026-11-20",
                                    "已报价", excerpt="法兰盘 5000 个，3.2，11-20 交")], now=NOW)
    parse_chat("x", s, mock=[_order(C_WZ, P_FLANGE, 6000, "3.2", "2026-11-20",
                                    "已报价", excerpt="法兰盘数量改成 6000 个")], now=NOW)
    r = _rec(s, C_WZ)
    src = str(r.get("source_excerpt", ""))
    return ("6000" in src), f"数量={r.get('qty')} ｜ 依据={src}"


def probe_bug07():
    """无任何字段变化时仍输出「✅ 已更新订单」，演示时误导。"""
    s = fresh()
    handle_message("__seed__", s, llm=None, mock=SEED_MOCK)
    card, _ = parse_chat("x", s, mock=SCENARIO1_MOCK, now=NOW)
    nmx = _rec(s, "王工（XX电气）")
    line = card.splitlines()[0] if card else ""
    fixed = not ("已更新" in card and nmx and nmx.get("qty") == 5000)
    return fixed, f"首行：{line}"


def probe_bug08():
    """数量异常偏大（5000 vs 台账 65，疑似多打零）未被判异常，被冲突判定抢先。"""
    s = fresh()
    parse_chat("x", s, mock=[_order(C_LW, "DZ47-63", 65, "26", "2026-10-24", "已确认",
                                    excerpt="DZ47 65 个")], now=NOW)
    card, _ = parse_chat("x", s, mock=[_order(C_LW, "DZ47-63", 5000, "26", "2026-10-24",
                                              "已确认", excerpt="数量写成 5000 了吧")], now=NOW)
    line = card.splitlines()[0] if card.strip() else "(空卡片)"
    return ("⛔" in card), f"判为异常={'⛔' in card} ｜ {line[:70]}"


def probe_bug09():
    """分批交期无法表达：台账交期是单值字段，「11-01 出 500 / 11-03 出 400」丢失。"""
    s = fresh()
    parse_chat("x", s, mock=[_order(C_LW, "PU 皮鞋面", 500, "12", "2026-11-01", "已确认",
                                    excerpt="PU 皮 500 米 11-01 到")], now=NOW)
    parse_chat("x", s, mock=[_order(C_LW, "PU 皮鞋面", 900, "12", "2026-11-03", "已确认",
                                    excerpt="加量改 900，得 11-03")], now=NOW)
    card, _ = parse_chat("x", s, mock=[_order(C_LW, "PU 皮鞋面", 900, "12", "2026-11-01",
                                              "已确认",
                                              excerpt="分两批：11-01 出 500，11-03 出 400")],
                         now=NOW)
    r = _rec(s, C_LW)
    # C-13 修正判据：原写法 `any(k in str(r) for k in ("batches","batch","split"))` 查的是
    # 字段【名】——store 的台账号 schema 恒含 delivery_batches 键（值为 None 也命中 "batch"），
    # 于是无论批次有没有真正解析入库，探针都返回已修复 → BUG-09 长期假绿。
    # 改为查【值】：必须是「含 date 与 qty 的非空列表」才算修好。
    batches = (r or {}).get("delivery_batches") or []
    has_batches = bool(batches) and all(
        isinstance(b, dict) and b.get("date") and b.get("qty") is not None for b in batches)
    line = card.splitlines()[0] if card.strip() else "(空卡片)"
    return has_batches, f"台账批次={batches} ｜ 交期={r and r.get('delivery_date')} ｜ {line[:50]}"


def probe_bug10():
    """任务期限只认 YYYY-MM-DD，「今天 17:00 前」这类带时刻的口语期限被置空。"""
    s = fresh()
    card, _ = parse_chat("x", s, mock=[
        {"type": "task", "task_content": "跟进物流单号", "owner": "老周",
         "task_deadline": "2026-10-21 17:00"},
    ], now=NOW)
    missed = "期限缺失" in card
    return (not missed), f"误判期限缺失={missed} ｜ {(card.strip() or '(空卡片)').splitlines()[0][:60]}"


def probe_bug11():
    """状态机缺「已取消 / 部分交付」：卡片判 ok，但状态字段不动，语义被静默吞掉。"""
    s = fresh()
    parse_chat("x", s, mock=[_order(C_LW, "轴承6204", 100, "6.0", "2026-11-10", "已确认",
                                    excerpt="轴承 100 个 11-10 交")], now=NOW)
    parse_chat("x", s, mock=[_order(C_LW, "轴承6204", 100, "6.0", "2026-11-10", "已取消",
                                    excerpt="那单先取消吧")], now=NOW)
    r = _rec(s, C_LW)
    return (r and r.get("status") == "已取消"), f"取消后状态={r and r.get('status')}（期望「已取消」）"


def probe_bug12():
    """纯任务派发（无订单）且信息齐全时，parse_chat 返回空卡片 —— 用户零反馈。"""
    s = fresh()
    card, meta = parse_chat("x", s, mock=[
        {"type": "task", "task_content": "跟进物流单号", "owner": "老周",
         "task_deadline": "2026-10-22"},
    ], now=NOW)
    has_output = bool(card and card.strip())
    return has_output, f"卡片非空={has_output} ｜ 卡片={card[:40]!r} ｜ 入库任务={len(meta.get('tasks', []))}"


def probe_bug13():
    """任务幂等缺失：同一段消息二次导入，订单去重（2→2）但任务重复入库（1→2），
    简报任务列表随之膨胀重复 —— 2026-10-23 模块五优化轮由 test_pipeline 尾段简报暴露。"""
    s = fresh()
    parse_chat("x", s, mock=SCENARIO1_MOCK, now=NOW)
    t1 = len(s.tasks)
    parse_chat("x", s, mock=SCENARIO1_MOCK, now=NOW)
    t2 = len(s.tasks)
    ids = [t.get("task_id") for t in s.tasks]
    return (t1 == t2), f"首次导入 {t1} 任务，再次导入 {t2} 任务（应相等）｜ 任务号={ids}"


def probe_regression_mock_dup():
    """回归验证：网页模拟模式「Mock 重复入库」（历史缺陷，5c507b7 已修）。"""
    s = fresh()
    handle_message("__seed__", s, llm=None, mock=SEED_MOCK)
    handle_message("x", s, llm=None, mock=SCENARIO1_MOCK)
    n1 = len(s.orders)
    handle_message("x", s, llm=None, mock=SCENARIO1_MOCK)
    n2 = len(s.orders)
    return (n1 == n2), f"首次导入后 {n1} 单，再次导入后 {n2} 单（应相等）"


DEFECTS = [
    dict(id="BUG-01", severity="🔴 高", title="占位单补全被误判「冲突」，缺失→补全闭环断掉",
         owner="模块二·8 validator.py", who="队友A",
         where="validator.classify_order 冲突分支把 status 纳入比对；"
               "来单默认状态「沟通中」与台账「待补充/已确认」互判矛盾",
         fix="冲突比对字段收敛为数值类（qty / unit_price / delivery_date），"
             "移除 status；并跳过状态为「待补充」的占位单，使其补全后直接转正为 ok。"
             "可在 contracts.py 增加 CONFLICT_FIELDS 常量统一口径。",
         probe=probe_bug01),
    dict(id="BUG-02", severity="🔴 高", title="变更动词表缺「加到/加」，口语增量误判冲突",
         owner="模块二·8 validator.py / contracts.CHANGE_VERBS", who="队友A",
         where="prompts.py 抽取规则 2 列举了「改成/变更为/调到/加到」，"
               "但 validator._CHANGE_VERB_SET 未含「加到」「加」",
         fix="把「加到」「加」「增到」「提到」补进 contracts.CHANGE_VERBS，"
             "并让 validator 从 contracts 导入（当前是本地重复定义，易漂移）。",
         probe=probe_bug02),
    dict(id="BUG-03", severity="🟠 中", title="简报把未来交期说成「已拖 X 天」",
         owner="模块四·5 agent.py briefing", who="队友C",
         where="abs(_days_to(...)) 对正数（未来）取绝对值，语义反了",
         fix="分情况：days>=0 →「距交期还有 N 天」；days<0 →「已逾期 N 天」；"
             "most_urgent 应取 days 最小者而非 pending[0]。",
         probe=probe_bug03),
    dict(id="BUG-04", severity="🟠 中", title="简报无交期时输出「已拖 999 天」",
         owner="模块四·5 agent.py briefing", who="队友C",
         where="_urgent() 对 None 返回 magic number 999",
         fix="无交期时 most_urgent 显示「待定交期」，不显示天数（路线梳理第八节已给对策）。",
         probe=probe_bug04),
    dict(id="BUG-05", severity="🟠 中", title="任务缺 owner / 缺 deadline 无追问话术",
         owner="模块二·7 validator.py", who="队友A",
         where="validate_extraction 对 task 只校验 content 非空，owner/deadline 缺失静默入库",
         fix="新增 classify_task：缺 owner → 追问「这项谁负责？」；缺 deadline → "
             "追问「什么时候要？」，与订单 missing 同样落地 flags.missing。",
         probe=probe_bug05),
    dict(id="BUG-06", severity="🟡 低", title="变更后原文依据未同步，数量 6000 依据仍写 5000",
         owner="模块三 store.apply_change / 模块四 query", who="队友D / 队友C",
         where="apply_change 只 patch 业务字段，未更新 source_excerpt",
         fix="apply_change 有变更时同步把 source_excerpt 更新为本次原文片段，"
             "保证「结论 + 依据」同源。",
         probe=probe_bug06),
    dict(id="BUG-07", severity="🟡 低", title="无任何字段变化仍输出「✅ 已更新订单」",
         owner="模块四·2 agent.py parse_chat", who="队友C",
         where="patch 为空时仍 append((\"update\", existing)) 并生成卡片",
         fix="patch 为空时输出「无需变更，台账已是最新」或直接跳过卡片。",
         probe=probe_bug07),
    # —— 2026-10-22 走查扩到模块一 v1.1 全集（22 组 / 10 类）后新登记 ——
    dict(id="BUG-08", severity="🟠 中",
         title="数量异常偏大未判异常：冲突判定抢先，异常规则缺「暴涨 N 倍」检测",
         owner="模块二·8 validator.py", who="队友A",
         where="classify_order 先比对台账数值命中 conflict 分支即返回，"
               "未给 abnormal 规则（数量暴涨/多打零）留出前置判定；"
               "当前异常规则仅覆盖负数与交期倒挂",
         fix="在 classify_order 中让异常判定优先于冲突判定；"
             "异常规则增补「数量 ≥ 台账 10 倍（或 > 单笔经验上限）」，"
             "命中即 ⛔ 拦截并要求澄清，而不是降级为冲突。",
         probe=probe_bug08),
    dict(id="BUG-09", severity="🟡 低", title="分批交期无法表达，多批次口径被判冲突",
         owner="模块三 store.py 字段 / 模块二·8 validator.py", who="队友D / 队友A",
         where="OrderRecord.delivery_date 是单值字段，"
               "「11-01 出 500 / 11-03 出 400」只能存一个日期，另一批丢失；"
               "回填报春交期时与台账打架被判冲突",
         fix="契约 4 增加可选字段 delivery_batches: list[{date, qty}]，"
             "有分批时冲突比对按批次聚合数量后判断，卡片展示「分两批：…」。",
         probe=probe_bug09),
    dict(id="BUG-10", severity="🟠 中", title="任务期限不支持时刻，口语期限被置空",
         owner="模块二·7 validator.py validate_extraction", who="队友A",
         where="DATE_RE = ^\\d{4}-\d{2}-\d{2}$ 不接受「2026-10-21 17:00」，"
               "task_deadline 直接被置 None → classify_task 恒判「期限缺失」",
         fix="放宽 DATE_RE 支持可选时刻（YYYY-MM-DD[ HH:MM]），"
             "或在抽取侧把「今天下班前 / 17:00 前」归一化为日期+时刻两字段；"
             "至少保证带时刻的期限不被静默丢弃。",
         probe=probe_bug10),
    dict(id="BUG-11", severity="🟠 中",
         title="状态机缺「已取消 / 部分交付」，语义被静默吞掉（卡片仍判 ok）",
         owner="模块二 contracts.STATUS_ORDER / 模块三 store.py", who="队友A / 队友D",
         where="STATUS_RANK 仅 5 态，无「已取消」「部分交付」；"
               "apply_change 对未知状态因 rank 取 0 而不推进，卡片仍输出「✅ 已更新订单」",
         fix="STATUS_ORDER 增补「已取消」「部分交付」两态并给出 rank 位置；"
             "部分交付需配合字段 delivered_qty / remain_qty（契约 4 只加不改）。",
         probe=probe_bug11),
    dict(id="BUG-12", severity="🟠 中", title="纯任务派发且信息齐全时返回空卡片，用户零反馈",
         owner="模块四·2 agent.py parse_chat", who="队友C",
         where="out（订单）为空时 first_new=None，for 循环不生成任何卡片；"
               "任务入库成功但界面无输出 —— 演示「任务派发」场景会静默",
         fix="orders 为空而 tasks 非空时，单独生成任务卡片（✅ 已登记任务：…负责人/期限），"
             "保证每条任务都有可见回执。",
         probe=probe_bug12),
    dict(id="BUG-13", severity="🟠 中", title="任务幂等缺失：同一消息二次导入，任务重复入库",
         owner="模块三 store.add_task / 模块四 agent.py parse_chat", who="队友D / 队友C",
         where="订单侧有主键去重（同单二次导入不新增，REG-01 已把关），任务侧无任何去重："
               "同一段聊天二次导入，订单 2→2 不变，任务 1→2 且每次新发 TSK 号；"
               "简报「逾期任务/待办」列表随之重复膨胀（test_pipeline 尾段简报实测可见）。",
         fix="建议：add_task 前按 (content, owner, deadline, order_id) 判重，"
             "命中既有任务时跳过或转为更新，与订单幂等口径对齐。",
         probe=probe_bug13),
]

REGRESSIONS = [
    dict(id="REG-01", title="网页模拟模式 Mock 重复入库（历史缺陷）",
         owner="模块六 web_app.py", who="队长/模块六",
         note="5c507b7 已用 seed + 关键词路由修复，此处做回归把关",
         probe=probe_regression_mock_dup),
]


def run():
    print("=" * 78)
    print("模块五·3 已知缺陷看板（模块五只登记归责，不越界改他人模块文件）")
    print("=" * 78)
    open_cnt = 0
    print("\n[未修复缺陷]")
    for d in DEFECTS:
        fixed, evidence = d["probe"]()
        flag = "✅ 已修复" if fixed else "⚠️ 未修复"
        if not fixed:
            open_cnt += 1
        print(f"\n  {flag} {d['id']} [{d['severity']}] {d['title']}")
        print(f"      归属：{d['owner']}（{d['who']}）")
        print(f"      根因：{d['where']}")
        print(f"      证据：{evidence}")
        if not fixed:
            print(f"      建议：{d['fix']}")

    print("\n[回归把关]")
    for r in REGRESSIONS:
        fixed, evidence = r["probe"]()
        print(f"  {'✅ 通过' if fixed else '❌ 回归失败'} {r['id']} {r['title']}"
              f"（{r['owner']}）｜ {evidence}")

    print(f"\n合计：缺陷 {len(DEFECTS)} 项，未修复 {open_cnt} 项；回归 {len(REGRESSIONS)} 项。")
    print("→ 明细与验收结论见 模块五_测试报告.md")
    return open_cnt


if __name__ == "__main__":
    run()
