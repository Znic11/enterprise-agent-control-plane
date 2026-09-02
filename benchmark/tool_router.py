"""ToolRouter — 统一工具路由模块(Phase 1)。

本模块合并了此前两套重复实现(见 docs/HANDOFF.md 2.2):
  * benchmark/tool_router.py   —— TF-IDF 粗筛(类 API,主路径,保留)
  * orchestrators/tool_router.py —— 关键词 + LLM 路由(函数 API,已并入)

统一后的三级漏斗(docs/tool_router_design.md):
  ② 粗筛   TF-IDF 余弦 top-30(k_candidate),置信度低于 min_score 回退全量(⑤)
  ③ 精排   可选轻量 LLM 在候选集内重选(注入 llm_call_fn,provider 无关);
            解析失败 / 选中 <5 个 → 回退 TF-IDF 子集
  兜底     route_keywords(纯关键词重叠,零成本,只在 TF-IDF 完全失效时作为
           最后手段,也供离线消融对比)

用法:
    # orchestrator 内(推荐,索引只建一次)
    router = ToolRouter(tools)
    subset, meta = router.route(task_text)

    # 便捷函数(每次调用即建索引;512 工具量级下开销可忽略)
    result = route(task_text, tools, top_k=20, llm_call_fn=fn, prefer_llm=True)
    result.selected   # List[str] 工具名
    result.to_metadata()

诚实性边界:route() 只接受任务文本与工具定义,不读取任务配置的
selected_tools 字段(那是离线评估用的 ground truth,执行时读=答案泄露)。

V2 升级点:把 TFIDFIndex 换成 sentence-transformers embedding 余弦,接口不变。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 20
DEFAULT_K_CANDIDATE = 30
MIN_LLM_SELECTED = 5  # LLM 精排选中数低于此值 → 回退粗筛子集(宁多勿少)


# ---------------------------------------------------------------------------
# 文本处理
# ---------------------------------------------------------------------------


def _stem(w: str) -> str:
    """轻量词干化(纯规则,零依赖):归一复数,弥补 TF-IDF 词汇不匹配。

    entitlements -> entitlement, products -> product, entries -> entry。
    只做保守的复数归一(不做 es/ing 剥离,避免 updates->updat vs update
    这类不对称);长度阈值防止过度剥离(case/sla/status 不受影响)。
    """
    if len(w) <= 4:
        return w
    if w.endswith("ies") and len(w) > 5:
        return w[:-3] + "y"
    if w.endswith("sses"):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is", "ses")):
        return w[:-1]
    return w


def _tokenize(text: str) -> List[str]:
    """TF-IDF tokenize:小写 + 拆分 snake_case + 去停用词/短词 + 轻量词干化。

    停用词与 >=3 长度过滤必须做:snake_case 拆分会把 post_to_feed 拆出 "to",
    不滤的话虚词会把无关工具顶进 top-k(实测 rank 2)。
    """
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9_]+", " ", text)
    toks: List[str] = []
    for w in text.split():
        toks.extend(w.split("_"))  # create_new_case -> create, new, case
    return [_stem(t) for t in toks if len(t) >= 3 and t not in STOPWORDS]


# 语法虚词,对关键词路由无信号。注意保留企业实体/动作名词(case, record,
# create, update, find, ...)—— 它们正是工具名的构成成分。
STOPWORDS = {
    "the", "a", "an", "to", "of", "for", "on", "in", "with", "and", "or",
    "is", "are", "be", "by", "at", "from", "as", "it", "this", "that",
    "their", "them", "they", "his", "her", "using", "please", "need",
    "needs", "must", "should", "via", "all", "any", "each", "per", "not",
    "no", "do", "does", "make", "sure", "more", "than", "then", "will",
    "would", "can", "could", "may", "might", "also", "into", "over",
    "under", "between", "within", "about", "after", "before", "during",
    "until", "while", "out", "up", "down", "off", "some", "such", "only",
    "very", "just", "one", "two", "user", "customer",
}


def keyword_tokenize(text: str) -> List[str]:
    """关键词路由 tokenize:去停用词、丢短 token、复数归一。"""
    words = re.split(r"[^a-zA-Z0-9]+", (text or "").lower())
    return [_stem(w) for w in words if w and w not in STOPWORDS and len(w) >= 3]


def build_tool_signature(tool: Dict[str, Any]) -> str:
    """工具语义签名 = 名称(加权×2)+ description 前两句 + 参数名列表。

    工具名是最强语义信号(find_entitlements 直接对应"查 entitlement"),
    重复出现使 TF 翻倍,缓解 TF-IDF 词汇不匹配问题。
    """
    name = tool["name"]
    desc = (tool.get("description") or "").split(".")[:2]
    desc = ". ".join(desc)[:200]
    params = list((tool.get("input_schema") or {}).get("properties", {}).keys())
    return f"{name} {name} | {desc} | params: {', '.join(params[:12])}"


# ---------------------------------------------------------------------------
# TF-IDF 检索索引(纯 stdlib,零第三方依赖)
# ---------------------------------------------------------------------------


class TFIDFIndex:
    """文档集合 -> TF-IDF 权重;查询文本 -> 余弦相似度 top-k。"""

    def __init__(self, docs: Dict[str, str]):
        self.doc_names = list(docs.keys())
        self.doc_tokens = {n: _tokenize(d) for n, d in docs.items()}
        df: Counter = Counter()  # 文档频率
        for toks in self.doc_tokens.values():
            df.update(set(toks))
        self.N = len(self.doc_names)
        self.idf = {t: math.log((self.N + 1) / (f + 1)) + 1 for t, f in df.items()}

    def _tfidf_vec(self, toks: List[str]) -> Dict[str, float]:
        cnt = Counter(toks)
        return {t: cnt[t] / len(toks) * self.idf.get(t, 1) for t in cnt if t in self.idf}

    @staticmethod
    def _cosine(q: Dict[str, float], d: Dict[str, float]) -> float:
        dot = sum(q[t] * d.get(t, 0) for t in q)
        nq = math.sqrt(sum(v * v for v in q.values()))
        nd = math.sqrt(sum(v * v for v in d.values()))
        return dot / (nq * nd) if nq and nd else 0.0

    def search(self, query: str, top_k: int = 30) -> List[Tuple[float, str]]:
        qv = self._tfidf_vec(_tokenize(query))
        if not qv:
            return []
        scored = [(self._cosine(qv, self._tfidf_vec(toks)), name)
                  for name, toks in self.doc_tokens.items()]
        scored.sort(reverse=True)
        return scored[:top_k]


# ---------------------------------------------------------------------------
# 路由器主体(三级漏斗的第 ② 级粗筛)
# ---------------------------------------------------------------------------

# 只读/查询类工具前缀。企业任务几乎总是"先查后写"(resolve 实体再操作),
# 而任务文本只描述实体、往往不含 "find/list/get" 这类动词词面 —— 实测约
# 1/4 的必要工具因此得 0 分落榜。给这类工具一个小的排序地板分(通用启发式,
# 不读任何任务标注,无答案泄露),precision 换 recall,符合"宁多勿少"原则。
LOOKUP_PREFIXES = ("find_", "list_", "get_", "search_", "retrieve_", "check_")
LOOKUP_FLOOR = 0.08


class ToolRouter:
    """TF-IDF 粗筛路由。route() 返回 (子集工具列表, 元数据)。"""

    def __init__(
        self,
        tools: List[Dict[str, Any]],
        k_candidate: int = DEFAULT_K_CANDIDATE,
        k_final: int = DEFAULT_TOP_K,
        min_score: float = 0.05,
    ):
        if k_candidate < 1 or k_final < 1:
            raise ValueError(f"k_candidate/k_final must be >= 1, got {k_candidate}/{k_final}")
        self.tools = tools
        self.by_name = {t["name"]: t for t in tools}
        docs = {t["name"]: build_tool_signature(t) for t in tools}
        self.index = TFIDFIndex(docs)
        # k_candidate(LLM 精排的候选输入池)与 k_final(粗筛输出数)解耦:
        # 二者可独立,k_final > k_candidate 在纯粗筛/饱和曲线实验里合法(输出给足
        # k_final,精排阶段才在 k_candidate 候选内收敛)。旧实现 hits[:k_final] 从
        # 候选池截取,导致 k_final > k_candidate 时静默截断(实测 80 只给 30)。
        self.k_candidate = k_candidate
        self.k_final = k_final
        self.min_score = min_score

    def route(self, task_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        # 先在全池上打分(地板分要作用于全池,0 分 lookup 才有机会入选)
        raw = self.index.search(task_text, top_k=len(self.tools))
        if not raw or raw[0][0] < self.min_score:
            # ⑤ 置信度回退:语义差异过大,回退全量(执行不至于全灭)
            return self.tools, {
                "fallback": "low_confidence",
                "top_score": raw[0][0] if raw else 0.0,
                "subset_size": len(self.tools),
            }
        # 只读工具地板分 + 全量排序(不做 k_candidate 截断,见 __init__ 注释)
        boosted = [(s + LOOKUP_FLOOR if n.startswith(LOOKUP_PREFIXES) else s, n)
                   for s, n in raw]
        boosted.sort(reverse=True)
        cand = boosted[: self.k_candidate]                       # ③ 精排候选输入池
        out = boosted[: self.k_final]                            # 粗筛输出(≥候选时给足)
        subset = [self.by_name[n] for _, n in out]
        return subset, {
            "candidate_size": len(cand),
            "candidate_names": [n for _, n in cand],
            "subset_size": len(subset),
            "top_score": raw[0][0],
            "bottom_score": out[-1][0] if out else 0.0,
        }


def expand_tool(tool_name: str, full_pool: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """④ 渐进式扩展:执行中请求子集外工具时,从全量池取 schema。"""
    for t in full_pool:
        if t["name"] == tool_name:
            return t
    return None


# ---------------------------------------------------------------------------
# 关键词路由(零成本兜底,也供离线消融对比)
# ---------------------------------------------------------------------------


def keyword_score(tool: Dict[str, Any], task_tokens: set) -> float:
    """工具名/description 与任务 token 的重叠度。工具名权重×2(最强信号)。"""
    name_tokens = set(keyword_tokenize(tool.get("name", "")))
    desc_tokens = set(keyword_tokenize(tool.get("description", "")))
    name_hits = len(name_tokens & task_tokens)
    desc_hits = len(desc_tokens & task_tokens)
    return 2.0 * name_hits + 0.8 * desc_hits


def route_keywords(
    task_text: str,
    all_tools: List[Dict[str, Any]],
    top_k: int = DEFAULT_TOP_K,
) -> List[str]:
    """纯关键词重叠路由(零成本、确定性)。"""
    task_tokens = set(keyword_tokenize(task_text))
    if not task_tokens or not all_tools:
        return []
    scored = sorted(all_tools, key=lambda t: keyword_score(t, task_tokens), reverse=True)
    hits = [t["name"] for t in scored if keyword_score(t, task_tokens) > 0]
    return hits[:top_k]


# ---------------------------------------------------------------------------
# LLM 精排(三级漏斗的第 ③ 级;llm_call_fn 由调用方注入,provider 无关)
# ---------------------------------------------------------------------------

ROUTER_PROMPT = """You are a tool router for an enterprise agent. Given a user task and a list of candidate tools, select the tools needed to complete the task.

