"""MetaToolOrchestrator — 元工具(Meta-Tool)模式的工具调用 orchestrator。

参考 Spring AI Alibaba 的元工具设计(OpenAI tool search / Anthropic tool
search 同思路):系统只向执行 LLM 暴露一个只读的 ``_tool_search`` 元工具,
LLM 需要更多业务工具时**显式调用**它(传入描述所需功能的关键词),系统拦截
这次调用,用 ``benchmark.tool_router.ToolRouter.search``(TF-IDF,复用
Phase-1 唯一路由实现,零新检索代码)在全工具池检索,把命中的真实工具 schema
**动态注入(bind)** 进后续轮次的可调用集 —— LLM 在下一轮"看到"这些真实工具
后自行选择并正式调用;真实调用仍走 ``base._execute_tool_call``。

与 react_router(Phase-1:事前路由 top-k 子集 + 执行期意图级发现 A/B/C)
并行存在,供同模型同 split 的端到端对照:
  react_router     —— 系统事前/事后兜底,把工具集"喂"给模型
  meta_tool_router —— 把"何时检索、搜什么"完全交给 LLM 显式决策(元工具)

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
        --num_runs 1
"""

import json
import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from benchmark.llm_client import LLMClient, get_text_content
from benchmark.models import BenchmarkConfig
from benchmark.tool_router import ToolRouter

from .base import AgentOrchestrator

logger = logging.getLogger(__name__)

TOOL_SEARCH_NAME = "_tool_search"
DEFAULT_SEARCH_TOP_K = 6        # 与 react_router 意图级检索 intent_top_k 一致
DEFAULT_SEARCH_MIN_SCORE = 0.03  # 与 react_router intent_min_score 一致(拍脑袋初值,未调参)
DEFAULT_CACHE_SIZE = 64         # 同会话 query->hits 缓存上限
DEFAULT_ZERO_HIT_FALLBACK = 3   # 连续零命中达此值 -> 兜底绑定全池,防死锁(None=关闭兜底)


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


def _first_sentence(desc: str, limit: int = 120) -> str:
    """工具描述首句(检索结果回喂给模型时保持上下文精简)。"""
    first = (desc or "").split(".")[0].strip()
    return first[:limit]


class MetaToolOrchestrator(AgentOrchestrator):
    """Meta-Tool 模式 ReAct 循环:初始只 bind ``_tool_search``,检索命中的真实
    工具 schema 动态并入后续轮次的可调用集。

    Args:
        tool_search_top_k:        每次 _tool_search 最多检索并返回多少工具。
        tool_search_min_score:    TF-IDF 分数下限(低于视为零命中)。
        cache_size:               同会话 query->hits 缓存条目上限(不跨任务)。
        boost_lookup:              检索时对 find_/list_/get_ 前缀工具加 LOOKUP_FLOOR
                                   地板分(与 route() 的只读启发式一致)。
        fallback_all_after_zero_hits: 连续零命中达此值 -> 兜底绑定域内全池并继续;
                                   None = 关闭兜底(零命中永远只回喂提示)。
        warmup_top_k:             预热模式:首轮就把 route(user_prompt) 粗筛出的
                                   top-k 工具一并注入(与 _tool_search 并存)。
                                   None = 纯 Meta-Tool 单通道(默认,先验证假设)。
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

        self.tool_search_top_k = tool_search_top_k
        self.tool_search_min_score = tool_search_min_score
        self.boost_lookup = boost_lookup
        self.fallback_all_after_zero_hits = fallback_all_after_zero_hits

        # 全池索引与 name -> def 映射(检索与执行共用)
        self._router = ToolRouter(available_tools) if available_tools else None
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
        if self._fallback_all:
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
    # 元数据(审计/离线指标)
    # ------------------------------------------------------------------

    def get_result_metadata(self) -> Dict[str, Any]:
        """Surface Meta-Tool telemetry so experiments can audit & compute
        offline metrics (meta_tool_searches / hits_avg / cache_hits ...).
        """
        return {
            "meta_tool": True,
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

    # ------------------------------------------------------------------
    # 执行循环
    # ------------------------------------------------------------------

    async def execute(self) -> Dict[str, Any]:
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
                logger.info("No tool calls requested. Task complete.")
                break

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

        return {
            "final_response": (
                get_text_content(messages[-1].content) if messages else ""
            ),
            "conversation_flow": conversation_flow,
            "tools_used": tools_used,
            "tool_results": tool_results,
            "messages": messages,
        }
