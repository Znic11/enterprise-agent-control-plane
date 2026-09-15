#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build a readable digest of *cross-run stable* completed-but-failed cases.

A cross-run stable failure = same task fails (agent finished, verifier says no)
in every independent run where it produced a final answer.

Usage:
    python scripts/digest_stable_failures.py --runs A,B,C --out digest.md
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_failure_traces import trace_task  # noqa: E402


def tkey(fn: str) -> str:
    m = re.search(r"task_(\d{8}_\d{6}_\d+)", os.path.basename(fn))
    return m.group(1) if m else os.path.basename(fn)


def load(run_dir: str):
    out = {}
    for f in sorted(glob.glob(os.path.join(run_dir, "*.json"))):
        if os.path.basename(f).startswith("_"):
            continue
        try:
            j = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        r = j["runs"][0]
        mr = (r.get("model_response") or "").strip()
        status = "ERR" if not mr else ("OK" if r.get("overall_success") else "FAIL")
        out[tkey(f)] = {"file": f, "status": status}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-result", type=int, default=500)
    args = ap.parse_args()

    run_dirs = [d.strip() for d in args.runs.split(",") if d.strip()]
    maps = [load(d) for d in run_dirs]
    keys = set(maps[0])
    for m in maps[1:]:
        keys &= set(m)

    stable = [k for k in sorted(keys)
              if all(m[k]["status"] == "FAIL" for m in maps)]

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(f"# Stable completed-but-failed cases\n\n")
        fh.write(f"runs: {run_dirs}\n\n")
        fh.write(f"common tasks: {len(keys)}  |  stable FAIL: {len(stable)}\n\n")
        fh.write("## Task index\n\n")
        for k in stable:
            fh.write(f"- {k}\n")
        fh.write("\n---\n\n")
        for k in stable:
            fp = maps[0][k]["file"]
            t = trace_task(fp, args.max_result)
            fh.write(f"## TASK {k}\n\n")
            fh.write(f"PROMPT: {t['user_prompt']}\n\n")
            fh.write(f"TOOLS USED ({t['n_calls']} calls): {t['tools_used']}\n\n")
            fh.write("FAILED VERIFIERS:\n")
            for fv in t["failed_verifiers"]:
                fh.write(f"- [{fv['cmp']}] {fv['desc']}\n")
                fh.write(f"    expected={fv['expected']} actual={fv['actual']}\n")
                fh.write(f"    SQL: {fv['query']}\n")
            fh.write("\nTRACE:\n")
            for tag, body in t["steps"]:
                fh.write(f"  {tag}: {body}\n")
            fh.write("\n")
            fh.write("*" * 90 + "\n\n")

    print(f"common={len(keys)} stable_fail={len(stable)} -> {args.out}")
    print("stable keys:", stable)


if __name__ == "__main__":
    main()
