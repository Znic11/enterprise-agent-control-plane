#!/usr/bin/env python3
"""Offline evaluation of the Tool Router — Phase 1 gate check.

Runs the router WITHOUT docker MCP servers or LLM API keys, using the local
task configs in ``data/revised/`` as ground truth:

* Full tool pool  = union of ``selected_tools`` across ALL domains
                    (simulates the ~512-tool cross-domain tool space).
* Ground truth    = each task's own ``selected_tools`` (the oracle subset).
* Routed subset   = ``tool_router.route_keywords(user_prompt, pool, top_k)``.

Metrics per task / domain / overall:
  recall@k        = |routed ∩ oracle| / |oracle|  (did we keep the tools needed?)
  full coverage   = oracle ⊆ routed  (one routing pass suffices — no progressive
                    discovery needed; best-case for the executor)
  exposed tools   = |routed| (token-cost proxy)

Usage:
    python scripts/eval_router_offline.py [--data_dir data/revised] [--top_k 20]

    # With a real full tool list (name+description) once the gym is running:
    python scripts/eval_router_offline.py --tools tools_dump.json

Stdlib only — no project dependencies required.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrators.tool_router import route_keywords  # noqa: E402


def load_tasks(data_dir: str) -> List[Dict[str, Any]]:
    tasks = []
    for pattern in ("*/task_*.json", "task_*.json"):
        for path in sorted(glob.glob(os.path.join(data_dir, pattern))):
            with open(path, encoding="utf-8") as f:
                tasks.append(json.load(f))
    return tasks


def load_full_tools(tools_path: str) -> List[Dict[str, Any]]:
    """Load a real tool list [{name, description, ...}]. Falls back to a
    name-only pool built from all selected_tools when the file is absent."""
    if not tools_path:
        return []
    with open(tools_path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "tools" in data:
        data = data["tools"]
    return [t for t in data if isinstance(t, dict) and t.get("name")]


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline tool router evaluation")
    parser.add_argument("--data_dir", default="data/revised")
    parser.add_argument("--tools", default=None,
                        help="Optional JSON file with the real full tool list "
                             "(list of {name, description}); otherwise a "
                             "name-only cross-domain pool is built from tasks.")
    parser.add_argument("--top_k", type=int, default=20)
    args = parser.parse_args()

    tasks = load_tasks(args.data_dir)
    if not tasks:
        print(f"No task configs found under {args.data_dir}")
        sys.exit(1)

    real_tools = load_full_tools(args.tools)
    if real_tools:
        full_tools = real_tools
        print(f"Using real full tool list: {len(full_tools)} tools")
    else:
        # Simulate the full cross-domain tool space with name-only entries.
        pool_names = sorted({t for task in tasks
                             for t in task.get("selected_tools", [])})
        full_tools = [{"name": n, "description": ""} for n in pool_names]
        print(f"Using simulated cross-domain pool: {len(full_tools)} tools "
              f"(no --tools file; add one for richer routing)")

    per_domain: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"recall": 0.0, "coverage": 0.0, "exposed": 0.0, "n": 0.0}
    )
    total_recall = 0.0
    total_coverage = 0.0
    total_exposed = 0.0

    print(f"\nRouting {len(tasks)} tasks with top_k={args.top_k}\n")
    print(f"{'domain':<10}{'task':<10}{'oracle':<8}{'routed':<8}{'recall@k':<10}{'covered':<8}")
    print("-" * 62)

    for task in tasks:
        oracle = set(task.get("selected_tools", []))
        domain = (task.get("gym_servers_config") or [{}])[0].get(
            "mcp_server_name", "unknown"
        )
        routed = set(route_keywords(task.get("user_prompt", ""), full_tools, args.top_k))

        recall = len(routed & oracle) / len(oracle) if oracle else 0.0
        covered = 1.0 if oracle and oracle <= routed else 0.0
        exposed = len(routed)
        total_recall += recall
        total_coverage += covered
        total_exposed += exposed

        d = per_domain[domain]
        d["recall"] += recall
        d["coverage"] += covered
        d["exposed"] += exposed
        d["n"] += 1

        task_id = os.path.basename(task.get("_file", "task"))
        print(f"{domain:<10}{task_id[:8]:<10}{len(oracle):<8}{exposed:<8}"
              f"{recall:>6.2f}    {('Y' if covered else '-')}")

    n = len(tasks)
    print("-" * 62)
    print(f"\n=== Overall (n={n}) ===")
    print(f"  Mean recall@k      : {total_recall / n:.3f} "
          f"(oracle tools kept by the router)")
    print(f"  Full coverage rate : {total_coverage / n:.3f} "
          f"(one pass suffices, no progressive discovery needed)")
    print(f"  Mean exposed tools : {total_exposed / n:.1f} "
          f"(vs {len(full_tools)} full — cost proxy)")

    print(f"\n=== By domain ===")
    print(f"{'domain':<28}{'n':<5}{'recall@k':<10}{'coverage':<10}{'exposed':<8}")
    for dom, s in sorted(per_domain.items()):
        nn = s["n"]
        print(f"{dom:<28}{int(nn):<5}{s['recall'] / nn:>7.3f}  "
              f"{s['coverage'] / nn:>7.3f}  {s['exposed'] / nn:>6.1f}")


if __name__ == "__main__":
    main()
