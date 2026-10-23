# -*- coding: utf-8 -*-
"""模块五 · 真实模式回归（B-1）—— 全程假 LLM，零网络，秒级可复跑。

背景（2026-10-23 队长复盘）：
    模块五此前所有测试（test_pipeline / test_scenarios / test_defects / test_e2e）
    **全部跑在 Mock 模式**，真实模式（走大模型抽取）零覆盖。后果已经发生过：
    真实模式里「网页按钮只发正文、丢掉微信群发言人昵称」，客户被抽成 null、
    整条消息被判信息不全 —— 12 项缺陷探针一个都没测出来，靠人工点按钮才发现。
    同批修的另外三处（网关超时降级 / 缺客户缺产品登记占位单 / 响应头禁缓存）
    同样没有任何测试把守。

本套件把这四处（含 sender 注入本身）全部纳入回归：

    T1  sender 注入 prompt：`[发送者:xxx] ` 前缀 + SENDER_RULE 随行；无 sender 不注入
    T2  sender 修复效果：无 sender → 落不到真实客户名下；有 sender → 正常建单
    T3  网关超时降级：LLM 必抛 TimeoutError，五类场景仍各出正确结论且不含异常字样
    T4  缺客户 / 缺产品：登记「待补充」占位单进台账，卡片点名缺什么（不再整条丢弃）
    T5  响应头禁缓存：Cache-Control: no-store（真实起 HTTP 服务实测 + 源码断言）

设计红线：
    1. **零网络**：LLM 一律是假对象，不产生任何外联；给 web_app 的 Key/URL 是假值，
       且在真正调用前就被替换掉；
    2. **反向可验证**：把 extractor.py 的 `if sender:` 改成 `if False:`，
       T1 / T2 必须变红（否则说明测试没在把守）—— 见文件末尾「反向验证」注释；
    3. 与 test_e2e.py（Mock 模式端到端）互补，不重复。

运行：python test_real_mode.py
"""
import inspect
import os
import re
import socket
import sys
import threading
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# —— 关键：必须在 import web_app 之前把环境伪装成「真实模式」——
# web_app 在 import 时就按环境变量决定 USE_MOCK / _llm：只有检测到 Key 才走真实抽取链路。
# 这里的 Key / URL 全是假值（且指向不可达端口），稍后立刻把 _llm 换成假 LLM，全程不联网。
for _k in ("DEEPSEEK_API_KEY", "OU_DEMO_MODE", "OU_COLLOQUIAL"):
    os.environ.pop(_k, None)
os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:59999/v1"
os.environ["OPENAI_API_KEY"] = "fake-key-no-network"
os.environ["OPENAI_MODEL"] = "fake-model"

from agent import handle_message, parse_chat          # noqa: E402
from extractor import extract                          # noqa: E402
from prompts import SENDER_RULE                        # noqa: E402
from sim_chats import WEB_DEMO_SEED                    # noqa: E402
from store import Store                                # noqa: E402

_T0 = time.time()
PASS, FAIL = [], []


def ok(cond, msg, detail=""):
    (PASS if cond else FAIL).append(msg)
    print(("  ✅ " if cond else "  ❌ ") + msg + ("" if cond else f"  ← {detail}"))
    return bool(cond)


# ---------------------------------------------------------------------------
# 假 LLM：三种人格
# ---------------------------------------------------------------------------
class FakeLLM:
    """记录 prompt，返回固定 JSON。绝不联网。"""

    def __init__(self, payload='[{"type":"order","customer":"李姐","product":"DZ47","qty":80}]'):
        self.payload = payload
        self.calls = []

    def chat(self, system, user, json_mode=False):
        self.calls.append({"system": system, "user": user, "json_mode": json_mode})
        return self.payload


class RuleAwareLLM(FakeLLM):
    """按 SENDER_RULE 行事的假 LLM（模拟真实模型的行为）：

    prompt 里有 `[发送者:xxx]` 就把 customer 抽成 xxx；没有就抽成 null
    —— 真实模型在正文无客户名时就是这个表现，这也是线上故障的根因。
    """

    def chat(self, system, user, json_mode=False):
        self.calls.append({"system": system, "user": user, "json_mode": json_mode})
        m = re.match(r"\[发送者:([^\]]+)\]\s*", user)
        cust = m.group(1) if m else None
        return (
            '[{"type":"order","customer":%s,"product":"DZ47-63 三相断路器",'
            '"qty":80,"unit_price":"26","delivery_date":"2026-10-27",'
            '"status_hint":"已确认","source_excerpt":"小陈，DZ47 那个加到 80 个"}]'
            % ("null" if cust is None else '"%s"' % cust)
        )


