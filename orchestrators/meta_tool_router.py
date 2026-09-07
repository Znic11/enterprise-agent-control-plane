"""MetaToolOrchestrator — 元工具(Meta-Tool)模式的工具调用 orchestrator。

参考 Spring AI Alibaba 的元工具设计(OpenAI tool search / Anthropic tool
search 同思路):系统只向执行 LLM 暴露一个只读的 ``_tool_search`` 元工具,
LLM 需要更多业务工具时**显式调用**它(传入描述所需功能的关键词),系统拦截
这次调用,用 ``benchmark.tool_router.ToolRouter.search`` 检索(后端可插拔:
hybrid = 稠密向量 + 稀疏 TF-IDF 融合,默认主通道;dense/tfidf 可切,见
benchmark/dense_retriever.py)在全工具池检索,把命中的真实工具 schema
**动态注入(bind)** 进后续轮次的可调用集 —— LLM 在下一轮"看到"这些真实工具
后自行选择并正式调用;真实调用仍走 ``base._execute_tool_call``。

历史:react_router(事前路由 top-k + 执行期意图级发现)对照实现已于 2026-09-04
移除 —— 用户判定其把问题复杂化;meta_tool_router 的"何时检索、搜什么"完全
交给 LLM 显式决策(元工具)为主通道,端到端对照基线 = react(oracle mode)。

注入机制(已与用户对齐):**bind 动态扩 + [system] 文本说明,对 LLMClient
零改动** —— ``llm_client.invoke_with_tools`` 每轮重新 bind 传入的 tools,
orchestrator 只要每轮传 ``[_tool_search] + 已注入工具 defs``,真实工具即进入
模型可调用集(schema 完整,严格 function-calling 兼容)。

安全边界(硬性):
  * ``_tool_search`` 是只读检索:拦截后只打分 + 返回 + 注入可见集,绝不自动
    执行真实工具;写工具副作用必须由模型在后续轮次显式调用触发。
  * 检索输入只来自本轮 LLM 的调用参数(query),不读取任务配置的 selected_tools
    (那是离线评估用的 ground truth,执行时读 = 答案泄露)。
  * 命中工具只进可见集;已注入 defs 每轮 bind,是否调用完全由模型决定。

运行(见 evaluate.py CLI):
    python evaluate.py --orchestrator meta_tool --llm_config conf/llm/<m>.json \
        --configs_folder <tasks> --output_folder results/meta_tool/<model> \
        --retrieval hybrid --num_runs 1
"""

import json
import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from benchmark.dense_retriever import DEFAULT_EMBEDDING_MODEL, get_embedder
from benchmark.llm_client import LLMClient, get_text_content
from benchmark.models import BenchmarkConfig
from benchmark.tool_router import DEFAULT_HYBRID_ALPHA, ToolRouter

from .base import AgentOrchestrator

logger = logging.getLogger(__name__)

TOOL_SEARCH_NAME = "_tool_search"
EXECUTE_TOOL_NAME = "_execute_tool"
DEFAULT_SEARCH_TOP_K = 6        # 每次 _tool_search 检索返回工具数(沿用历史初值)
DEFAULT_SEARCH_MIN_SCORE = 0.03  # 检索分数下限(拍脑袋初值,未调参)
DEFAULT_CACHE_SIZE = 64         # 同会话 query->hits 缓存上限
DEFAULT_ZERO_HIT_FALLBACK = 3   # 连续零命中达此值 -> 兜底绑定全池,防死锁(None=关闭兜底)
DEFAULT_RETRIEVAL = "tfidf"     # 检索后端默认 tfidf(纯 stdlib 保单测/离线一致);
                                # evaluate.py 端到端 CLI 默认 hybrid(evaluate 边界显式指定)

# ---------------------------------------------------------------------------
# verifier-in-the-loop(验证闭环,V1):仅在"宣布完成前"强制一轮终态核对。
# 红线:全程不读 self.config.verifiers(判据 SQL/expected_value 均不触达);
# 自查只走业务只读工具通道,标准 = 首轮模型自列的验收 checklist(从 user_prompt
# + 域政策推导),纠错信号 = "回读观察到的状态 vs 任务目标",非评分者信息。
# ---------------------------------------------------------------------------
DEFAULT_VERIFY_MAX_ROUNDS = 3   # gate 阶段"无只读证据就声称完成"的最大提醒次数;
                                # 达上限强制收尾并打 vl_forced_done(有界,绝不空转)

# 只读工具名启发式:用于断言 gate 阶段确实发生"读"证据(find_/get_/list_/...;
# email 域工具多为 email_get_/email_list_ 前缀)。仅用于计数/提醒,不阻断任何执行。
_RO_VERBS = frozenset({
    "get", "list", "find", "search", "retrieve", "check", "lookup", "read",
    "show", "view", "describe", "fetch", "count", "verify", "email_get", "email_list",
})


