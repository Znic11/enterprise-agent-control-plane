#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跑完一卷后,一条命令核对两项环境反馈修复是否仍然生效。

判定的是"修复在不在位",不是"成功率涨没涨"——后者需要同批对照卷,本脚本
不做归因。

对每一卷输出四组数:
  M1 isError 映射 : inner.isError=True 的调用数 / 其中被记成 success=True 的
                    数目(masked)。**修复生效的签名 = inner>0 且 masked=0**。
  M0 包装名修复   : tool_calls 里出现的畸形包装名数 / meta_tool_name_repairs
                    自报的修复数(两者应吻合)/ 未能修复数。
  门禁(verify_loop): vl_read_calls_gate 分布、无证据提醒次数、forced_done 数。
                    门禁变严后这三个数可能上升,是预期行为不是回归。
  结果            : ok / fail / err(空 model_response)。

用法:
    python scripts/check_fix_effect.py <run_dir> [run_dir ...]
例:
    python scripts/check_fix_effect.py out/email_fix_nomem/run_1
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

from benchmark.mcp_client import is_tool_error  # noqa: E402
from orchestrators.meta_tool_router import _looks_like_execute_wrapper  # noqa: E402

# 畸形包装名的粗糙识别(统计"现象规模",与修复判据分开)
WRAP_ANY = re.compile(r"^_execute_(?!tool$)")


def iter_tasks(folder: str):
    for f in sorted(glob.glob(os.path.join(folder, "*.json"))):
        if os.path.basename(f).startswith("_"):
            continue
        try:
            yield f, json.load(open(f, encoding="utf-8"))
        except Exception:
            continue


def check(folder: str) -> Counter:
    st = Counter()
    gate_vals = []
    for _, j in iter_tasks(folder):
        runs = j.get("runs") or []
        if not runs:
            continue
        r = runs[0]
        mr = (r.get("model_response") or "").strip()
        st["tasks"] += 1
        if not mr:
            st["err"] += 1
        elif r.get("overall_success"):
            st["ok"] += 1
        else:
            st["fail"] += 1

        flow = r.get("conversation_flow") or []

        # ---- M1: isError 映射 ----
        for m in flow:
            if m.get("type") != "tool_result":
                continue
            res = m.get("result")
            if not isinstance(res, dict):
                continue
            st["calls"] += 1
            inner = res.get("result")
            if is_tool_error(inner):
                st["inner_iserror"] += 1
                # 记录里仍写成 success=True => 旧映射仍在生效(修复缺失)
                if res.get("success", True) is True:
                    st["masked"] += 1
                else:
                    st["correctly_failed"] += 1

        # ---- M0: 包装名畸形与修复 ----
        for m in flow:
            if m.get("type") != "ai_message":
                continue
            for tc in m.get("tool_calls") or []:
                nm = tc.get("name") or ""
                if WRAP_ANY.match(nm):
                    st["bad_wrapper"] += 1
        st["self_reported_repairs"] += int(r.get("meta_tool_name_repairs") or 0)

        # ---- 门禁 ----
        if r.get("vl_enabled"):
            st["vl_tasks"] += 1
            gate_vals.append(int(r.get("vl_read_calls_gate") or 0))
            st["vl_no_read_reminders"] += int(r.get("vl_no_read_reminders") or 0)
            st["vl_forced_done"] += int(bool(r.get("vl_forced_done")))
            st["vl_correction_turns"] += int(r.get("vl_correction_turns") or 0)
    st["gate_mean"] = round(sum(gate_vals) / len(gate_vals), 2) if gate_vals else 0
    st["gate_zero"] = sum(1 for v in gate_vals if v == 0)
    return st


def main() -> None:
    dirs = sys.argv[1:]
    if not dirs:
        print(__doc__)
        raise SystemExit(2)

    for d in dirs:
        if not os.path.isdir(d):
            print(f"[skip] 不是目录: {d}")
            continue
        st = check(d)
        if not st["tasks"]:
            print(f"[skip] 没有任务记录: {d}")
            continue
        n = st["tasks"]
        print(f"\n=== {d} ===")
        print(f"任务 {n}  |  ok {st['ok']}  fail {st['fail']}  err {st['err']}"
              f"  (成功率 {st['ok'] / n:.1%})")

        print(f"\n[M1] 工具调用 {st['calls']} 条")
        print(f"     inner.isError=True        : {st['inner_iserror']}")
        print(f"       ├ 正确映射为失败         : {st['correctly_failed']}")
        print(f"       └ 仍被记成 success=True  : {st['masked']}")
        if st["inner_iserror"] == 0:
            print("     → 本卷没有工具执行错误,无法判定映射是否在位(换个卷看)")
        elif st["masked"] == 0:
            print("     → ✅ 修复在位:有内层错误,且没有一条被静默成成功")
        else:
            print(f"     → ❌ 修复缺失:{st['masked']} 条内层错误仍被记成成功")

        print(f"\n[M0] 畸形包装名")
        print(f"     conversation_flow 中出现  : {st['bad_wrapper']}")
        print(f"     meta_tool_name_repairs 自报: {st['self_reported_repairs']}")
        if st["bad_wrapper"] == 0:
            print("     → 本卷没有包装名畸形(可能是输入或模型行为差异)")
        elif st["self_reported_repairs"] == 0:
            print("     → ⚠️ 自报 0:该卷要么是修复前的卷,要么畸形名全都没被兜住")
            print("        (看 run JSON 有无 meta_tool_name_repairs 键:无键=修复前)")
        elif st["self_reported_repairs"] >= st["bad_wrapper"]:
            print("     → ✅ 修复在位:出现的畸形名都被兜住了")
        else:
            print(f"     → ⚠️ 有 {st['bad_wrapper'] - st['self_reported_repairs']} 条"
                  f"未被兜住(内层名不可解析),看错误提示是否已改为指明拼写")

        if st["vl_tasks"]:
            print(f"\n[门禁] verify_loop 开在 {st['vl_tasks']}/{n} 个任务上")
            print(f"     vl_read_calls_gate 均值   : {st['gate_mean']}"
                  f"  (其中为 0 的任务 {st['gate_zero']})")
            print(f"     无证据提醒合计            : {st['vl_no_read_reminders']}")
            print(f"     纠错回合合计              : {st['vl_correction_turns']}")
            print(f"     forced_done 任务数        : {st['vl_forced_done']}")
            print("     注:门禁变严后提醒/回合上升属预期,配合 err 数一起看")


if __name__ == "__main__":
    main()
