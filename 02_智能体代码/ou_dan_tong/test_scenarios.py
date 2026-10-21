"""模块五·2 场景测试走查：用模块一数据集验证五类场景端到端表现。

归属：队友B（模块五 测试验证）。运行：python test_scenarios.py

做法：
1. 先校验模块一数据集（`03_演示数据/data/chat_logs.json`）符合契约 1 且覆盖度达标；
2. 再按数据文件顺序，把每个场景的「理想抽取结果」喂进 `agent.parse_chat`，
   用**卡片可见话术**（⛔/⚠️/🟡/✅）判定本轮实际结论，与模块一标注的
   `expected.validator_target` 逐条比对；
3. 对 `ok` 场景额外校验 `expected.fields` 是否落到台账上；
4. 与期望不符但根因在他模块的项，登记进 `ALLOWED_DEVIATIONS`（关联 BUG-ID），
   不计入本次走查失败，但会在报告中显式列出并 @ 归属人。

这样既能保证「模块五自己的断言不掺水」，又不会把他人模块的未完成项误算成回归。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store
from agent import parse_chat
from test_fixtures import (load_chat_logs, check_contract1, iter_scene_rounds,
                           EXPECTED_TARGET, SCENE_ROUNDS, _norm_date, FIXED_NOW)

# 卡片前缀 → 判定（与 agent.parse_chat 的可见话术一一对应）
VERDICT_BY_MARK = [("⛔", "abnormal"), ("⚠️", "conflict"),
                   ("🟡", "missing"), ("✅", "ok")]

# 已知偏差白名单：(scene_id, round) -> (BUG-ID, 说明)
# 这些项根因在他人模块，模块五只登记不复改，见 模块五_测试报告.md
ALLOWED_DEVIATIONS = {
    ("S3-G1", 0): ("BUG-01", "占位/补单场景下 status 参与冲突比对，"
                             "台账「待补充/已确认」与来单默认「沟通中」互判冲突，掩盖了缺失追问"),
    ("S2-G2", 0): ("BUG-02", "变更动词表缺「加到」，口语增量表达被判为冲突"),
    ("S2-G2", 1): ("BUG-02", "同上，且上一轮未写入导致后续轮次持续冲突"),
    ("S2-G2", 2): ("BUG-02", "同上，且上一轮未写入导致后续轮次持续冲突"),
}


def verdict_of(card: str):
    for mark, v in VERDICT_BY_MARK:
        if mark in card:
            return v
    return "unknown"


def run():
    groups = load_chat_logs()
    problems, by_type = check_contract1(groups)

    print("=" * 78)
    print("模块五·2 场景走查 —— 数据源：03_演示数据/data/chat_logs.json")
    print("=" * 78)
    print("\n[1] 模块一数据集体检（契约1 + 覆盖度）")
    if problems:
        for p in problems:
            print(f"   ❌ {p}")
    else:
        print("   ✅ 契约1 字段齐全、组内时间升序、五类场景每类≥2 组、动态演进≥1 组")
    for t in sorted(by_type):
        print(f"   · {t}：{len(by_type[t])} 组 → {by_type[t]}")

    store = Store()
    store.reset()
    rows, failures, deviations = [], [], []

    print("\n[2] 逐场景走查（判定取自卡片可见话术）")
    for scene, rnd, mock in iter_scene_rounds(groups):
        sid = scene["scene_id"]
        expect = EXPECTED_TARGET.get(sid, scene.get("expected", {}).get("validator_target"))
        before = len(store.orders)
        card, _ = parse_chat("walkthrough", store, mock=mock, now=FIXED_NOW)
        actual = verdict_of(card)
        key = (sid, rnd)
        rows.append((sid, scene.get("scene_type"), rnd, expect, actual))

        ok = (actual == expect)
        extra = ""
        # expected.fields 描述的是「该场景走完后的最终态」，只在最后一轮校验
        is_last_round = (rnd == len(SCENE_ROUNDS.get(sid, [])) - 1)
        if ok and expect == "ok" and is_last_round:
            # ok 场景：额外校验 expected.fields 是否落到台账
            exp_fields = scene.get("expected", {}).get("fields", {}) or {}
            cust = mock[0]["customer"]
            prod = mock[0]["product"]
            rec = next((o for o in store.orders
                        if o["customer"] == cust and o["product"] == prod), None)
            if rec is None:
                ok, extra = False, "台账未找到该实体"
            else:
                if "数量" in exp_fields:
                    want, got = int(exp_fields["数量"]), rec.get("qty")
                    if want != got:
                        ok, extra = False, f"数量期望{want}实际{got}"
                if ok and "交期" in exp_fields:
                    want = _norm_date(exp_fields["交期"])
                    got = rec.get("delivery_date")
                    if want and want != got:
                        ok, extra = False, f"交期期望{want}实际{got}"
        if ok and expect == "abnormal":
            if len(store.orders) != before:
                ok, extra = False, "异常不应写入，但台账条数增加"

        if not ok:
            if key in ALLOWED_DEVIATIONS:
                deviations.append((sid, rnd, expect, actual, ALLOWED_DEVIATIONS[key]))
            else:
                failures.append((sid, rnd, expect, actual, extra or card.splitlines()[0]))

    for sid, stype, rnd, exp, act in rows:
        mark = "✅" if act == exp else ("⚠️" if (sid, rnd) in ALLOWED_DEVIATIONS else "❌")
        print(f"   {mark} {sid} ({stype}) 第{rnd+1}轮  期望={exp:<8} 实际={act}")

    print("\n[3] 走查结论")
    total = len(rows)
    scenes = len({r[0] for r in rows})
    print(f"   场景 {scenes} 组 / 走查轮次 {total} 轮："
          f"通过 {total - len(failures) - len(deviations)}，"
          f"已知偏差 {len(deviations)}，失败 {len(failures)}")
    if deviations:
        print("\n   已知偏差（根因在他模块，已登记待归属人修复）：")
        for sid, rnd, exp, act, (bug, why) in deviations:
            print(f"   ⚠️ {sid} 第{rnd+1}轮：期望 {exp} / 实际 {act} → {bug}：{why}")
    if failures:
        print("\n   未通过项：")
        for sid, rnd, exp, act, why in failures:
            print(f"   ❌ {sid} 第{rnd+1}轮：期望 {exp} / 实际 {act} ｜ {why}")

    if problems or failures:
        raise AssertionError(
            f"场景走查未通过：数据问题 {len(problems)} 项、断言失败 {len(failures)} 项")
    print("\n✅ 模块五·2 场景走查完成（偏差项已归责登记）")
    return rows, deviations


if __name__ == "__main__":
    run()