def _is_read_only_tool_name(name: str) -> bool:
    """按工具名前缀判断是否只读查询类(启发式,见 _RO_VERBS)。"""
    toks = str(name or "").lower().split("_")
    return bool(toks and (toks[0] in _RO_VERBS or "_".join(toks[:2]) in _RO_VERBS))


VL_PLANNING_PROMPT = (
    "Before performing the task, state your acceptance checklist: the concrete "
    "facts that must be true in the system when the task is complete, derived "
    "from the user request and the domain policy (entities to create/update, "
    "fields and their expected values, links/relationships to verify). "
    "Reply with ONLY a compact numbered checklist. Do not call any tool now."
)

VL_GATE_MESSAGE = (
    "You indicated the task is complete. Before finalizing, verify the final "
    "state against the acceptance checklist you stated at the start: every "
    "item must be true in the system NOW. Re-read the entity/entities you "
    "created or modified with read-only tools (get/find/list/search/retrieve...). "
    "If any item does not match, fix it first, then re-verify. When verified, "
    "reply with your final summary starting with 'FINAL:'."
)

VL_REMIND_MESSAGE = (
    "Your completion message did not include evidence from a read-only tool "
    "call. Re-read the affected entity/entities with a read-only tool and "
    "confirm their current state before replying 'FINAL:'."
)


def build_tool_search_def() -> Dict[str, Any]:
    """构造元工具 schema。字段用 MCP 标准 inputSchema(驼峰),与 llm_client
    的 ``_convert_mcp_tools_to_langchain`` 期望一致(bind 时才带完整参数)。"""
    return {
        "name": TOOL_SEARCH_NAME,
        "description": (
            "Read-only tool discovery. Search the enterprise tool pool for a tool "
            "that can perform an action you need. Provide a short keyword query "
            "describing the capability (e.g. 'update entitlement support level', "
            "'find cases for an account'). Matching tools become available for you "
            "to call in the next turn. This tool only searches; it never executes "
            "any business tool and never changes data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords describing the capability you need "
                                   "(tool names, entities, actions).",
                },
                "top_k": {
                    "type": "integer",
                    "description": "How many matching tools to return "
                                   f"(default {DEFAULT_SEARCH_TOP_K}).",
                },
            },
            "required": ["query"],
        },
    }


def build_execute_tool_def() -> Dict[str, Any]:
    """构造"统一执行"元工具 schema(dispatch=exec 模式专用)。

    方案 Y:bind 集永远固定为 [_tool_search, _execute_tool](前缀稳定,
    KV/前缀缓存友好)。模型先 _tool_search 看到命中工具的完整 schema
    (在 ToolMessage 返回里),再调 _execute_tool(name, args) 显式执行 ——
    真实工具名作为"字符串参数"传给元工具,由 orchestrator 拦截后分发到
    全池执行端。不依赖服务端对 bind 外工具名宽松/不校验,任何 FC 服务端
    (OpenAI/DeepSeek 严格模式亦)都接受;执行入口唯一,便于统一校验/审计。
    """
    return {
        "name": EXECUTE_TOOL_NAME,
        "description": (
            "Execute a real business tool by its exact name. Call this AFTER "
            "_tool_search returned matching tools and you have read their full "
            "input schemas. Provide the EXACT tool name from the search results "
            "and arguments conforming to that tool's schema. This is the ONLY "
            "way to trigger a real tool with side effects."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Exact name of the tool to execute (one of "
                                   "the names returned by _tool_search).",
                },
                "args": {
                    "type": "object",
                    "description": "Arguments conforming to the named tool's "
                                   "input schema (shown in the _tool_search result).",
                },
            },
            "required": ["name"],
        },
    }


def _first_sentence(desc: str, limit: int = 120) -> str:
    """工具描述首句(检索结果回喂给模型时保持上下文精简)。"""
    first = (desc or "").split(".")[0].strip()
    return first[:limit]


