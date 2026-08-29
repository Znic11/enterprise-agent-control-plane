"""离线评估脚本:用任务自带的 selected_tools 标注评估 ToolRouter 质量。

用法(项目根目录):
    python eval_router.py [--data_dir data/revised] [--top_k 20]

⚠️ 诚实性边界:selected_tools 仅用于【评估路由质量】,执行时路由器只输入
user_prompt,禁止读取该字段(否则等于答案泄露)。

⚠️ 近似池说明:本地 data/revised 只有少量任务,域工具池由该域所有任务的
selected_tools 并集近似(偏小、数字偏乐观)。真实数字需要连 MCP server
dump 全量工具池后替换(见文档 4.4)。
"""

import argparse
import glob
import json
import os
from collections import defaultdict

from benchmark.tool_router import ToolRouter, build_tool_signature

# 工具池里注入的"干扰工具"(来自其他域的典型企业工具名,仅用于 smoke test 逼真度)
NOISE_TOOLS = [
    {"name": "send_email_message", "description": "Send an email message to a recipient.", "input_schema": {"properties": {"to": {}, "subject": {}, "body": {}}}},
    {"name": "schedule_meeting", "description": "Schedule a meeting in the calendar with attendees.", "input_schema": {"properties": {"start": {}, "end": {}, "attendees": {}}}},
    {"name": "upload_drive_file", "description": "Upload a file to the shared drive.", "input_schema": {"properties": {"name": {}, "content": {}}}},
    {"name": "create_team_channel", "description": "Create a new team channel for collaboration.", "input_schema": {"properties": {"name": {}, "members": {}}}},
    {"name": "update_hr_employee_record", "description": "Update an employee HR record.", "input_schema": {"properties": {"employee_id": {}, "field": {}, "value": {}}}},
    {"name": "create_incident", "description": "Create a new IT incident record.", "input_schema": {"properties": {"short_description": {}, "urgency": {}}}},
    {"name": "post_to_feed", "description": "Post a message to the enterprise social feed.", "input_schema": {"properties": {"text": {}}}},
]


def load_tasks(data_dir: str):
    tasks = []
    for path in sorted(glob.glob(os.path.join(data_dir, "**", "*.json"), recursive=True)):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        domain = os.path.basename(os.path.dirname(path))
        selected = d.get("selected_tools") or []
        tasks.append({
            "path": path,
            "domain": domain,
            "user_prompt": d.get("user_prompt", ""),
            "selected_tools": set(selected),
            "gym": [g.get("mcp_server_name") for g in (d.get("gym_servers_config") or [])],
        })
    return tasks


def build_domain_pool(tasks, domain: str) -> list:
    """近似工具池 = 该域所有任务 selected_tools 的并集 + 干扰工具。"""
    pool_names = set()
    for t in tasks:
        if t["domain"] == domain:
            pool_names |= t["selected_tools"]
    pool = [{"name": n, "description": n.replace("_", " "), "input_schema": {"properties": {}}}
            for n in sorted(pool_names)]
    pool.extend(NOISE_TOOLS)
    return pool


def evaluate(tasks, top_k: int):
    print(f"{'domain':<8} {'tasks':>5} {'recall@k':>9} {'precision@k':>12} {'pool':>5} {'fallback':>9}")
    print("-" * 60)
    agg = defaultdict(lambda: {"tp": 0, "n_sel": 0, "n_pred": 0, "fallbacks": 0, "count": 0})

    for domain in sorted({t["domain"] for t in tasks}):
        dom_tasks = [t for t in tasks if t["domain"] == domain]
        pool = build_domain_pool(tasks, domain)
        router = ToolRouter(pool, k_candidate=30, k_final=top_k)

        tp = n_sel = n_pred = fallbacks = 0
        for t in dom_tasks:
            subset, meta = router.route(t["user_prompt"])
            pred = {s["name"] for s in subset}
            tp += len(pred & t["selected_tools"])
            n_sel += len(t["selected_tools"])
            n_pred += len(pred)
            if meta.get("fallback"):
                fallbacks += 1

        recall = tp / n_sel * 100 if n_sel else 0
        precision = tp / n_pred * 100 if n_pred else 0
        print(f"{domain:<8} {len(dom_tasks):>5} {recall:>8.1f}% {precision:>11.1f}% {len(pool):>5} {fallbacks:>9}")
        a = agg["ALL"]
        a["tp"] += tp; a["n_sel"] += n_sel; a["n_pred"] += n_pred
        a["fallbacks"] += fallbacks; a["count"] += len(dom_tasks)

    a = agg["ALL"]
    print("-" * 60)
    print(f"{'ALL':<8} {a['count']:>5} {a['tp']/a['n_sel']*100:>8.1f}% {a['tp']/a['n_pred']*100:>11.1f}% {'-':>5} {a['fallbacks']:>9}")
    print(f"\n目标:recall@k >= 90%(漏一个工具任务即失败);precision@k 0.5-0.7 即可(宁多勿少)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data_dir", default="data/revised")
    ap.add_argument("--top_k", type=int, default=20)
    args = ap.parse_args()

    tasks = load_tasks(args.data_dir)
    if not tasks:
        print(f"未找到任务: {args.data_dir}")
        return

    # 打印一个路由样例,便于人工核对
    sample = tasks[0]
    pool = build_domain_pool(tasks, sample["domain"])
    router = ToolRouter(pool, k_candidate=30, k_final=args.top_k)
    subset, meta = router.route(sample["user_prompt"])
    print("=== 样例路由核对 ===")
    print(f"任务: {sample['user_prompt'][:120]}...")
    print(f"ground truth({len(sample['selected_tools'])}): {sorted(sample['selected_tools'])}")
    pred = {s['name'] for s in subset}
    miss = sample['selected_tools'] - pred
    print(f"预测({len(pred)}): {sorted(pred)}")
    print(f"漏检(未进 top-{args.top_k}): {sorted(miss) if miss else '无'}")
    print(f"meta: {meta}\n")

    evaluate(tasks, args.top_k)


if __name__ == "__main__":
    main()