class DeadLLM:
    """必抛超时，模拟词元天下网关挂掉（实测单次 18~50s，偶发撞满 timeout）。"""

    def __init__(self):
        self.calls = []

    def chat(self, *a, **k):
        self.calls.append(a)
        raise TimeoutError("The read operation timed out")


def fresh_store():
    s = Store()
    s.reset()
    handle_message("__seed__", s, llm=None, mock=WEB_DEMO_SEED)
    return s


print("=" * 78)
print("模块五 · 真实模式回归（假 LLM，零网络）")
print("=" * 78)

# ===========================================================================
# T1 · sender 注入 prompt
# ===========================================================================
print("\n[T1] sender 注入：网页端必须把「这条消息是谁发的」一起下发")
cap = FakeLLM()
extract("小陈，DZ47 那个加到 80 个", cap, sender="李姐")
ok(len(cap.calls) == 1, "T1 只调用一次 LLM（未重试、未联网）", len(cap.calls))
call = cap.calls[0] if cap.calls else {"system": "", "user": ""}
ok(call["user"].startswith("[发送者:李姐] "), "T1 user 消息以 [发送者:李姐] 开头",
   call["user"][:40])
ok("小陈，DZ47 那个加到 80 个" in call["user"], "T1 原文完整保留、未被改写", call["user"][:60])
ok(SENDER_RULE.strip() in call["system"], "T1 system 随 sender 一起带上 SENDER_RULE")
ok(call.get("json_mode") is True, "T1 仍以 JSON 模式请求")

cap2 = FakeLLM()
extract("小陈，DZ47 那个加到 80 个", cap2, sender=None)
ok(cap2.calls[0]["user"] == "小陈，DZ47 那个加到 80 个",
   "T1 无 sender 时原文直传、不加前缀", cap2.calls[0]["user"][:40])
ok(SENDER_RULE.strip() not in cap2.calls[0]["system"],
   "T1 无 sender 时不追加 SENDER_RULE（不污染 prompt）")

# ===========================================================================
# T2 · sender 修复效果（真实故障复盘）
# ===========================================================================
print("\n[T2] sender 修复效果：不带 sender → 落不到客户名下；带 sender → 正常建单")
_TEXT = "小陈，DZ47 那个加到 80 个"

# 无 sender
llm_no = RuleAwareLLM()
s_no = fresh_store()
n0 = len(s_no.orders)
card_no, meta_no = parse_chat(_TEXT, s_no, llm=llm_no, sender=None)
ok("✅" not in card_no, "T2 无 sender：没有「已登记/已更新」成功卡片（订单未落到客户名下）",
   card_no[:70])
ok(len(meta_no["orders"]) == 0, "T2 无 sender：未产生正式订单", len(meta_no["orders"]))
placeholder = [o for o in s_no.orders if o.get("status") == "待补充"]
ok(len(s_no.orders) == n0 + 1 and len(placeholder) == 1,
   "T2 无 sender：只多出 1 条「待补充」占位单（PR #13 起不再整条丢弃）",
   f"{n0}→{len(s_no.orders)}，占位 {len(placeholder)}")
ok("客户信息" in card_no, "T2 无 sender：卡片说清缺的是「客户信息」", card_no[:80])
ok(meta_no["skipped"] == 0,
   "T2 无 sender：skipped=0（缺客户已改为登记占位单，2026-10-23 起的新契约）",
   meta_no["skipped"])

# 有 sender
llm_yes = RuleAwareLLM()
s_yes = fresh_store()
n1 = len(s_yes.orders)
card_yes, meta_yes = parse_chat(_TEXT, s_yes, llm=llm_yes, sender="李姐")
# 台账里已 seed「李姐（瓯越五金贸易）/ DZ47-63 三相断路器」，find_active 会模糊命中，
# 因此带 sender 的正确结果是「更新到该客户名下」而非新建 —— 这正是线上丢失 sender 时
# 拿不到的结果（对照 T2 无 sender 分支：落不到客户名下，只多一条待补充占位单）。
ok("✅" in card_yes and ("已更新订单" in card_yes or "已登记新订单" in card_yes),
   "T2 带 sender：正常出「✅ 已更新/已登记」成功卡片", card_yes[:70])
