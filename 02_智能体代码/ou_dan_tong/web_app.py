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
from sim_chats import (SCENARIO1_MOCK, SEED_MOCK, SCENARIO_NEW_MOCK,
                       SCENARIO_UPDATE_MOCK, SCENARIO_MISSING_MOCK,
                       SCENARIO_CONFLICT_MOCK, SCENARIO_ABNORMAL_MOCK)

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "static", "index.html")

_store = Store()
# 模块四·3：有 key 时创建 LLM 实例，用于查询/简报口语化
_llm = LLM() if (os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")) else None
# OU_DEMO_MODE=1 时强制用 mock 数据（seed + 预置抽取），但 LLM 口语化照常——演示用
_DEMO_MODE = os.getenv("OU_DEMO_MODE", "").lower() in ("1", "true", "yes", "on")
USE_MOCK = (not (os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY"))) or _DEMO_MODE
MODE_LABEL = "模拟模式（未配置 API Key，演示用预置数据）" if USE_MOCK and not _llm \
    else f"演示模式（Mock 数据 + LLM 口语化）" if _DEMO_MODE and _llm \
    else f"真实模式（{os.getenv('DEEPSEEK_MODEL', os.getenv('OPENAI_MODEL', 'LLM'))}）" if _llm \
    else "模拟模式（未配置 API Key，演示用预置数据）"
if USE_MOCK:
    # 演示预置在途单：使冲突/更新/异常场景能命中 existing（模块二可见）
    _store.reset()
    handle_message("__seed__", _store, llm=None, mock=SEED_MOCK)
_LOCK = threading.Lock()


def ledger_state():
    return {"orders": _store.orders, "tasks": _store.tasks,
            "mode": MODE_LABEL, "mock": USE_MOCK}


_SCENARIO_MOCKS = {
    "new": SCENARIO_NEW_MOCK,
    "update": SCENARIO_UPDATE_MOCK,
    "missing": SCENARIO_MISSING_MOCK,
    "conflict": SCENARIO_CONFLICT_MOCK,
    "abnormal": SCENARIO_ABNORMAL_MOCK,
}


def _pick_mock(scenario, text):
    """演示模式：按场景/关键词选择预置抽取结果，覆盖模块二五类判定。"""
    if scenario in _SCENARIO_MOCKS:
        return _SCENARIO_MOCKS[scenario]
    if any(k in text for k in ("冲突", "矛盾", "对不上")):
        return SCENARIO_CONFLICT_MOCK
    if any(k in text for k in ("异常", "负数", "笔误")):
        return SCENARIO_ABNORMAL_MOCK
    if any(k in text for k in ("缺失", "缺信息", "没说数量")):
        return SCENARIO_MISSING_MOCK
    if any(k in text for k in ("改成", "改为", "更新", "变更", "调成")):
        return SCENARIO_UPDATE_MOCK
    return SCENARIO1_MOCK


def do_chat(text, scenario=None):
    with _LOCK:
        mock = _pick_mock(scenario, text) if USE_MOCK else None
        result = handle_message(text, _store, llm=_llm, mock=mock)
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
            self._json(do_chat(text, scenario=payload.get("scenario")))
        elif self.path == "/api/reset":
            with _LOCK:
                _store.reset()
                if USE_MOCK:
                    handle_message("__seed__", _store, llm=None, mock=SEED_MOCK)
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
