#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Feature-classify cross-run stable completed-but-failed cases.

Outputs per-task features + aggregate breakdown to help diagnose *how* the
agent failed (not just "verifier said no").

Usage:
  python scripts/classify_stable_failures.py --runs A,B --label email
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_failure_traces import trace_task, is_error_run  # noqa: E402

WRITE_VERBS = re.compile(
    r"create|update|delete|send|insert|add|remove|modify|apply|patch|trash|import|"
    r"register|assign|enable|disable|set_|put_|batch|move|archive|link|onboard|"
    r"replace|revoke|purge|draft", re.I)
READ_VERBS = re.compile(r"get|list|find|search|fetch|retrieve|count|read|export|history", re.I)
WRAPPER = re.compile(r"^_execute_")


def tkey(fn):
    m = re.search(r"task_(\d{8}_\d{6}_\d+)", os.path.basename(fn))
    return m.group(1) if m else os.path.basename(fn)


def load(d):
    o = {}
    for f in sorted(glob.glob(os.path.join(d, "*.json"))):
        if os.path.basename(f).startswith("_"):
            continue
        try:
            j = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        r = j["runs"][0]
        mr = (r.get("model_response") or "").strip()
        o[tkey(f)] = {"file": f,
                      "status": "ERR" if (not mr or is_error_run(r)) else ("OK" if r.get("overall_success") else "FAIL")}
    return o


def classify_verifier(fv):
    """greater_than 的 expected 是基线、通过条件是 actual >= 1;
    actual == 0 表示被要求的实体/事件根本没出现,归 MISSING_STATE。"""
    exp, act, cmp_ = fv.get("expected"), fv.get("actual"), fv.get("cmp")
    try:
        e, a = float(exp), float(act)
    except Exception:
        return "VALUE_MISMATCH" if isinstance(exp, str) else "OTHER"
    if cmp_ == "greater_than":
        if a < 1:
            return "MISSING_STATE"
        if a <= e:
            return "NOT_INCREASED"
        return "OTHER"
    if cmp_ == "equals":
        if a < e:
            return "MISSING_STATE"
        if a > e:
            return "EXTRA_STATE"
        return "SUBTLE_VALUE"
    return "OTHER"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    run_dirs = [d.strip() for d in args.runs.split(",") if d.strip()]
    maps = [load(d) for d in run_dirs]
    keys = set(maps[0])
    for m in maps[1:]:
        keys &= set(m)
    stable = [k for k in sorted(keys) if all(m[k]["status"] == "FAIL" for m in maps)]

    rows = []
    agg = Counter()
    for k in stable:
        fp = maps[0][k]["file"]
        t = trace_task(fp, 200)
        tc_calls, exec_calls = [], []
        for m in json.load(open(fp, encoding="utf-8"))["runs"][0]["conversation_flow"]:
            if m.get("type") == "ai_message":
                for tc in (m.get("tool_calls") or []):
                    tc_calls.append(tc.get("name"))
        # recover the raw tool_calls incl. corrupted wrapper names
        corrupted = [n for n in tc_calls if n and WRAPPER.match(n) and n != "_execute_tool"]
        # business tools: either wrapped args.name or direct
        biz = []
        for m in json.load(open(fp, encoding="utf-8"))["runs"][0]["conversation_flow"]:
            if m.get("type") == "ai_message":
                for tc in (m.get("tool_calls") or []):
                    nm = tc.get("name") or ""
                    if nm == "_execute_tool":
                        inner = (tc.get("args") or {}).get("name")
                        if inner:
                            biz.append(inner)
                    elif not WRAPPER.match(nm) and nm != "_tool_search":
                        biz.append(nm)
        n_write = sum(1 for b in biz if WRITE_VERBS.search(b))
        n_read = sum(1 for b in biz if READ_VERBS.search(b))
        # tool errors among results
        errs = []
        for m in json.load(open(fp, encoding="utf-8"))["runs"][0]["conversation_flow"]:
            if m.get("type") == "tool_result":
                res = m.get("result")
                if isinstance(res, dict) and res.get("success") is False:
                    errs.append(m.get("tool_name"))

        vclasses = [classify_verifier(fv) for fv in t["failed_verifiers"]]
        for vc in vclasses:
            agg[vc] += 1
        rows.append({
            "task": k,
            "prompt": t["user_prompt"][:200],
            "n_failed_verifiers": len(t["failed_verifiers"]),
            "verifier_classes": vclasses,
            "corrupted_wrapper_calls": len(corrupted),
            "corrupted_names": sorted(set(corrupted)),
            "n_tool_errors": len(errs),
            "error_tools": errs[:5],
            "n_write_tools": n_write,
            "n_read_tools": n_read,
            "write_ratio": round(n_write / max(1, n_write + n_read), 2),
            "n_biz_calls": len(biz),
            "tools_used": t["tools_used"],
        })

    print(f"=== {args.label}: {len(stable)} stable failures ===")
    print("failed-verifier classes:", dict(agg))
    print("tasks with corrupted wrapper name:", sum(1 for r in rows if r["corrupted_wrapper_calls"]))
    print("tasks with >=1 tool error:", sum(1 for r in rows if r["n_tool_errors"]))
    print("tasks with ZERO write tool calls:", sum(1 for r in rows if r["n_write_tools"] == 0))
    print("mean write ratio:", round(sum(r["write_ratio"] for r in rows) / max(1, len(rows)), 3))
    fv_per_task = Counter(r["n_failed_verifiers"] for r in rows)
    print("failed verifiers per task:", dict(sorted(fv_per_task.items())))
    out = args.out or f"out/_classify_{args.label}.json"
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("wrote", out)


if __name__ == "__main__":
    main()
