"""离线评估脚本(统一版):用任务自带的 selected_tools 标注评估工具路由质量。

用法(项目根目录):
    python eval_router.py [--data_dir data/revised] [--top_k 20]
    python eval_router.py --pool_mode cross          # 模拟 512 工具跨域全池
    python eval_router.py --tools tools_dump.json    # 真实全量工具池(gym dump)
    python eval_router.py --llm_config conf/llm/mini.json
                                                     # 同一轮跑『粗筛』vs『粗筛+LLM精排』
                                                     # 两组消融对照(需 langchain 依赖与 key;
                                                     # 不需要 docker MCP server)

本脚本是 eval_router.py(TF-IDF 版)与 scripts/eval_router_offline.py(关键词版)
合并后的唯一评估入口,调用统一路由器 benchmark.tool_router.route()(即
react_router orchestrator 执行时用的同一入口)。

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
import asyncio
import glob
import json
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List

from benchmark.tool_router import batch_route, route

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


def _eval_one_variant(
    tasks: List[Dict[str, Any]],
    pools: Dict[str, List[Dict[str, Any]]],
    top_k: int,
    llm_call_async=None,
    concurrency: int = 8,
    verbose: bool = True,
) -> Dict[str, Dict[str, float]]:
    """对全部任务跑一遍指定路由配置(batch_route:索引建一次+LLM 并发),
    返回 {domain, ALL} 聚合指标。"""
    # 按域分组(域模式各域池不同;batch_route 的语义与 route() 逐任务一致)
    dom_texts: Dict[str, List[str]] = defaultdict(list)
    for t in tasks:
        dom_texts[t["domain"]].append(t["user_prompt"])

    results_by_prompt: Dict[str, Any] = {}
    for dom, texts in dom_texts.items():
        last = [0]
        def _progress(i: int, n: int, dom=dom):
            print(f"\r  [{dom}] LLM 精排 {i}/{n} ", end="", flush=True)
        pairs = asyncio.run(batch_route(
            texts, pools[dom], top_k=top_k,
            llm_call_fn_async=llm_call_async, concurrency=concurrency,
            progress_fn=_progress if llm_call_async else None,
        ))
        if llm_call_async:
            print()
        results_by_prompt.update(pairs)

    dom_agg = defaultdict(lambda: {"tp": 0, "n_sel": 0, "n_pred": 0, "full": 0, "fb": 0, "n": 0, "exposed": 0})
    all_agg = {"tp": 0, "n_sel": 0, "n_pred": 0, "full": 0, "fb": 0, "n": 0, "exposed": 0}
    macro_sum = 0.0

    for t in tasks:
        result = results_by_prompt[t["user_prompt"]]
        pred = set(result.selected)
        oracle = t.get("reachable_tools", t["selected_tools"])  # 可达 GT(池中真实存在)
        tp = len(pred & oracle)
        recall = tp / len(oracle) if oracle else 0.0
        precision = tp / len(pred) if pred else 0.0
        full = 1 if oracle and oracle <= pred else 0
        macro_sum += recall

        if verbose:
            n_raw = len(t["selected_tools"])
            gt_label = f"{len(oracle)}/{n_raw}" if len(oracle) != n_raw else str(n_raw)
            print(f"{t['domain']:<8} {t['id'][:28]:<28} |{gt_label:>6} |{len(pred):>3} | "
                  f"recall={recall:>6.1%} precision={precision:>6.1%} "
                  f"full={'Y' if full else '-'} fb={result.fallback or '-'} "
                  f"method={result.method}")

        a = dom_agg[t["domain"]]
        for agg in (a, all_agg):
            agg["tp"] += tp
            agg["n_sel"] += len(oracle)
            agg["n_pred"] += len(pred)
            agg["full"] += full
            agg["fb"] += 1 if result.fallback else 0
            agg["n"] += 1
            agg["exposed"] += len(pred)

    all_agg["macro"] = macro_sum / len(tasks) if tasks else 0.0
    return {**{d: dict(a) for d, a in dom_agg.items()}, "ALL": all_agg}


def print_variant(name: str, agg: Dict[str, Dict[str, float]], top_k: int, verbose: bool) -> None:
    print(f"\n=== [{name}] 汇总(micro:所有任务工具级合计)===")
    print(f"{'domain':<8}{'n':>4}{'recall@k':>10}{'precision@k':>13}{'full_cov':>10}{'exposed':>9}{'fallback':>10}")
    print("-" * 66)
    rows = [(d, a) for d, a in agg.items() if d != "ALL"]
    if verbose:
        for dom, a in sorted(rows):
            _print_agg_row(dom, a, top_k)
    _print_agg_row("ALL", agg["ALL"], top_k)
    print(f"macro recall@{top_k}(按任务平均): {agg['ALL']['macro']:.1%}")


def evaluate(
    tasks: List[Dict[str, Any]],
    pools: Dict[str, List[Dict[str, Any]]],
    top_k: int,
    llm_config_path: str = None,
    concurrency: int = 8,
) -> None:
    """跑 粗筛 / 粗筛+LLM精排 两组配置并对照(未提供 llm_config_path 时只跑粗筛)。

    粗筛先跑先出结果;langchain 导入与 client 初始化只在第二轮前发生
    (启动不再被依赖加载阻塞)。LLM 调用并发执行(--concurrency 限流)。
    """
    verbose = len(tasks) <= 50  # 任务多时跳过逐任务明细,只看汇总
    results = {}

    # 变体 1:纯粗筛(纯 CPU,毫秒级/任务)
    print(f"\n{'#' * 66}\n# 变体: TF-IDF 粗筛\n{'#' * 66}")
    t0 = time.perf_counter()
    results["TF-IDF 粗筛"] = _eval_one_variant(tasks, pools, top_k, llm_call_async=None,
                                               verbose=verbose)
    print(f"(粗筛耗时 {time.perf_counter() - t0:.2f}s)")

    if llm_config_path is None:
        print_variant("TF-IDF 粗筛", results["TF-IDF 粗筛"], top_k, verbose)
        _print_targets(top_k)
        return

    # 变体 2:粗筛 + LLM 精排 —— 此时才加载 langchain(粗筛结果已先输出)
    print("\n加载 LLM 精排客户端(langchain 导入 + client 初始化)...")
    t0 = time.perf_counter()
    llm_call_async, latencies = build_llm_call_fn(llm_config_path)
    print(f"(加载耗时 {time.perf_counter() - t0:.2f}s)")

    print(f"\n{'#' * 66}\n# 变体: 粗筛 + LLM 精排(并发={concurrency})\n{'#' * 66}")
    t0 = time.perf_counter()
    results["粗筛 + LLM 精排"] = _eval_one_variant(tasks, pools, top_k,
                                                   llm_call_async=llm_call_async,
                                                   concurrency=concurrency, verbose=verbose)
    elapsed = time.perf_counter() - t0
    print_variant("粗筛 + LLM 精排", results["粗筛 + LLM 精排"], top_k, verbose)

    if latencies:
        lat = sorted(latencies)
        n = len(lat)
        print(f"\nLLM 单次调用延迟({n} 次): "
              f"min={lat[0]:.2f}s p50={lat[n//2]:.2f}s max={lat[-1]:.2f}s "
              f"mean={sum(lat)/n:.2f}s | 串行总计≈{sum(lat):.1f}s,并发后实际 {elapsed:.1f}s")

    # 消融对照
    a1 = results["TF-IDF 粗筛"]["ALL"]
    a2 = results["粗筛 + LLM 精排"]["ALL"]
    print(f"\n=== 消融对照(粗筛 -> 粗筛+LLM精排)===")
    print(f"  recall@{top_k}: {a1['tp']/a1['n_sel']:.1%} -> {a2['tp']/a2['n_sel']:.1%} "
          f"({a2['tp']/a2['n_sel'] - a1['tp']/a1['n_sel']:+.1%})")
    print(f"  precision@{top_k}: {a1['tp']/a1['n_pred']:.1%} -> {a2['tp']/a2['n_pred']:.1%} "
          f"({a2['tp']/a2['n_pred'] - a1['tp']/a1['n_pred']:+.1%})")
    _print_targets(top_k)


def _print_targets(top_k: int) -> None:
    print(f"\n目标:recall@{top_k} >= 90%(漏一个工具任务即败;渐进式扩展会兜底到 1.0)")
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
    unreach = t.get("unreachable", set())
    miss = (t["selected_tools"] - pred) - unreach
    print("=== 样例路由核对 ===")
    print(f"任务: {t['user_prompt'][:140]}...")
    print(f"ground truth({len(t['selected_tools'])}): {sorted(t['selected_tools'])}")
    if unreach:
        print(f"⚠️ 其中 {len(unreach)} 个不在可用池中(跨域引用,路由器不可能选中): {sorted(unreach)}")
    print(f"预测({len(pred)}, method={result.method}): {sorted(pred)}")
    print(f"漏检(可达口径): {sorted(miss) if miss else '无'}")
    print(f"meta: {result.to_metadata()}\n")


def build_llm_call_fn(llm_config_path: str):
    """从 LLM 配置文件(conf/llm/<name>.json,与 evaluate.py 同格式)构建
    异步 llm_call_async(prompt) -> Awaitable[str] 及延迟记录列表。

    懒导入:只在用到 --llm_config 时才加载 langchain(纯粗筛路径零依赖)。
    异步 + 信号量并发是关键:网关延迟被并发摊平,串行 1150 次调用是不可用的。

    ⚠️ 诚实性边界:LLM 精排的输入只有任务文本与候选工具名/描述,
    绝不读取 selected_tools —— 该字段只在这里用作评分 ground truth。
    """
    from benchmark.llm_client import LLMClient, get_text_content
    from benchmark_utils import load_llm_configs
    from langchain_core.messages import HumanMessage

    cfg = load_llm_configs(llm_config_path)[0]
    client = LLMClient(
        provider=cfg.llm_provider,
        model=cfg.llm_model,
        api_key=cfg.llm_api_key,
        api_endpoint=cfg.llm_api_endpoint,
        api_version=cfg.llm_api_version,
        region=cfg.llm_region,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        top_p=cfg.top_p,
        effort=cfg.effort,
        reasoning=cfg.reasoning,
    )

    latencies: List[float] = []

    async def llm_call_async(prompt: str) -> str:
        t0 = time.perf_counter()
        resp = await client.llm.ainvoke([HumanMessage(content=prompt)])
        latencies.append(time.perf_counter() - t0)
        return get_text_content(resp.content)

    return llm_call_async, latencies


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline tool router evaluation (unified)")
    ap.add_argument("--data_dir", default="data/revised")
    ap.add_argument("--top_k", type=int, default=20)
    ap.add_argument("--pool_mode", choices=["domain", "cross"], default="domain",
                    help="domain=每域独立池(默认,贴近生产);cross=跨域合并池(模拟无域锁定的 512 工具空间)")
    ap.add_argument("--tools", default=None,
                    help="真实全量工具池 JSON(list[{name, description, input_schema}]);提供时忽略近似池")
    ap.add_argument("--llm_config", default=None,
                    help="LLM 配置 JSON(conf/llm/<name>.json 同格式);提供时同时评估"
                         "『粗筛』与『粗筛+LLM精排』两组并输出消融对照。需 langchain 依赖。")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="LLM 精排并发数(默认 8)。网关限流严格时调低,内网网关可调高。")
    args = ap.parse_args()

    tasks = load_tasks(args.data_dir)
    if not tasks:
        print(f"未找到任务: {args.data_dir}")
        sys.exit(1)

    if args.tools:
        real = load_real_tools(args.tools)
        if args.pool_mode == "domain" and any("_domain" in t for t in real):
            # dump 带来源域标记:按真实域分池(池大小=真实域内工具数,最贴生产口径)
            pools = defaultdict(list)
            for t in real:
                pools[t.get("_domain")].append(t)
            pools = dict(pools)
            print(f"使用真实工具池(按域分池): " +
                  ", ".join(f"{d}={len(p)}" for d, p in sorted(pools.items())))
        else:
            print(f"使用真实全量工具池: {len(real)} tools")
            pools = {d: real for d in {t["domain"] for t in tasks}}
        missing = {t["domain"] for t in tasks} - set(pools)
        if missing:
            print(f"⚠️ dump 中缺少域 {sorted(missing)},这些域将使用全量池")
            for d in missing:
                pools[d] = real
    elif args.pool_mode == "cross":
        pool = build_cross_pool(tasks)
        print(f"使用近似跨域全池: {len(pool)} tools(pool_mode=cross)")
        pools = {d: pool for d in {t["domain"] for t in tasks}}
    else:
        pools = {d: build_domain_pool(tasks, d) for d in {t["domain"] for t in tasks}}
        print("使用近似域内池(pool_mode=domain): " +
              ", ".join(f"{d}={len(p)}" for d, p in sorted(pools.items())))
    print(f"共 {len(tasks)} 任务, top_k={args.top_k}\n")

    # 可达性核对:GT 工具必须存在于该任务的可用池,否则任何路由器都不可能选中。
    # 本地数据已知问题:gym_servers_config 只挂单 server,但 GT 引用了其他域容器上的工具。
    unreach_total = 0
    for t in tasks:
        pool_names = {x["name"] for x in pools[t["domain"]]}
        t["unreachable"] = t["selected_tools"] - pool_names
        t["reachable_tools"] = t["selected_tools"] & pool_names
        unreach_total += len(t["unreachable"])
    if unreach_total:
        print(f"⚠️ {unreach_total} 个 GT 工具不在对应域池中(跨域引用,GT 在其他域的容器上):"
              f"这些工具任何路由器都选不到,以下 recall/full_cov 按『可达 GT』口径计算\n")

    if args.llm_config:
        print(f"已启用 LLM 精排(router LLM: {args.llm_config}, 并发={args.concurrency}),"
              f"将与纯粗筛做消融对照\n")

    print(f"{'domain':<8} {'task':<28} | GT |pred | per-task metrics")
    print("-" * 96)
    inspect_sample(tasks, pools, args.top_k)
    evaluate(tasks, pools, args.top_k,
             llm_config_path=args.llm_config, concurrency=args.concurrency)


if __name__ == "__main__":
    main()