Rules:
- Select ALL tools you would actually call for this task, including read-only lookups (find/list/get/search/retrieve) needed to obtain IDs or entity details referenced in the task.
- Enterprise tasks in this benchmark typically need 6-20 tools covering every sub-step (lookups, creates, updates, links). Do NOT over-truncate: a missing tool fails the task, an extra one only adds noise.
- When unsure, prefer INCLUDING a tool over missing it.
- Return ONLY a JSON array of tool names, no explanation, no markdown.

Task:
{task}

Candidate tools:
{tools}

Return: ["tool_a", "tool_b", ...]"""


def parse_llm_route_response(text: str) -> Optional[List[str]]:
    """尽力从 LLM 路由响应中提取工具名列表。"""
    if not text:
        return None
    # 1. 顶层 JSON 数组
    try:
        data = json.loads(text.strip())
        if isinstance(data, list):
            return [str(x) for x in data]
    except json.JSONDecodeError:
        pass
    # 2. 文本中的 [...] 块
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return [str(x) for x in data]
        except json.JSONDecodeError:
            pass
    # 3. 任意位置的带引号字符串
    names = re.findall(r'"([^"]+)"', text)
    return names or None


def _build_route_prompt(task_text: str, candidate_tools: List[Dict[str, Any]]) -> str:
    lines = []
    for tool in candidate_tools:
        name = str(tool.get("name", ""))
        desc = str(tool.get("description", "")).replace("\n", " ")[:120]
        lines.append(f"- {name}: {desc}")
    return ROUTER_PROMPT.format(task=task_text, tools="\n".join(lines))


def route_llm(
    task_text: str,
    candidate_tools: List[Dict[str, Any]],
    llm_call_fn: Callable[[str], str],
    top_k: int = DEFAULT_TOP_K,
) -> Optional[List[str]]:
    """LLM 在候选集内精排。失败/解析不出返回 None(由上层决定回退)。

    ``llm_call_fn(prompt) -> str`` 由调用方注入;本模块不依赖任何 LLM SDK。
    """
    prompt = _build_route_prompt(task_text, candidate_tools)
    try:
        raw = llm_call_fn(prompt)
    except Exception as e:  # noqa: BLE001 — 路由绝不能崩掉整个 run
        logger.warning(f"LLM router call failed ({e})")
        return None
    names = _parse_and_validate(raw, candidate_tools)
    return names[:top_k] if names else names


def _parse_and_validate(
    raw: str, candidate_tools: List[Dict[str, Any]]
) -> Optional[List[str]]:
    """解析 LLM 输出并过滤幻觉工具名(同步/异步精排共用)。"""
    names = parse_llm_route_response(raw)
    if names is None:
        logger.warning("LLM router returned unparseable output")
        return None
    valid = {str(t.get("name")) for t in candidate_tools}
    return [n for n in names if n in valid]


async def route_llm_async(
    task_text: str,
    candidate_tools: List[Dict[str, Any]],
    llm_call_fn_async: Callable[[str], Any],
    top_k: int = DEFAULT_TOP_K,
) -> Optional[List[str]]:
    """route_llm 的异步版(``llm_call_fn_async(prompt) -> Awaitable[str]``)。

    供 batch_route 并发使用;解析与校验规则与 route_llm 完全一致。
    """
    prompt = _build_route_prompt(task_text, candidate_tools)
    try:
        raw = await llm_call_fn_async(prompt)
    except Exception as e:  # noqa: BLE001 — 路由绝不能崩掉整个 run
        logger.warning(f"LLM router call failed ({e})")
        return None
    names = _parse_and_validate(raw, candidate_tools)
    return names[:top_k] if names else names


# ---------------------------------------------------------------------------
# 统一入口 + 审计元数据
# ---------------------------------------------------------------------------


@dataclass
class RouteResult:
    """一次路由的结果 + 诊断信息(供审计与实验归因)。"""

    selected: List[str] = field(default_factory=list)
    method: str = "tfidf"  # "tfidf" | "tfidf+llm" | "full_fallback" | "empty"
    full_tool_count: int = 0
    task_text: str = ""
    candidate_size: int = 0
    top_score: float = 0.0
    fallback: Optional[str] = None
    candidate_names: List[str] = field(default_factory=list)  # ③ 粗筛候选(精排输入),归因用

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "router_method": self.method,
            "router_selected_count": len(self.selected),
            "router_selected": self.selected,
            "router_full_tool_count": self.full_tool_count,
            "router_candidate_size": self.candidate_size,
            "router_candidate_names": self.candidate_names,
            "router_top_score": round(self.top_score, 4),
            "router_fallback": self.fallback,
        }


def route(
    task_text: str,
    all_tools: List[Dict[str, Any]],
    top_k: int = DEFAULT_TOP_K,
    llm_call_fn: Optional[Callable[[str], str]] = None,
    prefer_llm: bool = False,
    rerank_mode: str = "strict",
    candidate_k: Optional[int] = None,
) -> RouteResult:
    """统一路由入口:TF-IDF 粗筛 →(可选)LLM 精排 → 兜底。

    - 粗筛置信度低 → 回退全量工具(method="full_fallback")
    - prefer_llm 且提供了 llm_call_fn → 在粗筛候选上 LLM 精排;
      LLM 失败或选中 <5 个 → 回退粗筛 top-k 子集
    - rerank_mode:
        "strict" —— LLM 输出即最终(method="tfidf+llm"),噪声最少,
                  但 LLM 砍错会丢必需工具(实测 reasoner 过度截断)
        "union"  —— 精排结果 ∪ 粗筛 top-k 保底(method="tfidf+llm+union"),
                  精排不可能砍掉粗筛已召回的必需工具 → recall ≥ 纯粗筛。
                  注意:减噪/增补效果取决于 candidate_k > top_k —— 若候选
                  与输出同宽(默认 max(30,top_k)=top_k 时),LLM 只能从保底
                  集合里选,union ≡ 纯粗筛(no-op,白付延迟)。要发挥精排
                  价值请显式传 candidate_k(如 top_k=30, candidate_k=60):
                  粗筛保底前 30,LLM 在 60 候选里把 30-60 区间的必需工具捞回。
    - candidate_k:喂给 LLM 的候选宽度(粗筛前 N 个);默认 max(30, top_k),
      与输出 top_k 同宽。union 模式下应传大于 top_k 的值以获得增补空间。
    - 工具池为空 → selected=[],method="empty"(上层应视为配置错误)
    """
    if not all_tools:
        logger.warning("route() called with an empty tool pool")
        return RouteResult(selected=[], method="empty", full_tool_count=0, task_text=task_text)

    k_cand = candidate_k if candidate_k else max(DEFAULT_K_CANDIDATE, top_k)
    router = ToolRouter(all_tools, k_candidate=k_cand, k_final=top_k)
    subset, meta = router.route(task_text)
    top_score = meta.get("top_score", 0.0)

    if meta.get("fallback"):
        names = [t["name"] for t in all_tools]
        logger.warning(
            f"[ROUTER] low confidence (top_score={top_score:.4f} < {router.min_score}); "
            f"falling back to full pool ({len(names)} tools)"
        )
        return RouteResult(
            selected=names,
            method="full_fallback",
            full_tool_count=len(all_tools),
            task_text=task_text,
            top_score=top_score,
            fallback=meta["fallback"],
        )

    method = "tfidf"
    if prefer_llm and llm_call_fn is not None:
        cand_names = meta.get("candidate_names") or [t["name"] for t in subset]
        cand_tools = [router.by_name[n] for n in cand_names if n in router.by_name]
        llm_names = route_llm(task_text, cand_tools, llm_call_fn, top_k)
        if llm_names is None:
            logger.warning("[ROUTER] LLM rerank failed; keeping TF-IDF subset")
        elif len(llm_names) < MIN_LLM_SELECTED:
            logger.warning(
                f"[ROUTER] LLM selected only {len(llm_names)} tools (<{MIN_LLM_SELECTED}); "
                f"keeping TF-IDF subset"
            )
        else:
            if rerank_mode == "union":
                # 保底融合:精排选中的 ∪ 粗筛 top-k(按粗筛候选顺序去重)。
                # 精排只能"加回/排序",不能砍掉粗筛已召回的候选 —— 确保
                # 精排阶段不会因过度截断丢失必需工具(实测 23 个被砍工具
                # 中大量是 GT 必需,且与噪声在粗筛分数上不可分,纯阈值无法救回)。
                llm_set = set(llm_names)
                merged = [t["name"] for t in subset]
                for n in llm_names:
                    if n not in merged:
                        merged.append(n)
                subset = [router.by_name[n] for n in merged if n in router.by_name]
                method = "tfidf+llm+union"
                logger.info(
                    f"[ROUTER] union rerank: LLM {len(llm_set)} ∪ coarse {len(merged)} "
                    f"-> {len(subset)} tools (recall floor = coarse)"
                )
            else:  # strict
                subset = [router.by_name[n] for n in llm_names if n in router.by_name]
                method = "tfidf+llm"

    return RouteResult(
        selected=[t["name"] for t in subset],
        method=method,
        full_tool_count=len(all_tools),
        task_text=task_text,
        candidate_size=meta.get("candidate_size", len(subset)),
        top_score=top_score,
        candidate_names=meta.get("candidate_names", []),
    )


async def batch_route(
    task_texts: List[str],
    all_tools: List[Dict[str, Any]],
    top_k: int = DEFAULT_TOP_K,
    llm_call_fn_async: Optional[Callable[[str], Any]] = None,
    concurrency: int = 8,
    progress_fn: Optional[Callable[[int, int], None]] = None,
    rerank_mode: str = "strict",
    candidate_k: Optional[int] = None,
) -> Dict[str, RouteResult]:
    """批量路由:TF-IDF 索引只建一次 + LLM 精排并发执行(语义与 route() 一致)。

    单任务请用 route()(orchestrator 执行路径);同批多任务(离线评估/
    全量跑)用本函数 —— 否则每任务重建索引、且 LLM 调用串行等网关。

    回退规则与 route() 保持一致(改动需两处同步):
      置信度低 → full_fallback(全量);LLM 失败或选中 < MIN_LLM_SELECTED → 粗筛子集。
    rerank_mode 与 route() 同语义:"strict"=LLM 输出即最终;"union"=与粗筛
    top-k 取并集保底(recall ≥ 纯粗筛,精排永不砍掉粗筛已召回的必需工具)。
    candidate_k 与 route() 同语义:LLM 候选宽度,默认 max(30, top_k);
    union 模式下传大于 top_k 的值(如 top_k=30, candidate_k=60),LLM 才能
    增补粗筛 top-k 之外的候选工具(否则候选=输出,union ≡ 纯粗筛)。
    """
    import asyncio

    if not all_tools:
        logger.warning("batch_route() called with an empty tool pool")
        return {t: RouteResult(method="empty", task_text=t) for t in task_texts}

    # ① 粗筛:索引建一次,纯 CPU,毫秒级/任务
    k_cand = candidate_k if candidate_k else max(DEFAULT_K_CANDIDATE, top_k)
    router = ToolRouter(all_tools, k_candidate=k_cand, k_final=top_k)
    coarse = {t: router.route(t) for t in task_texts}

    # ② 精排:并发(信号量限流),网关延迟被并发摊平
    sem = asyncio.Semaphore(max(1, concurrency))
    done = [0]

    async def _one(task_text: str):
        subset, meta = coarse[task_text]
        top_score = meta.get("top_score", 0.0)
        common = dict(full_tool_count=len(all_tools), task_text=task_text, top_score=top_score)

        if meta.get("fallback"):  # ⑤ 置信度回退 → 全量
            return task_text, RouteResult(
                selected=[t["name"] for t in all_tools],
                method="full_fallback", fallback=meta["fallback"], **common)

        if llm_call_fn_async is None:  # 纯粗筛
            return task_text, RouteResult(
                selected=[t["name"] for t in subset], method="tfidf",
                candidate_size=meta.get("candidate_size", len(subset)), **common)

        cand_names = meta.get("candidate_names") or [t["name"] for t in subset]
        cand_tools = [router.by_name[n] for n in cand_names if n in router.by_name]
        async with sem:
            llm_names = await route_llm_async(task_text, cand_tools, llm_call_fn_async, top_k)
        done[0] += 1
        if progress_fn:
            progress_fn(done[0], len(task_texts))

        if llm_names is None:
            logger.warning("[ROUTER] LLM rerank failed; keeping TF-IDF subset")
        elif len(llm_names) < MIN_LLM_SELECTED:
            logger.warning(
                f"[ROUTER] LLM selected only {len(llm_names)} tools (<{MIN_LLM_SELECTED}); "
                f"keeping TF-IDF subset"
            )
        else:
            if rerank_mode == "union":
                # 保底融合:与粗筛 subset(top-k)并集,按粗筛顺序 + LLM 补充去重
                merged = [t["name"] for t in subset]
                for n in llm_names:
                    if n not in merged:
                        merged.append(n)
                subset = [router.by_name[n] for n in merged if n in router.by_name]
                return task_text, RouteResult(
                    selected=[t["name"] for t in subset], method="tfidf+llm+union",
                    candidate_size=meta.get("candidate_size", len(subset)),
                    candidate_names=cand_names, **common)
            else:  # strict
                subset = [router.by_name[n] for n in llm_names if n in router.by_name]
                return task_text, RouteResult(
                    selected=[t["name"] for t in subset], method="tfidf+llm",
                    candidate_size=meta.get("candidate_size", len(subset)),
                    candidate_names=cand_names, **common)

        return task_text, RouteResult(  # LLM 失败/过少 → 回退粗筛子集
            selected=[t["name"] for t in subset], method="tfidf",
            candidate_size=meta.get("candidate_size", len(subset)),
            candidate_names=cand_names, **common)

    pairs = await asyncio.gather(*(_one(t) for t in task_texts))
    return dict(pairs)
