"""模块五·4 端到端演示验收（可复跑）—— 队友B。

运行：python test_e2e.py

背景：模块五·4 原本只在报告里以「手工 HTTP 实测」的文字记录呈现，
合并后无法被他人复跑、也无法在回归时自动把守。本脚本把它自动化：
拉起真实的 `web_app.py` 服务 → 按评委视角走一遍关键路径 → 对**可见话术**
（⛔/⚠️/🟡/✅/📋）与**台账副作用**逐条断言。

设计要点：
1. **完全离线可复跑**：强制 `OU_DEMO_MODE=1` 并清空 LLM 相关环境变量，
   不依赖 API Key，任何人（含队长/CI）跑出来的结论一致；
2. **自动选空闲端口**，避免与本地已启动的演示服务冲突；
3. 断言「卡片话术」而非内部字段 —— 评委看什么就验什么；
4. 附带 REG-01（Mock 重复入库）回归把关，防止历史缺陷复发。
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
WEB_APP = os.path.join(_HERE, "web_app.py")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Checker:
    def __init__(self):
        self.total = 0
        self.bad = []

    def ok(self, cond, label, detail=""):
        self.total += 1
        if cond:
            print(f"   ✅ {label}")
        else:
            self.bad.append((label, detail))
            print(f"   ❌ {label} ｜ {detail}")
        return cond

    def report(self):
        print("\n" + "=" * 70)
        if self.bad:
            print(f"❌ 端到端验收未通过：{len(self.bad)}/{self.total} 项失败")
            for label, detail in self.bad:
                print(f"   · {label} ｜ {detail}")
            return False
        print(f"✅ 端到端验收通过：{self.total}/{self.total} 项")
        return True


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")

    def post(self, path, payload=None):
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def chat(self, text, scenario=None):
        _, d = self.post("/api/chat", {"message": text, "scenario": scenario})
        return d

    def ledger(self):
        _, d = self.get("/api/ledger")
        return json.loads(d)


def start_server(port):
    env = dict(os.environ)
    env["OU_PORT"] = str(port)
    env["OU_DEMO_MODE"] = "1"          # 强制预置数据，保证结论可复现
    for k in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        env.pop(k, None)               # 彻底离线，避免真实模式/网络抖动
    proc = subprocess.Popen([sys.executable, WEB_APP], cwd=_HERE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    # 等待服务就绪
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ledger",
                                        timeout=2):
                return proc
        except Exception:
            if proc.poll() is not None:
                out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
                raise RuntimeError(f"web_app 启动即退出：{out[:400]}")
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("web_app 启动超时（30s）")


def run():
    print("=" * 70)
    print("模块五·4 端到端演示验收（拉起 web_app.py 真实 HTTP 服务）")
    print("=" * 70)

    port = _free_port()
    proc = start_server(port)
    print(f"\n   服务已就绪：http://127.0.0.1:{port}（OU_DEMO_MODE=1，离线可复现）")
    ck = Checker()
    c = Client(port)

    try:
        # ---------- S1 首页可访问 ----------
        print("\n[S1] 打开网页")
        st, body = c.get("/")
        ck.ok(st == 200 and "<html" in body.lower(), "首页 HTTP 200 且返回 HTML",
              f"status={st}")

        # ---------- S2 台账预置 ----------
        print("\n[S2] 重置并查看预置台账")
        led = c.post("/api/reset")[1]
        n0 = len(led.get("orders", []))
        ck.ok(n0 >= 2, f"预置在途单 ≥2 笔（实际 {n0} 笔）", f"orders={n0}")
        ck.ok(led.get("mock") is True, "运行于模拟模式（离线可复跑）",
              f"mock={led.get('mock')}")

        # ---------- S3 冲突：不覆盖 + 待确认 ----------
        print("\n[S3] 粘贴冲突消息（评委点名：矛盾冲突）")
        r = c.chat("按 280 核，跟车间说的 300 对不上")
        reply = r.get("reply", "")
        ck.ok("⚠️" in reply or "冲突" in reply, "卡片呈现冲突话术", reply[:80])
        ck.ok("未自动覆盖" in reply or "人工确认" in reply,
              "明确「未自动覆盖 / 待人工确认」", reply[:80])

        # ---------- S4 异常：拦截不写入 ----------
        print("\n[S4] 粘贴异常消息（评委点名：明显异常）")
        before = len(c.ledger().get("orders", []))
        # 措辞需含 web_app 关键词路由的「异常 / 负数」，否则会落到 update 分支
        r = c.chat("DZ47 那个数量异常，给我写成负数 -300 了")
        reply = r.get("reply", "")
        ck.ok("⛔" in reply or "异常" in reply, "卡片呈现异常拦截话术", reply[:80])
        ck.ok("拦截" in reply or "未写入" in reply, "明确「已拦截 / 未写入」", reply[:80])
        after = len(c.ledger().get("orders", []))
        ck.ok(after == before, f"异常不写入台账（{before} → {after}）", f"{before}→{after}")

        # ---------- S5 缺失：占位追问 ----------
        print("\n[S5] 粘贴缺失消息（评委点名：缺失追问）")
        # 措辞需含路由关键词「缺失」，否则会落到默认新建分支
        r = c.chat("NMX-60 再走一批，信息缺失，数量和交期都还没说")
        reply = r.get("reply", "")
        ck.ok("🟡" in reply or "不全" in reply or "缺失" in reply,
              "卡片呈现缺失追问话术", reply[:80])
        ck.ok("补" in reply or "请" in reply, "给出可执行的补全引导", reply[:80])

        # ---------- S6 更新：命中已有单，不重复建单 ----------
        # ⚠️ 实体必须与 sim_chats.WEB_DEMO_SEED 对齐（2026-10-23 修正）：
        # 演示 seed 已从「王工/NMX-60」换成模块一真实实体（王总/李姐/陈老板…），
        # 本脚本原先断言的 NMX-60 已不在台账里，导致 qty=None 误判。
        # 现在用 seed 中的「李姐（瓯越五金贸易）/ DZ47-63 三相断路器」（预置 50），
        # 配 scenario="update"（预置抽取 80）→ 期望更新为 80。
        print("\n[S6] 粘贴更新消息（评委点名：最新状态 + 留痕）")
        before = len(c.ledger().get("orders", []))
        r = c.chat("小陈，DZ47 那个加到 80 个", scenario="update")
        reply = r.get("reply", "")
        ck.ok("✅" in reply or "已更新" in reply, "卡片呈现更新成功话术", reply[:80])
        after = len(c.ledger().get("orders", []))
        ck.ok(after == before, f"更新不重复建单（{before} → {after}）", f"{before}→{after}")
        dz = next((o for o in c.ledger().get("orders", [])
                   if "DZ47" in str(o.get("product", ""))), None)
        ck.ok(dz is not None and dz.get("qty") == 80,
              "台账数量已更新为 80（预置 50 → 80）",
              f"qty={dz and dz.get('qty')}")
        ck.ok(dz is not None and len(dz.get("changes", [])) >= 1,
              "变更留痕已记录", f"changes={dz and len(dz.get('changes', []))}")

        # ---------- S7 查询：返回最新态而非旧版 ----------
        print("\n[S7] 问「那单到哪了」（评委点名：最新状态）")
        r = c.chat("DZ47 那单到哪了？")
        reply = r.get("reply", "")
        ck.ok("80" in reply, "查询返回最新数量 80（而非旧版 50）", reply[:80])

        # ---------- S8 简报：四类统计 ----------
        print("\n[S8] 说「出今日简报」")
        r = c.chat("出今日简报")
        reply = r.get("reply", "")
        ck.ok("📋" in reply or "简报" in reply, "输出简报卡片", reply[:60])
        for kw in ("新增", "待报价", "临期", "逾期"):
            ck.ok(kw in reply, f"简报含「{kw}」统计", reply[:100])

        # ---------- S9 幂等回归（REG-01） ----------
        print("\n[S9] 幂等回归：重复导入同一消息不重复入库")
        before = len(c.ledger().get("orders", []))
        c.chat("小陈，DZ47 那个加到 80 个", scenario="update")
        mid = len(c.ledger().get("orders", []))
        c.chat("小陈，DZ47 那个加到 80 个", scenario="update")
        after = len(c.ledger().get("orders", []))
        ck.ok(before == mid == after,
              f"重复导入台账条数不变（{before} → {mid} → {after}）",
              f"{before}→{mid}→{after}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        print("\n   服务已关闭")

    return ck.report()


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
