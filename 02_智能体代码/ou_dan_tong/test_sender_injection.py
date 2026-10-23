# -*- coding: utf-8 -*-
"""发送者身份注入回归测试（无需 Key，秒级）。

背景（真实故障复盘）：
    网页端快捷按钮只把消息正文发给 /api/chat，丢掉了「这条消息是谁发的」。
    于是「小陈，DZ47 那个加到 80 个」这类客户自报消息，正文里没有客户名，
    LLM 只能把 customer 抽成 null → validate_extraction 判「缺客户」→ 整条进 skipped，
    用户看到的是「⚠️ 有 1 条信息不全（缺客户或产品），已跳过」，即"答案完全不准确"。
    实测：不带 sender → customer=None；带 sender=李姐 → customer='李姐'。

本测试用假 LLM 捕获实际下发的 prompt，确保：
    1. sender 被拼成 [发送者:xxx] 前缀进入 user 消息；
    2. SENDER_RULE 规则随 sender 一起下发；
    3. 无 sender 时不注入、不污染 prompt；
    4. 网页 5 个快捷按钮都带 sender；
    5. sender 能一路透传到 parse_chat → extract。
运行：python test_sender_injection.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extractor import extract
from agent import parse_chat
from prompts import SENDER_RULE
from store import Store
from sim_chats import WEB_DEMO_SEED
from agent import handle_message

ROOT = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []


def ok(cond, msg, detail=""):
    (PASS if cond else FAIL).append(msg)
    print(("  ✅ " if cond else "  ❌ ") + msg + ("" if cond else f"  ← {detail}"))


class _CaptureLLM:
    """假 LLM：记录 prompt，返回固定 JSON，绝不联网。"""

    def __init__(self, payload='[{"type":"order","customer":"李姐","product":"DZ47","qty":80}]'):
        self.payload = payload
        self.calls = []

    def chat(self, system, user, json_mode=False):
        self.calls.append({"system": system, "user": user, "json_mode": json_mode})
        return self.payload


print("=" * 68)
print("发送者身份注入回归（真实故障：customer=null 导致整条被丢弃）")
print("=" * 68)

# ---------------- T1 sender 注入 user 前缀 ----------------
print("\nT1 sender 注入 prompt")
cap = _CaptureLLM()
extract("小陈，DZ47 那个加到 80 个", cap, sender="李姐")
ok(len(cap.calls) == 1, "T1 只调用一次 LLM", len(cap.calls))
call = cap.calls[0]
ok(call["user"].startswith("[发送者:李姐] "), "T1 user 消息带 [发送者:李姐] 前缀", call["user"][:40])
ok("小陈，DZ47 那个加到 80 个" in call["user"], "T1 原文完整保留未被改写", call["user"][:60])
ok(SENDER_RULE.strip() in call["system"], "T1 system 包含 SENDER_RULE 规则")
ok(call["json_mode"] is True, "T1 仍以 JSON 模式请求")

# ---------------- T2 无 sender 不注入 ----------------
print("\nT2 无 sender 时不污染 prompt")
cap2 = _CaptureLLM()
extract("小陈，DZ47 那个加到 80 个", cap2, sender=None)
ok(cap2.calls[0]["user"] == "小陈，DZ47 那个加到 80 个", "T2 无 sender 时原文直传", cap2.calls[0]["user"][:40])
ok(SENDER_RULE.strip() not in cap2.calls[0]["system"], "T2 无 sender 时不追加 SENDER_RULE")

# ---------------- T3 sender 透传到 parse_chat ----------------
print("\nT3 sender 从 parse_chat 透传到 extract")
cap3 = _CaptureLLM()
s = Store(); s.reset()
handle_message("__seed__", s, llm=None, mock=WEB_DEMO_SEED)
parse_chat("小陈，DZ47 那个加到 80 个", s, llm=cap3, sender="李姐")
ok(cap3.calls and cap3.calls[0]["user"].startswith("[发送者:李姐] "),
   "T3 parse_chat(sender=...) 一路传到 extract",
   cap3.calls[0]["user"][:40] if cap3.calls else "未调用")

# ---------------- T4 带 sender 后不再被丢弃 ----------------
print("\nT4 带 sender 后订单不再判为「信息不全」")
s2 = Store(); s2.reset()
handle_message("__seed__", s2, llm=None, mock=WEB_DEMO_SEED)
card, meta = parse_chat("小陈，DZ47 那个加到 80 个", s2, llm=_CaptureLLM(), sender="李姐")
ok(meta["skipped"] == 0, "T4 不再产生 skipped", meta["skipped"])
ok(len(meta["orders"]) == 1, "T4 成功录入/更新 1 单", len(meta["orders"]))
ok("信息不全" not in card, "T4 卡片不再是「信息不全已跳过」", card[:60])

# ---------------- T5 前端 5 个按钮都带 sender ----------------
print("\nT5 网页快捷按钮均携带 sender")
html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
block = re.search(r"const CHAT_SCENARIOS\s*=\s*\[(.*?)\n\];", html, re.S)
ok(block is not None, "T5 找到 CHAT_SCENARIOS 定义")
if block:
    items = re.findall(r"scenario:\"(\w+)\"[^}]*?sender:\"([^\"]+)\"", block.group(1))
    ok(len(items) == 5, "T5 5 个按钮都声明了 sender", len(items))
    names = {k: v for k, v in items}
    expect = {"new": "阿珍", "update": "李姐", "missing": "王总", "conflict": "王总", "abnormal": "李姐"}
    for k, v in expect.items():
        ok(names.get(k) == v, f"T5 {k} 的 sender = {v}", names.get(k))
    ok("sender: sender||null" in html, "T5 发送请求体带上 sender 字段")

print("\n" + "=" * 68)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
    print("❌ FAILED")
    sys.exit(1)
print("✅ 发送者身份注入全部通过")
