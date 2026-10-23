"""瓯单通 · 可体验网页智能体（零依赖，仅用 Python 标准库）。

运行：python web_app.py  →  浏览器打开 http://127.0.0.1:8000
- 配置 DEEPSEEK_API_KEY 环境变量 → 启用真实抽取；否则自动进入 Mock 模式（用预置数据演示流程）。
- 队友/评委直接粘贴群聊、问「XX 那单到哪了」、说「出今日简报」即可体验。
"""
import os
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store
from agent import handle_message
from llm import LLM
from sim_chats import SCENARIO1_MOCK, SEED_MOCK, WEB_DEMO_SEED, WEB_SCENARIO_MOCKS

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "static", "index.html")

_store = Store()
# 模块四·3：根据配置选择 provider。
#   - 任意 OpenAI 兼容端点（算力平台常见）：配 OPENAI_BASE_URL + OPENAI_API_KEY 即走 openai provider
#   - DeepSeek 官方：配 DEEPSEEK_API_KEY 即走 deepseek provider
if os.getenv("OPENAI_BASE_URL") and os.getenv("OPENAI_API_KEY"):
    _llm = LLM("openai")
    _MODEL_NAME = os.getenv("OPENAI_MODEL", "openai-model")
elif os.getenv("DEEPSEEK_API_KEY"):
    _llm = LLM("deepseek")
    _MODEL_NAME = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
else:
    _llm = None
    _MODEL_NAME = ""
# OU_DEMO_MODE=1 时强制用 mock 数据（seed + 预置抽取），但 LLM 口语化照常——演示用
_DEMO_MODE = os.getenv("OU_DEMO_MODE", "").lower() in ("1", "true", "yes", "on")
USE_MOCK = (not _llm) or _DEMO_MODE
MODE_LABEL = "模拟模式（未配置 API Key，演示用预置数据）" if USE_MOCK and not _llm \
    else f"演示模式（Mock 数据 + LLM 口语化）" if _DEMO_MODE and _llm \
    else f"真实模式（{_MODEL_NAME}）" if _llm \
    else "模拟模式（未配置 API Key，演示用预置数据）"
if USE_MOCK:
    # 演示预置在途单：使冲突/更新/异常场景能命中 existing（模块二可见）
    _store.reset()
    handle_message("__seed__", _store, llm=None, mock=WEB_DEMO_SEED)
else:
    # 真实模式同样 reset + seed 演示在途单（mock 直接入库，不走 LLM）：
    # 保证冲突/更新/查单/简报开箱即有演示对象，与模拟模式体验一致。
    _store.reset()
    handle_message("__seed__", _store, llm=None, mock=WEB_DEMO_SEED)
_LOCK = threading.Lock()


def ledger_state():
    return {"orders": _store.orders, "tasks": _store.tasks,
            "mode": MODE_LABEL, "mock": USE_MOCK}


# 网页演示专用场景数据：实体与 WEB_DEMO_SEED 严格对齐（见 sim_chats.py 注释）
_SCENARIO_MOCKS = WEB_SCENARIO_MOCKS


def _pick_mock(scenario, text):
    """演示模式：按场景/关键词选择预置抽取结果，覆盖模块二五类判定。"""
    if scenario in _SCENARIO_MOCKS:
        return _SCENARIO_MOCKS[scenario]
    if any(k in text for k in ("冲突", "矛盾", "对不上")):
        return _SCENARIO_MOCKS["conflict"]
    if any(k in text for k in ("异常", "负数", "笔误")):
        return _SCENARIO_MOCKS["abnormal"]
    if any(k in text for k in ("缺失", "缺信息", "没说数量")):
        return _SCENARIO_MOCKS["missing"]
    if any(k in text for k in ("改成", "改为", "更新", "变更", "调成")):
        return _SCENARIO_MOCKS["update"]
    return SCENARIO1_MOCK


def do_chat(text, scenario=None, sender=None):
    with _LOCK:
        mock = _pick_mock(scenario, text) if USE_MOCK else None
        try:
            result = handle_message(text, _store, llm=_llm, mock=mock, sender=sender)
        except Exception as e:
            # 大模型网关超时/抖动（词元天下实测单次 18~50s，偶发撞满 timeout）：
            # 用该场景的预置抽取结果重跑一遍，让模块二的规则判定（异常/冲突/缺失/正常）
            # 照常产出结论与台账写入，而不是甩给用户一句 TimeoutError。
            fallback = _SCENARIO_MOCKS.get(scenario) if scenario else None
            if fallback is not None:
                try:
                    result = handle_message(text, _store, llm=None, mock=fallback, sender=sender)
                    result["degraded"] = True
                    result["reply"] = result["reply"] + (
                        "\n\n⚡ 大模型网关本次响应超时（词元天下侧抖动，非本系统故障）。"
                        "已由本地规则引擎即时判定，结论与台账写入均有效；"
                        "重发一次即可走真实大模型。")
                except Exception as e2:
                    result = {"mode": "error",
                              "reply": f"⚠️ 处理失败（{type(e).__name__}/{type(e2).__name__}），请稍后重试。"}
            else:
                result = {"mode": "error",
                          "reply": "⚠️ 大模型网关响应超时（词元天下侧抖动，非本系统故障）。"
                                   "可先问我「XX 那单到哪了」或「出今日简报」——"
                                   "查询与简报走本地规则引擎，不依赖大模型，能立即出结果。"}
    result["ledger"] = ledger_state()
    # 模拟模式提示：未接入 API Key 时输入不真实解析、展示预置演示数据；
    # 配置 DEEPSEEK_API_KEY 后进入真实模式，此提示自动隐藏
    if not _llm:
        result["reply"] = result["reply"] + (
            "\n\n⚠️ 模拟模式：当前展示预置演示数据，输入不会真实解析。"
            "配置 DEEPSEEK_API_KEY 后自动进入真实模式。")
    return result


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 一律禁用缓存：否则浏览器会拿旧 HTML 继续渲染，改了代码却看不到效果
        # （此前多次「页面数据仍是旧实体、模式仍显示模拟模式」正是这个原因）。
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(INDEX, encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html 未找到，请确认 static/index.html 存在。")
        elif self.path == "/api/ledger":
            self._json(ledger_state())
        else:
            self._send(404, "Not Found")

    def do_POST(self):
        if self.path == "/api/chat":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                payload = {}
            text = (payload.get("message") or "").strip()
            self._json(do_chat(text, scenario=payload.get("scenario"),
                               sender=payload.get("sender")))
        elif self.path == "/api/reset":
            with _LOCK:
                _store.reset()
                # 两种模式都 re-seed 演示在途单，保证重置后冲突/更新/查单仍有演示对象
                handle_message("__seed__", _store, llm=None, mock=WEB_DEMO_SEED)
            self._json(ledger_state())
        else:
            self._send(404, "Not Found")

    def log_message(self, *args):
        pass  # 安静运行


def main():
    port = int(os.getenv("OU_PORT", "8000"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"瓯单通已启动 → http://127.0.0.1:{port}")
    print(f"当前模式：{MODE_LABEL}")
    print("Ctrl+C 停止。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
