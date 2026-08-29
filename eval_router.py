"""离线评估脚本(统一版):用任务自带的 selected_tools 标注评估工具路由质量。

用法(项目根目录):
    python eval_router.py [--data_dir data/revised] [--top_k 20]
    python eval_router.py --pool_mode cross          # 模拟 512 工具跨域全池
    python eval_router.py --tools tools_dump.json    # 真实全量工具池(gym dump)

本脚本是 eval_router.py(TF-IDF 版)与 scripts/eval_router_offline.py(关键词版)
合并后的唯一评估入口,调用统一路由器 benchmark.tool_router.route()(即
react_router orchestrator 执行时用的同一入口,不带 LLM 精排)。

指标:
    recall@k       = |routed ∩ selected| / |selected|     ← 生死线,目标 ≥ 0.9
    precision@k    = |routed ∩ selected| / |routed|       ← 0.5-0.7 即可(宁多勿少)
    full coverage  = selected ⊆ routed(一次路由即可,无需渐进式扩展)
    exposed tools  = |routed|(token 成本代理)

⚠️ 诚实性边界:selected_tools 仅用于【评估路由质量】,执行时路由器只输入
user_prompt,禁止读取该字段(否则等于答案泄露)。

⚠️ 近似池说明(未提供 --tools 时):本地 data/revised 只有少量任务,域工具池由
该域所有任务的 selected_tools 并集 + 少量干扰工具近似(池偏小、数字偏乐观)。
真实数字需连 MCP server dump 全量工具池后用 --tools 传入(见 docs/tool_router_design.md 4.4)。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List

from benchmark.tool_router import route

# 干扰工具(来自其他域的典型企业工具,注入域内池以增加逼真度;仅 name-only 池用)
NOISE_TOOLS = [
    {"name": "send_email_message", "description": "Send an email message to a recipient.", "input_schema": {"properties": {"to": {}, "subject": {}, "body": {}}}},
    {"name": "schedule_meeting", "description": "Schedule a meeting in the calendar with attendees.", "input_schema": {"properties": {"start": {}, "end": {}, "attendees": {}}}},
    {"name": "upload_drive_file", "description": "Upload a file to the shared drive.", "input_schema": {"properties": {"name": {}, "content": {}}}},
    {"name": "create_team_channel", "description": "Create a new team channel for collaboration.", "input_schema": {"properties": {"name": {}, "members": {}}}},
    {"name": "update_hr_employee_record", "description": "Update an employee HR record.", "input_schema": {"properties": {"employee_id": {}, "field": {}, "value": {}}}},
    {"name": "create_incident", "description": "Create a new IT incident record.", "input_schema": {"properties": {"short_description": {}, "urgency": {}}}},
    {"name": "post_to_feed", "description": "Post a message to the enterprise social feed.", "input_schema": {"properties": {"text": {}}}},
]


def load_tasks(data_dir: str) -> List[Dict[str, Any]]:
    tasks = []
    for path in sorted(glob.glob(os.path.join(data_dir, "**", "*.json"), recursive=True)):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        domain = os.path.basename(os.path.dirname(path))
        gym = [g.get("mcp_server_name") for g in (d.get("gym_servers_config") or [])]
        tasks.append({
            "id": os.path.basename(path),
            "domain": domain,
            "gym": gym,
            "user_prompt": d.get("user_prompt", ""),
            "selected_tools": set(d.get("selected_tools") or []),
        })
    return tasks


def load_real_tools(tools_path: str) -> List[Dict[str, Any]]:
    with open(tools_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "tools" in data:
        data = data["tools"]
    return [t for t in data if isinstance(t, dict) and t.get("name")]


def build_domain_pool(tasks: List[Dict[str, Any]], domain: str) -> List[Dict[str, Any]]:
    """近似域工具池 = 该域所有任务 selected_tools 的并集 + 干扰工具。"""
    pool_names: set = set()
    for t in tasks:
        if t["domain"] == domain:
            pool_names |= t["selected_tools"]
    pool = [{"name": n, "description": n.replace("_", " "), "input_schema": {"properties": {}}}
            for n in sorted(pool_names)]
    pool.extend(NOISE_TOOLS)
    return pool


def build_cross_pool(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """近似跨域全池 = 所有域 selected_tools 并集(模拟 512 工具空间)。"""
    pool_names = sorted({n for t in tasks for n in t["selected_tools"]})
    return [{"name": n, "description": n.replace("_", " "), "input_schema": {"properties": {}}}
            for n in pool_names]


def evaluate(tasks: List[Dict[str, Any]], pools: Dict[str, List[Dict[str, Any]]], top_k: int) -> None:
    per_task_rows = []
    dom_agg = defaultdict(lambda: {"tp": 0, "n_sel": 0, "n_pred": 0, "full": 0, "fb": 0, "n": 0, "exposed": 0})
    all_agg = {"tp": 0, "n_sel": 0, "n_pred": 0, "full": 0, "fb": 0, "n": 0, "exposed": 0}

    for t in tasks:
        pool = pools[t["domain"]]
        result = route(t["user_prompt"], pool, top_k=top_k)
        pred = set(result.selected)
        oracle = t["selected_tools"]
        tp = len(pred & oracle)
        recall = tp / len(oracle) if oracle else 0.0
        precision = tp / len(pred) if pred else 0.0
        full = 1 if oracle and oracle <= pred else 0

        print(f"{t['domain']:<8} {t['id'][:28]:<28} |{len(oracle):>3} |{len(pred):>3} | "
              f"recall={recall:>6.1%} precision={precision:>6.1%} "
              f"full={'Y' if full else '-'} fb={result.fallback or '-'}")

        a = dom_agg[t["domain"]]
        for agg in (a, all_agg):
            agg["tp"] += tp
            agg["n_sel"] += len(oracle)
            agg["n_pred"] += len(pred)
            agg["full"] += full
            agg["fb"] += 1 if result.fallback else 0
            agg["n"] += 1
            agg["exposed"] += len(pred)
        per_task_rows.append((t["domain"], recall, full))

    print("\n=== 汇总(micro:所有任务工具级合计)===")
    print(f"{'domain':<8}{'n':>4}{'recall@k':>10}{'precision@k':>13}{'full_cov':>10}{'exposed':>9}{'fallback':>10}")
    print("-" * 66)
    for dom, a in sorted(dom_agg.items()):
        _print_agg_row(dom, a, top_k)
    _print_agg_row("ALL", all_agg, top_k)

    macro = sum(r for _, r, _ in per_task_rows) / len(per_task_rows) if per_task_rows else 0
    print(f"\nmacro recall@{top_k}(按任务平均): {macro:.1%}")
    print(f"目标:recall@{top_k} >= 90%(漏一个工具任务即败;渐进式扩展会兜底到 1.0)")
    print(f"     precision@{top_k} 0.5-0.7 即可(宁多勿少);full_cov 越高,渐进式扩展触发越少")


def _print_agg_row(label: str, a: Dict[str, float], top_k: int) -> None:
    recall = a["tp"] / a["n_sel"] * 100 if a["n_sel"] else 0
    precision = a["tp"] / a["n_pred"] * 100 if a["n_pred"] else 0
    full = a["full"] / a["n"] * 100 if a["n"] else 0
    exposed = a["exposed"] / a["n"] if a["n"] else 0
    print(f"{label:<8}{int(a['n']):>4}{recall:>9.1f}%{precision:>12.1f}%{full:>9.1f}%{exposed:>9.1f}{int(a['fb']):>10}")


def inspect_sample(tasks: List[Dict[str, Any]], pools: Dict[str, List[Dict[str, Any]]], top_k: int) -> None:
    """打印第一个任务的路由明细,便于人工核对漏检。"""
    t = tasks[0]
    result = route(t["user_prompt"], pools[t["domain"]], top_k=top_k)
    pred = set(result.selected)
    miss = t["selected_tools"] - pred
    print("=== 样例路由核对 ===")
    print(f"任务: {t['user_prompt'][:140]}...")
    print(f"ground truth({len(t['selected_tools'])}): {sorted(t['selected_tools'])}")
    print(f"预测({len(pred)}, method={result.method}): {sorted(pred)}")
    print(f"漏检: {sorted(miss) if miss else '无'}")
    print(f"meta: {result.to_metadata()}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline tool router evaluation (unified)")
    ap.add_argument("--data_dir", default="data/revised")
    ap.add_argument("--top_k", type=int, default=20)
    ap.add_argument("--pool_mode", choices=["domain", "cross"], default="domain",
                    help="domain=每域独立池(默认,贴近生产);cross=跨域合并池(模拟无域锁定的 512 工具空间)")
    ap.add_argument("--tools", default=None,
                    help="真实全量工具池 JSON(list[{name, description, input_schema}]);提供时忽略近似池")
    args = ap.parse_args()

    tasks = load_tasks(args.data_dir)
    if not tasks:
        print(f"未找到任务: {args.data_dir}")
        sys.exit(1)

    if args.tools:
        real = load_real_tools(args.tools)
        print(f"使用真实全量工具池: {len(real)} tools")
        pools = {d: real for d in {t["domain"] for t in tasks}}
    elif args.pool_mode == "cross":
        pool = build_cross_pool(tasks)
        print(f"使用近似跨域全池: {len(pool)} tools(pool_mode=cross)")
        pools = {d: pool for d in {t["domain"] for t in tasks}}
    else:
        pools = {d: build_domain_pool(tasks, d) for d in {t["domain"] for t in tasks}}
        print("使用近似域内池(pool_mode=domain): " +
              ", ".join(f"{d}={len(p)}" for d, p in sorted(pools.items())))
    print(f"共 {len(tasks)} 任务, top_k={args.top_k}\n")

    print(f"{'domain':<8} {'task':<28} | GT |pred | per-task metrics")
    print("-" * 96)
    inspect_sample(tasks, pools, args.top_k)
    evaluate(tasks, pools, args.top_k)


if __name__ == "__main__":
    main()
