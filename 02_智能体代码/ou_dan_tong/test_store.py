"""模块三（状态维护）自测：存储模型 / 最新状态解析 / 持久化读写 / 一致性校验 / 状态机。

直接运行：python test_store.py（无需 LLM Key，使用临时目录，不污染 runtime）。
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import (Store, normalize_status, can_transition, next_statuses,
                   PLACEHOLDER_STATUS, effective_delivery_date, _valid_batches,
                   _invalid_batches)


def _tmp_store():
    d = tempfile.mkdtemp(prefix="ou_store_test_")
    return Store(d), d


def _order(**kw):
    base = {"customer": "测试客户", "product": "测试产品", "spec": "A",
            "qty": 100, "unit_price": "3.0", "delivery_date": "2026-12-01",
            "status": "沟通中", "sample_flag": "无", "source_excerpt": "原始依据"}
    base.update(kw)
    return base


# —— 三·1 台账存储模型 ——
def test_model():
    s, d = _tmp_store()
    try:
        o = _order()
        o["order_id"] = s.next_order_id("20261021")
        s.add_order(o)
        assert o["order_id"] == "ORD-20261021-01", o["order_id"]
        # 契约 3：change_log 存在且与 changes 镜像
        assert o["change_log"] == o["changes"] == []
        for f in ("created_at", "updated_at", "flags", "customer", "product", "qty", "status"):
            assert f in o, f
        t = {"task_id": "TSK-20261021-01", "content": "打样", "owner": "李经理",
             "deadline": "2026-10-22", "status": "待办"}
        s.add_task(t)
        for f in ("created_at", "updated_at", "change_log", "order_id"):
            assert f in t, f
        print("✓ 三·1 存储模型：订单/任务字段齐全，change_log 与 changes 镜像")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# —— 三·2 最新状态解析 ——
def test_latest_state():
    s, d = _tmp_store()
    try:
        o = _order()
        o["order_id"] = "ORD-1"
        s.add_order(o)
        # 后值覆盖前值
        res = s.apply_latest("ORD-1", {"qty": 6000, "unit_price": None,
                                       "delivery_date": ""}, source="数量改成 6000")
        assert res["updated"] == ["qty"], res
        rec = s.get_order("ORD-1")
        assert rec["qty"] == 6000
        assert rec["unit_price"] == "3.0", "空值不得覆盖旧值"
        assert rec["delivery_date"] == "2026-12-01", "空串不得覆盖旧值"
        assert len(rec["change_log"]) == 1 and rec["change_log"][0]["new"] == 6000
        assert rec["change_log"][0]["old"] == 100

        # 冲突锁字段不自动覆盖
        s.lock_fields("ORD-1", ["qty", "delivery_date"], reason="与在途单矛盾")
        res = s.apply_latest("ORD-1", {"qty": 9999, "delivery_date": "2026-12-30"})
        assert res["updated"] == [] and set(res["blocked"]) == {"qty", "delivery_date"}, res
        assert s.get_order("ORD-1")["qty"] == 6000, "锁定字段不得被自动覆盖"

        # 人工确认后可写入，并解锁
        res = s.confirm_update("ORD-1", {"qty": 9999}, confirmed_by="队长", note="以客户书面为准")
        assert res["updated"] == ["qty"], res
        assert s.get_order("ORD-1")["qty"] == 9999
        assert "qty" not in (s.get_order("ORD-1")["flags"].get("locked_fields") or [])
        print("✓ 三·2 最新状态解析：后值覆盖前值、空值不覆盖、锁定字段需人工确认")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# —— 三·3 持久化与读写 ——
def test_persistence():
    d = tempfile.mkdtemp(prefix="ou_store_test_")
    try:
        s = Store(d)
        s.add_order({**_order(), "order_id": "ORD-1"})
        s.add_task({"task_id": "TSK-1", "content": "催料", "owner": "", "deadline": "2026-11-01"})
        # 重新加载（模拟重启）
        s2 = Store(d)
        assert len(s2.orders) == 1 and len(s2.tasks) == 1
        assert s2.get_order("ORD-1")["customer"] == "测试客户"
        assert s2.query_orders(customer="测试")[0]["order_id"] == "ORD-1"
        assert s2.query_tasks(all_status=True)[0]["task_id"] == "TSK-1"
        # 原子写盘：不得留下 .tmp
        assert not [f for f in os.listdir(d) if f.endswith(".tmp")]
        # 增改查
        s2.update_task("TSK-1", {"status": "已完成", "owner": "王工"})
        assert s2.get_task("TSK-1")["status"] == "已完成"
        assert s2.get_task("TSK-1")["change_log"][-1]["new"] == "王工"
        # 损坏文件兜底：非法 JSON → 备份 + 空台账，不崩溃
        with open(os.path.join(d, "orders.json"), "w", encoding="utf-8") as f:
            f.write("{坏掉的 json")
        s3 = Store(d)
        assert s3.orders == [] and s3.load_warnings, s3.load_warnings
        assert any("corrupt-" in f for f in os.listdir(d))
        print("✓ 三·3 持久化读写：重启可恢复、原子写盘、损坏文件自动备份兜底")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# —— 三·4 一致性校验与修复 ——
def test_integrity():
    d = tempfile.mkdtemp(prefix="ou_store_test_")
    try:
        s = Store(d)
        s.orders = [
            {**_order(), "order_id": "ORD-1"},                       # 正常
            {**_order(qty=-5), "order_id": "ORD-1"},                 # 重复号 + 负数量
            {**_order(qty=None), "order_id": "ORD-2", "customer": ""},  # 缺客户
        ]
        s.tasks = [{"task_id": "TSK-1", "content": "催料", "order_id": "ORD-404"},
                   {"task_id": "TSK-1", "content": ""}]
        issues = s.integrity_check()
        kinds = "；".join(i["issue"] for i in issues)
        assert any("重复" in i["issue"] for i in issues), kinds
        assert any("负数" in i["issue"] for i in issues), kinds
        assert any("缺客户" in i["issue"] for i in issues), kinds
        assert any("孤儿" in i["issue"] for i in issues), kinds
        assert any("缺任务内容" in i["issue"] for i in issues), kinds

        actions, remaining = s.repair()
        assert actions, "应有自动修复动作"
        assert any("重新编号" in a for a in actions), actions
        assert any("孤儿引用" in a for a in actions), actions
        # 修复后：单号唯一、孤儿引用清除、负数量被锁定待人工（值不被篡改）
        ids = [o["order_id"] for o in s.orders]
        assert len(ids) == len(set(ids)), ids
        assert all(t.get("order_id") is None for t in s.tasks if t.get("task_id"))
        bad = next(o for o in s.orders if o.get("qty") == -5)
        assert bad["flags"]["locked_fields"] == ["qty"] and bad["qty"] == -5
        assert any("数量异常" in r for r in remaining), remaining
        # 修复结果已落盘
        assert len(Store(d).orders) == 3
        print("✓ 三·4 一致性校验与修复：重复号/缺字段/负数量/孤儿引用可检出并修复")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# —— 三·5 进度状态机 ——
def test_state_machine():
    assert normalize_status("待确认") == "已确认"
    assert normalize_status("已发货") == "已交付"
    assert normalize_status("乱写") is None
    # 「已报价」已废弃：别名归一为「沟通中」（2026-10-23 产品口径）
    assert normalize_status("已报价") == "沟通中"
    assert can_transition("已确认", "沟通中") is False
    assert can_transition(PLACEHOLDER_STATUS, "已确认") is True
    assert next_statuses("已确认") == ["生产中", "部分交付", "已交付", "已取消"], next_statuses("已确认")

    s, d = _tmp_store()
    try:
        s.add_order({**_order(), "order_id": "ORD-1", "status": "沟通中"})
        chain = ["已确认", "生产中", "已交付"]
        for st in chain:
            r = s.set_order_status("ORD-1", st, speaker="李经理", raw=f"进度：{st}")
            assert r["ok"], r
        assert s.get_order("ORD-1")["status"] == "已交付"
        # 状态回退被拒
        r = s.set_order_status("ORD-1", "生产中")
        assert r["ok"] is False and "回退" in r["reason"], r
        # 跳级前进允许（沟通中→生产中）
        s.add_order({**_order(product="另一个"), "order_id": "ORD-2", "status": "沟通中"})
        assert s.set_order_status("ORD-2", "生产中")["ok"] is True
        # 非法状态被拒
        assert s.set_order_status("ORD-2", "瞎写状态")["ok"] is False
        # 每次流转均留痕
        assert len(s.get_order("ORD-1")["change_log"]) == len(chain)
        print("✓ 三·5 进度状态机：单向流转、禁止回退、非法状态拒绝、全程留痕")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# —— D-1 批次交期有效性解析 + D-3 模块级纯函数 + D-2 语义拆分 ——
def test_batch_delivery():
    s, d = _tmp_store()
    try:
        # 用例1：两批都已到期 → 有效交期=最晚批次日，计划排满且已交完
        full = _order(qty=900, delivery_date=None,
                      delivery_batches=[{"qty": 500, "date": "2026-11-01"},
                                        {"qty": 400, "date": "2026-11-03"}])
        full["order_id"] = "ORD-B1"
        s.add_order(full)
        assert s.effective_delivery_date(full) == "2026-11-03", full["delivery_batches"]
        # D-3：模块级纯函数与 Store 方法结果一致，可直接被模块二 import 复用
        assert effective_delivery_date(full) == "2026-11-03"
        bs = s.batch_summary(full, as_of="2026-11-03")
        assert bs["has_batches"] is True and bs["planned"] == 900, bs
        assert bs["shipped"] == 900 and bs["total"] == 900, bs
        assert bs["plan_complete"] is True and bs["done"] is True, bs
        assert bs["last_date"] == "2026-11-03" and bs["next_date"] is None, bs

        # 用例2：只交第一批（2026-11-01 已到，2026-11-03 未到）
        # 修复旧 bug：旧版按「计划量>=qty 就 done=True」，未来批次也算交完
        part = _order(qty=900, delivery_date=None,
                      delivery_batches=[{"qty": 500, "date": "2026-11-01"},
                                        {"qty": 400, "date": "2026-11-03"}])
        part["order_id"] = "ORD-B2"
        s.add_order(part)
        bs = s.batch_summary(part, as_of="2026-11-02")
        assert bs["planned"] == 900 and bs["shipped"] == 500, bs
        assert bs["plan_complete"] is True, "计划已排满"
        assert bs["done"] is False, "最晚批次日 11-03 未到，不得算交完"
        assert bs["next_date"] == "2026-11-03", bs
        # 到了 11-03 之后才算真交完
        bs2 = s.batch_summary(part, as_of="2026-11-03")
        assert bs2["done"] is True and bs2["next_date"] is None, bs2

        # 用例3：无批次 → 回退 delivery_date，has_batches=False
        plain = _order(qty=100, delivery_date="2026-11-05")
        plain["order_id"] = "ORD-B3"
        s.add_order(plain)
        assert s.effective_delivery_date(plain) == "2026-11-05"
        bs = s.batch_summary(plain)
        assert bs["has_batches"] is False and bs["done"] is False, bs

        # 用例4：全空 → None，done=False
        empty = _order(qty=None, delivery_date=None)
        empty["order_id"] = "ORD-B4"
        s.add_order(empty)
        assert s.effective_delivery_date(empty) is None
        assert s.batch_summary(empty)["done"] is False

        # 用例5：D-4/D-5 诊断——非法批次被跳过且可查原因
        bad = _order(qty=300, delivery_date=None,
                     delivery_batches=[{"qty": 100, "date": "2026-11-01"},
                                       {"qty": "一百", "date": "2026-11-02"},   # qty 非法
                                       {"qty": 200, "date": "2026-13-99"},      # 日期不存在
                                       "不是对象"])
        bad["order_id"] = "ORD-B5"
        s.add_order(bad)
        assert [b["qty"] for b in _valid_batches(bad)] == [100], "只留合法条目"
        invalid = _invalid_batches(bad)
        assert len(invalid) == 3, invalid
        assert any("qty" in r for _, _, r in invalid), invalid
        assert any("真实存在的日期" in r for _, _, r in invalid), invalid
        print("✓ D-1/D-3/D-2 批次语义：模块级纯函数、planned/shipped/done 拆分、非法批次可诊断")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# —— D-2 一致性校验覆盖批次字段 ——
def test_integrity_batches():
    s, d = _tmp_store()
    try:
        s.orders = [
            # 类型1：批次和 900 ≠ delivered_qty 500
            {**_order(qty=900), "order_id": "ORD-X1", "status": "生产中",
             "delivered_qty": 500, "remain_qty": 400,
             "delivery_batches": [{"qty": 400, "date": "2026-11-01"},
                                  {"qty": 500, "date": "2026-11-03"}]},
            # 类型2：delivered 500 + remain 100 ≠ qty 900
            {**_order(qty=900), "order_id": "ORD-X2", "status": "生产中",
             "delivered_qty": 500, "remain_qty": 100,
             "delivery_batches": [{"qty": 500, "date": "2026-11-01"}]},
            # 类型3：待补充占位单带了 delivery_batches
            {**_order(), "order_id": "ORD-X3", "status": "待补充",
             "delivery_batches": [{"qty": 10, "date": "2026-11-01"}]},
        ]
        issues = s.integrity_check()
        by_id = {}
        for i in issues:
            by_id.setdefault(i["id"], []).append(i["issue"])
        assert any("≠ delivered_qty" in m for m in by_id.get("ORD-X1", [])), by_id
        assert any("remain_qty" in m and "≠" in m for m in by_id.get("ORD-X2", [])), by_id
        assert any("待补充" in m for m in by_id.get("ORD-X3", [])), by_id
        # 定位粒度：三类问题都带具体 order_id（by_id 键即单号）

        # repair：给出明确标记「需人工确认」，不得静默篡改数据
        actions, remaining = s.repair()
        assert all(o.get("delivery_batches") for o in s.orders), "repair 不得删除批次数据"
        assert s.orders[0]["delivered_qty"] == 500, "repair 不得改 delivered_qty"
        assert s.orders[1]["remain_qty"] == 100, "repair 不得改 remain_qty"
        assert s.orders[2]["status"] == "待补充", "repair 不得改占位单状态"
        assert all(o["flags"].get("batch_inconsistent") for o in s.orders), "应打 batch_inconsistent 标记"
        batch_remaining = [r for r in remaining if "批次" in r]
        assert len(batch_remaining) == 3 and all("人工确认" in r for r in batch_remaining), remaining

        # D-1 修复验收：标记必须真实落盘（orders.json 文件内容里能读到 batch_inconsistent），
        # 重启后仍在 —— 旧版只进内存，重启即丢
        with open(os.path.join(d, "orders.json"), encoding="utf-8") as f:
            on_disk = f.read()
        assert "batch_inconsistent" in on_disk, "batch_inconsistent 标记未落盘"
        s2 = Store(d)
        assert all(o["flags"].get("batch_inconsistent") for o in s2.orders), "重启后标记丢失"

        # D-6：仅缺 delivered_qty 降为 warn（不再 error）；值对不上仍是 error
        s.orders = [{**_order(qty=100), "order_id": "ORD-W1", "status": "生产中",
                     "delivery_batches": [{"qty": 100, "date": "2026-11-01"}]}]
        levels = [(i["level"], i["issue"]) for i in s.integrity_check()
                  if i["id"] == "ORD-W1" and "delivered_qty" in i["issue"]]
        assert levels and all(lv == "warn" for lv, _ in levels), levels
        print("✓ D-2 一致性校验覆盖批次字段：3 类坏数据检出并定位到单号，repair 标记落盘可重启恢复")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_model()
    test_latest_state()
    test_persistence()
    test_integrity()
    test_state_machine()
    test_batch_delivery()
    test_integrity_batches()
    print("-" * 46)
    print("模块三 ALL TESTS PASSED ✅")
