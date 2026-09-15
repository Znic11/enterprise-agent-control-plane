#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线重放历史卷,核对 mcp_client 的 isError 映射修复效果。

原理
----
历史卷的 tool_result 里保存的是**原始 MCP 响应体**(inner payload),
它不受当时客户端映射逻辑影响。因此可以拿修好之后的判定函数
(`benchmark.mcp_client.is_tool_error`) 去重新裁决每一条记录:

    new_success = not is_tool_error(inner)

再和当时记录下来的 `success` 字段对比:
  * masked  = 记录里 success=True 但 inner.isError=True  → 旧代码漏判
  * 修复后这些 masked 全部应变成 success=False(flip 率 100%)
  * 顺带统计其中有多少是**只读工具**调用 —— 这些就是被 verify 门禁
    误当成"有效回读证据"的调用。

用法:
    python scripts/verify_iserror_fix.py [run_dir ...]
"""

from __future__ import annotations

import glob
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from benchmark.mcp_client import is_tool_error, mcp_error_text  # noqa: E402
from extract_failure_traces import is_error_run  # noqa: E402

try:
    from orchestrators.meta_tool_router import _is_read_only_tool_name as is_read_only
except Exception:  # pragma: no cover - 兜底:导入失败时退化为前缀判定
    import re

    _READ = re.compile(r"^(get|list|find|search|fetch|retrieve|count|read)", re.I)

    def is_read_only(name: str) -> bool:  # type: ignore[misc]
        return bool(_READ.match(name or ""))

DEFAULT_DIRS = [
    "out/full_tfidf/run_1",
    "out/meta_hybrid/run_1",
    "out/full_hybrid/run_1",
    "out/full_hybrid/run_2",
    "out/email_vloop/run_1",
    "out/email_vloop/run_2",
    "out/full_exec_email/run_1",
]


def iter_task_files(folder: str):
    if not os.path.isdir(folder):
        return
    for f in sorted(glob.glob(os.path.join(folder, "*.json"))):
        if os.path.basename(f).startswith("_"):
            continue
        try:
            yield f, json.load(open(f, encoding="utf-8"))
        except Exception:
            continue


def replay_run(run: dict) -> dict:
    """重放单条 run,返回该 run 的统计。"""
    stat = Counter()
    stat["empty"] = 0
    stat["masked_readonly"] = 0
    samples = []
    if not (run.get("conversation_flow") or []):
        # 整卷报错的空 run(只有 error/overall_success),没有可重放的调用
        stat["empty"] = 1
        stat["samples"] = samples
        return stat
    for m in run.get("conversation_flow") or []:
        if m.get("type") != "tool_result":
            continue
        res = m.get("result")
        if not isinstance(res, dict):
            continue
        inner = res.get("result")
        stat["calls"] += 1
        old_ok = res.get("success", True) is True
        err = is_tool_error(inner)
        if err:
            stat["inner_iserror"] += 1
        if old_ok and err:
            stat["masked"] += 1
            if is_read_only(m.get("tool_name") or ""):
                stat["masked_readonly"] += 1
            if len(samples) < 3:
                txt = (mcp_error_text(inner) or res.get("error") or "").strip()
                samples.append((m.get("tool_name"), txt[:180]))
        # 修复后的裁决
        if not err and old_ok:
            stat["still_ok"] += 1
        elif not old_ok and not err:
            stat["old_fail_new_ok"] += 1
    stat["samples"] = samples
    return stat


def main() -> None:
    dirs = sys.argv[1:] or DEFAULT_DIRS
    print(f"{'run':<28}{'tasks':>7}{'empty':>7}{'calls':>8}{'innerErr':>9}"
          f"{'masked':>8}{'ro-masked':>10}{'hid%':>7}")
    grand = Counter()
    all_samples = []
    for d in dirs:
        rows = []
        for _, j in iter_task_files(d):
            runs = j.get("runs") or []
            if not runs:
                continue
            rows.append(replay_run(runs[0]))
        if not rows:
            print(f"{d:<28}{'-':>7}  (no data)")
            continue
        calls = sum(r["calls"] for r in rows)
        empty = sum(r["empty"] for r in rows)
        inner = sum(r["inner_iserror"] for r in rows)
        masked = sum(r["masked"] for r in rows)
        ro = sum(r["masked_readonly"] for r in rows)
        hid = (100.0 * masked / inner) if inner else 0.0
        print(f"{d:<28}{len(rows):>7}{empty:>7}{calls:>8}{inner:>9}"
              f"{masked:>8}{ro:>10}{hid:>6.0f}%")
        grand["tasks"] += len(rows)
        grand["empty"] += empty
        grand["calls"] += calls
        grand["inner"] += inner
        grand["masked"] += masked
        grand["ro"] += ro
        grand["still_ok"] += sum(r["still_ok"] for r in rows)
        grand["old_fail_new_ok"] += sum(r["old_fail_new_ok"] for r in rows)
        for r in rows:
            all_samples.extend(r["samples"])

    print("\n=== 合计 ===")
    print(f"任务数 {grand['tasks']}(其中整卷报错空 run {grand['empty']})  "
          f"可重放工具调用 {grand['calls']}")
    print(f"inner.isError=True 的调用        : {grand['inner']}")
    print(f"其中被旧映射误报为 success=True : {grand['masked']}  (即 isError-masked)")
    print(f"  └ 属于只读工具(误当 verify 证据): {grand['ro']}")
    ok = grand["masked"] == grand["inner"] and grand["inner"] > 0
    print(f"修复后 masked → success=False 翻转: {grand['masked']}/{grand['inner']}"
          f"  {'= 100%(旧代码对内层错误零识别)' if ok else ''}")
    print(f"反向校验: 记录 success=True 且内层无错误 {grand['still_ok']} 条"
          f" → 新映射下仍为成功(无误伤)")
    print(f"          记录 success=False 但内层非 isError {grand['old_fail_new_ok']} 条"
          f" = 传输层失败,新映射下依旧判失败(不变)")
    if all_samples:
        print("\n=== 修复后模型将看到的可读错误文本(抽样) ===")
        seen = set()
        for nm, txt in all_samples:
            if nm in seen:
                continue
            seen.add(nm)
            print(f"[{nm}] {txt}")
            if len(seen) >= 8:
                break


if __name__ == "__main__":
    main()
