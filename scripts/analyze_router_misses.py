"""路由失败归因分析:从具体案例反推精度改善措施。

对每个任务做全池打分,把 ground truth 工具的漏检和子集内的误报逐个归因:

漏检(recall 损失)分类:
  zero_overlap        GT 工具与任务文本零词面重叠(得 0 分)—— 纯词面匹配天花板
  below_cutoff        进了候选(≤k_candidate)但排在 top_k 之后 —— 排序质量问题
  outside_candidates  连候选(≤k_candidate)都没进 —— 分数被同池工具碾压

误报(precision 损失)分类(占了 top_k 空位的非 GT 工具):
  noise_tool          评估池注入的跨域干扰工具(纯评估 artifact)
  family_overlap      与某个 GT 工具共享词根的同族工具(近义动作,如 add_new_entitlement
                      vs update_entitlement)—— 语义区分问题,LLM 精排的目标场景
  weak_match          其他弱匹配(通用词碰上)

用法:
    python scripts/analyze_router_misses.py [--data_dir data/revised] [--top_k 20]
                                            [--pool_mode domain|cross] [--tools dump.json]

输出:逐任务明细 + 分类聚合 + 按"改善措施 × 预期收益"排序的行动建议。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.tool_router import LOOKUP_FLOOR, LOOKUP_PREFIXES, ToolRouter  # noqa: E402
from eval_router import build_cross_pool, build_domain_pool, load_real_tools, load_tasks  # noqa: E402


def analyze_task(task, router: ToolRouter, pool, top_k: int, k_candidate: int, noise_names: set):
    """返回 (miss 分类计数, 误报分类计数, 漏检明细, 误报明细)。"""
    query = task["user_prompt"]
    raw = router.index.search(query, top_k=len(pool))  # 全池打分
    boosted = [(s + LOOKUP_FLOOR if n.startswith(LOOKUP_PREFIXES) else s, n) for s, n in raw]
    boosted.sort(reverse=True)
    rank_of = {n: i + 1 for i, (s, n) in enumerate(boosted)}
    score_of = {n: s for s, n in boosted}  # 修复:dict((score,name)) 会把键值倒置
    pred = {n for _, n in boosted[:top_k]}
    oracle = task["selected_tools"]

    miss_counter, fp_counter = Counter(), Counter()
    miss_detail, fp_detail = [], []

    # 漏检归因
    for name in sorted(oracle - pred):
        if score_of.get(name, 0.0) <= 0.0:
            cat = "zero_overlap"
        elif rank_of.get(name, 10**9) <= k_candidate:
            cat = "below_cutoff"
        else:
            cat = "outside_candidates"
        miss_counter[cat] += 1
        miss_detail.append((name, cat, score_of.get(name, 0.0), rank_of.get(name, -1)))

    # 误报归因
    for name in sorted(pred - oracle):
        if name in noise_names:
            cat = "noise_tool"
        else:
            # 与 GT 工具共享词根 → 同族近义
            toks = set(name.split("_"))
            gt_toks = set().union(*[set(g.split("_")) for g in oracle])
            cat = "family_overlap" if toks & gt_toks else "weak_match"
        fp_counter[cat] += 1
        fp_detail.append((name, cat, score_of.get(name, 0.0)))

    return miss_counter, fp_counter, miss_detail, fp_detail


def main():
    ap = argparse.ArgumentParser(description="Router miss/false-positive attribution")
    ap.add_argument("--data_dir", default="data/revised")
    ap.add_argument("--top_k", type=int, default=20)
    ap.add_argument("--pool_mode", choices=["domain", "cross"], default="domain")
    ap.add_argument("--tools", default=None)
    ap.add_argument("--show_tasks", type=int, default=3, help="展示前 N 个最差任务的明细")
    args = ap.parse_args()

    tasks = load_tasks(args.data_dir)
    if not tasks:
        print(f"未找到任务: {args.data_dir}")
        sys.exit(1)

    if args.tools:
        real = load_real_tools(args.tools)
        noise_names = set()
        if args.pool_mode == "domain" and any("_domain" in t for t in real):
            # 与 eval_router 同口径:dump 带 _domain 时按真实域分池
            by_dom = defaultdict(list)
            for t in real:
                by_dom[t.get("_domain")].append(t)
            pools = dict(by_dom)
            print(f"真实工具池(按域分池): " +
                  ", ".join(f"{d}={len(p)}" for d, p in sorted(pools.items())))
        else:
            seen, real = set(), [t for t in real if not
                                 (t["name"] in seen or seen.add(t["name"]))]
            pools = {d: real for d in {t["domain"] for t in tasks}}
        for d in {t["domain"] for t in tasks} - set(pools):
            pools[d] = real
            print(f"⚠️ dump 缺少域 {d},使用全量池")
    else:
        pools = ({"d": build_cross_pool(tasks) for d in {t["domain"] for t in tasks}}
                 if args.pool_mode == "cross"
                 else {d: build_domain_pool(tasks, d) for d in {t["domain"] for t in tasks}})
        noise_names = {t["name"] for t in pools[next(iter(pools))] if t["name"] in
                       {n["name"] for n in build_domain_pool(tasks, next(iter(pools)))}} & \
                      {n["name"] for n in __import__("eval_router").NOISE_TOOLS}

    k_candidate = max(30, args.top_k)
    total_miss, total_fp = Counter(), Counter()
    task_stats = []
    n_unreach = 0

    for t in tasks:
        pool = pools[t["domain"]]
        # 可达性核对:不在池中的 GT 任何路由器都选不到,不计入漏检分类
        unreach = t["selected_tools"] - {x["name"] for x in pool}
        n_unreach += len(unreach)
        t = dict(t, selected_tools=t["selected_tools"] & {x["name"] for x in pool})
        router = ToolRouter(pool, k_candidate=k_candidate, k_final=args.top_k)
        mc, fc, md, fd = analyze_task(t, router, pool, args.top_k, k_candidate, noise_names)
        total_miss.update(mc)
        total_fp.update(fc)
        recall = 1 - sum(mc.values()) / len(t["selected_tools"]) if t["selected_tools"] else 0
        precision = 1 - sum(fc.values()) / args.top_k
        task_stats.append({"t": t, "miss": mc, "fp": fc, "miss_detail": md, "fp_detail": fd,
                           "recall": recall, "precision": precision})

    # ===== 汇总 =====
    n_gt = sum(len(t["selected_tools"]) for t in tasks)
    n_slots = len(tasks) * args.top_k
    print(f"任务数 {len(tasks)} | top_k={args.top_k} | k_candidate={k_candidate} | "
          f"GT 工具总数 {n_gt}(可达口径) | 子集槽位总数 {n_slots}")
    if n_unreach:
        print(f"⚠️ 另有 {n_unreach} 个 GT 工具不在对应域池中(跨域引用),已从漏检统计中剔除")
    print(f"micro recall = {1 - sum(total_miss.values())/n_gt:.1%} | "
          f"micro precision = {1 - sum(total_fp.values())/n_slots:.1%}\n")

    print("=== 漏检归因(每类 × 计数 × 占全部漏检比例)===")
    miss_total = sum(total_miss.values())
    for cat, n in total_miss.most_common():
        print(f"  {cat:<20}{n:>4}  ({n/miss_total:.0%} of misses)" if miss_total else "")
    print("\n=== 误报归因(谁在浪费 top_k 槽位)===")
    fp_total = sum(total_fp.values())
    for cat, n in total_fp.most_common():
        print(f"  {cat:<20}{n:>4}  ({n/fp_total:.0%} of false positives)" if fp_total else "")

    # ===== 最差任务明细 =====
    worst = sorted(task_stats, key=lambda x: x["recall"])[: args.show_tasks]
    for s in worst:
        t = s["t"]
        print(f"\n=== 案例: {t['id'][:40]} (recall={s['recall']:.0%} precision={s['precision']:.0%}) ===")
        print(f"  任务: {t['user_prompt'][:110]}...")
        for name, cat, score, rank in s["miss_detail"]:
            print(f"  [漏检-{cat}] {name:<36} score={score:.4f} rank={rank}")
        for name, cat, score in s["fp_detail"][:6]:
            print(f"  [误报-{cat}] {name:<36} score={score:.4f}")

    # ===== 行动建议(按案例数据推导)=====
    print("\n=== 改善措施(按观察到的漏检/误报结构排序)===")
    z = total_miss.get("zero_overlap", 0)
    b = total_miss.get("below_cutoff", 0)
    o = total_miss.get("outside_candidates", 0)
    fam = total_fp.get("family_overlap", 0)
    noise = total_fp.get("noise_tool", 0)
    if z:
        print(f"1. zero_overlap={z}:任务只写实体不写工具动词/名词 → LLM 精排语义补选;"
              f"查询扩展(实体→域概念)可再啃一部分。")
    if b:
        print(f"2. below_cutoff={b}:rank 在 {args.top_k + 1}-{k_candidate} 之间,"
              f"差几名没进子集 → top_k 上调或提高 LOOKUP_FLOOR 后用本脚本重测。")
    if o:
        print(f"3. outside_candidates={o}:有分但被同族工具挤出候选 → 精排/名称加权。")
    if fam:
        print(f"4. family_overlap={fam}:同族近义占位 → LLM 精排可剔除(纯粗筛无法区分"
              f" add/update/delete 同实体动作),这是精排的核心价值点。")
    if noise:
        print(f"5. noise_tool={noise}:近似池的评估 artifact(--tools 真实池后消失),"
              f"但真实池的跨域竞争者同理 → LLM 精排。")


if __name__ == "__main__":
    main()
