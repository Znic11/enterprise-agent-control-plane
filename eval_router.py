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
meta_tool orchestrator 检索用的同一入口;--retrieval 可切 tfidf/dense/hybrid)。

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

from benchmark.dense_retriever import DEFAULT_EMBEDDING_MODEL, get_embedder
from benchmark.tool_router import (
    DEFAULT_HYBRID_ALPHA,
    RETRIEVAL_MODES,
    ToolRouter,
    batch_route,
    route,
)

_RETRIEVAL_LABEL = {"tfidf": "TF-IDF", "dense": "Dense", "hybrid": "Hybrid"}


def resolve_retrieval(
    retrieval: str,
    embedding_model: Optional[str] = None,
    embedding_device: Optional[str] = None,
) -> Any:
    """把 CLI --retrieval(含 "auto")解析为 (retrieval, embedder_or_None)。

    auto:服务端装了 sentence-transformers(可选依赖)就 hybrid,否则 tfidf ——
    离线诊断工具在缺依赖环境下自动降级并打印说明;显式指定 dense/hybrid 时
    缺依赖会抛出带安装提示的 ImportError(绝不静默降级)。
    """
    import importlib.util

    if retrieval not in (*RETRIEVAL_MODES, "auto"):
        raise SystemExit(f"--retrieval 必须是 {RETRIEVAL_MODES} 或 auto,got {retrieval!r}")
    if retrieval == "auto":
        has_st = importlib.util.find_spec("sentence_transformers") is not None
        retrieval = "hybrid" if has_st else "tfidf"
        print(f"(auto: 检索后端 -> {retrieval}"
              + ("" if has_st else "; 未检测到 sentence-transformers,离线沿用稀疏 TF-IDF"
                                  ",装 dense 可选依赖后自动切 hybrid)")
              + ")")
    if retrieval == "tfidf":
        return retrieval, None
    try:
        embedder = get_embedder(embedding_model or DEFAULT_EMBEDDING_MODEL, embedding_device)
    except ImportError:
        print(
            f"⚠️ 检索后端 {retrieval} 需要可选依赖: pip install '.[dense]' "
            f"(sentence-transformers + torch);或先 --retrieval tfidf"
        )
        raise
    return retrieval, embedder

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
    retrieval: str = "tfidf",
    embedder: Any = None,
    hybrid_alpha: float = DEFAULT_HYBRID_ALPHA,
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
            retrieval=retrieval, embedder=embedder, hybrid_alpha=hybrid_alpha,
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
    retrieval: str = "tfidf",
    embedder: Any = None,
    hybrid_alpha: float = DEFAULT_HYBRID_ALPHA,
) -> None:
    """跑 粗筛 / 粗筛+LLM精排 两组配置并对照(未提供 llm_config_path 时只跑粗筛)。

    ``retrieval``/``embedder``/``hybrid_alpha`` 见 resolve_retrieval:检索后端
    可插拔,默认 tfidf(离线一致性);dense/hybrid 需 sentence-transformers。

    粗筛先跑先出结果;langchain 导入与 client 初始化只在第二轮前发生
    (启动不再被依赖加载阻塞)。LLM 调用并发执行(--concurrency 限流)。

    rerank_mode:
        "strict" —— LLM 输出即最终(默认,噪声最少,但 LLM 砍错会丢必需工具);
        "union"  —— 精排结果 ∪ 粗筛 top-k 保底(method=<retrieval>+llm+union),
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
    label = _RETRIEVAL_LABEL.get(retrieval, retrieval)
    first_label = f"{label} 粗筛"

    # 变体 1:纯粗筛(纯 CPU,毫秒级/任务)
    print(f"\n{'#' * 66}\n# 变体: {first_label}(top_k={top_k})\n{'#' * 66}")
    t0 = time.perf_counter()
    results[first_label], by_prompt_all[first_label] = _eval_one_variant(
        tasks, pools, top_k, llm_call_async=None, verbose=verbose,
        retrieval=retrieval, embedder=embedder, hybrid_alpha=hybrid_alpha)
    print(f"(粗筛耗时 {time.perf_counter() - t0:.2f}s)")

    if llm_config_path is None:
        if rerank_mode != "strict":
            print(f"(提示: --rerank_mode {rerank_mode} 仅对 LLM 精排生效,本次未提供 --llm_config)")
        print_variant(first_label, results[first_label], top_k, verbose)
        _print_targets(top_k)
        return

    # 变体 2:粗筛 + LLM 精排 —— 此时才加载 langchain(粗筛结果已先输出)
    print("\n加载 LLM 精排客户端(langchain 导入 + client 初始化)...")
    t0 = time.perf_counter()
    llm_call_async, latencies = build_llm_call_fn(llm_config_path)
    print(f"(加载耗时 {time.perf_counter() - t0:.2f}s)")

    second_name = f"{label} 粗筛 + LLM 精排" if rerank_mode == "strict" else f"{label} 粗筛 + LLM 精排(union)"
    if candidate_k is not None and candidate_k > top_k and rerank_mode == "union":
        second_name += f" 保底{top_k}+候选{candidate_k}"
    print(f"\n{'#' * 66}\n# 变体: {second_name}(并发={concurrency})\n{'#' * 66}")
    t0 = time.perf_counter()
    results[second_name], by_prompt_all[second_name] = _eval_one_variant(
        tasks, pools, top_k, llm_call_async=llm_call_async,
        concurrency=concurrency, verbose=verbose, rerank_mode=rerank_mode,
        candidate_k=candidate_k,
        retrieval=retrieval, embedder=embedder, hybrid_alpha=hybrid_alpha)
    elapsed = time.perf_counter() - t0
    print_variant(second_name, results[second_name], top_k, verbose)

    _print_miss_attribution(tasks, by_prompt_all[first_label],
                            by_prompt_all[second_name], verbose)

    if latencies:
        lat = sorted(latencies)
        n = len(lat)
        print(f"\nLLM 单次调用延迟({n} 次): "
              f"min={lat[0]:.2f}s p50={lat[n//2]:.2f}s max={lat[-1]:.2f}s "
              f"mean={sum(lat)/n:.2f}s | 串行总计≈{sum(lat):.1f}s,并发后实际 {elapsed:.1f}s")

    # 消融对照
    a1 = results[first_label]["ALL"]
    a2 = results[second_name]["ALL"]
    print(f"\n=== 消融对照({first_label} -> {second_name})===")
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


# ---------------------------------------------------------------------------
# Meta-Tool 离线指标(诊断检索器在"分批能力片段" query 下的覆盖;诚实口径见下)
# ---------------------------------------------------------------------------

# MetaToolOrchestrator.get_result_metadata() 输出的 run 级指标键(日志聚合用)
META_TOOL_METRIC_KEYS = (
    "meta_tool_search_calls",
    "meta_tool_searches",
    "meta_tool_cache_hits",
    "meta_tool_zero_hits",
    "meta_tool_fallback_all",
    "meta_tool_hits_avg",
)


def _segment_queries(user_prompt: str, max_queries: int) -> List[str]:
    """启发式:把任务文本切成候选检索 query(句/段),模拟执行期模型分轮
    表达能力需求时 query 的词法形态。

    ⚠️ 这不是 LLM 自生成 query —— 只用于评估检索器对"功能片段"这类 query
    的覆盖,是端到端指标的下界/诊断,不能替代真实端到端实验。
    """
    import re

    parts = [
        p.strip()
        for p in re.split(r"[.;!?\n]+", user_prompt or "")
        if p and len(p.strip()) >= 12
    ]
    if not parts:
        # 兜底:整段前 200 字符作为单条 query
        return [user_prompt[:200]] if user_prompt else []
    return [p[:200] for p in parts[:max_queries]]


def simulate_meta_tool(
    tasks: List[Dict[str, Any]],
    pools: Dict[str, List[Dict[str, Any]]],
    search_top_k: int = 6,
    min_score: float = 0.03,
    max_searches: int = 4,
    verbose: bool = True,
    retrieval: str = "tfidf",
    embedder: Any = None,
    hybrid_alpha: float = DEFAULT_HYBRID_ALPHA,
):
    """Meta-Tool 离线仿真:模拟执行期逐轮 ``_tool_search(query)`` 的检索形态。

    query 来自任务文本自动切段(启发式,见 _segment_queries);每轮检索的 top-k
    命中并入"可用集"(对应 orchestrator 的 bind 注入),统计:

      final_recall     可用集对 reachable GT 的最终覆盖率(多轮累计)
      first_recall     第一轮检索即覆盖的比例(单轮能捞到多少)
      hits_avg         平均每轮检索命中 GT 工具数(对应 meta_tool_hits_avg)
      zero_rate        零命中轮次占比
      final_precision  可用集里真正是 GT 的比例

    返回 (聚合 {domain:…, ALL:…}, 每任务明细)。诚实口径:仿真 query 非 LLM
    生成,数字只用于诊断检索器与参数(端到端增益需服务端 evaluate.py
    --orchestrator meta_tool)。``retrieval``/``embedder`` 见 resolve_retrieval。
    """
    routers: Dict[str, ToolRouter] = {}
    pool_names: Dict[str, set] = {}
    per_task: List[Dict[str, Any]] = []

    def _router(key: str) -> Tuple[ToolRouter, set]:
        if key not in routers:
            pool = pools.get(key) or next(iter(pools.values()), [])
            routers[key] = ToolRouter(
                pool,
                retrieval=retrieval, embedder=embedder, hybrid_alpha=hybrid_alpha,
            )
            pool_names[key] = {x["name"] for x in pool}
        return routers[key], pool_names[key]

    for t in tasks:
        key = t.get("pool_key") or t["domain"]
        router, names = _router(key)
        oracle = t.get("reachable_tools", t["selected_tools"])
        queries = _segment_queries(t["user_prompt"], max_searches)

        avail: set = set()
        gt_hits_per_search: List[int] = []
        zero = 0
        for q in queries:
            raw = router.search(
                q, top_k=search_top_k, min_score=min_score, boost_lookup=False
            )
            found = {n for _s, n in raw if n in names}
            gt_hits_per_search.append(len(found & oracle))
            if not found:
                zero += 1
            avail |= found
        searches_done = len(gt_hits_per_search) or 1
        tp = len(avail & oracle)
        per_task.append(
            {
                "id": t["id"],
                "domain": t["domain"],
                "oracle_size": len(oracle),
                "avail_size": len(avail),
                "recall": tp / len(oracle) if oracle else 0.0,
                "first_tp": gt_hits_per_search[0] if gt_hits_per_search else 0,
                "first_recall": (
                    (gt_hits_per_search[0] / len(oracle)) if oracle and gt_hits_per_search else 0.0
                ),
                "hits_avg": sum(gt_hits_per_search) / searches_done,
                "zero": zero,
                "searches": len(gt_hits_per_search),
                "tp": tp,
                "precision": tp / len(avail) if avail else 0.0,
                "full": 1 if oracle and avail and oracle <= avail else 0,
            }
        )

    agg: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "tp": 0, "gt": 0, "fp": 0, "avail": 0,
                 "first_tp": 0, "hits": 0, "zero": 0, "full": 0, "searches": 0}
    )
    all_agg = agg["ALL"]
    for p in per_task:
        for a in (agg[p["domain"]], all_agg):
            a["n"] += 1
            a["tp"] += p["tp"]
            a["gt"] += p["oracle_size"]
            a["first_tp"] += p["first_tp"]
            a["avail"] += p["avail_size"]
            a["hits"] += p["hits_avg"] * p["searches"]
            a["zero"] += p["zero"]
            a["full"] += p["full"]
            a["searches"] += p["searches"]

    if verbose:
        print(f"\n=== Meta-Tool 离线仿真(top_k={search_top_k}, min_score={min_score}, "
              f"≤{max_searches} 轮检索)===")
        print("> ⚠️ query 由任务文本自动切段模拟(非 LLM 生成):数字 = 检索器对"
              "『分批能力片段』query 的覆盖下界/诊断,")
        print(">   端到端增益需服务端跑 evaluate.py --orchestrator meta_tool。")
        print(f"{'domain':<8}  {'n':>3}  {'final_recall':>12}  {'first_recall':>12}  "
              f"{'hits_avg':>8}  {'zero%':>7}  {'full_cov':>8}  {'precision':>9}")
        print("-" * 78)
        for dom in sorted(a for a in agg if a != "ALL"):
            _print_meta_sim_row(dom, agg[dom])
        _print_meta_sim_row("ALL", all_agg)
    return dict(agg), per_task


def _print_meta_sim_row(label: str, a: Dict[str, Any]) -> None:
    recall = a["tp"] / a["gt"] * 100 if a["gt"] else 0
    first = a["first_tp"] / a["gt"] * 100 if a["gt"] else 0
    hits_avg = a["hits"] / a["searches"] if a["searches"] else 0
    zero = a["zero"] / a["searches"] * 100 if a["searches"] else 0
    full = a["full"] / a["n"] * 100 if a["n"] else 0
    prec = a["tp"] / a["avail"] * 100 if a["avail"] else 0
    print(f"{label:<8}  {int(a['n']):>3}  {recall:>11.1f}%  {first:>11.1f}%  "
          f"{hits_avg:>8.2f}  {zero:>6.1f}%  {full:>7.1f}%  {prec:>8.1f}%")


def analyze_meta_tool_runs(
    results_dir: str, tasks: Optional[List[Dict[str, Any]]] = None
) -> None:
    """聚合 evaluate.py --orchestrator meta_tool 结果 JSON 里的 run 级 meta_tool_*
    元数据,输出执行期真实指标(meta_tool_searches / cache_hits / hits_avg ...)。

    可选传入 tasks(load_tasks 结果)时,额外按 benchmark_config.user_prompt
    匹配任务 GT,计算 final_recall = |注入集 ∩ reachable GT| / |GT|(注入集 =
    meta_tool_injected;兜底全池的 run 单独标注,该口径会虚高)。⚠️ selected_tools
    仅在此离线聚合中作为 GT,运行期 orchestrator 从未读取它。
    """
    files = sorted(glob.glob(os.path.join(results_dir, "results_*.json")))
    if not files:
        print(f"⚠️ 未在 {results_dir} 找到 results_*.json")
        return
    by_prompt = {t["user_prompt"]: t for t in (tasks or [])}
    rows = []
    for fp in files:
        with open(fp, encoding="utf-8") as f:
            data = json.load(f)
        for run in data.get("runs", []):
            if not any(k in run for k in META_TOOL_METRIC_KEYS):
                continue
            row: Dict[str, Any] = {
                "file": os.path.basename(fp),
                "run_number": run.get("run_number"),
                "success": run.get("overall_success"),
            }
            for k in META_TOOL_METRIC_KEYS:
                row[k] = run.get(k, 0)
            row["meta_tool_injected"] = run.get("meta_tool_injected", [])
            # 可选 final_recall:注入集(可用集)对可达 GT 的覆盖
            if by_prompt:
                t = by_prompt.get(data.get("benchmark_config", {}).get("user_prompt"))
                if t is not None:
                    oracle = t.get("reachable_tools", t["selected_tools"])
                    injected = set(run.get("meta_tool_injected", []))
                    row["final_recall"] = (
                        len(injected & oracle) / len(oracle) if oracle else 0.0
                    )
                    row["oracle_size"] = len(oracle)
            rows.append(row)
    if not rows:
        print("⚠️ 日志中未发现 meta_tool_* 元数据(确认运行用了 --orchestrator meta_tool)")
        return

    n = len(rows)
    s = lambda k: sum(r.get(k, 0) for r in rows)  # noqa: E731
    ok = sum(1 for r in rows if r.get("success"))
    print(f"\n=== Meta-Tool 运行日志聚合({n} runs, 成功 {ok})===")
    print(f"  meta_tool_search_calls(模型发起的 _tool_search): {s('meta_tool_search_calls')}")
    print(f"  meta_tool_searches(实际检索,缓存未命中):        {s('meta_tool_searches')}")
    print(f"  meta_tool_cache_hits(query->tools 缓存命中):    {s('meta_tool_cache_hits')}")
    print(f"  meta_tool_zero_hits(零命中轮):                  {s('meta_tool_zero_hits')}")
    hit_vals = [r["meta_tool_hits_avg"] for r in rows if r.get("meta_tool_hits_avg")]
    if hit_vals:
        print(f"  meta_tool_hits_avg 均值(每检索命中工具数):     {sum(hit_vals) / len(hit_vals):.2f}")
    fb = [r for r in rows if r.get("meta_tool_fallback_all")]
    print(f"  触发全池兜底(fallback_all)的 run:               {len(fb)}")
    fr = [r for r in rows if "final_recall" in r]
    if fr:
        print(f"  final_recall(注入集覆盖可达 GT,均值):           "
              f"{sum(r['final_recall'] for r in fr) / len(fr):.1%} "
              f"({len(fr)} runs;兜底 run 会虚高,见上方标注)")
    print(f"\n{'file':<34}{'run':>4}{'ok':>4}{'calls':>7}{'searches':>9}"
          f"{'cache':>7}{'zero':>6}{'fallback':>9}")
    for r in rows[:50]:
        print(f"{r['file'][:34]:<34}{int(r['run_number'] or 0):>4}"
              f"{'Y' if r.get('success') else '-':>4}"
              f"{int(r.get('meta_tool_search_calls', 0)):>7}"
              f"{int(r.get('meta_tool_searches', 0)):>9}"
              f"{int(r.get('meta_tool_cache_hits', 0)):>7}"
              f"{int(r.get('meta_tool_zero_hits', 0)):>6}"
              f"{'Y' if r.get('meta_tool_fallback_all') else '-':>9}")


def inspect_sample(
    tasks: List[Dict[str, Any]],
    pools: Dict[str, List[Dict[str, Any]]],
    top_k: int,
    retrieval: str = "tfidf",
    embedder: Any = None,
    hybrid_alpha: float = DEFAULT_HYBRID_ALPHA,
) -> None:
    """打印第一个任务的路由明细,便于人工核对漏检。"""
    t = tasks[0]
    pool = pools[t.get("pool_key") or t["domain"]]
    result = route(
        t["user_prompt"], pool, top_k=top_k,
        retrieval=retrieval, embedder=embedder, hybrid_alpha=hybrid_alpha,
    )
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
    # ---- 检索后端(稠密向量检索替换 TF-IDF;参考 Spring AI Alibaba)----
    ap.add_argument("--retrieval", default="auto", choices=["auto", "tfidf", "dense", "hybrid"],
                    help="检索后端:auto=装了 sentence-transformers 则 hybrid,否则 tfidf"
                         "(默认);tfidf=稀疏(零依赖,旧口径);dense=纯稠密向量;"
                         "hybrid=稠密+稀疏加权融合(推荐,dense/hybrid 需 pip install '.[dense]')")
    ap.add_argument("--embedding_model", default=None,
                    help=f"稠密向量模型(默认 {DEFAULT_EMBEDDING_MODEL},英文轻量;中文任务可换多语模型)")
    ap.add_argument("--embedding_device", default=None, help="cuda / cpu(默认 auto)")
    ap.add_argument("--hybrid_alpha", type=float, default=DEFAULT_HYBRID_ALPHA,
                    help="hybrid 融合中稠密权重(默认 0.5;稀疏权重=1-alpha)。"
                         "调高=更偏语义,调低=更偏词面精确匹配")
    # ---- Meta-Tool 离线指标(仿真 + 运行日志聚合)----
    ap.add_argument("--meta_sim", action="store_true",
                    help="跑 Meta-Tool 离线仿真:任务文本切段模拟逐轮 _tool_search,"
                         "统计 final_recall/first_recall/hits_avg/zero_pct(诊断检索器覆盖;"
                         "query 非 LLM 生成,非端到端口径)")
    ap.add_argument("--meta_sim_top_k", type=int, default=6,
                    help="仿真中每次检索返回工具数(默认 6,与 orchestrator 默认一致)")
    ap.add_argument("--meta_sim_min_score", type=float, default=0.03,
                    help="仿真中检索分数下限(低于记零命中;默认 0.03)")
    ap.add_argument("--meta_sim_max_searches", type=int, default=4,
                    help="仿真中最多模拟几轮检索(默认 4)")
    ap.add_argument("--analyze_meta_runs", default=None,
                    help="聚合 evaluate.py --orchestrator meta_tool 的结果目录 "
                         "(results_*.json 的 run 级 meta_tool_* 元数据);可选传入 "
                         "--data_dir 任务以计算 final_recall(GT 仅离线用)")
    args = ap.parse_args()

    retrieval, embedder = resolve_retrieval(
        args.retrieval, args.embedding_model, args.embedding_device
    )
    args.retrieval = retrieval  # 供后续调用透传(auto 已解析为具体后端)

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
    inspect_sample(
        tasks, pools, args.top_k,
        retrieval=retrieval, embedder=embedder, hybrid_alpha=args.hybrid_alpha,
    )
    evaluate(tasks, pools, args.top_k,
             llm_config_path=args.llm_config, concurrency=args.concurrency,
             rerank_mode=args.rerank_mode, candidate_k=args.candidate_k,
             retrieval=retrieval, embedder=embedder, hybrid_alpha=args.hybrid_alpha)

    # Meta-Tool 离线指标:仿真(本地可跑)+ 运行日志聚合(服务端 evaluate 产物)
    if args.meta_sim:
        simulate_meta_tool(
            tasks, pools,
            search_top_k=args.meta_sim_top_k,
            min_score=args.meta_sim_min_score,
            max_searches=args.meta_sim_max_searches,
            retrieval=retrieval, embedder=embedder, hybrid_alpha=args.hybrid_alpha,
        )
    if args.analyze_meta_runs:
        analyze_meta_tool_runs(args.analyze_meta_runs, tasks=tasks)


if __name__ == "__main__":
    main()
