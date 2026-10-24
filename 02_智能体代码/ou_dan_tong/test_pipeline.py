"""模块五·1 管线自测：用 Mock 抽取验证 入库/变更/查询/简报 全链路（无需 Key）。

归属：队友B（模块五 测试验证）。运行：python test_pipeline.py

覆盖范围（在原有 5 条断言基础上扩展为 11 组、共 30+ 条断言）：
  T1  新建入库：订单/任务落库、单号格式
  T2  变更更新：命中已有单（不重复建单）+ 留痕字段完整
  T3  查询应答：命中客户 + 附原文依据
  T4  日报简报：四类统计均有输出
  T5  迭代去重：同一单二次导入不新增记录
  T6  冲突检测：矛盾值不覆盖，台账值保持不变 + flags.conflict 可见
  T7  异常拦截：负数量不写入，台账条数与值均不变
  T8  缺失占位：登记占位单（待补充）+ flags.missing 可见
  T9  状态只进不退：低阶状态不覆盖台账高阶状态
  T10 任务入库：与订单关联，写入台账
  T11 动态演进：多轮连续变更，履历累积且查询返回最新状态

说明：已知缺陷（不属于本次断言范围）统一登记在 `test_defects.py`，
避免把他人模块的未完成项误判为管线回归。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store, _valid_batches, _invalid_batches
from store import effective_delivery_date as store_effective_date
# 批次交期口径以 contracts 为准（A-2/validator 实际消费的那份）；
# store 侧同名函数是运行时实现，②' 断言两者必须一致。
from contracts import effective_delivery_date
from agent import parse_chat, query, briefing
from sim_chats import (SCENARIO1_MOCK, SCENARIO3_MOCK, SEED_MOCK,
                       SCENARIO_CONFLICT_MOCK, SCENARIO_ABNORMAL_MOCK,
                       SCENARIO_MISSING_MOCK)

NOW = "2026-10-21T10:00:00"


# ---------------------------------------------------------------------------
# 轻量断言器：收集全部失败后再抛出，一次运行看清所有问题
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def ok(self, cond, msg, evidence=""):
        if cond:
            self.passed += 1
        else:
            self.failed.append(f"{msg}" + (f" ｜ 实际：{evidence}" if evidence else ""))
        return bool(cond)

    def section(self, name):
        print(f"  ── {name}")

    def report(self):
        if self.failed:
            print(f"\n❌ {len(self.failed)} 条断言未通过（通过 {self.passed} 条）：")
            for f in self.failed:
                print(f"   · {f}")
            raise AssertionError(f"test_pipeline 失败 {len(self.failed)} 条")
        print(f"\n✅ test_pipeline：{self.passed} 条断言全部通过")


def _order(**kw):
    base = {"type": "order", "customer": "温州机电", "product": "法兰盘", "spec": "",
            "qty": None, "unit_price": "待报价", "delivery_date": None,
            "status_hint": "沟通中", "sample_flag": "无", "source_excerpt": ""}
    base.update(kw)
    return base


def test_pipeline():
    ck = Checker()
    store = Store()
    store.reset()

    # ---------------- T1 新建入库 ----------------
    ck.section("T1 新建入库")
    _, info = parse_chat("dummy", store, mock=SCENARIO1_MOCK, now=NOW)
    ck.ok(len(info["orders"]) == 2, "T1 场景1 应入库 2 单", info["orders"])
    ck.ok(len(info["tasks"]) == 1, "T1 场景1 应入库 1 任务")
    ck.ok(store.orders[0]["order_id"].startswith("ORD-"), "T1 订单号应为 ORD- 前缀",
          store.orders[0]["order_id"])
    ck.ok(store.tasks[0]["task_id"].startswith("TSK-"), "T1 任务号应为 TSK- 前缀")

    # ---------------- T2 变更更新 ----------------
    ck.section("T2 变更更新与留痕")
    parse_chat("dummy", store, mock=SCENARIO3_MOCK, now=NOW)
    nmx = next(o for o in store.orders if o["product"] == "NMX-60 断路器外壳")
    ck.ok(nmx["qty"] == 6000, "T2 变更应 UPDATE 为 6000", nmx["qty"])
    ck.ok(len(nmx["changes"]) >= 1, "T2 变更应留痕", nmx.get("changes"))
    ch = nmx["changes"][-1]
    for k in ("time", "field", "old", "new", "source"):
        ck.ok(k in ch, f"T2 履历应含字段 {k}", list(ch))
    ck.ok("5000" in str(ch["old"]) and "6000" in str(ch["new"]),
          "T2 履历应记录 5000→6000", ch)

    # ---------------- T3 查询应答 ----------------
    ck.section("T3 查询应答")
    ans = query("万博那单到哪了？", store)
    ck.ok("万博机电" in ans, "T3 查询应命中客户", ans)
    ck.ok("依据" in ans, "T3 查询应附原文依据", ans)

    # ---------------- T4 日报简报 ----------------
    ck.section("T4 日报简报")
    b = briefing(store)
    for key in ("新增订单", "沟通中（未定价）", "临期交付", "逾期任务"):
        ck.ok(key in b, f"T4 简报应含「{key}」", b)

    # ---------------- T5 迭代去重 ----------------
    ck.section("T5 迭代去重（不重复建单）")
    n_before = len(store.orders)
    parse_chat("dup", store, mock=SCENARIO1_MOCK, now=NOW)
    ck.ok(len(store.orders) == n_before, "T5 同一单二次导入不应新增记录",
          f"{n_before}→{len(store.orders)}")

    # ---------------- T6 冲突检测 ----------------
    ck.section("T6 冲突检测（不覆盖 + 标记可见）")
    s = Store(); s.reset()
    parse_chat("x", s, mock=SEED_MOCK, now=NOW)
    n_before = len(s.orders)  # 不硬编码，容忍 SEED_MOCK 演示在途单数量变化
    base = next(o for o in s.orders if o["customer"] == "温州机电")
    qty_before = base["qty"]
    card, _ = parse_chat("x", s, mock=SCENARIO_CONFLICT_MOCK, now=NOW)
    after = next(o for o in s.orders if o["customer"] == "温州机电")
    ck.ok("冲突" in card, "T6 冲突应有可见卡片话术", card)
    ck.ok(after["qty"] == qty_before, "T6 冲突不应覆盖台账数量",
          f"{qty_before}→{after['qty']}")
    ck.ok(after["delivery_date"] == "2026-11-25", "T6 冲突不应覆盖台账交期",
          after["delivery_date"])
    ck.ok("conflict" in (after.get("flags") or {}), "T6 台账应可见 flags.conflict",
          after.get("flags"))
    ck.ok(len(s.orders) == n_before, "T6 冲突不应新增订单", len(s.orders))

    # ---------------- T7 异常拦截 ----------------
    ck.section("T7 异常拦截（不写入）")
    n_before = len(s.orders)
    qty_before = after["qty"]
    card, _ = parse_chat("x", s, mock=SCENARIO_ABNORMAL_MOCK, now=NOW)
    cur = next(o for o in s.orders if o["customer"] == "温州机电")
    ck.ok("异常" in card or "⛔" in card, "T7 异常应有可见拦截话术", card)
    ck.ok(len(s.orders) == n_before, "T7 异常不应新增订单", len(s.orders))
    ck.ok(cur["qty"] == qty_before, "T7 异常不应写入负数量", cur["qty"])

    # ---------------- T8 缺失占位 ----------------
    ck.section("T8 缺失占位（待补充 + 追问 + 已知字段展示）")
    s2 = Store(); s2.reset()
    card, _ = parse_chat("x", s2, mock=SCENARIO_MISSING_MOCK, now=NOW)
    ck.ok("缺失" in card or "不全" in card or "🟡" in card,
          "T8 缺失应有可见追问话术", card)
    ph = next((o for o in s2.orders if o["customer"] == "龙湾机械"), None)
    ck.ok(ph is not None, "T8 缺失应登记占位单", [o["customer"] for o in s2.orders])
    if ph:
        ck.ok(ph["status"] == "待补充", "T8 占位单状态应为「待补充」", ph["status"])
        ck.ok("missing" in (ph.get("flags") or {}), "T8 台账应可见 flags.missing",
              ph.get("flags"))
    # 2026-10-24 应队长需求：缺失卡片必须先展示已知字段再询问
    ck.ok("订单号" in card, "T8 缺失卡片应展示订单号", card[:80])
    ck.ok("客户" in card, "T8 缺失卡片应展示客户字段", card[:80])
    ck.ok("产品" in card, "T8 缺失卡片应展示产品字段", card[:80])
    ck.ok("单价" in card, "T8 缺失卡片应展示单价字段", card[:80])
    ck.ok("数量" in card, "T8 缺失卡片应展示数量字段", card[:80])
    ck.ok("交期" in card, "T8 缺失卡片应展示交期字段", card[:80])
    ck.ok("改成" in card or "告诉我" in card,
          "T8 缺失卡片应询问客户是否要更改某字段", card[-80:])

    # ---------------- T9 状态只进不退 ----------------
    ck.section("T9 状态机只进不退")
    s3 = Store(); s3.reset()
    parse_chat("x", s3, mock=[_order(customer="温州机电", product="法兰盘", qty=300,
               unit_price="8.0", delivery_date="2026-11-25", status_hint="生产中",
               source_excerpt="法兰盘300个，11-25交")], now=NOW)
    parse_chat("x", s3, mock=[_order(customer="温州机电", product="法兰盘", qty=300,
               unit_price="8.0", delivery_date="2026-11-25", status_hint="沟通中",
               source_excerpt="法兰盘那单还是沟通中")], now=NOW)
    cur = s3.orders[0]
    ck.ok(cur["status"] == "生产中", "T9 低阶状态不应回退高阶状态", cur["status"])

    # ---------------- T10 任务入库 ----------------
    ck.section("T10 任务入库与关联")
    s4 = Store(); s4.reset()
    parse_chat("x", s4, mock=[
        _order(customer="瑞安精工", product="轴承座", spec="UC205", qty=120,
               unit_price="5.5", delivery_date="2026-12-01", status_hint="沟通中",
               source_excerpt="轴承座UC205 120个 12月1号交"),
        {"type": "task", "task_content": "安排打样 20 个", "owner": "李经理",
         "task_deadline": "2026-10-22"},
    ], now=NOW)
    ck.ok(len(s4.tasks) == 1, "T10 任务应入库", len(s4.tasks))
    ck.ok(s4.tasks[0]["owner"] == "李经理", "T10 任务应保留 owner", s4.tasks[0])
    ck.ok(s4.tasks[0].get("order_id", "").startswith("ORD-"),
          "T10 任务应关联订单号", s4.tasks[0].get("order_id"))

    # ---------------- T11 动态演进 ----------------
    ck.section("T11 动态演进（多轮变更 → 查询返回最新）")
    s5 = Store(); s5.reset()
    for qty, date, excerpt in [
        (400, "2026-11-09", "Y2-132 电机壳 400 个，11-09 交"),
        (500, "2026-11-05", "电机壳改 500 个，交期赶 11 月 5 号"),
        (500, "2026-11-08", "交期改成 11-08 先出 300，11-15 出 200"),
    ]:
        parse_chat("x", s5, mock=[_order(customer="王总（永嘉精密机械）",
                   product="电机壳 Y2-132", qty=qty, delivery_date=date,
                   status_hint="已确认", source_excerpt=excerpt)], now=NOW)
    motor = s5.orders[0]
    ck.ok(motor["qty"] == 500, "T11 演进后数量应为最新 500", motor["qty"])
    ck.ok(motor["delivery_date"] == "2026-11-08", "T11 演进后交期应为最新 11-08",
          motor["delivery_date"])
    ck.ok(len(motor.get("changes", [])) >= 2, "T11 演进应累积多条履历",
          len(motor.get("changes", [])))
    ck.ok(len(s5.orders) == 1, "T11 演进全程只应有 1 单", len(s5.orders))
    ans = query("电机壳那单到哪了", s5)
    ck.ok("500" in ans and "2026-11-08" in ans, "T11 查询应返回最新状态而非旧版", ans)

    # ---------------- T12 新场景类型不破坏契约 ----------------
    # 模块一 v1.1 新增「取消订单 / 部分交付 / 任务派发 / 价格报价」四类。
    # 这些语义当前尚未落地（见 BUG-11/12），但**管线本身不能崩、不能污染台账**：
    # 消息能进、记录不重复、主键不漂移。语义落地与否由 test_defects 探针把关。
    ck.section("T12 新场景类型管线健壮性（取消/部分交付/报价）")
    s6 = Store()
    s6.reset()
    parse_chat("x", s6, mock=[_order(customer="陈老板（瑞安汽配）", product="轴承6204",
               qty=100, unit_price="6.0", delivery_date="2026-11-10",
               status_hint="已确认", source_excerpt="轴承 100 个 11-10 交")], now=NOW)
    parse_chat("x", s6, mock=[_order(customer="陈老板（瑞安汽配）", product="轴承6204",
               qty=100, unit_price="6.0", delivery_date="2026-11-10",
               status_hint="已取消", source_excerpt="那单先取消吧")], now=NOW)
    ck.ok(len(s6.orders) == 1, "T12 取消订单不应新建记录（主键不漂移）", len(s6.orders))
    ck.ok(s6.orders[0]["customer"] == "陈老板（瑞安汽配）", "T12 取消后客户主键不变",
          s6.orders[0]["customer"])

    parse_chat("x", s6, mock=[_order(customer="陈老板（瑞安汽配）", product="轴承6204",
               qty=100, unit_price="6.0", delivery_date="2026-11-10",
               status_hint="部分交付", source_excerpt="先发了 40 个")], now=NOW)
    ck.ok(len(s6.orders) == 1, "T12 部分交付不应新建记录", len(s6.orders))

    s7 = Store()
    s7.reset()
    parse_chat("x", s7, mock=[_order(customer="阿强", product="304不锈钢法兰盖",
               qty=None, unit_price="待报价", delivery_date=None,
               status_hint="沟通中", source_excerpt="先问下价，数量没定")], now=NOW)
    parse_chat("x", s7, mock=[_order(customer="阿强", product="304不锈钢法兰盖",
               qty=200, unit_price="9.8", delivery_date=None,
               status_hint="沟通中", source_excerpt="先 200 个，单价 9.8 锁死")], now=NOW)
    ck.ok(len(s7.orders) == 1, "T12 报价闭环（询价→报价）只应有 1 单", len(s7.orders))
    ck.ok(s7.orders[0]["qty"] == 200, "T12 报价后数量应落到 200", s7.orders[0].get("qty"))
    ck.ok(str(s7.orders[0]["unit_price"]) == "9.8", "T12 报价后单价应落到 9.8",
          s7.orders[0].get("unit_price"))

    # ---------------- T13 纯任务派发不静默 ----------------
    # BUG-12：orders 为空时 parse_chat 返回空卡片。此处只断言「任务确实入库」，
    # 卡片可见性由 test_defects.BUG-12 探针把关，避免断言与缺陷状态耦合。
    ck.section("T13 纯任务派发入库")
    s8 = Store()
    s8.reset()
    _, meta8 = parse_chat("x", s8, mock=[
        {"type": "task", "task_content": "跟进物流单号", "owner": "老周",
         "task_deadline": "2026-10-22"}], now=NOW)
    ck.ok(len(s8.tasks) == 1, "T13 任务应入库", len(s8.tasks))
    ck.ok(s8.tasks[0]["task_id"].startswith("TSK-"), "T13 任务号格式应为 TSK-*",
          s8.tasks[0].get("task_id"))
    ck.ok(s8.tasks[0]["owner"] == "老周", "T13 任务应保留负责人", s8.tasks[0].get("owner"))
    # BUG-13 晋升（2026-10-24，探针已转绿 -> 硬断言）：任务与订单同口径幂等——
    # 同一段消息二次导入，任务不得重复入库、任务号不得漂移。
    # 此前只挂在 test_defects 探针（退出码 0 不阻断），回归会悄悄漏掉。
    _t13_id = s8.tasks[0]["task_id"]
    parse_chat("x", s8, mock=[
        {"type": "task", "task_content": "跟进物流单号", "owner": "老周",
         "task_deadline": "2026-10-22"}], now=NOW)
    ck.ok(len(s8.tasks) == 1, "T13 任务幂等：同消息二次导入不新增任务",
          [t.get("task_id") for t in s8.tasks])
    ck.ok(s8.tasks[0]["task_id"] == _t13_id, "T13 任务幂等：任务号不漂移",
          f"{_t13_id} -> {s8.tasks[0]['task_id']}")

    # ---------------- T14 批次交期契约（D-1/D-3，PR #14 合入） ----------------
    # 批次交期的**单一真相源**是 contracts.effective_delivery_date —— A-2（validator 冲突比对）
    # 实际消费的就是这一份；store 侧同名函数是运行时用的另一份。
    # 模块五两扇门一起锁：口径若被改动、或两份实现漂移，T14 立刻红。
    ck.section("T14 批次交期契约（effective_delivery_date）")
    # ① 无批次：回退单值 delivery_date
    ck.ok(effective_delivery_date({"delivery_date": "2026-11-03"}) == "2026-11-03",
          "T14 无批次时应回退 delivery_date")
    # ② 有批次：取最晚批次日期（全部批次交完才算交完）
    o_b = {"delivery_date": "2026-11-01",
           "delivery_batches": [{"qty": 500, "date": "2026-11-01"},
                                {"qty": 400, "date": "2026-11-03"}]}
    ck.ok(effective_delivery_date(o_b) == "2026-11-03",
          "T14 有批次时应取最晚批次日期（11-03），而非单值字段（11-01）",
          effective_delivery_date(o_b))
    # ②' 两份实现必须同口径：contracts（A-2 用）与 store（运行时用）不得漂移
    ck.ok(store_effective_date(o_b) == effective_delivery_date(o_b),
          "T14 contracts 与 store 两份 effective_delivery_date 口径必须一致",
          f"store={store_effective_date(o_b)} contracts={effective_delivery_date(o_b)}")
    # ③ 非法批次被剔除：假日期 2026-13-01（日历上不存在）不得参与
    o_bad = {"delivery_date": "2026-11-03",
             "delivery_batches": [{"qty": 500, "date": "2026-13-01"},
                                  {"qty": 400, "date": "2026-11-02"}]}
    ck.ok(effective_delivery_date(o_bad) == "2026-11-02",
          "T14 假日期批次（2026-13-01）应被剔除，取合法批次最晚值",
          effective_delivery_date(o_bad))
    ck.ok(len(_invalid_batches(o_bad)) == 1,
          "T14 _invalid_batches 应诊断出被剔除的条目及原因",
          _invalid_batches(o_bad))
    # ④ 批次全非法：回退单值字段，不得返回 None/抛异常
    o_allbad = {"delivery_date": "2026-11-05",
                "delivery_batches": [{"qty": -1, "date": "2026-11-01"},
                                     {"qty": "五百", "date": "2026-11-02"}]}
    ck.ok(effective_delivery_date(o_allbad) == "2026-11-05",
          "T14 批次全非法（负数量/非整数）应回退 delivery_date",
          effective_delivery_date(o_allbad))
    ck.ok(_valid_batches(o_allbad) == [],
          "T14 批次全非法时 _valid_batches 应为空列表")
    # ⑤ 全空：返回 None（不抛异常）
    ck.ok(effective_delivery_date({}) is None,
          "T14 无任何交期信息应返回 None 而非抛异常")
    # ⑥ 全链路：分批消息落库后，台账单**必须真的带批次**，且有效交期取最晚批次。
    #    关键：必须先断言 delivery_batches 非空 —— 否则一旦批次没落库，
    #    effective 会回退 delivery_date，只要 mock 的 delivery_date 恰好等于最晚批次日，
    #    这条断言就会「靠回退蒙对」而永远绿（v1.7 原写法即此问题）。
    #    走两步也贴合真实场景：首单给单值交期 → 客户后来说要分批改期。
    #    注意用「分批」而非「分两批」：后者当前不被 _parse_batches 识别（见 C-13）。
    s9 = Store()
    s9.reset()
    parse_chat("x", s9, mock=[_order(customer="阿珍（鹿城鞋材）", product="PU 皮鞋面",
               qty=900, delivery_date="2026-11-05", status_hint="已确认",
               source_excerpt="PU 皮鞋面 900 个，11-05 交")], now=NOW)
    parse_chat("x", s9, mock=[_order(customer="阿珍（鹿城鞋材）", product="PU 皮鞋面",
               qty=900, delivery_date="2026-11-03", status_hint="已确认",
               source_excerpt="改成 11-03 交，分批：11-01 出 500，11-03 出 400")], now=NOW)
    rec9 = next((o for o in s9.orders if "PU 皮鞋面" in str(o.get("product", ""))), None)
    ck.ok(rec9 is not None and rec9.get("delivery_batches"),
          "T14 全链路：分批消息必须落库 delivery_batches（空则该断言失去意义）",
          rec9 and rec9.get("delivery_batches"))
    ck.ok(rec9 is not None and effective_delivery_date(rec9) == "2026-11-03",
          "T14 全链路：分批单有效交期应为最晚批次 11-03",
          rec9 and effective_delivery_date(rec9))

    # ---- T15 卡片换行守卫（2026-10-24 应队长需求）----
    # 每个字段独占一行，旧版「客户｜产品」「单价｜交期｜状态」挤一行用 ｜ 分隔，
    # 移动端窄屏折行错位。守卫：拆行后每行不得同时含两个不同字段标签。
    ck.section("T15 卡片换行：每项字段独占一行")
    from prompts import card_text, append_card_text, query_text
    _o15 = {"order_id": "ORD-TEST-15", "customer": "测试客户", "product": "测试产品",
            "spec": "A1", "qty": 100, "unit_price": "9.9", "delivery_date": "2026-11-01",
            "status": "已确认", "sample_flag": "无",
            "source_excerpt": "减少10%即90个"}
    _card = card_text(_o15, [], is_new=False)
    _lines = [ln for ln in _card.splitlines() if ln.strip()]
    # 每行最多含 1 个字段标签（👤📦📊💰📅📌📎），不得把两个不同字段挤一行
    _labels = ["👤", "📦", "📊", "💰", "📅", "📌", "📎"]
    _bad_lines = [ln for ln in _lines if sum(1 for lb in _labels if lb in ln) > 1]
    ck.ok(not _bad_lines, "T15 卡片每行不得挤两个以上字段标签", _bad_lines)
    ck.ok(any("👤 客户" in ln for ln in _lines), "T15 含「客户」独立行", _card[:80])
    ck.ok(any("📦 产品" in ln for ln in _lines), "T15 含「产品」独立行", _card[:80])
    ck.ok(any("📊 数量" in ln for ln in _lines), "T15 含「数量」独立行", _card[:80])
    ck.ok(any("💰 单价" in ln for ln in _lines), "T15 含「单价」独立行", _card[:80])
    ck.ok(any("📅 交期" in ln for ln in _lines), "T15 含「交期」独立行", _card[:80])
    ck.ok(any("📌 状态" in ln for ln in _lines), "T15 含「状态」独立行", _card[:80])
    ck.ok(any("📎 依据" in ln for ln in _lines), "T15 含「依据」独立行", _card[:80])
    # query_text 同口径
    _q = query_text(_o15)
    _qlines = [ln for ln in _q.splitlines() if ln.strip()]
    ck.ok(len(_qlines) >= 3, "T15 查询回复也按字段换行（≥3行）", _q[:80])

    # ---- T16 变更关键词契约守卫（2026-10-24 应队长需求 + BUG-14）----
    # CHANGE_VERBS 必须覆盖「减少/调整/增加」系动词，否则「发货量减少10%」
    # 即使抽取正确给出 900 也会被判冲突拦截不落库。
    ck.section("T16 变更关键词契约：减少/调整/增加 系动词必须命中")
    from contracts import CHANGE_VERBS, RATIO_KEYWORDS
    for v in ("减少", "减少到", "调整", "调整为", "增加", "增加到"):
        ck.ok(v in CHANGE_VERBS, f"T16 CHANGE_VERBS 必须含「{v}」", CHANGE_VERBS)
    # 百分比/倍数量词触发词
    for kw in ("百分之", "%", "一半", "几倍", "翻倍"):
        ck.ok(kw in RATIO_KEYWORDS, f"T16 RATIO_KEYWORDS 必须含「{kw}」", RATIO_KEYWORDS)
    # 受控实验：预置 1000，发「减少10%即900公斤」抽取行，必须落库 900 且无冲突
    s16 = Store()
    s16.reset()
    parse_chat("x", s16, mock=[_order(customer="温州机电", product="法兰盘",
                                       qty=1000, unit_price="18.5",
                                       delivery_date="2026-10-28",
                                       status_hint="已确认",
                                       source_excerpt="1吨 10-28交")], now=NOW)
    _c16, _ = parse_chat("x", s16, mock=[_order(customer="温州机电", product="法兰盘",
                                                 qty=900, unit_price="18.5",
                                                 delivery_date="2026-10-28",
                                                 status_hint="已确认",
                                                 source_excerpt="发货量减少10%，即只发900公斤")],
                         now=NOW)
    _r16 = s16.orders[0] if s16.orders else {}
    ck.ok(_r16.get("qty") == 900, "T16 减少10%后台账应落库 900", _r16.get("qty"))
    ck.ok("冲突待确认" not in (_c16 or ""), "T16 减少系动词命中后不得判冲突", (_c16 or "")[:60])
    ck.ok("减少10%" in (_r16.get("source_excerpt") or ""),
          "T16 依据必须保留「减少10%」关键词", _r16.get("source_excerpt"))

    # ---- T17 冲突卡片：先展示台账真实数据再询问（2026-10-24 应队长需求）----
    # 旧版只输出「⚠️ 冲突待确认：数值矛盾…」，客户看不到台账里记的是什么。
    ck.section("T17 冲突卡片：先展示台账数据再询问是否以此为准")
    s17 = Store(); s17.reset()
    parse_chat("x", s17, mock=[_order(customer="温州机电", product="法兰盘", qty=1000,
               unit_price="18.5", delivery_date="2026-10-28", status_hint="已确认",
               source_excerpt="法兰盘1000个 18.5 10-28交")], now=NOW)
    # 来单 900 且无变更动词 → 冲突
    _c17, _ = parse_chat("x", s17, mock=[_order(customer="温州机电", product="法兰盘",
                          qty=900, unit_price="18.5", delivery_date="2026-10-28",
                          status_hint="已确认", source_excerpt="只发900个")], now=NOW)
    ck.ok("冲突待确认" in (_c17 or ""), "T17 冲突应触发冲突卡片", (_c17 or "")[:60])
    ck.ok("台账中记录" in (_c17 or ""), "T17 冲突卡片应展示「台账中记录」区块",
          (_c17 or "")[:80])
    ck.ok("订单号" in (_c17 or ""), "T17 冲突卡片应展示订单号", (_c17 or "")[:80])
    ck.ok("客户" in (_c17 or ""), "T17 冲突卡片应展示客户字段", (_c17 or "")[:80])
    ck.ok("产品" in (_c17 or ""), "T17 冲突卡片应展示产品字段", (_c17 or "")[:80])
    ck.ok("数量" in (_c17 or ""), "T17 冲突卡片应展示数量字段", (_c17 or "")[:80])
    ck.ok("1000" in (_c17 or ""), "T17 冲突卡片应展示台账真实数量 1000", (_c17 or "")[:80])
    ck.ok("以我们台账" in (_c17 or "") or "以此为准" in (_c17 or ""),
          "T17 冲突卡片应询问「数据以台账为准」", (_c17 or "")[-80:])

    # ---- T18 冲突确认路由：拒绝→转人工 / 同意→确认（2026-10-24 应队长需求）----
    ck.section("T18 冲突确认路由：拒绝→转人工 / 同意→确认")
    from agent import handle_message, _REJECT_MARKERS, _CONFIRM_MARKERS
    from prompts import HUMAN_HANDOFF
    # 拒绝词表必须覆盖常见表达
    for w in ("不是", "不对", "不同意", "错了", "搞错"):
        ck.ok(w in _REJECT_MARKERS, f"T18 _REJECT_MARKERS 必须含「{w}」", _REJECT_MARKERS)
    for w in ("是的", "确认", "对的", "以此为准"):
        ck.ok(w in _CONFIRM_MARKERS, f"T18 _CONFIRM_MARKERS 必须含「{w}」", _CONFIRM_MARKERS)
    # 受控实验：拒绝 → 转人工
    _r18a = handle_message("不是这单，数据不对", s17)
    ck.ok(_r18a["mode"] == "handoff", "T18 拒绝应 mode=handoff", _r18a.get("mode"))
    ck.ok(_r18a["reply"] == HUMAN_HANDOFF,
          "T18 拒绝应回复转人工话术", _r18a.get("reply"))
    # 受控实验：同意 → 确认台账为准
    _r18b = handle_message("是的，以你们为准", s17)
    ck.ok(_r18b["mode"] == "confirm", "T18 同意应 mode=confirm", _r18b.get("mode"))
    ck.ok("台账" in _r18b["reply"] or "为准" in _r18b["reply"],
          "T18 同意应回复确认台账为准", _r18b.get("reply"))
    # 边界：含查询词的消息不应被误判为转人工（如「不是吧，那单到哪了」）
    _r18c = handle_message("不是吧，那单到哪了", s17)
    ck.ok(_r18c["mode"] != "handoff",
          "T18 含查询词的消息不应误判为转人工", _r18c.get("mode"))

    # ---- T19 信息缺失多轮消歧（2026-10-24 应队长需求）----
    # 客户信息缺失时逐个拿匹配订单问「是否是这个订单」：
    # 回「是」→给更新后订单；回「否」→给下一个；全部否→转人工。
    ck.section("T19 信息缺失多轮消歧：逐个询问→是给订单/否下一个/全否转人工")
    from agent import handle_message as _hm, reset_session_ctx as _rsc
    from prompts import disambig_card_text, HUMAN_HANDOFF as _HH
    s19 = Store(); s19.reset()
    # 预置3个王总的订单 + 1个李姐的（不应被消歧命中）
    s19.add_order({"order_id":"D-01","customer":"王总","product":"法兰盘","qty":1000,
                    "unit_price":"18.5","delivery_date":"2026-10-28","status":"已确认",
                    "source_excerpt":"王总 法兰盘 1000个"})
    s19.add_order({"order_id":"D-02","customer":"王总","product":"电机壳","qty":200,
                    "unit_price":"32","delivery_date":"2026-11-05","status":"生产中",
                    "source_excerpt":"王总 电机壳 200个"})
    s19.add_order({"order_id":"D-03","customer":"王总","product":"齿轮","qty":50,
                    "unit_price":"8","delivery_date":"2026-11-10","status":"沟通中",
                    "source_excerpt":"王总 齿轮 50个"})
    s19.add_order({"order_id":"D-04","customer":"李姐","product":"断路器","qty":80,
                    "unit_price":"26","delivery_date":"2026-10-27","status":"已确认",
                    "source_excerpt":"李姐 断路器 80个"})
    _SK19 = "test_t19"
    # 第1轮：触发消歧（客户名王总，缺产品/数量/交期）
    _r19a = _hm("王总的货帮我改下", s19, mock=[{"type":"order","customer":"王总",
                "product":None,"qty":None,"unit_price":None,"delivery_date":None,
                "status_hint":None,"sample_flag":"无","source_excerpt":"王总的货帮我改下"}],
                sender="王总", session_key=_SK19)
    ck.ok(_r19a["mode"] == "disambig_start", "T19 第1轮应 mode=disambig_start", _r19a.get("mode"))
    ck.ok("第 1/3" in (_r19a.get("reply") or ""), "T19 第1轮应显示第1/3个候选", (_r19a.get("reply") or "")[:80])
    ck.ok("D-01" in (_r19a.get("reply") or ""), "T19 第1轮应展示第一个候选 D-01", (_r19a.get("reply") or "")[:80])
    # 第2轮：回「不是」→下一个候选
    _r19b = _hm("不是", s19, session_key=_SK19)
    ck.ok(_r19b["mode"] == "disambig_next", "T19 第2轮应 mode=disambig_next", _r19b.get("mode"))
    ck.ok("第 2/3" in (_r19b.get("reply") or ""), "T19 第2轮应显示第2/3个候选", (_r19b.get("reply") or "")[:80])
    ck.ok("D-02" in (_r19b.get("reply") or ""), "T19 第2轮应展示第二个候选 D-02", (_r19b.get("reply") or "")[:80])
    # 第3轮：回「不是」→第三个候选
    _r19c = _hm("不对", s19, session_key=_SK19)
    ck.ok(_r19c["mode"] == "disambig_next", "T19 第3轮应 mode=disambig_next", _r19c.get("mode"))
    ck.ok("第 3/3" in (_r19c.get("reply") or ""), "T19 第3轮应显示第3/3个候选", (_r19c.get("reply") or "")[:80])
    ck.ok("D-03" in (_r19c.get("reply") or ""), "T19 第3轮应展示第三个候选 D-03", (_r19c.get("reply") or "")[:80])
    # 第4轮：回「不是」→全部否完→转人工
    _r19d = _hm("不是", s19, session_key=_SK19)
    ck.ok(_r19d["mode"] == "handoff", "T19 第4轮全部否完应 mode=handoff", _r19d.get("mode"))
    ck.ok(_r19d.get("reply") == _HH, "T19 第4轮应回复转人工话术", _r19d.get("reply"))
    # 路径B：客户回「是」→给确认的订单信息
    _rsc(_SK19 + "b")
    s19b = Store(); s19b.reset()
    s19b.add_order({"order_id":"DB-01","customer":"王总","product":"法兰盘","qty":1000,
                     "unit_price":"18.5","delivery_date":"2026-10-28","status":"已确认",
                     "source_excerpt":"王总 法兰盘 1000个"})
    s19b.add_order({"order_id":"DB-02","customer":"王总","product":"电机壳","qty":200,
                     "unit_price":"32","delivery_date":"2026-11-05","status":"生产中",
                     "source_excerpt":"王总 电机壳 200个"})
    _hm("王总的货帮我改下", s19b, mock=[{"type":"order","customer":"王总",
        "product":None,"qty":None,"unit_price":None,"delivery_date":None,
        "status_hint":None,"sample_flag":"无","source_excerpt":"王总的货帮我改下"}],
        sender="王总", session_key=_SK19 + "b")
    _r19e = _hm("是的", s19b, session_key=_SK19 + "b")
    ck.ok(_r19e["mode"] == "disambig_confirm", "T19 路径B 回「是」应 mode=disambig_confirm", _r19e.get("mode"))
    ck.ok("DB-01" in (_r19e.get("reply") or ""), "T19 路径B 应展示确认的订单 DB-01", (_r19e.get("reply") or "")[:100])
    # find_candidates 排除占位单
    _cands = s19.find_candidates(customer="王总")
    ck.ok(all(c["status"] != "待补充" for c in _cands), "T19 find_candidates 不应返回占位单",
          [c["status"] for c in _cands])
    ck.ok(len(_cands) == 3, "T19 find_candidates 应返回3个王总的在途单（排除李姐+占位单）", len(_cands))

    # ---------------- T20 无变化卡片展示台账现值（2026-10-24 应队长需求） ----------------
    # 场景：客户要求「加到 80 个」而台账已是 80（先前已更新过）→ 幂等 nochange。
    # 旧版只回「数据已一致，未重复写入」，客户困惑「我要求改了为什么说无变化」。
    # 现要求：nochange 卡片必须展示台账现值（数量/交期/状态）+ 引导语。
    ck.section("T20 无变化卡片：展示台账现值 + 引导")
    s20 = Store(); s20.reset()
    parse_chat("x", s20, mock=[_order(customer="李姐（瓯越五金贸易）", product="DZ47-63 三相断路器",
                                      qty=80, unit_price="26", delivery_date="2026-10-27",
                                      status_hint="已确认",
                                      source_excerpt="李姐 DZ47 80个")], now=NOW)
    _c20, _ = parse_chat("小陈，DZ47 那个加到 80 个", s20,
                         mock=[_order(customer="李姐（瓯越五金贸易）", product="DZ47-63 三相断路器",
                                      qty=80, unit_price="26", delivery_date="2026-10-27",
                                      status_hint="已确认", intent="update",
                                      source_excerpt="小陈，DZ47 那个加到 80 个")], now=NOW)
    ck.ok("⏸️" in _c20 and "无变化" in _c20, "T20 同值变更应走无变化卡片", _c20[:80])
    _oid20 = next((o["order_id"] for o in s20.orders
                   if o["customer"] == "李姐（瓯越五金贸易）"), "")
    ck.ok(_oid20 in _c20, "T20 无变化卡片应含订单号", _c20[:120])
    ck.ok("👤 客户：李姐（瓯越五金贸易）" in _c20, "T20 应展示台账客户", _c20[:150])
    ck.ok("📊 数量：×80" in _c20, "T20 应展示台账当前数量 80", _c20[:200])
    ck.ok("📅 交期：2026-10-27" in _c20, "T20 应展示台账交期", _c20[:250])
    ck.ok("未重复写入" in _c20, "T20 应说明未重复写入的原因", _c20[-160:])
    ck.ok("目标值" in _c20, "T20 应引导客户改成其他目标值", _c20[-120:])
    # 语义守护：nochange 不应篡改台账
    _r20 = next((o for o in s20.orders if o["order_id"] == _oid20), None)
    ck.ok(_r20 is not None and _r20["qty"] == 80, "T20 无变化后台账数量应保持 80", _r20 and _r20["qty"])

    # ---------------- T21 追加 + 数量模糊：登记追加占位单，不漏单（2026-10-24 修复） ----------------
    # 场景：王总已有在途单（法兰），客户说「再帮我排上一批」（「一批」是模糊量词，qty=None，
    # intent=append）。旧逻辑被 `_has_qty` 挡在追加分支外 → 落入「existing 存在」的 MISSING 分支，
    # 只追问、不建单 → 这笔追加整笔丢失、汇总少计一笔（用户实测漏单 + 回复自相矛盾）。
    # 现要求：登记「待补充」追加占位单（挂原单、is_append、进台账），台账 +1，文案不自相矛盾。
    ck.section("T21 追加+数量模糊：登记追加占位单（防漏单）")
    s21 = Store(); s21.reset()
    parse_chat("x", s21, mock=[_order(customer="王总（永嘉精密机械）", product="304法兰盘 DN80",
                                     qty=250, unit_price="18.5", delivery_date="2026-10-28",
                                     status_hint="已确认",
                                     source_excerpt="王总 304法兰盘 DN80 250个 18.5 10-28交")],
               now=NOW)
    _base21 = s21.orders[0]["order_id"]
    _n21 = len(s21.orders)
    _c21, _ = parse_chat("小陈，法兰那批的货再帮我排上一批，我这边急", s21,
                         mock=[_order(customer="王总（永嘉精密机械）", product="304法兰盘 DN80",
                                      qty=None, unit_price="待报价", delivery_date=None,
                                      status_hint="沟通中", intent="append",
                                      source_excerpt="小陈，法兰那批的货再帮我排上一批，我这边急")],
                         now=NOW)
    ck.ok(len(s21.orders) == _n21 + 1, "T21 追加+模糊数量应新增一笔（不漏单）", len(s21.orders))
    _a21 = next((o for o in s21.orders if o.get("is_append")), None)
    ck.ok(_a21 is not None, "T21 新单应标记 is_append", _a21)
    if _a21:
        ck.ok(_a21["status"] == "待补充", "T21 追加占位单状态应为「待补充」", _a21["status"])
        ck.ok(_a21.get("parent_order_id") == _base21, "T21 追加单应挂到原单下",
              [_a21.get("parent_order_id"), _base21])
    ck.ok("已登记追加单" in _c21, "T21 卡片应明确「已登记追加单」", _c21[:80])
    ck.ok("🟡" in _c21, "T21 卡片应保留 🟡（与缺失判定口径一致）", _c21[:40])
    ck.ok("数量待补" in _c21, "T21 卡片应说明数量待补", _c21[:120])
    ck.ok(_base21 in _c21, "T21 卡片应注明追加自哪一单", _c21[:140])
    # 语义守护：本单数量未知时，不能把原单既有数量（250）当作「本单已知」展示，
    # 否则又会出现「已知250却说数量缺失」的自相矛盾。
    ck.ok("×250" not in _c21, "T21 卡片不应把原单数量当本单已知值（防文案矛盾）", _c21[:200])

    # ---------------- T22 关键字段变更：直接更新 + 留变更履历（2026-10-24 恢复） ----------------
    # 撤销 #34 的「先确认再更改」：改数量 / 单价 / 交期一律即时落库并留变更履历，
    # 不再挂起等客户回「是」。状态 / 规格 / 样品等低风险字段本就直接落库。
    ck.section("T22 关键字段变更：直接更新 + 留变更履历")

    def _mk22():
        _s = Store(); _s.reset()
        parse_chat("x", _s, mock=[_order(customer="李姐（瓯越五金贸易）",
                                         product="DZ47-63 三相断路器", qty=50,
                                         unit_price="26", delivery_date="2026-10-27",
                                         status_hint="已确认",
                                         source_excerpt="李姐 DZ47 50个")], now=NOW)
        return _s

    _UPD22 = [_order(customer="李姐（瓯越五金贸易）", product="DZ47-63 三相断路器",
                     qty=80, unit_price=None, delivery_date=None, status_hint=None,
                     intent="update", source_excerpt="小陈，DZ47 那个加到 80 个")]

    # ① 改数量 → 直接更新（不挂起），台账即时落库 80
    s22 = _mk22()
    _r22a = handle_message("小陈，DZ47 那个加到 80 个", s22, mock=_UPD22,
                           sender="李姐", session_key="t22_direct_a")
    _rep22a = _r22a.get("reply", "")
    ck.ok("变更待确认" not in _rep22a, "T22 ① 改数量不应再出现「变更待确认」", _rep22a[:80])
    ck.ok("已更新订单" in _rep22a or "✅" in _rep22a,
          "T22 ① 改数量应直接出「已更新订单」", _rep22a[:80])
    ck.ok("📊 数量：×80" in _rep22a, "T22 ① 卡片应展示新数量 80", _rep22a[:200])
    _o22 = next((o for o in s22.orders if "DZ47" in str(o.get("product"))), None)
    ck.ok(_o22 is not None and _o22["qty"] == 80, "T22 ① 改数量应即时落库为 80",
          _o22 and _o22["qty"])

    # ② 变更履历：保留 50 → 80 记录（与台账「变更履历」一致）
    _ch22 = (_o22 or {}).get("changes") or []
    ck.ok(len(_ch22) >= 1, "T22 ② 应记录变更履历", len(_ch22))
    if _ch22:
        _last22 = _ch22[-1]
        ck.ok("50" in str(_last22.get("old")) and "80" in str(_last22.get("new")),
              "T22 ② 履历应为 数量 50 → 80", _last22)

    # ③ 连续两次改数量 → 每次直接落库，最后一次生效
    _r22c = handle_message("小陈，DZ47 那个加到 100 个", s22,
                           mock=[_order(customer="李姐（瓯越五金贸易）",
                                        product="DZ47-63 三相断路器", qty=100,
                                        unit_price=None, delivery_date=None,
                                        status_hint=None, intent="update",
                                        source_excerpt="加到 100 个")],
                           sender="李姐", session_key="t22_direct_a")
    _o22c = next((o for o in s22.orders if "DZ47" in str(o.get("product"))), None)
    ck.ok(_o22c is not None and _o22c["qty"] == 100, "T22 ③ 再次改数量应直接落库为 100",
          _o22c and _o22c["qty"])
    ck.ok("📊 数量：×100" in _r22c.get("reply", ""), "T22 ③ 卡片应展示 100",
          _r22c.get("reply", "")[:200])

    # ④ 无挂起时回「是」不应误改台账（确认 #34 残留路由已彻底移除）
    _q22 = _o22c["qty"]
    handle_message("是", s22, session_key="t22_direct_a")
    _o22d = next((o for o in s22.orders if "DZ47" in str(o.get("product"))), None)
    ck.ok(_o22d is not None and _o22d["qty"] == _q22,
          "T22 ④ 无挂起时回「是」不应误改台账", _o22d and _o22d["qty"])
    ck.report()
    print("-" * 46)
    print(briefing(store))
    return True


if __name__ == "__main__":
    # Windows 上 store._save_one() 的原子写 `os.replace` 偶发 PermissionError
    # （WinError 5，杀毒/索引服务占用旧文件；实测约 1/3 概率）。
    # 这是**环境问题、不是断言失败**，也不在模块五职责内（store.py 属模块三/队友D），
    # 故只在脚本侧重试并明确标注，避免「一键回归」被它误报成测试失败。
    # 各 section 开头都会 store.reset()，重跑是幂等的。
    for _attempt in range(1, 4):
        try:
            test_pipeline()
            break
        except PermissionError as _e:      # pragma: no cover - 依赖本机环境
            if _attempt == 3:
                print("\n❌ 环境问题：store 写盘被占用，重试 3 次仍失败，非断言失败")
                print(f"   {_e}")
                print("   归因：Windows 文件锁，位于 store.py 的 os.replace（模块三/队友D）")
                sys.exit(2)
            print(f"   ⚠️ store 写盘被占用（Windows 文件锁），重试第 {_attempt + 1} 次…")
            time.sleep(0.05 * _attempt)
    print("\nALL TESTS PASSED ✅")