class MetaToolOrchestrator(AgentOrchestrator):
    """Meta-Tool 模式 ReAct 循环:初始只 bind ``_tool_search``,检索命中的真实
    工具 schema 动态并入后续轮次的可调用集。

    Args:
        tool_search_top_k:        每次 _tool_search 最多检索并返回多少工具。
        tool_search_min_score:    检索分数下限(低于视为零命中)。
        cache_size:               同会话 query->hits 缓存条目上限(不跨任务)。
        boost_lookup:              检索时对 find_/list_/get_ 前缀工具加 LOOKUP_FLOOR
                                   地板分(与 route() 的只读启发式一致)。
        fallback_all_after_zero_hits: 连续零命中达此值 -> 兜底绑定域内全池并继续;
                                   None = 关闭兜底(零命中永远只回喂提示)。
        warmup_top_k:             预热模式:首轮就把 route(user_prompt) 粗筛出的
                                   top-k 工具一并注入(与 _tool_search 并存)。
                                   None = 纯 Meta-Tool 单通道(默认,先验证假设)。
        retrieval:                检索后端 "tfidf"(默认,纯 stdlib)/ "dense" /
                                   "hybrid"。稠密通道见 benchmark/dense_retriever;
                                   evaluate.py 端到端 CLI 默认 hybrid。
        embedding_model:          retrieval != "tfidf" 时的本地 embedding 模型
                                   (默认 BAAI/bge-small-en-v1.5,见 dense_retriever)。
        embedding_device:         "cuda"/"cpu"/None(auto;默认 None)。
        embedder:                 (测试/调用方注入用)已构造好的 TextEmbedder;
                                  为 None 时由 get_embedder(embedding_model) 提供。
        hybrid_alpha:             hybrid 融合的稠密权重(稀疏权重 = 1-alpha)。
    """

    def __init__(
        self,
        llm_client: "LLMClient",
        mcp_clients: Dict[str, "MCPClient"],
        tool_to_server_mapping: Dict[str, str],
        available_tools: List[Dict[str, Any]],
        config: "BenchmarkConfig",
        max_iterations: int = 50,
        tool_search_top_k: int = DEFAULT_SEARCH_TOP_K,
        tool_search_min_score: float = DEFAULT_SEARCH_MIN_SCORE,
        cache_size: int = DEFAULT_CACHE_SIZE,
        boost_lookup: bool = False,
        fallback_all_after_zero_hits: Optional[int] = DEFAULT_ZERO_HIT_FALLBACK,
        warmup_top_k: Optional[int] = None,
        retrieval: str = DEFAULT_RETRIEVAL,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_device: Optional[str] = None,
        embedder: Any = None,
        hybrid_alpha: float = DEFAULT_HYBRID_ALPHA,
        dispatch: str = "inject",
        verify_loop: bool = False,
        verify_max_rounds: int = DEFAULT_VERIFY_MAX_ROUNDS,
    ):
        super().__init__(
            llm_client=llm_client,
            mcp_clients=mcp_clients,
            tool_to_server_mapping=tool_to_server_mapping,
            available_tools=available_tools,
            config=config,
            max_iterations=max_iterations,
        )
        if tool_search_top_k < 1:
            raise ValueError(f"tool_search_top_k must be >= 1, got {tool_search_top_k}")
        if cache_size < 1:
            raise ValueError(f"cache_size must be >= 1, got {cache_size}")
        if warmup_top_k is not None and warmup_top_k < 1:
            raise ValueError(f"warmup_top_k must be >= 1 or None, got {warmup_top_k}")
        if verify_max_rounds < 1:
            raise ValueError(f"verify_max_rounds must be >= 1, got {verify_max_rounds}")

        self.tool_search_top_k = tool_search_top_k
        self.tool_search_min_score = tool_search_min_score
        self.boost_lookup = boost_lookup
        self.fallback_all_after_zero_hits = fallback_all_after_zero_hits
        self.retrieval = retrieval
        self.hybrid_alpha = hybrid_alpha
        if dispatch not in ("inject", "exec"):
            raise ValueError(f"dispatch must be 'inject' or 'exec', got {dispatch!r}")
        self.dispatch = dispatch
        self.verify_loop = verify_loop
        self.verify_max_rounds = verify_max_rounds

        # verifier-in-loop 运行期状态(每次 execute() 开始重置)
        self._vl_checklist: str = ""
        self._vl_gate_active = False
        self._vl_gate_rounds = 0
        self._vl_no_read = 0
        self._vl_read_calls = 0
        self._vl_read_tools: List[str] = []
        self._vl_correction_turns = 0
        self._vl_forced_done = False
        self._vl_accepted = False
        self._vl_final_marker = False
        self._vl_plan_calls = 0

        # 检索后端:默认 tfidf(纯 stdlib);dense/hybrid 时优先用注入的 embedder
        # (单测/调用方提供),否则经 get_embedder 拿模块级单例(每进程加载一次)。
        embedder_ = embedder
        if retrieval != "tfidf" and embedder_ is None and available_tools:
            embedder_ = get_embedder(embedding_model, embedding_device)
        if retrieval != "tfidf":
            logger.info(
                f"[META-TOOL] retrieval={retrieval} "
                f"embedder={'injected' if embedder is not None else embedding_model} "
                f"(device={embedding_device or 'auto'}) hybrid_alpha={hybrid_alpha}"
            )

        # 全池索引与 name -> def 映射(检索与执行共用)
        self._router = (
            ToolRouter(
                available_tools,
                retrieval=retrieval,
                embedder=embedder_,
                hybrid_alpha=hybrid_alpha,
            )
            if available_tools
            else None
        )
        self._all_tools_by_name = {str(t["name"]): t for t in available_tools}

        # 状态:注入集合(保序)、query->hits 缓存、计数
        self._injected: List[str] = []
        self._cache: "OrderedDict[str, List[Tuple[float, str]]]" = OrderedDict()
        self._cache_size = cache_size
        self.search_calls = 0   # LLM 发起 _tool_search 的次数(含缓存命中)
        self.searches = 0       # 实际执行 TF-IDF 检索的次数(缓存未命中)
        self.cache_hits = 0
        self._total_hits = 0
        self.zero_hits = 0
        self._consecutive_zero = 0
        self._fallback_all = False
        self._warmup_names: List[str] = []

        if warmup_top_k is not None:
            self._warmup(warmup_top_k)

    # ------------------------------------------------------------------
    # 注入 / 可见集管理
    # ------------------------------------------------------------------

    def _admit(self, name: str) -> bool:
        """把真实工具并入注入集(可见集)。返回是否本轮新增(去重)。"""
        if name in self._all_tools_by_name and name not in self._injected:
            self._injected.append(name)
            return True
        return False

    def _warmup(self, top_k: int) -> None:
        """(预留)预热模式:首轮就注入 route(user_prompt) 的粗筛 top-k 工具,
        与 _tool_search 并存 —— 冷启动即有保底工具,便于后续与纯 Meta-Tool 对照。"""
        if self._router is None or not (self.config.user_prompt or "").strip():
            logger.warning("[META-TOOL] warmup_top_k set but pool/prompt empty; skip")
            return
        subset, meta = self._router.route(self.config.user_prompt)
        names = [t["name"] for t in subset[:top_k]]
        for n in names:
            self._admit(n)
        self._warmup_names = list(names)
        logger.info(
            f"[META-TOOL] warmup injected {len(names)} routed tools "
            f"(method={meta.get('fallback') or 'tfidf'})"
        )

    def _visible_tools(self) -> List[Dict[str, Any]]:
        """每轮 bind 给 LLM 的工具 = [_tool_search] + 当前可见真实工具。

        兜底(fallback_all)时直接全池;否则按注入顺序给出 defs。模型只可能
        调用这些工具 —— 严格 function-calling 下 _tool_search 永远合法。
        """
        tools: List[Dict[str, Any]] = [build_tool_search_def()]
        if self.dispatch == "exec":
            # 方案 Y:bind 集永远固定 [_tool_search, _execute_tool] —— 前缀稳定,
            # KV/前缀缓存友好(真实工具绝不进 bind 集,只经 _execute_tool 分发)。
            tools.append(build_execute_tool_def())
        elif self._fallback_all:
            tools.extend(self.available_tools)
        else:
            tools.extend(
                self._all_tools_by_name[n] for n in self._injected
                if n in self._all_tools_by_name
            )
        return tools

    # ------------------------------------------------------------------
    # query -> hits 缓存(同会话,不跨任务;检索是确定性 TF-IDF)
    # ------------------------------------------------------------------

    def _cache_get(self, key: str) -> Optional[List[Tuple[float, str]]]:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def _cache_put(self, key: str, hits: List[Tuple[float, str]]) -> None:
        self._cache[key] = hits
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    # ------------------------------------------------------------------
    # 检索核心(同步、确定性,单测友好)
    # ------------------------------------------------------------------

    def _search(
        self, query: str, top_k: int
    ) -> Tuple[List[Tuple[float, str]], bool]:
        """按 query 检索全池,返回 ((score, name)...过滤 min_score, 是否缓存命中)。

        只打分返回,命中并入注入集由调用方决定 —— 与 ToolRouter.search 的
        "admission is the caller's decision" 语义一致。
        """
        if self._router is None or not query or not query.strip():
            return [], False
        key = query.strip()
        cached = self._cache_get(key)
        if cached is not None:
            self.cache_hits += 1
            return list(cached), True
        self.searches += 1
        raw = self._router.search(
            key,
            top_k=top_k,
            min_score=self.tool_search_min_score,
            boost_lookup=self.boost_lookup,
        )
        hits = [(s, n) for s, n in raw if n in self._all_tools_by_name]
        self._cache_put(key, list(hits))
        return hits, False

    def _handle_tool_search(
        self, args: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """拦截 _tool_search 调用:解析参数 -> 检索 -> 注入可见集。

        返回 (tool_result, note):tool_result 形如 {"success": True, "result": {...}}
        (result 内含 found/count/note);note 为可选文本注入说明(仅当本轮有新工具
        首次注入时给出,减少消息噪音)。
        """
        query = str((args or {}).get("query", "")).strip()
        raw_k = (args or {}).get("top_k")
        try:
            top_k = int(raw_k) if raw_k not in (None, "") else self.tool_search_top_k
        except (TypeError, ValueError):
            top_k = self.tool_search_top_k
        top_k = max(1, min(top_k, 50))

        self.search_calls += 1
        hits, from_cache = self._search(query, top_k)

        newly_added: List[str] = []
        for _s, name in hits:
            if self._admit(name):
                newly_added.append(name)

        found = [
            {
                "name": n,
                "score": round(s, 4),
                "description": _first_sentence(
                    self._all_tools_by_name[n].get("description", "")
                ),
            }
            for s, n in hits
        ]

        note: Optional[str] = None
        if hits:
            self._consecutive_zero = 0
            self._total_hits += len(hits)
            payload: Dict[str, Any] = {
                "query": query,
                "count": len(hits),
                "found": found,
                "cache_hit": from_cache,
            }
            if newly_added:
                payload["newly_added"] = newly_added
                if self.dispatch == "exec":
                    # 方案 Y:命中工具不进 bind 集,必须把完整 schema 文本回喂,
                    # 模型才能通过 _execute_tool 填对参数。仅首次注入时给全量,
                    # 已注入工具只列名字(避免上下文膨胀)。
                    schemas = {
                        n: self._all_tools_by_name[n].get(
                            "input_schema",
                            self._all_tools_by_name[n].get("inputSchema", {}),
                        )
                        for n in newly_added
                    }
                    payload["schemas"] = schemas
                    note = (
                        "New tools are available. Call _execute_tool with the "
                        "exact tool name and args matching its schema above: "
                        f"{', '.join(newly_added)}. Already-available tools: "
                        f"{', '.join(self._injected)}."
                    )
                else:
                    note = (
                        "Tools matching your search are now bound and callable: "
                        f"{', '.join(newly_added)}. Call the one that fits your need "
                        "using its schema."
                    )
        else:
            # 零命中:回喂引导,绝不中断;连续达阈值触发全池兜底防死锁
            self.zero_hits += 1
            self._consecutive_zero += 1
            payload = {
                "query": query,
                "count": 0,
                "found": [],
                "cache_hit": from_cache,
                "note": (
                    "No tool matched your query. Rephrase with more specific "
                    "capability keywords (entity + action), or finish if you "
                    "cannot proceed."
                ),
            }
            if (
                self.fallback_all_after_zero_hits is not None
                and self._consecutive_zero >= self.fallback_all_after_zero_hits
                and not self._fallback_all
            ):
                self._fallback_all = True
                if self.dispatch == "exec":
                    # 方案 Y:bind 集必须保持固定(前缀缓存友好),不能 bind 全池;
                    # 兜底改为给出全池工具名单,引导模型用 _execute_tool 直接点名。
                    payload["note"] = (
                        "Repeated searches found no match. All pool tools are: "
                        f"{', '.join(sorted(self._all_tools_by_name))}. Call "
                        "_execute_tool with the exact name and best-guess args."
                    )
                    logger.warning(
                        f"[META-TOOL] {self._consecutive_zero} consecutive zero-hit "
                        f"searches; exec mode: listed full pool names instead of binding"
                    )
                else:
                    payload["note"] = (
                        "Repeated searches found no match. The full tool pool is now "
                        "bound and available - call the exact tool you need directly."
                    )
                    logger.warning(
                        f"[META-TOOL] {self._consecutive_zero} consecutive zero-hit "
                        f"searches; fell back to binding the full pool"
                    )

        return {"success": True, "result": payload, "error": None}, note

    # ------------------------------------------------------------------
    # verifier-in-loop(验证闭环 V1):首轮自列 checklist + 收尾强制只读回读
    # ------------------------------------------------------------------

    def _reset_vl_state(self) -> None:
        """每次 execute() 开始时重置运行期状态(或chestrator 每任务新实例,双保险)。"""
        self._vl_checklist = ""
        self._vl_gate_active = False
        self._vl_gate_rounds = 0
        self._vl_no_read = 0
        self._vl_read_calls = 0
        self._vl_read_tools = []
        self._vl_correction_turns = 0
        self._vl_forced_done = False
        self._vl_accepted = False
        self._vl_final_marker = False
        self._vl_plan_calls = 0

    def _append_system_note(
        self,
        messages: List[Any],
        conversation_flow: List[Dict[str, Any]],
        note: str,
        stage: Optional[str] = None,
    ) -> None:
        """追加一条 [system] 文本说明(插在全部 ToolMessage 之后,下一轮 AI 前可见),
        并同步 conversation_flow(带 stage 便于审计/演示样例提取)。"""
        messages.append(HumanMessage(content=f"[system] {note}"))
        entry: Dict[str, Any] = {"type": "system_message", "content": note}
        if stage:
            entry["stage"] = stage
        conversation_flow.append(entry)

    async def _elicit_checklist(
        self,
        messages: List[Any],
        conversation_flow: List[Dict[str, Any]],
    ) -> None:
        """首轮用一次专用调用让模型自列验收 checklist(不 bind 工具、不执行任何
        调用)。checklist 由模型从 user_prompt + 域政策推导 —— 标准可审计、
        域无关、零 verifier 泄露。非空则注入主会话供全程与收尾参照。"""
        self._vl_plan_calls += 1
        plan_messages = [
            SystemMessage(content=self.config.system_prompt),
            HumanMessage(content=self.config.user_prompt),
            HumanMessage(content=VL_PLANNING_PROMPT),
        ]
        try:
            response = await self.llm_client.invoke_with_tools(plan_messages, [])
        except Exception as e:  # noqa: BLE001 — 规划失败不致命,gate 消息仍可兜底
            logger.error(f"[VERIFY-LOOP] checklist elicitation failed: {e}")
            return
        text = get_text_content(response.content)
        checklist = (text or "").strip()
        if not checklist:
            logger.warning("[VERIFY-LOOP] empty checklist from model; skip injection")
            return
        self._vl_checklist = checklist[:2000]
        self._append_system_note(
            messages,
            conversation_flow,
            "Acceptance checklist (self-derived, verify each item before FINAL):\n"
            + self._vl_checklist,
            stage="verify_loop_checklist",
        )
        logger.info(
            f"[VERIFY-LOOP] acceptance checklist elicited "
            f"({len(self._vl_checklist)} chars)"
        )

    def _vl_note_real_tool(self, real_name: str, success: bool) -> None:
        """gate 激活期间记录成功的只读证据(业务只读工具调用)。"""
        if not self._vl_gate_active or not success:
            return
        if _is_read_only_tool_name(real_name):
            self._vl_read_calls += 1
            if real_name not in self._vl_read_tools:
                self._vl_read_tools.append(real_name)

    def _verify_gate_step(self) -> Tuple[bool, Optional[str], Optional[str]]:
        """收尾门禁判定。返回 (accept_done, note, stage):
        - verify_loop 关:直接放行(行为与旧版完全一致)。
        - 首次声称完成:激活 gate,注入核查引导(不直接 break)。
        - 已激活且有只读证据:放行(证据 = 真实调过业务只读工具)。
        - 已激活但无证据:提醒;达 verify_max_rounds 强制收尾(vl_forced_done)。
        """
        if not self.verify_loop:
            return True, None, None
        if not self._vl_gate_active:
            self._vl_gate_active = True
            return False, VL_GATE_MESSAGE, "verify_loop_gate"
        if self._vl_read_calls > 0:
            return True, None, None
        self._vl_no_read += 1
        if self._vl_no_read >= self.verify_max_rounds:
            self._vl_forced_done = True
            logger.warning(
                f"[VERIFY-LOOP] no read evidence after {self._vl_no_read} "
                f"reminder(s); forcing done"
            )
            return True, None, None
        return False, VL_REMIND_MESSAGE, "verify_loop_remind"

    # ------------------------------------------------------------------
    # 元数据(审计/离线指标)
    # ------------------------------------------------------------------

    def get_result_metadata(self) -> Dict[str, Any]:
        """Surface Meta-Tool telemetry so experiments can audit & compute
        offline metrics (meta_tool_searches / hits_avg / cache_hits ...).
        """
        meta: Dict[str, Any] = {
            "meta_tool": True,
            "meta_tool_retrieval": self.retrieval,
            "meta_tool_hybrid_alpha": self.hybrid_alpha,
            "meta_tool_dispatch": self.dispatch,
            "meta_tool_search_calls": self.search_calls,
            "meta_tool_searches": self.searches,
            "meta_tool_cache_hits": self.cache_hits,
            "meta_tool_hits_avg": (
                round(self._total_hits / self.searches, 4) if self.searches else 0.0
            ),
            "meta_tool_zero_hits": self.zero_hits,
            "meta_tool_injected": list(self._injected),
            "meta_tool_fallback_all": bool(self._fallback_all),
            "meta_tool_warmup_names": list(self._warmup_names),
        }
        if self.verify_loop:
            meta["vl_enabled"] = True
            meta["vl_checklist"] = self._vl_checklist[:500]
            meta["vl_gate_rounds"] = self._vl_gate_rounds
            meta["vl_read_calls_gate"] = self._vl_read_calls
            meta["vl_read_tools"] = list(self._vl_read_tools)
            meta["vl_no_read_reminders"] = self._vl_no_read
            meta["vl_correction_turns"] = self._vl_correction_turns
            meta["vl_forced_done"] = self._vl_forced_done
            meta["vl_plan_calls"] = self._vl_plan_calls
            meta["vl_final_marker"] = self._vl_final_marker
        return meta

    # ------------------------------------------------------------------
    # 执行循环
    # ------------------------------------------------------------------

    async def execute(self) -> Dict[str, Any]:
        if self.verify_loop:
            self._reset_vl_state()
        messages = [
            SystemMessage(content=self.config.system_prompt),
            HumanMessage(content=self.config.user_prompt),
        ]
        conversation_flow = [
            {"type": "system_message", "content": self.config.system_prompt},
            {"type": "user_message", "content": self.config.user_prompt},
        ]
        tools_used: List[str] = []
        tool_results: List[Dict[str, Any]] = []

        # verifier-in-loop:首轮先让模型自列验收 checklist(不 bind、不执行任何工具)
        if self.verify_loop:
            await self._elicit_checklist(messages, conversation_flow)

        for iteration in range(self.max_iterations):
            visible = self._visible_tools()
            logger.info(
                f"\n--- Iteration {iteration + 1} --- "
                f"(binding {len(visible)} tools: 1 meta + {len(visible) - 1} real)"
            )

            response = await self.llm_client.invoke_with_tools(messages, visible)
            messages.append(response)

            assistant_text = get_text_content(response.content)
            conversation_flow.append(
                {
                    "type": "ai_message",
                    "content": assistant_text,
                    "usage_metadata": (
                        response.usage_metadata
                        if hasattr(response, "usage_metadata")
                        else {}
                    ),
                    "response_metadata": (
                        response.response_metadata
                        if hasattr(response, "response_metadata")
                        else {}
                    ),
                    "tool_calls": [
                        {"name": tc["name"], "args": tc["args"]}
                        for tc in (response.tool_calls or [])
                    ],
                }
            )
            logger.info(f"LLM Response: {assistant_text}")

            if not response.tool_calls:
                # verifier-in-loop:模型声称完成 ≠ 允许收工 —— 先过收尾门禁:
                # 首次声称 -> 注入"按 checklist 用只读工具回读核对"的核查轮;
                # 已核查且有只读证据 -> 放行;无证据反复声称 -> 有界提醒后强制收尾。
                accept_done, gate_note, gate_stage = self._verify_gate_step()
                if gate_note is not None:
                    self._vl_gate_rounds += 1
                    self._append_system_note(
                        messages, conversation_flow, gate_note, stage=gate_stage
                    )
                    logger.info(
                        f"[VERIFY-LOOP] gate active: injected {gate_stage} "
                        f"(round {self._vl_gate_rounds}, read_evidence="
                        f"{self._vl_read_calls})"
                    )
                    continue
                if accept_done:
                    self._vl_accepted = True
                logger.info("No tool calls requested. Task complete.")
                break

            if self.verify_loop and self._vl_gate_active:
                # gate 激活后模型仍在调工具 = 纠错/补读回合
                self._vl_correction_turns += 1

            # 本轮检索产生的注入说明文本:必须插在全部 ToolMessage 之后(工具结果
            # 需紧跟对应 tool_call),下一轮 AI 前模型即可看到新增工具名单。
            pending_notes: List[str] = []

            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"] or {}
                tool_call_id = tool_call.get("id", "")

                if tool_name == TOOL_SEARCH_NAME:
                    # 元工具:拦截 -> 检索 -> 注入可见集(只读,绝不执行真实工具)
                    tool_result, note = self._handle_tool_search(tool_args)
                    if note:
                        pending_notes.append(note)
                    target_gym = None
                    logger.info(
                        f"[META-TOOL] search query='{tool_args.get('query', '')}' "
                        f"-> found={tool_result['result'].get('count', 0)}"
                    )
                elif tool_name == EXECUTE_TOOL_NAME:
                    # 方案 Y:统一执行元工具 -> 解析 (name, args) -> 校验池内 ->
                    # 分发到真实执行。bind 集固定,真实工具永不 bind;报错在此
                    # 集中捕获并回喂,便于引导模型自纠。
                    real_name = str((tool_args or {}).get("name", "")).strip()
                    real_args = (tool_args or {}).get("args") or {}
                    target_gym = None
                    if real_name not in self._all_tools_by_name:
                        logger.error(
                            f"[META-TOOL] _execute_tool -> unknown real tool "
                            f"'{real_name}'; guiding back to {TOOL_SEARCH_NAME}"
                        )
                        tool_result = {
                            "success": False,
                            "error": (
                                f"Tool '{real_name}' does not exist in the tool "
                                f"pool. Use {TOOL_SEARCH_NAME} to find the exact "
                                f"name, then retry {EXECUTE_TOOL_NAME}."
                            ),
                        }
                    else:
                        if real_name not in self._injected:
                            self._admit(real_name)
                        try:
                            exec_result = await self._execute_tool_call(
                                real_name, real_args
                            )
                            tool_result = exec_result["result"]
                            target_gym = exec_result["gym_server"]
                            logger.info(
                                f"Tool result success: {tool_result.get('success')}"
                            )
                            if real_name not in tools_used:
                                tools_used.append(real_name)
                            self._vl_note_real_tool(real_name, True)
                        except Exception as e:  # noqa: BLE001 — 单次失败不中断 run
                            logger.error(
                                f"Tool '{real_name}' execution failed via "
                                f"{EXECUTE_TOOL_NAME}: {e}"
                            )
                            tool_result = {
                                "success": False,
                                "error": f"{type(e).__name__}: {e}",
                            }
                    # 审计工具名用真实工具名(而非包装层 _execute_tool)
                    tool_name = real_name
                elif tool_name in self._all_tools_by_name:
                    # 真实工具:严格 FC 下它必然在本轮 bind 集内(此前已注入);
                    # 防御性放行(若因异常未注入则补注入)。
                    if tool_name not in self._injected:
                        self._admit(tool_name)
                    try:
                        exec_result = await self._execute_tool_call(
                            tool_name, tool_args
                        )
                        tool_result = exec_result["result"]
                        target_gym = exec_result["gym_server"]
                        logger.info(
                            f"Tool result success: {tool_result.get('success')}"
                        )
                        if tool_name not in tools_used:
                            tools_used.append(tool_name)
                        self._vl_note_real_tool(tool_name, True)
                    except Exception as e:  # noqa: BLE001 — 单次失败不中断整个 run
                        logger.error(f"Tool '{tool_name}' execution failed: {e}")
                        tool_result = {
                            "success": False,
                            "error": f"{type(e).__name__}: {e}",
                        }
                        target_gym = None
                else:
                    # 全池都不存在的名字:严格 FC 下不应发生(模型只能输出 bind 内
                    # 工具);防御性回错误并引导走 _tool_search,不 raise 不炸 run。
                    logger.error(
                        f"Tool '{tool_name}' not in pool (not bindable); "
                        f"guiding model back to {TOOL_SEARCH_NAME}"
                    )
                    tool_result = {
                        "success": False,
                        "error": (
                            f"Tool '{tool_name}' does not exist in the tool pool. "
                            f"Use {TOOL_SEARCH_NAME} to discover the right tool."
                        ),
                    }
                    target_gym = None

                tool_results.append(
                    {
                        "tool_name": tool_name,
                        "arguments": tool_args,
                        "result": tool_result,
                        "gym_server": target_gym,
                    }
                )

                content = (
                    tool_result.get("result", {})
                    if tool_result.get("success", False)
                    else tool_result
                )
                messages.append(
                    ToolMessage(
                        content=json.dumps(content),
                        tool_call_id=tool_call_id,
                    )
                )
                conversation_flow.append(
                    {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "result": tool_result,
                        "gym_server": target_gym,
                    }
                )

            for note in pending_notes:
                messages.append(HumanMessage(content=f"[system] {note}"))
                conversation_flow.append(
                    {"type": "system_message", "content": note}
                )

        final_response = (
            get_text_content(messages[-1].content) if messages else ""
        )

        # verifier-in-loop 收尾审计:
        #  - FINAL 标记:模型最终回复是否以 'FINAL:' 开头(不强校验,仅供统计);
        #  - 若循环因 max_iterations 耗尽而 gate 仍未放行(无只读证据),如实打标。
        if self.verify_loop:
            self._vl_final_marker = str(final_response).strip().startswith("FINAL:")
            if self._vl_gate_active and not self._vl_accepted and not self._vl_forced_done:
                self._vl_forced_done = True
                logger.warning(
                    "[VERIFY-LOOP] iteration budget exhausted while gate unresolved; "
                    "forced done (vl_forced_done=True)"
                )
            logger.info(
                f"[VERIFY-LOOP] summary: gate_rounds={self._vl_gate_rounds} "
                f"read_evidence={self._vl_read_calls} "
                f"reminders={self._vl_no_read} corrections={self._vl_correction_turns} "
                f"forced_done={self._vl_forced_done} final_marker={self._vl_final_marker}"
            )

        return {
            "final_response": final_response,
            "conversation_flow": conversation_flow,
            "tools_used": tools_used,
            "tool_results": tool_results,
            "messages": messages,
        }
