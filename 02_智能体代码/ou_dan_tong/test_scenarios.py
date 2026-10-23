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
from test_fixtures import (load_chat_logs, check_contract1, check_fixture_coverage,
                           iter_scene_rounds, EXPECTED_TARGET, ROUND_EXPECT,
                           SCENE_ROUNDS, _norm_date, FIXED_NOW)

# 卡片前缀 → 判定（与 agent.parse_chat 的可见话术一一对应）
VERDICT_BY_MARK = [("⛔", "abnormal"), ("⚠️", "conflict"),
                   ("🟡", "missing"), ("✅", "ok")]

# 已知偏差白名单：(scene_id, round) -> (BUG-ID, 说明)
# 这些项根因在他人模块，模块五只登记不复改，见 模块五_测试报告.md
#
# ── B-3 白名单复核（2026-10-23）：原 7 项逐条复验后收敛为 2 项 ──
#   已可移除 5 项：S3-G1 第1轮(BUG-01)、S5-G3 第1轮(BUG-08)、S7-T1 第1轮(BUG-10)、
#     S8-C1 第2轮 与 S8-P1 第1轮(BUG-11) —— 完整场景走查已通过，且经定向复验
#     确认缺陷行为本身已修复（详见 模块五_测试报告.md「白名单复核结论」一节）。
#     继续留着豁免只会掩盖真实回归，故删除。
#   场景残留 2 项（下方保留）：最小复现已修（探针转绿），但真实场景仍被同源问题
#     拦下，保留豁免并写明归属人 + 可照抄的复现文本，等对方合入后按 B-2 清零。
#   探针误报 0 项。
ALLOWED_DEVIATIONS = {
    # 「加到」类变更动词已修复；「算了还是 65」这类口语修正表达仍无变更动词覆盖，
    # 数量 65 与台账 80 被判冲突。定向复验：classify_order(raw_text="算了还是 65 吧")
    # → CONFLICT（应为 OK）。
    ("S2-G2", 1): ("BUG-02", "上一轮写入成功后，本轮「算了还是 65」口语修正仍判冲突"
                             "——「加到」类已修复，口语减量/修正表达残留"),
    # 台账 delivery_batches 字段已加（探针绿），但冲突比对仍用单值 delivery_date，
    # 分批回填第 1 批交期时先撞冲突，批次根本写不进去。
    # 定向复验：台账交期 11-03 ←「分两批：11-01 出 500，11-03 出 400」
    # → ⚠️ 冲突待确认，delivery_batches 仍为 None。
    ("S6-E2", 2): ("BUG-09", "分批交期仍撞冲突：冲突比对用单值 delivery_date，"
                             "未走批次有效交期，第 3 轮回填第一批交期被拦"),
}

# 场景残留项 → (归属人, 可照抄的复现文本, 期望 vs 实际)
# 用途：走查输出第 [4] 节直接把「谁去修 + 怎么复现」打出来，避免口头描述走样。
DEVIATION_OWNER = {
    ("S2-G2", 1): (
        "队长 A-1（contracts.CHANGE_VERBS 补「算了还是」等口语修正表达）",
        "台账 李姐（瓯越五金贸易）/ DZ47-63 三相断路器 数量=80 → "
        "来单「算了还是 65 个，客户临时砍了」",
        "期望 ok（更新为 65），实际 conflict",
    ),
    ("S6-E2", 2): (
        "队长 A-2（冲突比对改走批次有效交期）+ 队友D D-1（delivery_batches 落库）",
        "台账 阿珍（鹿城鞋材）/ PU 皮鞋面 交期=2026-11-03 → "
        "来单「分两批：11-01 出 500，11-03 出 400」",
        "期望 ok（登记批次 11-01/500、11-03/400），"
        "实际 conflict（交期 台账 11-03 ↔ 来单 11-01）",
    ),
}

# 语义落库校验：(scene_id, round) -> (期望状态, BUG-ID)
# 卡片判定为 ok 只说明「没报错」，不代表语义真正落地。对新增场景类型
# （部分交付/取消订单）额外校验状态字段，防止「静默吞掉语义」被判通过。
EXPECT_STATUS = {
    ("S8-C1", 1): ("已取消", "BUG-11"),
    ("S8-P1", 0): ("部分交付", "BUG-11"),
}


