"""离线评估脚本(统一版):用任务自带的 selected_tools 标注评估工具路由质量。

用法(项目根目录):
    python eval_router.py [--data_dir data/revised] [--top_k 20]
    python eval_router.py --pool_mode cross          # 模拟 512 工具跨域全池
    python eval_router.py --tools tools_dump.json    # 真实全量工具池(gym dump)
    python eval_router.py --pool_mode gym --tools tools_dump.json
                                                     # 按任务 gym_servers_config 建池:
                                                     # 任务可用池 = 挂载 server 的域池并集
                                                     # (混合域任务天然支持,域信号零泄露)
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
from typing import Any, Dict, List, Optional

from benchmark.tool_router import batch_route, route

# server 名 → dump _domain 的映射(任务 gym_servers_config 里写的是挂载哪些
# MCP server;这是执行器运行时本来就有的配置,不是答案泄露)。本地样例:
#   sn-csm-server → csm | gym-itsm-mcp → itsm
# 远程完整环境的 server 名需按 RUN_GUIDE 核对,必要时补映射。
SERVER_DOMAIN_HINTS: Dict[str, str] = {
    "csm": "csm",
    "itsm": "itsm",
    "email": "email",
    "hr": "hr",
    "teams": "teams",
    "drive": "drive",
    "calendar": "calendar",
}


def resolve_server_domain(server_name: str) -> Optional[str]:
    """把 mcp_server_name 解析成 dump 里的 _domain 标签。

    规则:先查精确映射表;再尝试子串匹配(sn-csm-server / gym-itsm-mcp 里
    找已知域关键字),避免远程 server 命名变体(如 sn-*-server)逐个手写。
    """
    if not server_name:
        return None
    low = server_name.lower()
    for key, dom in SERVER_DOMAIN_HINTS.items():
        if key in low:
            return dom
    return None


def build_gym_pools(
    tasks: List[Dict[str, Any]], by_domain: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, List[Dict[str, Any]]]:
    """按任务 gym_servers_config 建池:pool_key = 挂载 server 的域集合。

    域信号来自任务配置(执行器运行时挂哪些 server),零泄露;混合域任务
    (gym_servers_config 挂多个 server)取这些域的并集 —— 这是『先确定
    领域再筛工具』的正确实现:不是用 TF-IDF/lexicon 猜域,而是直接读
    任务声明。跨域同名工具按 (name,_domain) 全保留(与 dump 一致)。
    """
    pools: Dict[str, List[Dict[str, Any]]] = {}
    for t in tasks:
        doms = []
        for srv in t.get("gym") or []:
            d = resolve_server_domain(srv)
            if d and d not in doms:
                doms.append(d)
        # 解析不到任何域(未知 server 名)→ 退回任务文件夹域,并保持 name 去重
        if not doms and t.get("domain"):
            doms = [t["domain"]]
        key = "|".join(sorted(doms)) or "unknown"
        if key not in pools:
            # 多域并集 + 按 name 去重(先出现者优先,与 executor DUPLICATE 行为一致;
            # ToolRouter.by_name/TFIDFIndex 均按 name 建索引,同名只能保留一份)
            pool: List[Dict[str, Any]] = []
            seen: set = set()
            for d in doms:
                for tool in by_domain.get(d, []):
                    if tool["name"] not in seen:
                        seen.add(tool["name"])
                        pool.append(tool)
            pools[key] = pool
        t["pool_key"] = key
        t["_pool_domains"] = doms
    return pools

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
    rerank_mode: str = "strict",
    candidate_k: Optional[int] = None,
) -> Dict[str, Dict[str, float]]:
    """对全部任务跑一遍指定路由配置(batch_route:索引建一次+LLM 并发),
    返回 {domain, ALL} 聚合指标。

    分组键 = 任务级 pool_key(默认=文件夹域;gym 模式=任务挂载 server 的域
    并集)。同一 pool_key 的任务共享一个池 → batch_route 索引只建一次;
    不同 pool_key(混合域任务)分池各自跑,语义与 route() 逐任务一致。
    """
    # 按任务级 pool_key 分组(各任务自带可用池;batch_route 按池批量)
    pool_texts: Dict[str, List[str]] = defaultdict(list)
    pool_of: Dict[str, Dict[str, Any]] = {}
    for t in tasks:
        key = t.get("pool_key") or t["domain"]
        pool_texts[key].append(t["user_prompt"])
        pool_of[key] = pools.get(key) or pools.get(t["domain"]) or pools[t["domain"]]

    results_by_prompt: Dict[str, Any] = {}
    for key, texts in pool_texts.items():
        last = [0]
        def _progress(i: int, n: int, key=key):
            print(f"\r  [{key}] LLM 精排 {i}/{n} ", end="", flush=True)
        pairs = asyncio.run(batch_route(
            texts, pool_of[key], top_k=top_k,
            llm_call_fn_async=llm_call_async, concurrency=concurrency,
            progress_fn=_progress if llm_call_async else None,
            rerank_mode=rerank_mode, candidate_k=candidate_k,
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
    agg = {**{d: dict(a) for d, a in dom_agg.items()}, "ALL": all_agg}
    return agg, results_by_prompt


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
    rerank_mode: str = "strict",
    candidate_k: Optional[int] = None,
) -> None:
    """跑 粗筛 / 粗筛+LLM精排 两组配置并对照(未提供 llm_config_path 时只跑粗筛)。

    粗筛先跑先出结果;langchain 导入与 client 初始化只在第二轮前发生
    (启动不再被依赖加载阻塞)。LLM 调用并发执行(--concurrency 限流)。

    rerank_mode:
        "strict" —— LLM 输出即最终(默认,噪声最少,但 LLM 砍错会丢必需工具);
        "union"  —— 精排结果 ∪ 粗筛 top-k 保底(method=tfidf+llm+union),
                  精排不可能砍掉粗筛已召回的必需工具 → recall ≥ 纯粗筛,
                  对应『召回优先 + 任务不能因缺工具失败』的生产口径。
    candidate_k: 喂给 LLM 的候选宽度(默认 max(30, top_k))。union 模式下
        必须 > top_k 才有意义:候选=输出时(如 top_k=60 默认候选也是 60)
        LLM 从保底集合里选,union ≡ 纯粗筛,白付延迟(实测 13 任务 0 提升)。
        推荐 top_k=30 + candidate_k=60:粗筛保底 30,LLM 在 60 候选内
        增补 30-60 区间的必需工具 → 噪声从 60 压到 ~30-45 且召回不掉。
    """
    verbose = len(tasks) <= 50  # 任务多时跳过逐任务明细,只看汇总
    results = {}
    by_prompt_all = {}

    # 变体 1:纯粗筛(纯 CPU,毫秒级/任务)
    print(f"\n{'#' * 66}\n# 变体: TF-IDF 粗筛(top_k={top_k})\n{'#' * 66}")
    t0 = time.perf_counter()
    results["TF-IDF 粗筛"], by_prompt_all["TF-IDF 粗筛"] = _eval_one_variant(
        tasks, pools, top_k, llm_call_async=None, verbose=verbose)
    print(f"(粗筛耗时 {time.perf_counter() - t0:.2f}s)")

    if llm_config_path is None:
        if rerank_mode != "strict":
            print(f"(提示: --rerank_mode {rerank_mode} 仅对 LLM 精排生效,本次未提供 --llm_config)")
        print_variant("TF-IDF 粗筛", results["TF-IDF 粗筛"], top_k, verbose)
        _print_targets(top_k)
        return

    # 变体 2:粗筛 + LLM 精排 —— 此时才加载 langchain(粗筛结果已先输出)
    print("\n加载 LLM 精排客户端(langchain 导入 + client 初始化)...")
    t0 = time.perf_counter()
    llm_call_async, latencies = build_llm_call_fn(llm_config_path)
    print(f"(加载耗时 {time.perf_counter() - t0:.2f}s)")

    second_name = "粗筛 + LLM 精排" if rerank_mode == "strict" else f"粗筛 + LLM 精排(union)"
    if candidate_k is not None and candidate_k > top_k and rerank_mode == "union":
        second_name += f" 保底{top_k}+候选{candidate_k}"
    print(f"\n{'#' * 66}\n# 变体: {second_name}(并发={concurrency})\n{'#' * 66}")
    t0 = time.perf_counter()
    results[second_name], by_prompt_all[second_name] = _eval_one_variant(
        tasks, pools, top_k, llm_call_async=llm_call_async,
        concurrency=concurrency, verbose=verbose, rerank_mode=rerank_mode,
        candidate_k=candidate_k)
    elapsed = time.perf_counter() - t0
    print_variant(second_name, results[second_name], top_k, verbose)

    _print_miss_attribution(tasks, by_prompt_all["TF-IDF 粗筛"],
                            by_prompt_all[second_name], verbose)

    if latencies:
        lat = sorted(latencies)
        n = len(lat)
        print(f"\nLLM 单次调用延迟({n} 次): "
              f"min={lat[0]:.2f}s p50={lat[n//2]:.2f}s max={lat[-1]:.2f}s "
              f"mean={sum(lat)/n:.2f}s | 串行总计≈{sum(lat):.1f}s,并发后实际 {elapsed:.1f}s")

    # 消融对照
    a1 = results["TF-IDF 粗筛"]["ALL"]
    a2 = results[second_name]["ALL"]
    print(f"\n=== 消融对照(粗筛 -> {second_name})===")
    print(f"  recall@{top_k}: {a1['tp']/a1['n_sel']:.1%} -> {a2['tp']/a2['n_sel']:.1%} "
          f"({a2['tp']/a2['n_sel'] - a1['tp']/a1['n_sel']:+.1%})")
    print(f"  precision@{top_k}: {a1['tp']/a1['n_pred']:.1%} -> {a2['tp']/a2['n_pred']:.1%} "
          f"({a2['tp']/a2['n_pred'] - a1['tp']/a1['n_pred']:+.1%})")
    _print_targets(top_k)


def _print_miss_attribution(tasks, coarse_by_prompt, llm_by_prompt, verbose: bool) -> None:
    """漏检归因:区分『粗筛没捞进来』和『粗筛捞了但被 LLM 精排砍掉』。

    仅对 method=tfidf+llm / tfidf+llm+union 的任务可精确二分(其 candidate_names
    = 精排输入);回退任务(超时/低选数)漏检全部记粗筛侧,并单独标注。

    union 模式下 selected ⊇ 粗筛 top-k,故『精排砍掉』恒为 0(只要 GT 在粗筛
    候选内就不会丢)—— 归因结果会自然显示漏检全部落在粗筛侧,这正是
    rerank_mode=union 的预期行为(精排不再制造漏检,只负责加回与排序)。
    """
    print(f"\n=== 漏检归因(粗筛漏选 vs LLM 精排砍掉)===")
    tot_rerank, tot_coarse, n_fallback, n_rerank = 0, 0, 0, 0
    for t in tasks:
        oracle = t.get("reachable_tools", t["selected_tools"])
        lres = llm_by_prompt[t["user_prompt"]]
        if lres.method not in ("tfidf+llm", "tfidf+llm+union"):
            n_fallback += 1
            miss = oracle - set(lres.selected)
            tot_coarse += len(miss)
            if verbose and miss:
                print(f"  {t['id'][:28]:<28} [回退,{lres.method}] 粗筛漏 {len(miss)}: {sorted(miss)}")
            continue
        n_rerank += 1
        cand = set(getattr(lres, "candidate_names", []) or [])
        rmiss = (oracle & cand) - set(lres.selected)   # 精排砍掉:进了候选但被 LLM 剔除
        cmiss = oracle - cand                          # 粗筛漏选:连候选都没进
        tot_rerank += len(rmiss)
        tot_coarse += len(cmiss)
        if verbose and (rmiss or cmiss):
            parts = []
            if cmiss:
                parts.append(f"粗筛漏 {len(cmiss)}: {sorted(cmiss)}")
            if rmiss:
                parts.append(f"精排砍 {len(rmiss)}: {sorted(rmiss)}")
            print(f"  {t['id'][:28]:<28} " + " | ".join(parts))
    print(f"\n  合计: 粗筛漏选 {tot_coarse} 个 | 精排砍掉 {tot_rerank} 个"
          f" | 回退任务 {n_fallback} 个(超时/低选数,未参与精排) | 精排生效 {n_rerank} 个")
    if tot_rerank and tot_rerank > tot_coarse:
        print("  → 漏检主要来自精排过度截断:可调高 ROUTER_PROMPT 工具数下限,"
              "或改用 --rerank_mode union(精排与粗筛 top-k 取并集,召回优先保底)。")
    elif tot_coarse and tot_coarse > tot_rerank:
        print("  → 漏检主要来自粗筛:候选阶段就没捞到,调 top_k/LOOKUP_FLOOR 或查询扩展。")
    elif tot_rerank and tot_coarse:
        print("  → 粗筛与精排各贡献约一半漏检:两侧都需要调(粗筛扩容 + 精排防过度截断)。")


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
    pool = pools[t.get("pool_key") or t["domain"]]
    result = route(t["user_prompt"], pool, top_k=top_k)
    pred = set(result.selected)
    unreach = t.get("unreachable", set())
    miss = (t["selected_tools"] - pred) - unreach
    doms = t.get("_pool_domains") or [t["domain"]]
    print("=== 样例路由核对 ===")
    print(f"任务: {t['user_prompt'][:140]}...")
    print(f"可用池域: {doms}(共 {len(pool)} 工具)")
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
    ap.add_argument("--pool_mode", choices=["domain", "cross", "gym"], default="domain",
                    help="domain=每域独立池(默认,贴近生产);cross=跨域合并池(模拟无域锁定的"
                         "512 工具空间);gym=按任务 gym_servers_config 建池(任务挂哪个 server"
                         "就用哪几个域的池并集,混合域天然支持;域信号来自任务配置,零泄露)")
    ap.add_argument("--tools", default=None,
                    help="真实全量工具池 JSON(list[{name, description, input_schema}]);提供时忽略近似池")
    ap.add_argument("--llm_config", default=None,
                    help="LLM 配置 JSON(conf/llm/<name>.json 同格式);提供时同时评估"
                         "『粗筛』与『粗筛+LLM精排』两组并输出消融对照。需 langchain 依赖。")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="LLM 精排并发数(默认 8)。网关限流严格时调低,内网网关可调高。")
    ap.add_argument("--rerank_mode", choices=["strict", "union"], default="strict",
                    help="LLM 精排结果合成模式:strict=LLM 输出即最终(默认,噪声最少,"
                         "但 LLM 砍错会丢必需工具);union=精排选中 ∪ 粗筛 top-k 保底"
                         "(method=tfidf+llm+union),精排不可能砍掉粗筛已召回的必需工具,"
                         "recall ≥ 纯粗筛 ——『召回优先、任务不因缺工具失败』口径")
    ap.add_argument("--candidate_k", type=int, default=None,
                    help="喂给 LLM 的粗筛候选宽度(默认 max(30, top_k),即候选=输出)。"
                         "union 模式下必须 > top_k 才有意义:如 --top_k 30 --candidate_k 60,"
                         "粗筛保底 30、LLM 从 60 候选内增补 30-60 区间的必需工具,"
                         "噪声从 60 压到 ~30-45 且召回不掉;候选=输出时 union ≡ 纯粗筛(白付延迟)")
    args = ap.parse_args()

    tasks = load_tasks(args.data_dir)
    if not tasks:
        print(f"未找到任务: {args.data_dir}")
        sys.exit(1)

    by_domain: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    real = None
    if args.tools:
        real = load_real_tools(args.tools)
        if any("_domain" in t for t in real):
            for t in real:
                by_domain[t.get("_domain")].append(t)
            by_domain = dict(by_domain)
            print(f"真实工具池按域分池: " +
                  ", ".join(f"{d}={len(p)}" for d, p in sorted(by_domain.items())))

    if args.pool_mode == "gym":
        # 生产口径:执行器只挂任务 gym_servers_config 声明的 server → 可用池 =
        # 这些 server 的域池并集。这是『先定域再筛』的正确实现:域不靠 TF-IDF/
        # lexicon 猜(猜错硬锁比不锁更差,实测 top-1 词法命中率仅 77%),直接读
        # 任务配置 —— 运行时零泄露、混合域天然覆盖。
        if not real:
            print("⚠️ pool_mode=gym 需要真实工具池(--tools tools_dump.json),"
                  "否则无从按域取工具;回退近似域内池")
            pools = {d: build_domain_pool(tasks, d) for d in {t["domain"] for t in tasks}}
        elif not by_domain:
            print("⚠️ dump 无 _domain 标记,gym 模式退化为跨域全池(name 去重)")
            seen, dedup = set(), []
            for t in real:
                if t["name"] not in seen:
                    seen.add(t["name"])
                    dedup.append(t)
            pools = {d: dedup for d in {t["domain"] for t in tasks}}
            pools["ALL"] = dedup
            for t in tasks:
                t["pool_key"] = "ALL"
                t["_pool_domains"] = list(t["gym"]) or [t["domain"]]
        else:
            pools = build_gym_pools(tasks, by_domain)
        print("使用 gym 配置域池(pool_mode=gym)")
        pool_keys = sorted({t.get("pool_key", t["domain"]) for t in tasks})
        for k in pool_keys:
            if k in pools:
                print(f"  [{k}] {len(pools[k])} tools")
    elif args.tools and args.pool_mode == "domain" and by_domain:
        # dump 带来源域标记:任务按文件夹域取对应真实域池(最贴生产口径)
        pools = dict(by_domain)
        missing = {t["domain"] for t in tasks} - set(pools)
        if missing:
            print(f"⚠️ dump 中缺少域 {sorted(missing)},这些域将使用近似池")
            for d in missing:
                pools[d] = build_domain_pool(tasks, d)
        for t in tasks:
            t["pool_key"] = t["domain"]
            t["_pool_domains"] = [t["domain"]]
        print(f"使用真实工具池(任务按文件夹域取池,共 {len(tasks)} 任务)")
    elif args.tools:
        # 跨域重名工具:全量池按 name 去重(先出现者优先,与 executor 运行时一致),
        # 避免 ToolRouter.by_name 索引相互覆盖
        seen, dedup = set(), []
        for t in real:
            if t["name"] not in seen:
                seen.add(t["name"])
                dedup.append(t)
        print(f"使用真实全量工具池: {len(real)} 条 -> 按 name 去重后 {len(dedup)} 个"
              f"(跨域重名 {len(real) - len(dedup)} 个,官方各域容器本就存在同名工具)")
        pools = {d: dedup for d in {t["domain"] for t in tasks}}
        pools["ALL"] = dedup  # pool_key="ALL" 的任务走这里
        for t in tasks:
            t["pool_key"] = "ALL"
            t["_pool_domains"] = [t["domain"]]
    elif args.pool_mode == "cross":
        pool = build_cross_pool(tasks)
        print(f"使用近似跨域全池: {len(pool)} tools(pool_mode=cross)")
        pools = {d: pool for d in {t["domain"] for t in tasks}}
        pools["ALL"] = pool
        for t in tasks:
            t["pool_key"] = "ALL"
            t["_pool_domains"] = [t["domain"]]
    else:
        pools = {d: build_domain_pool(tasks, d) for d in {t["domain"] for t in tasks}}
        print("使用近似域内池(pool_mode=domain): " +
              ", ".join(f"{d}={len(p)}" for d, p in sorted(pools.items())))
        for t in tasks:
            t["pool_key"] = t["domain"]
            t["_pool_domains"] = [t["domain"]]
    print(f"共 {len(tasks)} 任务, top_k={args.top_k}\n")

    # 可达性核对:GT 工具必须存在于该任务的可用池(pool_key 对应池),
    # 否则任何路由器都不可能选中。gym 模式下跨域 GT 引用会因多域并集而可达,
    # 这正是生产 executor 的行为(挂多个 server → 工具全部可用)。
    unreach_total = 0
    for t in tasks:
        key = t.get("pool_key") or t["domain"]
        pool = pools.get(key) or pools.get(t["domain"]) or next(iter(pools.values()))
        pool_names = {x["name"] for x in pool}
        t["unreachable"] = t["selected_tools"] - pool_names
        t["reachable_tools"] = t["selected_tools"] & pool_names
        unreach_total += len(t["unreachable"])
    if unreach_total:
        print(f"⚠️ {unreach_total} 个 GT 工具不在对应可用池中(跨域引用,GT 在其他域的容器上):"
              f"这些工具任何路由器都选不到,以下 recall/full_cov 按『可达 GT』口径计算\n")

    if args.llm_config:
        hint = f"rerank_mode={args.rerank_mode}"
        if args.candidate_k is not None:
            hint += f", candidate_k={args.candidate_k}(LLM 候选>输出才有增补空间)"
        print(f"已启用 LLM 精排(router LLM: {args.llm_config}, 并发={args.concurrency}, "
              f"{hint}),将与纯粗筛做消融对照\n")

    print(f"{'domain':<8} {'task':<28} | GT |pred | per-task metrics")
    print("-" * 96)
    inspect_sample(tasks, pools, args.top_k)
    evaluate(tasks, pools, args.top_k,
             llm_config_path=args.llm_config, concurrency=args.concurrency,
             rerank_mode=args.rerank_mode, candidate_k=args.candidate_k)


if __name__ == "__main__":
    main()
