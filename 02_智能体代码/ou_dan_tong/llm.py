"""极简 OpenAI 兼容 LLM 调用封装，仅用标准库，零依赖。"""
import json
import time
import urllib.request
import urllib.error

from config import (
    DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL,
    OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL,
)

# 偶发网关抖动（500/502/503/504/超时）自动重试：演示时不能因为一次抖动就空回复
_RETRYABLE = (500, 502, 503, 504)
_RETRIES = 3


class LLM:
    def __init__(self, provider: str = "auto"):
        if provider == "auto":
            # 自动探测：配了 OpenAI 兼容端点优先，否则走 DeepSeek 官方
            provider = "openai" if (OPENAI_BASE_URL and OPENAI_API_KEY) else "deepseek"
        self.provider = provider

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        if self.provider == "deepseek":
            key, base, model = DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
        else:
            key, base, model = OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
        if not key:
            raise RuntimeError(
                "未检测到 API Key。请设置环境变量 DEEPSEEK_API_KEY "
                "(或 OPENAI_API_KEY/OPENAI_BASE_URL/OPENAI_MODEL) 后运行真实抽取。"
            )
        url = base.rstrip("/") + "/chat/completions"
        messages = [{"role": "system", "content": system}]
        if user:
            messages.append({"role": "user", "content": user})
        payload = {"model": model, "messages": messages, "temperature": 0.2, "stream": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        last_err = None
        for attempt in range(_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code not in _RETRYABLE or attempt == _RETRIES - 1:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
                # 502/503/504、连接超时、以及网关偶发「200 + 空/HTML 体」导致的
                # JSON 解析失败，均视为瞬时抖动重试，避免演示时空回复
                last_err = e
                if attempt == _RETRIES - 1:
                    raise
            time.sleep(1.5 * (attempt + 1))  # 1.5s / 3s 退避
        raise last_err
