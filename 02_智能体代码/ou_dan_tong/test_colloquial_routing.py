"""模块五·1 补充：口语化匹配回归测试（无 Key，纯本地确定性）。

归属：队友B（模块五 测试验证）。运行：python test_colloquial_routing.py

背景：网页「5 个快捷按钮」发送的是模块一 chat_logs.json 里客户的真实原话
（无括号备注、纯口语），真实 LLM 抽取时常见的口语变形会让「在途单命中」失败，
进而把本应「更新」的消息误判成「缺失占位」。本测试用模拟抽取结果复现这些变形
并断言路由正确。演示 seed 已对齐模块一实体（WEB_DEMO_SEED）：

  · 种子在途单：王总/永嘉精密机械/304法兰盘DN80、李姐/瓯越五金/DZ47-63、陈老板/瑞安汽配/轴承6204
  · 新建按钮：阿珍/鹿城鞋材/EVA 鞋底（不在 seed，演示新建）
  · 改数量按钮：李姐/DZ47-63 → 50→80
  · 信息缺失按钮：王总/法兰（命中在途单但缺数量/交期）→ 缺失占位
  · 数据冲突按钮：王总/法兰 DN80 单价 18 vs 台账 18.5 → 冲突
  · 异常拦截按钮：李姐/DZ47-63 数量 -300 → 异常

覆盖：new / update / missing / conflict / abnormal 五类判定均走到正确分支。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store
from agent import handle_message
from sim_chats import WEB_DEMO_SEED

# 模拟真实 LLM 可能抽出的「口语化变形」结果（正是此前误判的根因）
MOCKS = {
    "new": [
        {"type": "order", "customer": "鹿城鞋材", "product": "EVA 发泡鞋底", "spec": "",
         "qty": 800, "unit_price": "12.5", "delivery_date": "2026-11-10", "status_hint": "沟通中",
         "sample_flag": "无", "source_excerpt": "EVA 发泡鞋底 800双 11-10交"},
    ],
    # 关键：客户「李姐（瓯越五金）」、产品「DZ47 那个」——语序颠倒 + 后缀包裹
    "update": [
        {"type": "order", "customer": "李姐（瓯越五金）", "product": "DZ47 那个",
         "qty": 80, "source_excerpt": "DZ47 那个加到 80 个"},
    ],
    # 客户「王总」、产品「法兰那批」——口语变形，应命中在途单 O1 但缺数量/交期 → 缺失
    "missing": [
        {"type": "order", "customer": "王总", "product": "法兰那批", "spec": "",
         "qty": None, "unit_price": "待报价", "delivery_date": None, "status_hint": "沟通中",
         "sample_flag": "无", "source_excerpt": "法兰那批的货再帮我排上一批"},
    ],
    # 单价 18 vs 台账 18.5 → 冲突（两个口径）
    "conflict": [
        {"type": "order", "customer": "王总（永嘉精密机械）", "product": "法兰 DN80",
         "unit_price": "18", "source_excerpt": "法兰 DN80 那单单价按 18 算"},
    ],
    "abnormal": [
        {"type": "order", "customer": "李姐（瓯越五金贸易）", "product": "DZ47-63 三相断路器",
         "qty": -300, "source_excerpt": "DZ47 那个数量改成 -300"},
    ],
}

# 与网页 CHAT_SCENARIOS 完全一致（模块一 chat_logs.json 客户原话）
TEXTS = {
    "new":     "老周，秋季那款运动鞋鞋底 EVA 发泡的，先给我 800 双，最好 11 月 10 号前到，客户催得紧",
    "update":  "小陈，DZ47 那个加到 80 个",
    "missing": "小陈，法兰那批的货再帮我排上一批，我这边急",
    "conflict": "小陈，法兰 DN80 那单单价你按 18 算，上次说好的",
    "abnormal": "小陈，DZ47 那个数量给我改成 -300",
}

CHECKS = {
    "new":     lambda r: "已登记新订单" in r,
    "update":  lambda r: "已更新订单" in r and "80" in r,
    "missing": lambda r: "信息不全已占位" in r,
    "conflict": lambda r: "冲突待确认" in r,
    "abnormal": lambda r: "异常拦截" in r,
}


def main():
    store = Store()
    store.reset()
    handle_message("__seed__", store, llm=None, mock=WEB_DEMO_SEED)

    dz = store.find_active("李姐（瓯越五金贸易）", "DZ47-63 三相断路器")
    assert dz and dz["qty"] == 50, "seed DZ47-63 应为 50"

    failed = []
    for k in ("new", "update", "missing", "conflict", "abnormal"):
        res = handle_message(TEXTS[k], store, llm=None, mock=MOCKS[k])
        reply = res["reply"]
        if not CHECKS[k](reply):
            failed.append((k, reply))
            print(f"❌ {k:8s} 路由错误：{reply[:60]!r}")
        else:
            print(f"✅ {k:8s} 路由正确：{reply.splitlines()[0][:46]}")

    # update 必须把在途单数量从 50 改为 80（而非新建/占位）
    dz2 = store.find_active("李姐（瓯越五金贸易）", "DZ47-63 三相断路器")
    if not dz2 or dz2["qty"] != 80:
        failed.append(("update-qty", f"DZ47-63 qty={dz2['qty'] if dz2 else None}"))

    if failed:
        print(f"\n=== 失败 {len(failed)} 项：{[f[0] for f in failed]} ===")
        raise SystemExit(1)
    print("\n=== 口语化匹配回归（模块一对齐）全部通过 ✅ ===")


if __name__ == "__main__":
    main()
