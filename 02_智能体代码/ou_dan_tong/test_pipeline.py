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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store
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
    for key in ("新增订单", "待报价", "临期交付", "逾期任务"):
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
    ck.ok(len(s.orders) == 2, "T6 冲突不应新增订单", len(s.orders))

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
    ck.section("T8 缺失占位（待补充 + 追问）")
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

    # ---------------- T9 状态只进不退 ----------------
    ck.section("T9 状态机只进不退")
    s3 = Store(); s3.reset()
    parse_chat("x", s3, mock=[_order(customer="温州机电", product="法兰盘", qty=300,
               unit_price="8.0", delivery_date="2026-11-25", status_hint="生产中",
               source_excerpt="法兰盘300个，11-25交")], now=NOW)
    parse_chat("x", s3, mock=[_order(customer="温州机电", product="法兰盘", qty=300,
               unit_price="8.0", delivery_date="2026-11-25", status_hint="已报价",
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

    ck.report()
    print("-" * 46)
    print(briefing(store))
    return True


if __name__ == "__main__":
    test_pipeline()
    print("\nALL TESTS PASSED ✅")
