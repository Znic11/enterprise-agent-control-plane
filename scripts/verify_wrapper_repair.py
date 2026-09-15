#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""离线重放历史卷,核对包装层名修复(M0)的覆盖率。

原理
----
历史卷的 ai_message.tool_calls 记录的是**模型原始输出**,包含被解码器弄坏的
包装层名(如 _execute_ttool)。用修好之后的判定函数
(`orchestrators.meta_tool_router._looks_like_execute_wrapper`) + 内层 args.name
是否命中该 run 可见的工具集合,即可离线算出"能被修复的有多少"。

可见工具集合的近似:该 run 内 _tool_search 返回过的 found[].name,加上该 run
内实际被成功执行过的 tool_result.tool_name。这足以判定内层名是否为真实工具。

用法:
    python scripts/verify_wrapper_repair.py [run_dir ...]
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orchestrators.meta_tool_router import _looks_like_execute_wrapper  # noqa: E402

# 退化名的粗糙识别(仅用于"现象规模"统计,与修复判据无关)
WRAP_ANY = re.compile(r"^_execute_(?!tool$)")

DEFAULT_DIRS = [
    "out/email_vloop/run_1",
    "out/email_vloop/run_2",
    "out/full_exec_email/run_1",
    "out/full_tfidf/run_1",
    "out/meta_hybrid/run_1",
    "out/full_hybrid/run_1",
]


def visible_tools(flow) -> set:
    """该 run 内可视为"池内"的工具名集合(检索命中 + 实际执行过的)。"""
    names = set()
    for m in flow:
        if m.get("type") == "tool_result":
            nm = m.get("tool_name")
            if nm:
                names.add(nm)
            res = m.get("result")
            if isinstance(res, dict):
                inner = res.get("result")
                if isinstance(inner, dict):
                    for f in inner.get("found") or []:
                        if isinstance(f, dict) and f.get("name"):
                            names.add(f["name"])
                    for s in (inner.get("schemas") or {}):
                        names.add(s)
    return names


def scan(folder: str, tools_out: Counter) -> Counter:
    st = Counter()
    for f in sorted(glob.glob(os.path.join(folder, "*.json"))):
        if os.path.basename(f).startswith("_"):
            continue
        try:
            j = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        runs = j.get("runs") or []
        if not runs:
            continue
        flow = runs[0].get("conversation_flow") or []
        st["tasks"] += 1
        if not flow:
            st["empty"] += 1
            continue
        pool = visible_tools(flow)
        hit_task = False
        for m in flow:
            if m.get("type") != "ai_message":
                continue
            for tc in m.get("tool_calls") or []:
                nm = tc.get("name") or ""
                if not WRAP_ANY.match(nm):
                    continue
                st["bad_calls"] += 1
                hit_task = True
                args = tc.get("args") or {}
                inner = str(args.get("name", "")).strip() if isinstance(args, dict) else ""
                st["predicate_hit"] += int(_looks_like_execute_wrapper(nm))
                if inner and inner in pool:
                    st["repairable"] += 1
                    tools_out[inner] += 1
                else:
                    st["not_repairable"] += 1
        if hit_task:
            st["tasks_hit"] += 1
    return st


def main() -> None:
    dirs = sys.argv[1:] or DEFAULT_DIRS
    print(f"{'run':<28}{'tasks':>7}{'badCalls':>9}{'predHit':>9}{'repairable':>11}"
          f"{'unrepairable':>13}{'tasksHit':>10}")
    g = Counter()
    tools: Counter = Counter()
    for d in dirs:
        st = scan(d, tools)
        if not st["bad_calls"]:
            note = "all-error run" if st["tasks"] == st["empty"] else "no degradation"
            print(f"{d:<28}{st['tasks']:>7}{0:>9}  ({note}, {st['empty']} empty)")
        else:
            print(f"{d:<28}{st['tasks']:>7}{st['bad_calls']:>9}{st['predicate_hit']:>9}"
                  f"{st['repairable']:>11}{st['not_repairable']:>13}{st['tasks_hit']:>10}")
        for k in ("tasks", "bad_calls", "predicate_hit", "repairable",
                  "not_repairable", "tasks_hit"):
            g[k] += st[k]

    print("\n=== 合计 ===")
    print(f"退化的包装层调用        : {g['bad_calls']}  (触达任务 {g['tasks_hit']})")
    print(f"命中修复判据(前缀形似)  : {g['predicate_hit']}")
    print(f"其中内层名可解析 -> 修复 : {g['repairable']}")
    print(f"内层名也不可用 -> 不修复 : {g['not_repairable']}")
    cov = (100.0 * g["repairable"] / g["bad_calls"]) if g["bad_calls"] else 0.0
    print(f"修复覆盖率(对退化调用)  : {cov:.0f}%")
    if tools:
        print(f"被挡住的真实工具(去重 {len(tools)} 个): "
              f"{', '.join(sorted(tools))}")


if __name__ == "__main__":
    main()
