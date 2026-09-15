#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Extract compact diagnostic traces for completed-but-failed agent runs.

A "completed-but-failed" case = the agent produced a final answer (non-empty
model_response) but overall_success is False. Timeouts / upstream errors are
excluded and counted separately.

Usage:
    python scripts/extract_failure_traces.py <run_dir> [--out DIR] [--max-result N]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from collections import Counter

TRUNC = 700


def _short(v, n=TRUNC):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    s = s.replace("\n", " ")
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= n else s[:n] + f" …[+{len(s)-n}]"


def is_error_run(run: dict) -> bool:
    mr = (run.get("model_response") or "").strip()
    if not mr:
        return True
    low = mr.lower()
    if "upstream connect error" in low or "reset reason" in low:
        return True
    if "timeout" in low and len(mr) < 400:
        return True
    return False


def trace_task(fp: str, max_result: int = TRUNC) -> dict:
    d = json.load(open(fp, encoding="utf-8"))
    r = d["runs"][0]
    cfg = d.get("benchmark_config", {})
    cf = r.get("conversation_flow", [])

    steps = []
    for m in cf:
        t = m.get("type")
        if t == "user_message":
            steps.append(("USER", _short(m.get("content"), 900)))
        elif t == "system_message":
            stage = m.get("stage")
            if stage:  # internal steering, keep short
                steps.append((f"SYS[{stage}]", _short(m.get("content"), 200)))
        elif t == "ai_message":
            for tc in (m.get("tool_calls") or []):
                steps.append(("CALL", f"{tc.get('name')}({_short(tc.get('args'), 400)})"))
            txt = (m.get("content") or "").strip()
            if txt:
                steps.append(("AI", _short(txt, 500)))
        elif t == "tool_result":
            res = m.get("result")
            if isinstance(res, dict) and res.get("success") is False:
                steps.append(("TOOL_ERR", f"{m.get('tool_name')} -> {_short(res, max_result)}"))
            else:
                steps.append(("RESULT", f"{m.get('tool_name')} -> {_short(res, max_result)}"))

    vr = r.get("verification_results") or {}
    failed = []
    for desc, v in vr.items():
        if not isinstance(v, dict):
            continue
        if v.get("passed") is False:
            failed.append({
                "desc": desc,
                "expected": v.get("expected"),
                "actual": v.get("actual"),
                "cmp": v.get("comparison_type"),
                "query": v.get("query"),
            })

    return {
        "file": os.path.basename(fp),
        "task_id": os.path.basename(fp).replace(".json", ""),
        "user_prompt": cfg.get("user_prompt", ""),
        "n_tools_available": cfg.get("total_tools_available"),
        "tools_used": r.get("tools_used"),
        "n_calls": len([s for s in steps if s[0] == "CALL"]),
        "steps": steps,
        "failed_verifiers": failed,
        "pass_rate": (r.get("verification_summary") or {}).get("pass_rate"),
        "final_response": r.get("model_response", ""),
        "meta": {
            k: r.get(k) for k in (
                "meta_tool_searches", "meta_tool_cache_hits", "meta_tool_zero_hits",
                "meta_tool_fallback_all", "vl_enabled", "vl_gate_rounds",
                "vl_read_calls_gate", "vl_correction_turns", "vl_forced_done",
                "mem_folds", "mem_valid_facts", "mem_invalid_facts",
            ) if r.get(k) is not None
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-result", type=int, default=TRUNC)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.run_dir, "*.json")))
    ok, fail, err = [], [], []
    for fp in files:
        try:
            d = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        r = d["runs"][0]
        if is_error_run(r):
            err.append(os.path.basename(fp))
        elif r.get("overall_success"):
            ok.append(os.path.basename(fp))
        else:
            fail.append(os.path.basename(fp))

    traces = [trace_task(os.path.join(args.run_dir, f), args.max_result) for f in fail]

    print(f"[{args.run_dir}] files={len(files)} ok={len(ok)} completed-fail={len(fail)} err={len(err)}")

    # failure signature aggregation
    sig = Counter()
    for t in traces:
        for fv in t["failed_verifiers"]:
            sig[fv["cmp"]] += 1
    print("  failed-verifier comparison types:", dict(sig))

    out = args.out or os.path.join(args.run_dir, "_failure_traces.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"run_dir": args.run_dir, "ok": ok, "failed": fail, "err": err, "traces": traces},
                  fh, ensure_ascii=False, indent=1)
    print("  wrote", out)
    return traces


if __name__ == "__main__":
    main()