def verdict_of(card: str):
    for mark, v in VERDICT_BY_MARK:
        if mark in card:
            return v
    return "unknown"


def run():
    groups = load_chat_logs()
    problems, by_type = check_contract1(groups)

    # 覆盖度闸门：数据集扩充而夹具没跟上时直接判失败（2026-10-22 新增）
    cov_problems, cov = check_fixture_coverage(groups)
    problems = list(problems) + cov_problems

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

    print(f"\n[1b] 走查夹具覆盖度闸门（数据集 {cov['scenes_total']} 组）")
    pct = (cov["scenes_covered"] / cov["scenes_total"] * 100) if cov["scenes_total"] else 0
    flag = "✅" if not cov["missing"] else "❌"
    print(f"   {flag} 场景覆盖 {cov['scenes_covered']}/{cov['scenes_total']} ({pct:.0f}%)"
          f" ｜ 走查轮次 {cov['rounds_total']} 轮")
    for t in sorted(cov["by_type"]):
        e = cov["by_type"][t]
        print(f"      · {t}：{e['covered']}/{e['total']} 组已配夹具")

    store = Store()
    store.reset()
    rows, failures, deviations = [], [], []

    print("\n[2] 逐场景走查（判定取自卡片可见话术）")
    for scene, rnd, mock in iter_scene_rounds(groups):
        sid = scene["scene_id"]
        # 期望取值优先级：按轮覆盖 > 场景级（模块一 expected.validator_target）
        expect = ROUND_EXPECT.get(
            (sid, rnd),
            EXPECTED_TARGET.get(sid, scene.get("expected", {}).get("validator_target")))
        before = len(store.orders)
        card, _ = parse_chat("walkthrough", store, mock=mock, now=FIXED_NOW)
        actual = verdict_of(card)
        key = (sid, rnd)
        ok = (actual == expect)
        extra = ""
        # expected.fields 描述的是「该场景走完后的最终态」，只在最后一轮校验
        is_last_round = (rnd == len(SCENE_ROUNDS.get(sid, [])) - 1)
        if ok and expect == "ok" and is_last_round:
            # ok 场景：额外校验 expected.fields 是否落到台账
            exp_fields = scene.get("expected", {}).get("fields", {}) or {}
            cust = mock[0].get("customer")
            prod = mock[0].get("product")
            # 任务型场景（mock[0] 为任务 dict，无 customer/product）不做订单字段落库校验
            if cust and prod:
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
        # 语义落库校验：卡片判 ok 但仍可能「静默吞掉语义」（如部分交付未转状态）
        if ok and key in EXPECT_STATUS:
            want_status, bug = EXPECT_STATUS[key]
            cust = mock[0].get("customer")
            prod = mock[0].get("product")
            # 任务型场景跳过订单状态校验
            if cust and prod:
                rec = next((o for o in store.orders
                            if o["customer"] == cust and o["product"] == prod), None)
                got = (rec or {}).get("status")
                if got != want_status:
                    ok = False
                    extra = (f"{bug}：状态期望「{want_status}」实际「{got}」"
                             f"（语义未落地）")

        if ok and expect == "abnormal":
            if len(store.orders) != before:
                ok, extra = False, "异常不应写入，但台账条数增加"

        rows.append((sid, scene.get("scene_type"), rnd, expect, actual, ok))

        if not ok:
            if key in ALLOWED_DEVIATIONS:
                bug, why = ALLOWED_DEVIATIONS[key]
                if extra:  # 语义校验的实测细节一并带出，便于归属人定位
                    why = f"{why}｜实测：{extra}"
                deviations.append((sid, rnd, expect, actual, (bug, why)))
            else:
                failures.append((sid, rnd, expect, actual, extra or card.splitlines()[0]))

    for sid, stype, rnd, exp, act, ok in rows:
        # ok 为 False 时：已归责 → ⚠️；否则 → ❌
        mark = "✅" if ok else ("⚠️" if (sid, rnd) in ALLOWED_DEVIATIONS else "❌")
        note = "" if ok else "  ← 语义未落地" if (sid, rnd) in EXPECT_STATUS else ""
        print(f"   {mark} {sid} ({stype}) 第{rnd+1}轮  期望={exp:<8} 实际={act}{note}")

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

    audit_whitelist(rows)

    if problems or failures:
        raise AssertionError(
            f"场景走查未通过：数据问题 {len(problems)} 项、断言失败 {len(failures)} 项")
    print("\n✅ 模块五·2 场景走查完成（偏差项已归责登记）")
    return rows, deviations


