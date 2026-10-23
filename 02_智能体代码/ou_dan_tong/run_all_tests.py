# -*- coding: utf-8 -*-
"""模块五 · 一键回归入口 —— 顺序跑五个脚本，环境问题自动自愈重试，汇总退出码。

归属：队友B（模块五 测试验证）。运行：python run_all_tests.py

为什么需要它（而不是让队长手敲五条命令）：
  1. Windows 上 store._save_one() 的 os.replace 偶发 PermissionError(WinError 5)
     （杀毒/索引服务占用旧文件，实测约 1/3 概率）——**这不是断言失败，也不在模块五
     职责内**（store.py 属模块三/队友D）。它会随机命中任何会写盘的脚本
     （test_pipeline、test_real_mode 的 T8 HTTP 层都踩到过）。本入口统一识别并
     自动重试，避免「一键回归」被环境问题误报成测试失败。
  2. 把五条命令收敛成一条，并对每个脚本给出明确的退出码语义（见下）。

退出码语义：
  0 = 全部通过（含重试后通过）
  1 = 至少一个脚本断言失败（真正的测试失败，别重跑，看明细）
  2 = 至少一个脚本连续 3 次都是环境问题（Windows 文件锁），非断言失败

只读不改：本脚本只编排调用，不碰任何他人模块。
"""
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

SCRIPTS = [
    ("test_pipeline", "模块五·1 管线自测"),
    ("test_scenarios", "模块五·2 场景走查"),
    ("test_defects", "模块五·3 缺陷看板"),
    ("test_real_mode", "模块五·B-1 真实模式回归"),
    ("test_e2e", "模块五·4 端到端验收"),
]

_ENV_MARK = ("PermissionError", "WinError 5")


def run_one(name, label):
    """跑单个脚本；环境问题自动重试（最多 3 次）。返回 (状态, 退出码, 尾部输出)。"""
    for attempt in range(1, 4):
        r = subprocess.run([sys.executable, f"{name}.py"], cwd=_HERE,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            tail = (r.stdout or "").strip().splitlines()
            return ("✅ 通过" + (f"（重试 {attempt-1} 次后）" if attempt > 1 else ""), 0,
                    tail[-1] if tail else "")
        is_env = r.returncode == 2 or any(m in (r.stderr or "") for m in _ENV_MARK)
        if is_env and attempt < 3:
            print(f"   ⚠️ {name} 遇到 Windows 文件锁（环境问题，非断言失败），重试第 {attempt + 1} 次…")
            time.sleep(0.05 * attempt)
            continue
        if is_env:
            return ("❌ 环境问题(重试3次仍失败)", 2, (r.stderr or "").strip().splitlines()[-1]
                    if r.stderr else "PermissionError")
        # 真正的断言失败：不重试，直接透出尾部帮助定位
        tail = (r.stdout or "").strip().splitlines()
        return ("❌ 断言失败", 1, tail[-1] if tail else "")


def main():
    print("=" * 64)
    print("模块五 · 一键回归（五脚本）")
    print("=" * 64)
    results, worst = [], 0
    for name, label in SCRIPTS:
        status, code, tail = run_one(name, label)
        results.append((name, label, status, code, tail))
        if code:
            worst = max(worst, code) if not (worst == 1) else 1
    print("\n" + "=" * 64)
    print(f"{'脚本':<16} {'说明':<24} 结果")
    print("-" * 64)
    for name, label, status, code, tail in results:
        print(f"{name:<16} {label:<24} {status}")
        if code:
            print(f"    └─ 末行：{tail[:70]}")
    print("-" * 64)
    if worst == 0:
        print("✅ 全部通过（含环境问题重试后通过）")
        return 0
    if worst == 1:
        print("❌ 存在断言失败 —— 真正的测试失败，请按上方末行定位，勿盲目重跑")
        return 1
    print("⚠️ 存在环境问题（Windows 文件锁，非断言失败）—— 可稍后重跑或排查杀毒/索引占用")
    return 2


if __name__ == "__main__":
    sys.exit(main())
