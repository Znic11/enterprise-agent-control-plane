"""记忆实验卷分析:evaluate.py 产物直读 mem_* 字段 + 与对照卷配对。

背景:`scripts/analyze_vloop_runs.py` 只认 vl_* 字段,对 mem_*(Phase 3 记忆)
不可见 —— 本脚本补齐记忆维度(折叠触发率/事实库新鲜度/成本),并复用同一
McNemar 精确配对口径,供 V1.1 复跑卷(run_mem_v11,fold_after=8)分析用。

分析内容:
  ① 单卷:clean 成功率 / error / 成本(耗时、LLM 轮次、业务工具数)
  ② 记忆行为:mem_folds 分布(折叠触发率 —— 检验力核心)、mem_facts /
     mem_valid_facts / mem_invalid_facts(V1.1 写时协调是否真的在作废旧事实)、
     mem_rounds / recap 长度
  ③ 配对翻转(可选 --baseline_dir):同任务干净配对 saved/regressed + McNemar
  ④ folds>0 子集:记忆真正生效样本的单看(成功率 + 相对 baseline 配对)

用法:
    python scripts/analyze_memory_runs.py \
        --mem_dir out/email_memory/run_mem_v11/run_1 \
        --baseline_dir out/email_vloop/run_1 \
        --compare_dir out/email_memory/run_mem/run_1 \
        --out out/memory_report_v11
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional


def load(folder: str) -> Dict[str, Dict[str, Any]]:
    """载入目录下所有 results_*.json -> {任务名: 代表 run 摘要}。

    代表 run = 第一个无 error 的 run,否则 runs[0](与 compute_score /
    analyze_vloop_runs 口径一致)。任务名 = 去掉 results_ 前缀与 .json 后缀。
    """
    recs: Dict[str, Dict[str, Any]] = {}
    if not os.path.isdir(folder):
        sys.exit(f"[analyze_memory] not a folder: {folder}")
    for root, _, files in os.walk(folder):
        for fn in sorted(files):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(root, fn), "r", encoding="utf-8") as f:
                data = json.load(f)
            runs = data.get("runs") or []
            if not runs:
                continue
            run = next((r for r in runs if not r.get("error")), runs[0])
            name = fn.replace("results_", "").replace(".json", "")
            flow = run.get("conversation_flow") or []
            rounds = sum(1 for e in flow
                         if isinstance(e, dict) and e.get("type") == "ai_message")
            recs[name] = {
                "error": bool(run.get("error")),
                "error_text": str(run.get("error") or "")[:200],
                "ok": bool(run.get("overall_success")),
                "vpass": float((run.get("verification_summary") or {})
                               .get("pass_rate", 0.0) or 0.0),
                "n_tools": len(run.get("tools_used") or []),
                "ms": int(run.get("execution_time_ms") or 0),
                "rounds": rounds,
                # ---- mem_* (Phase 3) ----
                "mem_enabled": bool(run.get("mem_enabled")),
                "mem_facts": int(run.get("mem_facts") or 0),
                "mem_valid": int(run.get("mem_valid_facts") or 0),
                "mem_invalid": int(run.get("mem_invalid_facts") or 0),
                "mem_folds": int(run.get("mem_folds") or 0),
                "mem_recap_chars": int(run.get("mem_recap_chars") or 0),
                "mem_rounds": int(run.get("mem_rounds") or 0),
                # ---- vl_* (Phase 2,顺带) ----
                "vl_gate_rounds": int(run.get("vl_gate_rounds") or 0),
            }
    return recs


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def summarize(recs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    n = len(recs)
    clean = [r for r in recs.values() if not r["error"]]
    ok = [r for r in clean if r["ok"]]
    n_c = len(clean)
    folds = Counter(r["mem_folds"] for r in clean)
    mem_on = [r for r in clean if r["mem_enabled"]]
    act = [r for r in mem_on if r["mem_folds"] > 0]
    return {
        "files": n,
        "errors": sum(1 for r in recs.values() if r["error"]),
        "clean": n_c,
        "success": len(ok),
        "success_rate": len(ok) / n_c if n_c else 0.0,
        "vpass_mean": _mean([r["vpass"] for r in clean]),
        "cost_sec_mean": _mean([r["ms"] for r in clean]) / 1000.0,
        "rounds_mean": _mean([r["rounds"] for r in clean]),
        "tools_mean": _mean([r["n_tools"] for r in clean]),
        "mem_enabled": len(mem_on),
        "folds_dist": dict(sorted(folds.items())),
        "folds_gt0": len(act),
        "activation_rate": len(act) / len(mem_on) if mem_on else 0.0,
        "facts_mean": _mean([r["mem_facts"] for r in mem_on]),
        "valid_mean": _mean([r["mem_valid"] for r in mem_on]),
        "invalid_mean": _mean([r["mem_invalid"] for r in mem_on]),
        "files_with_invalid": sum(1 for r in mem_on if r["mem_invalid"] > 0),
        "files_all_invalid": sum(1 for r in mem_on
                                 if r["mem_facts"] > 0 and r["mem_valid"] == 0),
        "recap_chars_mean": _mean([r["mem_recap_chars"] for r in act]),
        "gate_rounds_mean": _mean([r["vl_gate_rounds"] for r in clean]),
    }


def fold_gt0_subset(recs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    act = [r for r in recs.values() if not r["error"] and r["mem_folds"] > 0]
    ok = [r for r in act if r["ok"]]
    return {
        "n": len(act),
        "success": len(ok),
        "success_rate": len(ok) / len(act) if act else 0.0,
        "folds_mean": _mean([r["mem_folds"] for r in act]),
        "facts_mean": _mean([r["mem_facts"] for r in act]),
        "valid_mean": _mean([r["mem_valid"] for r in act]),
        "invalid_mean": _mean([r["mem_invalid"] for r in act]),
        "rounds_mean": _mean([r["rounds"] for r in act]),
        "names": sorted(k for k, v in recs.items()
                        if not v["error"] and v["mem_folds"] > 0),
    }


def _mcnemar_exact_p(b: int, n: int) -> float:
    if n == 0:
        return 1.0
    k = min(b, n - b)
    p = sum(math.comb(n, i) * (0.5 ** n) for i in range(k + 1))
    return min(1.0, 2.0 * p)


def pair_flip(base: Dict[str, Dict[str, Any]],
              treat: Dict[str, Dict[str, Any]],
              only_names: Optional[set] = None) -> Dict[str, Any]:
    b = {k: v for k, v in base.items() if not v["error"]}
    t = {k: v for k, v in treat.items() if not v["error"]}
    common = sorted(set(b) & set(t))
    if only_names is not None:
        common = [k for k in common if k in only_names]
    saved, regressed, both_pass, both_fail = [], [], [], []
    for name in common:
        bo, to = b[name]["ok"], t[name]["ok"]
        if bo and to:
            both_pass.append(name)
        elif not bo and not to:
            both_fail.append(name)
        elif not bo and to:
            saved.append(name)
        else:
            regressed.append(name)
    return {
        "paired": len(common),
        "saved": saved, "regressed": regressed,
        "both_pass": both_pass, "both_fail": both_fail,
        "saved_n": len(saved), "regressed_n": len(regressed),
        "mcnemar_p": _mcnemar_exact_p(len(saved), len(saved) + len(regressed)),
    }


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def print_summary(tag: str, s: Dict[str, Any]) -> None:
    print("=" * 74)
    print(f"① {tag}")
    print("=" * 74)
    print(f"文件 {s['files']}(error {s['errors']})  干净 {s['clean']}")
    print(f"Success  : {s['success']}/{s['clean']} = {_pct(s['success_rate'])}"
          f"   VerifierPass {_pct(s['vpass_mean'])}")
    print(f"成本     : 耗时 {s['cost_sec_mean']:.0f}s  轮次 {s['rounds_mean']:.1f}"
          f"  业务工具 {s['tools_mean']:.1f}  gate轮次 {s['gate_rounds_mean']:.2f}")
    print(f"记忆     : mem_on {s['mem_enabled']}  folds 分布 {s['folds_dist']}")
    print(f"          折叠触发 {s['folds_gt0']}/{s['mem_enabled']} "
          f"= {_pct(s['activation_rate'])}")
    print(f"事实库   : facts {s['facts_mean']:.1f}  valid {s['valid_mean']:.1f}"
          f"  invalid {s['invalid_mean']:.1f}")
    print(f"          有失效事实的文件 {s['files_with_invalid']}"
          f"  全失效文件 {s['files_all_invalid']}")
    print(f"recap    : {s['recap_chars_mean']:.0f} chars(折叠均值)")


def print_pair(tag: str, pf: Dict[str, Any]) -> None:
    print("-" * 74)
    print(f"② 配对 {tag}:paired {pf['paired']}  saved {pf['saved_n']}"
          f"  regressed {pf['regressed_n']}  McNemar p={pf['mcnemar_p']:.4f}"
          f"  {'显著' if pf['mcnemar_p'] < 0.05 else '未达显著'}")
    if pf["saved"]:
        print(f"   saved    : {', '.join(pf['saved'])}")
    if pf["regressed"]:
        print(f"   regressed: {', '.join(pf['regressed'])}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mem_dir", required=True, help="记忆实验卷目录")
    ap.add_argument("--baseline_dir", default=None,
                    help="对照卷(同链无 memory;配对用)")
    ap.add_argument("--compare_dir", default=None,
                    help="另一记忆卷(如 V1 fold=12),用于折叠触发率/成本对比")
    ap.add_argument("--out", default="memory_report", help="报告前缀")
    args = ap.parse_args()

    mem = load(args.mem_dir)
    ms = summarize(mem)
    print_summary(f"记忆卷 {args.mem_dir}", ms)
    sub = fold_gt0_subset(mem)
    print(f"   folds>0 子集: n={sub['n']} success {sub['success']}/{sub['n']}"
          f" = {_pct(sub['success_rate'])}  folds均值 {sub['folds_mean']:.1f}"
          f"  rounds均值 {sub['rounds_mean']:.1f}")

    report: Dict[str, Any] = {"mem_dir": args.mem_dir, "mem_summary": ms,
                              "folds_gt0_subset": sub, "pairs": {}}

    if args.baseline_dir:
        base = load(args.baseline_dir)
        bs = summarize(base)
        print_summary(f"对照卷 {args.baseline_dir}", bs)
        report["baseline_summary"] = bs
        pf = pair_flip(base, mem)
        print_pair("记忆卷 vs 对照卷(全部干净任务)", pf)
        report["pairs"]["vs_baseline_all"] = pf
        pf_sub = pair_flip(base, mem, only_names=set(sub["names"]))
        print_pair("记忆卷 vs 对照卷(folds>0 子集)", pf_sub)
        report["pairs"]["vs_baseline_folds_gt0"] = pf_sub

    if args.compare_dir:
        cmp_ = load(args.compare_dir)
        cs = summarize(cmp_)
        print_summary(f"历史记忆卷 {args.compare_dir}", cs)
        report["compare_summary"] = cs
        pf2 = pair_flip(mem, cmp_)   # 以 V1.1 为基准,v11 相对 V1
        print_pair("V1.1(fold8) vs 历史记忆卷(fold12)", pf2)
        report["pairs"]["v11_vs_v1"] = pf2

    out_json = f"{args.out}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("=" * 74)
    print(f"报告已写: {out_json}")
    print("JSON_SUMMARY=" + json.dumps({
        "success_rate": ms["success_rate"],
        "folds_gt0": ms["folds_gt0"],
        "activation_rate": ms["activation_rate"],
        "invalid_mean": ms["invalid_mean"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