def audit_whitelist(rows):
    """白名单自维护体检（2026-10-22 新增）。

    ALLOWED_DEVIATIONS 是硬编码的「已知偏差」豁免表，随着队友修复推进它会过时。
    过时的白名单会**掩盖真实回归**（本该失败的项被静默豁免），所以这里做三项体检：

      1. 失效项：白名单里的 (场景, 轮次) 本轮已经通过 → 该偏差已消失，应移除豁免；
      2. 孤儿归责：白名单引用的 BUG-ID 在缺陷看板里不存在（拼错或已下线）；
      3. 探针已绿但场景仍偏差：说明「最小复现修好了、完整场景仍残留」，提示归属人复核。

    本节只输出提示、不判失败 —— 白名单清理属于测试负责人的主动维护动作，
    但若长期不清理会自动累积噪音，故在此显式点名。
    """
    try:
        from test_defects import DEFECTS as _DEFECTS
        known_ids = {d["id"] for d in _DEFECTS}
        fixed_map = None
    except Exception:      # 缺陷看板不可用时跳过第 2/3 项
        known_ids, fixed_map = None, None

    # 注意列序：rows 每行是 (sid, scene_type, rnd, expect, actual, ok)，
    # 轮次在第 3 列（r[2]），不是 r[1]（2026-10-23 修正：此前取错列导致
    # 「失效项」永远查不出来，5 项过期豁免被静默保留）。
    ok_keys = {(r[0], r[2]) for r in rows if r[5]}
    stale = sorted(k for k in ALLOWED_DEVIATIONS if k in ok_keys)
    orphan = []
    probe_green = []
    no_owner = sorted(k for k in ALLOWED_DEVIATIONS if k not in DEVIATION_OWNER)
    if known_ids is not None:
        for k, (bug, _why) in ALLOWED_DEVIATIONS.items():
            if bug not in known_ids:
                orphan.append((k, bug))
        # 只对仍偏差的项做「探针已绿」提示
        if fixed_map is None:
            try:
                from test_defects import DEFECTS as D2
                for k, (bug, _why) in ALLOWED_DEVIATIONS.items():
                    if k in ok_keys or bug not in known_ids:
                        continue
                    d = next(x for x in D2 if x["id"] == bug)
                    fixed, _ev = d["probe"]()
                    if fixed:
                        probe_green.append((k, bug))
            except Exception:
                pass

    print("\n[4] 白名单自检（ALLOWED_DEVIATIONS 维护体检）")
    if not (stale or orphan or probe_green or no_owner):
        print("   ✅ 无失效项、无孤儿归责、无「探针已绿但场景仍偏差」项")
        return
    if stale:
        print(f"   🧹 {len(stale)} 项已失效（本轮已通过，必须移除豁免，否则掩盖回归）：")
        for sid, rnd in stale:
            print(f"      · {sid} 第{rnd+1}轮 → {ALLOWED_DEVIATIONS[(sid, rnd)][0]}")
    if orphan:
        print("   🔗 归责到不存在的缺陷 ID（请核对拼写或已下线）：")
        for (sid, rnd), bug in orphan:
            print(f"      · {sid} 第{rnd+1}轮 → {bug}")
    if probe_green:
        print("   🔍 探针已转绿但完整场景仍偏差（最小复现已修、场景残留）—— 归属人与复现：")
        for (sid, rnd), bug in probe_green:
            print(f"      · {sid} 第{rnd+1}轮 → {bug}")
            if (sid, rnd) in DEVIATION_OWNER:
                who, repro, want = DEVIATION_OWNER[(sid, rnd)]
                print(f"        归属：{who}")
                print(f"        复现：{repro}")
                print(f"        判定：{want}")
    if no_owner:
        print("   📮 未登记归属人/复现文本的残留项（补齐后归属人才能照抄复现）：")
        for sid, rnd in no_owner:
            print(f"      · {sid} 第{rnd+1}轮 → {ALLOWED_DEVIATIONS[(sid, rnd)][0]}")


if __name__ == "__main__":
    run()
