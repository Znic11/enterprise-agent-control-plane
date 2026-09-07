"""verifier-in-the-loop 收口分析:单卷 vl_* 行为统计 + 与 baseline 的配对翻转。

输入是 evaluate.py 的产物目录(evaluate 输出结构: <folder>/run_N/results_*.json,
每个文件 = 一个任务 config 的一次 execute_benchmark 结果,内含 runs[] + statistics)。
本脚本支持:
 ① 单卷统计(--vloop_dir):aggregate 每个 run 的 vl_* 字段 —— gate 拦截率 /
    只读证据 / forced_done / correction_turns / checklist 长度,并按 gate 行为
    分层看 success rate(判断 gate 是"救回"还是"拖累");
 ② 配对翻转(--baseline_dir):与 baseline 同任务名逐一配对,统计
    saved(baseline fail -> vloop pass)/ regressed(baseline pass -> vloop fail)
    / both_fail / both_pass,给出 McNemar 精确双侧 p 值(纯 stdlib,math.comb);
 ③ error 归类:列 error 文件并按 超时/网络/其他 粗分类(verify_loop 每任务 +1
    规划调用 + gate 轮次 -> 单任务变长,需确认 error 不是 gate 引入);
 ④ 演示样例抽取:找 success & gate_rounds>=1 的文件,把 conversation_flow 中
    stage=verify_loop_gate/remind/checklist 附近 ±N 条消息切成可读文本,
    输出为 report JSON + Markdown(Phase 2 验收的"自查纠错"演示素材)。

用法:
    python scripts/analyze_vloop_runs.py --vloop_dir out/email_vloop/run_1 \
        [--baseline_dir out/email_base/run_1] [--out vloop_report]

输出:终端表格 + <out>.json / <out>.md(演示样例等长文本进 md)。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VL_STAGES = ("verify_loop_checklist", "verify_loop_gate", "verify_loop_remind")
FINAL_KEYS = (
    "vl_enabled", "vl_checklist", "vl_gate_rounds", "vl_read_calls_gate",
    "vl_read_tools", "vl_no_read_reminders", "vl_correction_turns",
    "vl_forced_done", "vl_plan_calls", "vl_final_marker",
)


# ---------------------------------------------------------------------------
# 载入与规整
# ---------------------------------------------------------------------------

def _run_of_file(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """取该任务文件的代表 run:优先第一个无 error 的 run,否则 runs[0]。
    与 compute_score 的"文件有 error = 任一 run 有 error"口径尽量一致。"""
    runs = data.get("runs") or []
    if not runs:
        return None
    for r in runs:
        if not r.get("error"):
            return r
    return runs[0]


def load_folder(folder: str) -> List[Dict[str, Any]]:
    """载入一个 evaluate 结果目录(flat run_N/results_*.json 或直接含 json)。"""
    records: List[Dict[str, Any]] = []
    if not os.path.isdir(folder):
        sys.exit(f"[analyze_vloop] not a folder: {folder}")
    for root, _, files in os.walk(folder):
        for fn in sorted(files):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(root, fn)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            run = _run_of_file(data)
            if run is None:
                continue
            rec = {
                "file": os.path.basename(fn).replace(".json", ""),
                "path": path,
                "error": bool(run.get("error")),
                "error_text": str(run.get("error", ""))[:300],
                "overall_success": bool(run.get("overall_success")),
                "verifier_pass": (
                    (run.get("verification_summary") or {}).get("pass_rate", 0.0)
                ),
                "tools_used": list(run.get("tools_used") or []),
                "conversation_flow": run.get("conversation_flow") or [],
            }
            for k in FINAL_KEYS:
                rec[k] = run.get(k, None)
            records.append(rec)
    return records


def vl(rec: Dict[str, Any], key: str, default: Any = 0) -> Any:
    v = rec.get(key)
    return default if v is None else v


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


# ---------------------------------------------------------------------------
# ① 单卷 vl_* 行为统计
# ---------------------------------------------------------------------------

def summarize_vloop(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(records)
    errs = [r for r in records if r["error"]]
    clean = [r for r in records if not r["error"]]
    n_c = len(clean)
    ok = [r for r in clean if r["overall_success"]]
    vl_on = [r for r in records if vl(r, "vl_enabled", False)]
    gated = [r for r in vl_on if vl(r, "vl_gate_rounds") > 0]
    forced = [r for r in gated if vl(r, "vl_forced_done")]
    corrected = [r for r in gated if vl(r, "vl_correction_turns") > 0]
    read_ev = [r for r in gated if vl(r, "vl_read_calls_gate") > 0]

    def rate(ok_list: List[Dict[str, Any]], base: List[Dict[str, Any]]) -> float:
        return len(ok_list) / len(base) if base else 0.0

    def mean(items: List[Dict[str, Any]], key: str) -> float:
        vals = [vl(r, key) for r in items if vl(r, key, None) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    gated_ok = [r for r in gated if r["overall_success"]]
    ungated_ok = [r for r in vl_on if vl(r, "vl_gate_rounds") == 0
                  and r["overall_success"]]
    ungated = [r for r in vl_on if vl(r, "vl_gate_rounds") == 0]
    forced_ok = [r for r in forced if r["overall_success"]]

    summary = {
        "total_files": n,
        "error_files": len(errs),
        "clean_files": n_c,
        "success_files": len(ok),
        "success_rate": rate(ok, clean),
        "verifier_pass_mean": (
            sum(r["verifier_pass"] for r in clean) / n_c if n_c else 0.0
        ),
        "vl_enabled_files": len(vl_on),
        "gated_files": len(gated),
        "gated_rate": rate(gated, vl_on),
        "gated_success_rate": rate(gated_ok, gated),
        "ungated_success_rate": rate(ungated_ok, ungated),
        "forced_done_files": len(forced),
        "forced_done_success_rate": rate(forced_ok, forced),
        "corrected_files": len(corrected),
        "read_evidence_files": len(read_ev),
        "mean_gate_rounds": mean(gated, "vl_gate_rounds"),
        "mean_read_calls_gate": mean(gated, "vl_read_calls_gate"),
        "mean_no_read_reminders": mean(gated, "vl_no_read_reminders"),
        "mean_correction_turns": mean(gated, "vl_correction_turns"),
        "mean_checklist_len": (
            sum(len(str(vl(r, "vl_checklist", ""))) for r in vl_on)
            / len(vl_on) if vl_on else 0.0
        ),
        "final_marker_rate": (
            sum(1 for r in vl_on if vl(r, "vl_final_marker", False)) / len(vl_on)
            if vl_on else 0.0
        ),
    }
    return summary


def print_vloop(summary: Dict[str, Any]) -> None:
    print("=" * 72)
    print("① 单卷 vl_* 行为统计(verify_loop 卷)")
    print("=" * 72)
    print(f"总文件            : {summary['total_files']}  (error {summary['error_files']})")
    print(f"Success           : {summary['success_files']}/{summary['clean_files']} "
          f"= {_fmt_pct(summary['success_rate'])}")
    print(f"Verifier Pass 均值: {_fmt_pct(summary['verifier_pass_mean'])}")
    print("-" * 72)
    print(f"vl_enabled 文件   : {summary['vl_enabled_files']}")
    print(f"gate 触发文件     : {summary['gated_files']} "
          f"({_fmt_pct(summary['gated_rate'])} of vl_on)")
    print(f"  ├ gated 成功率  : {_fmt_pct(summary['gated_success_rate'])}"
          f"   (gate 触发后仍成功的比例)")
    print(f"  └ 未触发成功率  : {_fmt_pct(summary['ungated_success_rate'])}"
          f"   (对照:没被 gate 拦的任务)")
    print(f"forced_done       : {summary['forced_done_files']} 个"
          f"(无证据强制收尾,成功率 {_fmt_pct(summary['forced_done_success_rate'])})")
    print(f"纠错回合文件      : {summary['corrected_files']} 个")
    print(f"只读证据文件      : {summary['read_evidence_files']} 个")
    print(f"mean gate_rounds  : {summary['mean_gate_rounds']:.2f}")
    print(f"mean read_calls   : {summary['mean_read_calls_gate']:.2f}")
    print(f"mean reminders    : {summary['mean_no_read_reminders']:.2f}")
    print(f"mean corrections  : {summary['mean_correction_turns']:.2f}")
    print(f"mean checklist    : {summary['mean_checklist_len']:.0f} chars")
    print(f"FINAL 标记率      : {_fmt_pct(summary['final_marker_rate'])}")


# ---------------------------------------------------------------------------
# ② 配对翻转 + McNemar(纯 stdlib 精确双侧)
# ---------------------------------------------------------------------------

def _mcnemar_exact_p(b: int, n: int) -> float:
    """McNemar 精确双侧:翻转对总数为 n,利于 treatment 的为 b。
    p = 2 * P(X <= min(b, n-b)) under Binomial(n, 0.5),封顶 1。"""
    if n == 0:
        return 1.0
    k = min(b, n - b)
    p = 0.0
    for i in range(k + 1):
        p += math.comb(n, i) * (0.5 ** n)
    return min(1.0, 2.0 * p)


def pair_flip(baseline: List[Dict[str, Any]], vloop: List[Dict[str, Any]]) -> Dict[str, Any]:
    base_by_name = {r["file"]: r for r in baseline if not r["error"]}
    vloop_by_name = {r["file"]: r for r in vloop if not r["error"]}
    common = sorted(set(base_by_name) & set(vloop_by_name))
    saved, regressed, both_pass, both_fail = [], [], [], []
    for name in common:
        b_ok, v_ok = base_by_name[name]["overall_success"], vloop_by_name[name]["overall_success"]
        if b_ok and v_ok:
            both_pass.append(name)
        elif not b_ok and not v_ok:
            both_fail.append(name)
        elif not b_ok and v_ok:
            saved.append(name)
        else:
            regressed.append(name)
    b_disc = len(saved)
    n_disc = b_disc + len(regressed)
    return {
        "paired": len(common),
        "saved": saved,
        "regressed": regressed,
        "both_pass": both_pass,
        "both_fail": both_fail,
        "saved_n": b_disc,
        "regressed_n": len(regressed),
        "mcnemar_p": _mcnemar_exact_p(b_disc, n_disc),
    }


def print_pair(pf: Dict[str, Any]) -> None:
    print("=" * 72)
    print("② 配对翻转(baseline vs verify_loop,仅干净任务配对)")
    print("=" * 72)
    print(f"配对任务数   : {pf['paired']}")
    print(f"saved        : {pf['saved_n']}  (baseline fail -> vloop pass)")
    print(f"regressed    : {pf['regressed_n']}  (baseline pass -> vloop fail)")
    print(f"both_pass    : {len(pf['both_pass'])}  both_fail: {len(pf['both_fail'])}")
    print(f"McNemar p    : {pf['mcnemar_p']:.4f} "
          f"({'显著(p<0.05)' if pf['mcnemar_p'] < 0.05 else '未达显著'})")
    if pf["saved"]:
        print(f"saved 明细   : {', '.join(pf['saved'][:20])}")
    if pf["regressed"]:
        print(f"regressed 明细: {', '.join(pf['regressed'][:20])}")


# ---------------------------------------------------------------------------
# ③ error 归类
# ---------------------------------------------------------------------------

def classify_errors(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    errs = []
    for r in records:
        if not r["error"]:
            continue
        t = r["error_text"].lower()
        if any(k in t for k in ("timeout", "readtimeout", "timed out")):
            cls = "timeout"
        elif any(k in t for k in ("connection", "refused", "network", "resolve",
                                  "econnreset", "dns")):
            cls = "network"
        elif any(k in t for k in ("rate", "429", "quota", "too many")):
            cls = "rate_limit"
        else:
            cls = "other"
        errs.append({"file": r["file"], "class": cls, "text": r["error_text"]})
    return errs


# ---------------------------------------------------------------------------
# ④ 演示样例抽取(gate 拦截 -> 只读回读 -> 纠错 -> 通过 的轨迹)
# ---------------------------------------------------------------------------

def extract_demos(records: List[Dict[str, Any]], limit: int = 3,
                  context: int = 4) -> List[Dict[str, Any]]:
    cands = [r for r in records
             if not r["error"] and r["overall_success"]
             and vl(r, "vl_gate_rounds", 0) > 0
             and (vl(r, "vl_correction_turns", 0) > 0
                  or vl(r, "vl_read_calls_gate", 0) > 0)]
    cands.sort(key=lambda r: (vl(r, "vl_correction_turns"), vl(r, "vl_gate_rounds")),
               reverse=True)
    demos = []
    for rec in cands[:limit]:
        flow = rec["conversation_flow"]
        idxs = [i for i, e in enumerate(flow)
                if isinstance(e, dict) and e.get("stage") in VL_STAGES]
        segs = []
        for i in idxs:
            lo, hi = max(0, i - context), min(len(flow), i + context + 1)
            seg = []
            for e in flow[lo:hi]:
                t = e.get("type", "?")
                if t == "ai_message":
                    seg.append(f"[ai] {str(e.get('content',''))[:160]}")
                elif t == "tool_result":
                    nm = e.get("tool_name", "?")
                    res = e.get("result", {})
                    okk = res.get("success", "") if isinstance(res, dict) else ""
                    seg.append(f"[tool:{nm}] success={okk}")
                elif t == "system_message":
                    stage = e.get("stage")
                    mark = f" <stage:{stage}>" if stage else ""
                    seg.append(f"[system{mark}] {str(e.get('content',''))[:200]}")
                elif t == "user_message":
                    seg.append(f"[user] {str(e.get('content',''))[:120]}")
            segs.append("\n".join(seg))
        demos.append({
            "file": rec["file"],
            "gate_rounds": vl(rec, "vl_gate_rounds"),
            "correction_turns": vl(rec, "vl_correction_turns"),
            "read_calls_gate": vl(rec, "vl_read_calls_gate"),
            "read_tools": list(vl(rec, "vl_read_tools", [])),
            "tools_used": rec["tools_used"],
            "final_response": str(
                next((e.get("content", "") for e in reversed(flow)
                      if isinstance(e, dict) and e.get("type") == "ai_message"), "")
            )[:300],
            "segments": segs,
        })
    return demos


def write_markdown(path: str, summary: Dict[str, Any],
                   pf: Optional[Dict[str, Any]], errs: List[Dict[str, Any]],
                   demos: List[Dict[str, Any]]) -> None:
    lines = ["# verifier-in-the-loop 收口报告", ""]
    s = summary
    lines += [
        f"- 卷: {s['total_files']} 文件(error {s['error_files']})",
        f"- Avg Success: **{s['success_files']}/{s['clean_files']} = "
        f"{_fmt_pct(s['success_rate'])}**, Verifier Pass 均值 {_fmt_pct(s['verifier_pass_mean'])}",
        f"- gate 触发 {s['gated_files']}/{s['vl_enabled_files']} vl_on 文件;"
        f"gated 成功率 {_fmt_pct(s['gated_success_rate'])} vs 未触发 {_fmt_pct(s['ungated_success_rate'])}",
        f"- forced_done {s['forced_done_files']} 个(成功率 {_fmt_pct(s['forced_done_success_rate'])})",
        f"- 纠错文件 {s['corrected_files']} / 只读证据文件 {s['read_evidence_files']}",
        "",
    ]
    if pf:
        lines += [
            "## 配对翻转",
            "",
            f"- 配对 {pf['paired']}:saved {pf['saved_n']},regressed {pf['regressed_n']},"
            f"McNemar p = {pf['mcnemar_p']:.4f}",
            f"- saved: {', '.join(pf['saved'])}" if pf["saved"] else "- saved: 无",
            f"- regressed: {', '.join(pf['regressed'])}" if pf["regressed"] else "- regressed: 无",
            "",
        ]
    if errs:
        by = Counter(e["class"] for e in errs)
        lines += ["## error 归类", "", f"- {dict(by)}", ""]
        for e in errs:
            lines.append(f"- [{e['class']}] {e['file']}: {e['text'][:120]}")
        lines.append("")
    lines += ["## 自查纠错演示样例", ""]
    for i, d in enumerate(demos, 1):
        lines += [
            f"### Demo {i}: {d['file']}",
            f"- gate_rounds={d['gate_rounds']}, corrections={d['correction_turns']}, "
            f"read_evidence={d['read_calls_gate']}, read_tools={d['read_tools']}",
            f"- tools_used: {', '.join(d['tools_used'])}",
            f"- final: {d['final_response']}",
            "",
            "```",
        ]
        for seg in d["segments"]:
            lines.append(seg)
            lines.append("---")
        lines += ["```", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vloop_dir", required=True, help="verify_loop 结果目录(可含 run_N)")
    ap.add_argument("--baseline_dir", default=None, help="baseline 结果目录(配对用)")
    ap.add_argument("--out", default="vloop_report", help="报告输出前缀(生成 .json/.md)")
    ap.add_argument("--demos", type=int, default=3, help="演示样例条数")
    args = ap.parse_args()

    vloop = load_folder(args.vloop_dir)
    if not vloop:
        sys.exit(f"[analyze_vloop] vloop_dir 没有可读 results_*.json: {args.vloop_dir}")
    summary = summarize_vloop(vloop)
    print_vloop(summary)

    pf = None
    if args.baseline_dir:
        base = load_folder(args.baseline_dir)
        if not base:
            sys.exit(f"[analyze_vloop] baseline_dir 没有可读 results_*.json: {args.baseline_dir}")
        pf = pair_flip(base, vloop)
        print_pair(pf)

    errs = classify_errors(vloop)
    if errs:
        print("=" * 72)
        print("③ error 归类")
        print("=" * 72)
        print(f"  {dict(Counter(e['class'] for e in errs))}")
        for e in errs[:10]:
            print(f"  [{e['class']}] {e['file']}: {e['text'][:120]}")

    demos = extract_demos(vloop, limit=args.demos)
    out_json, out_md = f"{args.out}.json", f"{args.out}.md"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "pair_flip": pf, "errors": errs,
                   "demos": demos}, f, ensure_ascii=False, indent=2)
    write_markdown(out_md, summary, pf, errs, demos)
    print("=" * 72)
    print(f"报告已写: {out_json} / {out_md}")

    # 供接续处理的机器可读摘要
    print("JSON_SUMMARY=" + json.dumps({
        "success_rate": summary["success_rate"],
        "verifier_pass": summary["verifier_pass_mean"],
        "gated": summary["gated_files"],
        "forced_done": summary["forced_done_files"],
        **({"saved": pf["saved_n"], "regressed": pf["regressed_n"],
            "mcnemar_p": pf["mcnemar_p"]} if pf else {}),
    }))


if __name__ == "__main__":
    main()
