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
  3 = 缺文件 / 缺本地依赖模块（打包漏文件或目录不完整）—— 不是测试失败

只读不改：本脚本只编排调用，不碰任何他人模块。

为什么加了 preflight（打包完整性自检）：
  交付包曾出现「zip 里漏打 test_fixtures.py / test_e2e.py」的情况 —— 脚本存在但
  依赖模块不在。旧版会把这类结构性缺失当成断言失败（末行为空，无法定位）或误判成
  文件锁重试 3 次，误导排查方向。现在运行前先静态扫一遍 import，缺失即立刻报出
  具体缺哪个文件，不再浪费 3 次重试。
"""
import os
import re
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
# 标准库白名单：本项目零第三方依赖，凡不在标准库又找不到同名 .py 的，即视为缺失
_STDLIB = set(getattr(sys, "stdlib_module_names", ()))
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w]*)", re.M)


def _tail(*streams):
    """取最后一行有效输出：优先 stdout，stdout 为空时退回 stderr（import 阶段崩溃时）。"""
    for s in streams:
        lines = [ln for ln in (s or "").strip().splitlines() if ln.strip()]
        if lines:
            return lines[-1]
    return ""


def preflight():
    """运行前的静态完整性自检。返回 [(脚本名, 问题描述), ...]；空列表表示通过。"""
    problems = []
    for name, _label in SCRIPTS:
        path = os.path.join(_HERE, f"{name}.py")
        if not os.path.exists(path):
            problems.append((name, f"{name}.py 文件缺失"))
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for mod in sorted(set(_IMPORT_RE.findall(src))):
            if mod in _STDLIB:
                continue
            if os.path.exists(os.path.join(_HERE, f"{mod}.py")):
                continue
            problems.append((name, f"依赖的本地模块 {mod}.py 缺失"))
    return problems


def run_one(name, label):
    """跑单个脚本。返回 (状态, 退出码, 尾部输出)。

    三种失败的分类与处置：
      · 缺模块（exit 3）—— 立刻定性，绝不重试（重试 100 次也还是缺文件）
      · 文件锁等环境问题（exit 2）—— 自动重试最多 3 次
      · 其他失败 —— 复跑一次：两次都失败才判定为**确定性断言失败**（exit 1）；
        若复跑通过，说明是偶发抖动（多为 Windows 文件锁被脚本内部捕获后转成了
        断言样式），归为 exit 2 并明确标注「首次失败·重跑通过」，避免把环境噪声
        报成测试失败，也避免把真失败洗成环境噪声。
    """
    first_tail = None
    for attempt in range(1, 4):
        r = subprocess.run([sys.executable, f"{name}.py"], cwd=_HERE,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            if first_tail is not None:
                return ("⚠️ 首次失败·复跑通过（偶发抖动，非确定性失败）", 2, first_tail)
            return ("✅ 通过" + (f"（重试 {attempt-1} 次后）" if attempt > 1 else ""), 0,
                    _tail(r.stdout))
        err = r.stderr or ""
        # 文件锁痕迹可能落在 stdout（脚本内部捕获后打印）也可能在 stderr（traceback），
        # 两个流都要扫，否则会被误判成断言失败 —— 曾导致 real_mode 的偶发
        # WinError 5 报成「❌ 断言失败」，误导排查方向。
        both = (r.stdout or "") + "\n" + err
        # 运行时才暴露的模块缺失（preflight 扫不到的间接依赖）：立刻定性，不重试
        m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", both)
        if m:
            return (f"❌ 缺模块 {m.group(1)}.py", 3, _tail(r.stdout, err))
        is_env = r.returncode == 2 or any(mk in both for mk in _ENV_MARK)
        if is_env:
            if attempt < 3:
                print(f"   ⚠️ {name} 遇到 Windows 文件锁（环境问题，非断言失败），重试第 {attempt + 1} 次…")
                time.sleep(0.05 * attempt)
                continue
            return ("❌ 环境问题(重试3次仍失败)", 2, _tail(r.stdout, err) or "PermissionError")
        # 未识别为环境问题：复跑一次以区分确定性与偶发
        first_tail = _tail(r.stdout, err)
        if attempt == 1:
            print(f"   ⏳ {name} 首次失败，复跑一次以区分「确定性断言失败」与「偶发抖动」…")
            continue
        return ("❌ 断言失败（连续 2 次）", 1, first_tail)


def main():
    print("=" * 64)
    print("模块五 · 一键回归（五脚本）")
    print("=" * 64)

    # ① 打包完整性自检：缺文件/缺依赖立刻报出，不浪费 3 次重试
    problems = preflight()
    if problems:
        print("\n❌ 完整性自检未通过（缺文件，非测试失败）：")
        for name, desc in problems:
            print(f"   · [{name}] {desc}")
        print("\n   处理：从仓库/开发目录补齐上述 .py 到本目录后重跑；")
        print("         若是别人给的压缩包，说明打包环节漏了文件，需重新打包。")
        return 3

    print("\n完整性自检 ✅（脚本与本地依赖齐全）\n")

    results, worst = [], 0
    for name, label in SCRIPTS:
        status, code, tail = run_one(name, label)
        results.append((name, label, status, code, tail))
        if code:
            # 优先级：断言失败(1) > 缺文件(3) > 环境问题(2)
            order = {1: 3, 3: 2, 2: 1}
            worst = max(worst, code, key=lambda c: order.get(c, 0)) if worst else code
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
    if worst == 3:
        print("❌ 缺文件 / 缺本地依赖模块 —— 打包或目录不完整，补齐后重跑")
        return 3
    print("⚠️ 存在环境问题（Windows 文件锁，非断言失败）—— 可稍后重跑或排查杀毒/索引占用")
    return 2


if __name__ == "__main__":
    sys.exit(main())
