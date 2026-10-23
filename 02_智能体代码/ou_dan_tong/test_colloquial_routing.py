"""模块五·1 补充：口语化匹配回归测试（无 Key，纯本地确定性）。

归属：队友B（模块五 测试验证）。运行：python test_colloquial_routing.py

背景：网页「5 个快捷按钮」发送的是纯口语消息（参照模块一模拟数据话术），
真实 LLM 抽取时常见的口语变形会让「在途单命中」失败，进而把本应「更新」的
消息误判成「缺失占位」。本测试用模拟抽取结果复现这些变形并断言路由正确：

  - 客户抽成「XX电气的王工」（语序颠倒）↔ 台账「王工（XX电气）」
  - 产品抽成「NMX-60 那批」（后缀包裹）↔ 台账「NMX-60 断路器外壳」
  - 变更动词出现在原文而非 source_excerpt 时，仍应判为「主动更新」而非「冲突」

覆盖：new / update / missing / conflict / abnormal 五类判定均走到正确分支。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store
from agent import handle_message
from sim_chats import SEED_MOCK

# 模拟真实 LLM 可能抽出的「口语化变形」结果（正是此前误判的根因）
MOCKS = {
    "new": [
        {"type": "order", "customer": "瑞安精工", "product": "轴承座 UC205", "spec": "",
         "qty": 120, "unit_price": "5.5", "delivery_date": "2026-12-01", "status_hint": "沟通中",
         "sample_flag": "无", "source_excerpt": "瑞安精工 轴承座 UC205 120个 12月1号交"},
    ],
    # 关键：客户「XX电气的王工」、产品「NMX-60 那批」——语序颠倒 + 后缀包裹
    "update": [
        {"type": "order", "customer": "XX电气的王工", "product": "NMX-60 那批",
         "qty": 6000, "source_excerpt": "NMX-60 那批数量改成 6000 个"},
    ],
    "missing": [
        {"type": "order", "customer": "龙湾机械", "product": "齿轮箱", "spec": "",
         "qty": None, "unit_price": "待报价", "delivery_date": None, "status_hint": "沟通中",
         "sample_flag": "无", "source_excerpt": "龙湾机械 齿轮箱 再走一批（数量交期都没说）"},
    ],
    "conflict": [
        {"type": "order", "customer": "温州机电", "product": "法兰盘",
         "delivery_date": "2026-11-28", "source_excerpt": "温州机电 法兰盘 交期 11-28 你核对一下"},
    ],
    "abnormal": [
        {"type": "order", "customer": "温州机电", "product": "法兰盘",
         "qty": -50, "source_excerpt": "法兰盘 数量改成 -50（手滑多打负号）"},
    ],
}

TEXTS = {
    "new":     "李经理，我们瑞安精工想订一批轴承座 UC205，120个，12月1号交，单价5.5你看行不？",
    "update":  "李经理，我是XX电气的王工，NMX-60 那批数量改成 6000 个",
    "missing": "李经理，我们龙湾机械想再走一批齿轮箱，你帮我排一下",
    "conflict": "李经理，我们温州机电的法兰盘，交期是 11 月 28 号，你核对一下",
    "abnormal": "李经理，我们温州机电的法兰盘，数量改成 -50",
}

CHECKS = {
    "new":     lambda r: "已登记新订单" in r,
    "update":  lambda r: "已更新订单" in r and "6000" in r,
    "missing": lambda r: "信息不全已占位" in r,
    "conflict": lambda r: "冲突待确认" in r,
    "abnormal": lambda r: "异常拦截" in r,
}


def main():
    store = Store()
    store.reset()
    handle_message("__seed__", store, llm=None, mock=SEED_MOCK)

    nmx = store.find_active("王工（XX电气）", "NMX-60 断路器外壳")
    assert nmx and nmx["qty"] == 5000, "seed NMX-60 应为 5000"

    failed = []
    for k in ("new", "update", "missing", "conflict", "abnormal"):
        res = handle_message(TEXTS[k], store, llm=None, mock=MOCKS[k])
        reply = res["reply"]
        if not CHECKS[k](reply):
            failed.append((k, reply))
            print(f"❌ {k:8s} 路由错误：{reply[:60]!r}")
        else:
            print(f"✅ {k:8s} 路由正确：{reply.splitlines()[0][:46]}")

    # update 必须把在途单数量从 5000 改为 6000（而非新建/占位）
    nmx2 = store.find_active("王工（XX电气）", "NMX-60 断路器外壳")
    if not nmx2 or nmx2["qty"] != 6000:
        failed.append(("update-qty", f"NMX-60 qty={nmx2['qty'] if nmx2 else None}"))

    if failed:
        print(f"\n=== 失败 {len(failed)} 项：{[f[0] for f in failed]} ===")
        raise SystemExit(1)
    print("\n=== 口语化匹配回归全部通过 ✅ ===")


if __name__ == "__main__":
    main()