ok(len(meta_yes["orders"]) == 1, "T2 带 sender：成功落单 1 单", len(meta_yes["orders"]))
ok(meta_yes["skipped"] == 0, "T2 带 sender：skipped=0", meta_yes["skipped"])
hit = next((o for o in s_yes.orders if "李姐" in str(o.get("customer"))
            and o.get("qty") == 80), None)
ok(hit is not None, "T2 带 sender：台账里李姐那单数量已更新为 80",
   str([(o.get("customer"), o.get("qty")) for o in s_yes.orders]))
ok(not any(o.get("status") == "待补充" and o.get("customer") == "" for o in s_yes.orders),
   "T2 带 sender：不产生「无客户」占位脏单")

# ===========================================================================
# T3 · 网关超时降级（DeadLLM）
# ===========================================================================
print("\n[T3] 网关超时降级：LLM 必抛 TimeoutError，五类场景仍要出结论")
import web_app  # noqa: E402  （必须晚于上面的环境变量伪装）

ok(web_app.USE_MOCK is False,
   "T3 运行在真实模式链路（USE_MOCK=False，非 Mock 分支）", web_app.USE_MOCK)
web_app._llm = DeadLLM()

_SCENARIOS = [
    ("new", "老周，秋季那款运动鞋鞋底 EVA 发泡的，先给我 800 双，最好 11 月 10 号前到",
     "阿珍", "✅ 已登记新订单"),
    ("update", "小陈，DZ47 那个加到 80 个", "李姐", "✅ 已更新订单"),
    ("missing", "小陈，法兰那批的货再帮我排上一批，我这边急", "王总", "🟡"),
    ("conflict", "小陈，法兰 DN80 那单单价你按 18 算，上次说好的", "王总", "⚠️ 冲突待确认"),
    ("abnormal", "小陈，DZ47 那个数量给我改成 -300", "李姐", "⛔ 异常拦截"),
]

for name, text, sender, mark in _SCENARIOS:
    web_app._store.reset()
    handle_message("__seed__", web_app._store, llm=None, mock=WEB_DEMO_SEED)
    before = len(web_app._store.orders)
    r = web_app.do_chat(text, scenario=name, sender=sender)
    reply = r.get("reply", "")
    ok(mark in reply, f"T3 {name}：降级后仍出「{mark}」结论", reply[:90])
    ok("已由本地规则引擎" in reply, f"T3 {name}：明确告知已由本地规则引擎判定", reply[:90])
    ok("TimeoutError" not in reply and "timed out" not in reply.lower(),
       f"T3 {name}：回复里绝不出现 TimeoutError 字样", reply[:90])
    ok(r.get("degraded") is True, f"T3 {name}：结果带 degraded 标记", r.get("degraded"))
    after = len(web_app._store.orders)
    if name == "abnormal":
        ok(after == before, f"T3 {name}：异常拦截不写入台账（{before}→{after}）",
           f"{before}→{after}")
    if name == "new":
        ok(after == before + 1, f"T3 {name}：新建确实入库（{before}→{after}）",
           f"{before}→{after}")

# 无 scenario 的裸消息：也要给出可执行的引导，而不是抛错
web_app._store.reset()
handle_message("__seed__", web_app._store, llm=None, mock=WEB_DEMO_SEED)
r = web_app.do_chat("帮我看看今天整体情况", scenario=None, sender=None)
ok("TimeoutError" not in r.get("reply", ""), "T3 无场景兜底：回复里无异常字样",
   r.get("reply", "")[:80])

