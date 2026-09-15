#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Quantify failure mechanisms across runs (all runs, not only stable failures)."""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_failure_traces import is_error_run  # noqa: E402

WRAPPER_BAD = re.compile(r"^_execute_(?!tool$)")
WRITE = re.compile(r"create|update|delete|send|insert|add|remove|modify|apply|patch|trash|"
                   r"import|register|assign|enable|disable|revoke|purge|link|onboard|move", re.I)
READ = re.compile(r"^(get|list|find|search|fetch|retrieve|count)", re.I)


def iter_runs(folder):
    if folder.startswith("_"):
        return
    for f in sorted(glob.glob(os.path.join(folder, "*.json"))):
        if os.path.basename(f).startswith("_"):
            continue
        try:
            yield f, json.load(open(f, encoding="utf-8"))
        except Exception:
            continue


def analyse(d):
    rows = []
    for f, j in iter_runs(d):
        r = j["runs"][0]
        mr = (r.get("model_response") or "").strip()
        status = "ERR" if (not mr or is_error_run(r)) else ("OK" if r.get("overall_success") else "FAIL")
        calls, biz, iserr, succ_false = [], [], 0, 0
        for m in r.get("conversation_flow") or []:
            if m.get("type") == "ai_message":
                for tc in (m.get("tool_calls") or []):
                    nm = tc.get("name") or ""
                    calls.append(nm)
                    if nm == "_execute_tool":
                        inner = (tc.get("args") or {}).get("name")
                        if inner:
                            biz.append(inner)
                    elif nm != "_tool_search" and not WRAPPER_BAD.match(nm) and not nm.startswith("_"):
                        biz.append(nm)
            if m.get("type") == "tool_result":
                res = m.get("result")
                if isinstance(res, dict):
                    if res.get("success") is False:
                        succ_false += 1
                    inner = res.get("result")
                    if isinstance(inner, dict) and inner.get("isError") is True:
                        iserr += 1
        nw = sum(1 for b in biz if WRITE.search(b))
        nr = sum(1 for b in biz if READ.match(b))
        # churn: same write tool invoked >=3 times
        wc = Counter(b for b in biz if WRITE.search(b))
        churn = sum(v for v in wc.values() if v >= 3)
        # trailing read-only tail
        tail = 0
        for b in reversed(biz):
            if READ.match(b):
                tail += 1
            else:
                break
        rows.append({
            "file": os.path.basename(f), "status": status, "n_calls": len(calls),
            "bad_wrapper": sum(1 for c in calls if WRAPPER_BAD.match(c)),
            "iserr_masked": iserr, "succ_false": succ_false,
            "n_write": nw, "n_read": nr, "churn": churn, "tail_read": tail,
        })
    return rows


def main():
    dirs = sys.argv[1:] or [
        "out/email_vloop/run_1", "out/email_vloop/run_2", "out/full_exec_email/run_1",
        "out/meta_hybrid/run_1", "out/full_tfidf/run_1",
    ]
    print(f"{'run':<30}{'n':>4}{'OK':>4}{'FAIL':>5}{'ERR':>4} | "
          f"{'badWrap':>8}{'tasks':>6} | {'isErrMask':>10}{'tasks':>6} | "
          f"{'0-write(F)':>11}{'churn':>7}")
    grand = Counter()
    for d in dirs:
        rows = analyse(d)
        st = Counter(r["status"] for r in rows)
        badw = sum(r["bad_wrapper"] for r in rows)
        badwt = sum(1 for r in rows if r["bad_wrapper"])
        iem = sum(r["iserr_masked"] for r in rows)
        iemt = sum(1 for r in rows if r["iserr_masked"])
        fails = [r for r in rows if r["status"] == "FAIL"]
        zero_w = sum(1 for r in fails if r["n_write"] == 0)
        churn = sum(1 for r in fails if r["churn"] > 0)
        print(f"{d:<30}{len(rows):>4}{st['OK']:>4}{st['FAIL']:>5}{st['ERR']:>4} | "
              f"{badw:>8}{badwt:>6} | {iem:>10}{iemt:>6} | {zero_w:>11}{churn:>7}")
        grand["badw"] += badw
        grand["iem"] += iem
        grand["fail"] += len(fails)
        grand["zero_w"] += zero_w
    print("\nTOTAL bad-wrapper calls:", grand["badw"],
          "| isError-masked calls:", grand["iem"],
          "| completed-failures:", grand["fail"],
          "| of which zero-write:", grand["zero_w"])


if __name__ == "__main__":
    main()
