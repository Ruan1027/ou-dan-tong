"""LLM 结构化抽取。支持 mock 模式（无 Key 也能测全管线）。"""
import datetime
import json

from llm import LLM
from prompts import EXTRACTION_SYSTEM, SENDER_RULE


def _normalize(parsed):
    """把 LLM 返回的 JSON 归一化为 list[dict]。
    真实模型即使提示词要求数组，也常返回 {"orders":[...]} 等包装对象，
    或混入字符串元素；在此统一规整，避免下游 validate_extraction 崩溃。"""
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    if isinstance(parsed, dict):
        for k in ("orders", "items", "data", "results", "tasks", "extract", "extractions"):
            v = parsed.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        if "type" in parsed:  # 单条对象直接包成数组
            return [parsed]
        return []
    return []


def extract(chat_text: str, llm: LLM = None, mock=None, sender: str = None):
    if mock is not None:
        return mock
    if llm is None:
        llm = LLM()
    # 注入当前日期：口语日期（「下个月15号」）依赖「今天」才能归一化，否则模型只能瞎猜
    today = datetime.date.today().isoformat()
    sys_prompt = EXTRACTION_SYSTEM + f"\n当前日期：{today}"
    user_msg = chat_text
    # 注入发送者：真实微信群每条消息都带发言人昵称。缺了它，客户自报类消息
    # （「小陈，DZ47 那个加到 80 个」）会因为正文无客户名而抽成 customer=null → 整条被丢弃
    if sender:
        sys_prompt += SENDER_RULE
        user_msg = f"[发送者:{sender}] {chat_text}"
    raw = llm.chat(sys_prompt, user_msg, json_mode=True)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # 兜底：模型在 JSON 前后夹了说明文字时，截取首个完整的 [..] 或 {..} 再试
        parsed = None
        for lo, hi in (("[", "]"), ("{", "}")):
            i, j = raw.find(lo), raw.rfind(hi)
            if i != -1 and j > i:
                try:
                    parsed = json.loads(raw[i:j + 1])
                    break
                except json.JSONDecodeError:
                    continue
        if parsed is None:
            raise
    items = _normalize(parsed)
    # 规则①：本平台直接面向顾客，不存在企业内部上下级转达 —— **发信人就是客户本人**。
    # 不能只补「customer 为空」的情况：真实模型常把消息里被 @ 的我方业务员
    # （「小陈，DZ47 那个加到 80 个」里的「小陈」）当成客户抽出来，customer 非空，
    # 于是「补空值」这种兜底根本不触发，下游照样判成「缺少客户信息」。
    # 因此这里改为：有 sender 时，customer **强制以发信人为准**。
    if sender:
        s = sender.strip()
        for it in items:
            if it.get("type") != "order":
                continue
            cand = (it.get("customer") or "").strip()
            if not cand:
                it["customer"] = s
            elif s not in cand:
                # LLM 抽的名字不是发信人 → 以发信人为准；
                # 原值若像公司名（≥4 字），作为括号备注保留，避免丢信息。
                it["customer"] = f"{s}（{cand}）" if len(cand) >= 4 else s
    return items
