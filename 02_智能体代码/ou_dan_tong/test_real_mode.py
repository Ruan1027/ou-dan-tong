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
    T6  真实模式 query / briefing：网关挂掉也要能查单、能出简报（此前零覆盖）
        —— 含 BUG-03 / BUG-04 双向守卫：未来交期说「还有 N 天」、真逾期说「已逾期」
    T7  多轮 sender 一致性：第 N 轮发言人不得串到别的客户名下（防跨轮串台）
    T8  HTTP 层 sender 透传：POST /api/chat 的 payload.sender 必须到达 do_chat
        —— 10-23 线上故障就发生在这一层（T1/T2/T3 都在函数层，唯独这一跳此前零覆盖）

设计红线：
    1. **零网络**：LLM 一律是假对象，不产生任何外联；给 web_app 的 Key/URL 是假值，
       且在真正调用前就被替换掉；
    2. **反向可验证**：把 extractor.py 的 `if sender:` 改成 `if False:`，
       T1 / T2 必须变红（否则说明测试没在把守）—— 见文件末尾「反向验证」注释；
    3. 与 test_e2e.py（Mock 模式端到端）互补，不重复。

运行：python test_real_mode.py
"""
import datetime
import inspect
import json
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
from agent import query, briefing                      # noqa: E402
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


class ContentAwareLLM(FakeLLM):
    """按**正文内容**抽取的假 LLM（比 RuleAwareLLM 更贴近真实模型）。

    为什么需要它：RuleAwareLLM 无论正文是什么都返回同一条 DZ47-63/80，
    用它做多轮测试时每一轮抽到的都是同一条数据 —— 于是「上一轮的 sender /
    上下文串到下一轮」这类 bug **根本测不出来**。本类按正文里的产品关键词与
    数量分别返回不同结果，才能真正把守多轮一致性。
    """

    _PRODUCTS = (("DZ47", "DZ47-63 三相断路器"),
                 ("法兰", "304法兰盘 DN80"),
                 ("轴承", "轴承 6204"),
                 ("继电器", "继电器 JQX-13F"))

    def chat(self, system, user, json_mode=False):
        self.calls.append({"system": system, "user": user, "json_mode": json_mode})
        m = re.match(r"\[发送者:([^\]]+)\]\s*", user)
        cust = m.group(1) if m else None
        body = user[m.end():] if m else user
        prod = next((p for k, p in self._PRODUCTS if k in body), "通用件")
        qm = re.search(r"(?:加到|改成|改为|到)\s*(\d+)", body) or re.search(r"(\d+)\s*个", body)
        qty = int(qm.group(1)) if qm else 100
        return (
            '[{"type":"order","customer":%s,"product":"%s","qty":%d,'
            '"unit_price":"26","delivery_date":"2026-10-27",'
            '"status_hint":"已确认","source_excerpt":"%s"}]'
            % ("null" if cust is None else '"%s"' % cust, prod, qty, body[:20])
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

# ===========================================================================
# T6 · 真实模式下的 查询 / 简报 链路（此前零覆盖）
# ===========================================================================
print("\n[T6] 真实模式 query / briefing：网关挂掉也要能查单、能出简报")
_s6 = fresh_store()

_q = query("李姐那单到哪了", _s6, llm=DeadLLM())
ok("李姐" in _q, "T6 query 降级：命中李姐那单", _q[:90])
ok("50" in _q, "T6 query 降级：回显台账真实数量 50", _q[:90])
ok("TimeoutError" not in _q and "timed out" not in _q.lower(),
   "T6 query 降级：不含任何异常字样", _q[:90])
ok("✅" in _q or "已确认" in _q, "T6 query 降级：仍带状态标记", _q[:90])

_b = briefing(_s6, llm=DeadLLM())
ok(_b.lstrip().startswith("📋"), "T6 briefing 降级：仍出简报", _b[:60])
# 简报日期用「真实今天」断言，不硬编码年份前缀——否则跨年后这条会静默挂掉
_today = datetime.date.today().isoformat()
ok(_today in _b, "T6 briefing 降级：简报带今日日期", f"today={_today} ｜ {_b[:60]}")
ok("TimeoutError" not in _b and "timed out" not in _b.lower(),
   "T6 briefing 降级：不含任何异常字样", _b[:80])
# BUG-03 / BUG-04 的真实模式守卫（双向：未来交期与已逾期各断言一次，
# 避免写成「恒真」的弱守卫 —— 若只断言「没有已拖」，无论代码怎么改都绿）
ok("已拖" not in _b,
   "T6 briefing：未来交期没被说成「已拖 X 天」（BUG-03 旧措辞守卫）", _b[:130])
ok("999" not in _b,
   "T6 briefing：无「已拖 999 天」式的缺失兜底（BUG-04 守卫）", _b[:130])
ok("还有" in _b and "已逾期" not in _b,
   "T6 briefing：未来交期说「还有 N 天」而非「已逾期」（BUG-03 正向守卫）", _b[:130])

# 反向侧：把一单交期改到过去，简报必须改口说「已逾期」并列入临期交付
# —— 这条同时证明上面那条不是恒真断言。
_s6b = fresh_store()
# 简报的「最急」只统计「沟通中」的待报价单，故必须挑这种状态的单才测得准
_overdue = next((o for o in _s6b.orders
                 if o.get("delivery_date") and o.get("status") == "沟通中"), None)
if _overdue is not None:
    _overdue["delivery_date"] = "2026-09-01"
_b2 = briefing(_s6b, llm=DeadLLM())
ok("已逾期" in _b2, "T6 briefing：真逾期时改口说「已逾期」（守卫区分度）", _b2[:200])
ok("2026-09-01" in _b2, "T6 briefing：逾期单被列入临期交付清单", _b2[:200])

# ===========================================================================
# T7 · 多轮 sender 一致性：第 N 轮的发言人不能串到别的客户名下
# ===========================================================================
print("\n[T7] 多轮 sender 一致性：两轮不同发言人，各自落到自己客户名下")
_s7 = fresh_store()
_n7 = len(_s7.orders)
_llm7 = ContentAwareLLM()
handle_message("小陈，DZ47 那个加到 80 个", _s7, llm=_llm7, sender="李姐")
handle_message("小陈，法兰那批加到 400 个", _s7, llm=_llm7, sender="王总")

ok(_llm7.calls[-1]["user"].startswith("[发送者:王总] "),
   "T7 第2轮 prompt 的 sender 是王总（不带上一轮的李姐）",
   _llm7.calls[-1]["user"][:40])
ok("[发送者:李姐]" not in _llm7.calls[-1]["user"],
   "T7 第2轮 prompt 里没有上一轮发言人残留", _llm7.calls[-1]["user"][:60])

_li = next((o for o in _s7.orders if "李姐" in str(o.get("customer"))
            and "DZ47" in str(o.get("product"))), None)
_wang = next((o for o in _s7.orders if "王总" in str(o.get("customer"))
              and "法兰" in str(o.get("product"))), None)
ok(_li is not None and _li.get("qty") == 80,
   "T7 李姐那单（DZ47）已更新为 80",
   f"{_li and (_li.get('customer'), _li.get('product'), _li.get('qty'))}")
ok(_wang is not None and _wang.get("qty") == 400,
   "T7 王总那单（法兰）已更新为 400",
   f"{_wang and (_wang.get('customer'), _wang.get('product'), _wang.get('qty'))}")
ok(not any("王总" in str(o.get("customer", "")) and "DZ47" in str(o.get("product", ""))
           for o in _s7.orders),
   "T7 王总名下没有李姐的产品（无跨轮串台）",
   str([(o.get("customer"), o.get("product")) for o in _s7.orders]))
ok(not any("李姐" in str(o.get("customer", "")) and o.get("qty") == 400
           for o in _s7.orders),
   "T7 李姐那单没被第2轮的数量污染",
   str([(o.get("customer"), o.get("qty")) for o in _s7.orders]))
ok(len(_s7.orders) == _n7,
   f"T7 两轮都是更新而非新建脏单（{_n7}→{len(_s7.orders)}）",
   f"{_n7}→{len(_s7.orders)}")

# ===========================================================================
# T8 · 真实模式 HTTP 端到端：POST /api/chat 的 sender 透传（此前只测了函数层）
# ===========================================================================
print("\n[T8] HTTP 层 sender 透传：网页端 POST 必须把 sender 一起带来")
# 10-23 线上故障发生在这一层：前端按钮 POST /api/chat 时没带 sender，
# do_chat 收到 sender=None → 客户抽成 null → 占位单。T1/T2/T3 都在函数层测过，
# 唯独「payload.get("sender") → do_chat(sender=...)」这一跳没有测试把守，补上。


def _post_json(port, path, payload=None, raw=None):
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _reseed():
    web_app._store.reset()
    handle_message("__seed__", web_app._store, llm=None, mock=WEB_DEMO_SEED)


web_app._llm = RuleAwareLLM()   # 真实模式链路 + 按 sender 行事的假模型，零网络
_p8 = _free_port()
_srv8 = ThreadingHTTPServer(("127.0.0.1", _p8), web_app.Handler)
_th8 = threading.Thread(target=_srv8.serve_forever, daemon=True)
_th8.start()
try:
    # ① 带 sender：HTTP 层正确透传 → 正常落单到客户名下
    _reseed()
    r = _post_json(_p8, "/api/chat",
                   {"message": "小陈，DZ47 那个加到 80 个", "sender": "李姐"})
    _reply = r.get("reply", "")
    ok(isinstance(r, dict) and "reply" in r, "T8 ①响应是合法 JSON 且含 reply 字段", str(type(r)))
    ok("✅" in _reply and ("已更新订单" in _reply or "已登记新订单" in _reply),
       "T8 ①带 sender：HTTP 透传后正常出「✅ 已更新/已登记」", _reply[:70])
    _hit = next((o for o in web_app._store.orders
                 if "李姐" in str(o.get("customer")) and o.get("qty") == 80), None)
    ok(_hit is not None, "T8 ①带 sender：李姐那单经 HTTP 已更新为 80",
       str([(o.get("customer"), o.get("qty")) for o in web_app._store.orders]))

    # ② 不带 sender：HTTP 层就缺 sender → 占位单路径（线上故障的复现面）
    _reseed()
    _n0 = len(web_app._store.orders)
    r = _post_json(_p8, "/api/chat", {"message": "小陈，DZ47 那个加到 80 个"})
    _reply = r.get("reply", "")
    ok("✅" not in _reply, "T8 ②不带 sender：不出「✅ 已更新」成功卡片", _reply[:70])
    _placeholders = [o for o in web_app._store.orders
                     if o.get("status") == "待补充"]
    ok(len(web_app._store.orders) == _n0 + 1 and len(_placeholders) == 1,
       "T8 ②不带 sender：只多出 1 条「待补充」占位单",
       f"{_n0}→{len(web_app._store.orders)}，占位 {len(_placeholders)}")
    ok("客户信息" in _reply, "T8 ②不带 sender：卡片点名缺「客户信息」", _reply[:80])

    # ③ 空 message / ④ 畸形 JSON：都不应 500，应给出引导
    _reseed()
    r = _post_json(_p8, "/api/chat", {"message": ""})
    ok("reply" in r and "✅" not in r["reply"],
       "T8 ③空消息：不 500，返回引导话术", r.get("reply", "")[:60])
    r = _post_json(_p8, "/api/chat", raw=b"{not-valid-json")
    ok("reply" in r, "T8 ④畸形 JSON：不 500，payload 兜底为空后仍返回引导",
       r.get("reply", "")[:60])
finally:
    _srv8.shutdown()
    _srv8.server_close()

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
#
# T6 / T7 的反向验证（已实测）：
#   1. T7：把第2轮的 `sender="王总"` 改成 `sender="李姐"`（模拟跨轮串台）
#      → 5 项转红，并暴露真实后果：多出一条「李姐 / 400」脏单、王总那单没被更新。
#   2. T6：把 briefing 的 `tag = "已逾期" if days < 0 else f"还有 {days} 天"` 的
#      判断反过来 → 「未来交期说『还有 N 天』」与「真逾期说『已逾期』」两条同时变红。
#      （双向断言的意义：只断言「没有『已拖』」是恒真的弱守卫，代码怎么改都绿。）
#   3. T6 / T7 共同依赖的假 LLM 选择：多轮一律用 ContentAwareLLM（按正文抽），
#      不要用 RuleAwareLLM —— 后者每轮都返回同一条 canned 数据，串台根本测不出来。
#   4. T8：把 web_app.py 的 `sender=payload.get("sender")` 改成 `sender=None`
#      （模拟前端 POST 没带 sender 的线上故障）→ T8① 两项转红：
#      出占位单、李姐那单没被更新、台账多出一条「无客户 / 80」脏单。
#      ⚠️ 反向验证做替换时不要带行尾注释——会把闭合括号注释掉，变成 SyntaxError。
# ---------------------------------------------------------------------------