# ===========================================================================
# T4 · 缺客户 / 缺产品 → 登记「待补充」占位单
# ===========================================================================
print("\n[T4] 缺客户 / 缺产品：登记占位单进台账，卡片点名缺什么（不再整条丢弃）")
_CASES = [
    ("缺客户", [{"type": "order", "customer": None, "product": "齿轮箱",
                 "qty": 100, "delivery_date": "2026-11-05",
                 "source_excerpt": "齿轮箱再走一批 100 个"}]),
    ("缺产品", [{"type": "order", "customer": "龙湾机械", "product": None,
                 "qty": 100, "delivery_date": "2026-11-05",
                 "source_excerpt": "龙湾机械再走一批 100 个"}]),
    ("两者都缺", [{"type": "order", "customer": None, "product": None,
                  "qty": 100, "delivery_date": "2026-11-05",
                  "source_excerpt": "再走一批 100 个"}]),
]
for label, mock in _CASES:
    s = Store()
    s.reset()
    n = len(s.orders)
    card, meta = parse_chat("walkthrough", s, mock=mock)
    ok("🟡" in card, f"T4 {label}：卡片是「🟡 待补充」而非静默丢弃", card[:70])
    ok(meta["skipped"] == 0, f"T4 {label}：skipped=0（不再丢弃）", meta["skipped"])
    ok(len(s.orders) == n + 1, f"T4 {label}：台账新增 1 条",
       f"{n}→{len(s.orders)}")
    rec = s.orders[-1] if s.orders else {}
    ok(rec.get("status") == "待补充", f"T4 {label}：新增单状态=待补充", rec.get("status"))
    if label == "缺客户":
        ok("客户信息" in card, "T4 缺客户：卡片点名缺「客户信息」", card[:90])
        ok("产品" in card and "齿轮箱" in card, "T4 缺客户：同时回显已知产品「齿轮箱」",
           card[:90])
    elif label == "缺产品":
        ok("产品信息" in card, "T4 缺产品：卡片点名缺「产品信息」", card[:90])
        ok("龙湾机械" in card, "T4 缺产品：同时回显已知客户「龙湾机械」", card[:90])
    else:
        ok("客户信息" in card and "产品信息" in card,
           "T4 两者都缺：卡片同时点名客户与产品", card[:90])

# ===========================================================================
# T5 · 响应头禁缓存
# ===========================================================================
print("\n[T5] 响应头禁缓存：改了代码必须立刻生效，不能被浏览器缓存蒙住")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


from http.server import ThreadingHTTPServer  # noqa: E402

_port = _free_port()
_srv = ThreadingHTTPServer(("127.0.0.1", _port), web_app.Handler)
_th = threading.Thread(target=_srv.serve_forever, daemon=True)
_th.start()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{_port}/", timeout=10) as resp:
        _cc = resp.headers.get("Cache-Control", "")
        _pragma = resp.headers.get("Pragma", "")
        _expires = resp.headers.get("Expires", "")
        _html = resp.read().decode("utf-8", "replace")
    ok("no-store" in _cc, "T5 HTML 响应含 Cache-Control: no-store", _cc)
    ok("no-cache" in _cc or _pragma.lower() == "no-cache",
       "T5 同时带 no-cache / Pragma: no-cache", f"{_cc} | {_pragma}")
    ok(_expires == "0", "T5 Expires: 0（历史响应立即失效）", _expires)
    ok("<html" in _html.lower(), "T5 首页确实返回 HTML", _html[:40])

    # JSON 接口同样禁缓存（否则 /api/ledger 会拿旧台账渲染）
    _req = urllib.request.Request(f"http://127.0.0.1:{_port}/api/ledger", method="GET")
    with urllib.request.urlopen(_req, timeout=10) as resp2:
        _cc2 = resp2.headers.get("Cache-Control", "")
    ok("no-store" in _cc2, "T5 /api/ledger 接口同样禁缓存", _cc2)
finally:
    _srv.shutdown()
    _srv.server_close()

_src = inspect.getsource(web_app.Handler._send)
ok("Cache-Control" in _src and "no-store" in _src,
   "T5 源码层：Handler._send 硬性写入 no-store（防止被后续改动悄悄删掉）")

# ---------------------------------------------------------------------------
print("\n" + "=" * 78)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项 ｜ 耗时 {time.time() - _T0:.2f}s（零网络）")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
    print("❌ FAILED")
    sys.exit(1)
print("✅ 真实模式回归全部通过")

# ---------------------------------------------------------------------------
# 反向验证（维护者必读，改 extractor.py 后请照做一次）：
#   1. 把 extractor.py 里 `if sender:` 改成 `if False:`
#   2. 运行 python test_real_mode.py —— T1 的「[发送者:李姐] 前缀 / SENDER_RULE」
#      与 T2 的「带 sender 正常建单」必须全部变红
#   3. 验完改回 `if sender:`，再跑一次确认转绿
# 同理：把 web_app.do_chat 的 try/except 降级分支删掉 → T3 必红；
#       把 Handler._send 的 Cache-Control 行删掉 → T5 必红；
#       把 validator.classify_order 的「缺客户/缺产品」分支改回丢弃 → T4 必红。
# 任一项「拆了还绿」都说明这条测试没在把守，需要补断言。
# ---------------------------------------------------------------------------
