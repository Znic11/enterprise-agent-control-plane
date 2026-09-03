"""ReAct orchestrator with a Tool Router (Phase 1 deliverable).

Built on top of the official ``orchestrators/react.py`` loop, adding three
capabilities the stock ReAct agent lacks:

1. TOOL ROUTING
   Before execution starts, the full tool set (up to ~512 tools) is reduced to
   a top-k subset using ``benchmark.tool_router`` (unified module): TF-IDF
   coarse filter, optional LLM rerank over the top-30 candidates when
   ``router_llm_client`` is provided, and graceful fallbacks. The subset is
   logged so every run is auditable.

2. PROGRESSIVE DISCOVERY (exact-name + intent-level)
   If the executing LLM calls a tool that was NOT in the routed subset (a
   legitimate miss — routing is lossy on purpose), the tool definition is
   expanded on the fly, added to the active set, and the call proceeds. This
   guarantees the router can never block a correct action, only reduce noise.
   Name-level expansion alone cannot fire under strict function-calling
   (``bind_tools`` only exposes the active subset, so the model cannot emit an
   out-of-subset name). Intent-level retrieval covers the real gap: when the
   model expresses a need but cannot name the tool — unknown/typo'd call
   (trigger A), tool error that implies a missing tool, or text admitting
   "no tool available" without a call (trigger C) — its words + arg keys are
   used as a query against the full pool (``ToolRouter.search``), and hits are
   added to the active set only (never auto-executed; write-tool side effects
   still require an explicit model call).

3. ROUTE METADATA
   The routing decision (method, selected names, full count) and any
   intent-retrieved tools are merged into the run result via
   ``get_result_metadata()`` so experiments can attribute gains/losses to
   routing decisions and compute recall@k offline.

To run:
    python evaluate.py --hf_dataset ServiceNow-AI/EnterpriseOps-Gym \\
        --domain teams --mode oracle \\
        --llm_config conf/llm/<model>.json \\
        --output_folder results/react_router/<model>/teams/oracle \\
        --orchestrator react_router --router_top_k 20 --num_runs 1
"""

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from benchmark.llm_client import LLMClient, get_text_content
from benchmark.models import BenchmarkConfig

from .base import AgentOrchestrator
from benchmark.tool_router import route, RouteResult, ToolRouter

logger = logging.getLogger(__name__)


class ReactRouterOrchestrator(AgentOrchestrator):
    """ReAct loop over a routed (top-k) tool subset with progressive discovery.

    Args:
        router_top_k:        Max tools exposed to the executor at any time.
        router_llm_client:   Optional LLMClient used for LLM-based routing.
                             If None, keyword routing is used (free).
        enable_discovery:    Allow out-of-subset tool calls to expand the set
                             via exact-name match AND intent-level retrieval.
        intent_top_k:        How many tools to retrieve per intent search.
        intent_min_score:    TF-IDF floor for intent-retrieved tools.
    """

    # 模型文本里表示"缺工具"的信号。命中才触发 intent 检索,避免把正常收尾
    # ("no more tool calls needed")误判为能力缺口而空转一轮。
    _GAP_SIGNALS = (
        "no tool", "not available", "unavailable", "cannot find",
        "can't find", "unable to find", "no available tool", "no such tool",
        "don't have", "do not have", "doesn't have", "lack a tool", "need a tool",
        "need to find", "is there a tool", "isn't a tool", "no way to",
    )

    def __init__(
        self,
        llm_client: "LLMClient",
        mcp_clients: Dict[str, "MCPClient"],
        tool_to_server_mapping: Dict[str, str],
        available_tools: List[Dict[str, Any]],
        config: "BenchmarkConfig",
        max_iterations: int = 50,
        router_top_k: int = 20,
        router_llm_client: Optional["LLMClient"] = None,
        enable_discovery: bool = True,
        intent_top_k: int = 6,
        intent_min_score: float = 0.03,
    ):
        super().__init__(
            llm_client=llm_client,
            mcp_clients=mcp_clients,
            tool_to_server_mapping=tool_to_server_mapping,
            available_tools=available_tools,
            config=config,
            max_iterations=max_iterations,
        )
        self.router_top_k = router_top_k
        self.router_llm_client = router_llm_client
        self.enable_discovery = enable_discovery
        self.intent_top_k = intent_top_k
        self.intent_min_score = intent_min_score
        self._route_result: Optional[RouteResult] = None
        self._discovered: List[str] = []
        self._intent_retrieved: List[str] = []  # 由意图检索(非精确名)发现的工具

        # name -> full tool def for progressive discovery lookups
        self._all_tools_by_name = {str(t["name"]): t for t in available_tools}
        # 全池 TF-IDF 索引,意图级检索复用(与 route 的索引各自独立,构建开销毫秒级)
        self._intent_index = ToolRouter(available_tools) if enable_discovery else None

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def _run_routing(self) -> RouteResult:
        """Route tools before execution. Uses LLM routing when a router LLM is
        configured, otherwise free keyword routing."""
        if self.router_llm_client is not None:
            async def llm_call(prompt: str) -> str:
                response = await self.router_llm_client.llm.ainvoke(
                    [HumanMessage(content=prompt)]
                )
                return get_text_content(response.content)

            return route(
                self.config.user_prompt,
                self.available_tools,
                top_k=self.router_top_k,
                llm_call_fn=llm_call,
                prefer_llm=True,
            )
        return route(
            self.config.user_prompt,
            self.available_tools,
            top_k=self.router_top_k,
        )

    def get_result_metadata(self) -> Dict[str, Any]:
        """Surface routing decisions so experiments can be audited."""
        meta: Dict[str, Any] = {}
        if self._route_result is not None:
            meta.update(self._route_result.to_metadata())
        if self._discovered:
            meta["router_discovered"] = self._discovered
        if self._intent_retrieved:
            meta["router_intent_retrieved"] = self._intent_retrieved
        return meta

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _active_tools(self) -> List[Dict[str, Any]]:
        """The currently exposed tool subset (routed + progressively discovered)."""
        if self._route_result is None:
            return []
        names = set(self._route_result.selected) | set(self._discovered)
        return [self._all_tools_by_name[n] for n in names if n in self._all_tools_by_name]

    def _discover_if_needed(self, tool_calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Expand the active tool set with any called-but-unrouted tools."""
        if not self.enable_discovery or not tool_calls:
            return tool_calls
        active = set(self._route_result.selected) | set(self._discovered)
        for tc in tool_calls:
            name = tc.get("name")
            if name and name not in active and name in self._all_tools_by_name:
                self._discovered.append(name)
                logger.warning(
                    f"Progressive discovery: expanding tool '{name}' "
                    f"(was outside routed subset)"
                )
        return tool_calls

    # ------------------------------------------------------------------
    # Intent-level retrieval (意图级发现)
    # ------------------------------------------------------------------

    def _active_names(self) -> set:
        """当前活跃工具名集合(路由子集 + 已发现)。"""
        if self._route_result is None:
            return set()
        return set(self._route_result.selected) | set(self._discovered)

    def _intent_discover(
        self, query_text: str, reason: str, caller: Optional[str] = None
    ) -> List[str]:
        """意图级发现核心:用一段"能力需求文本"在全池检索,把命中的真实工具
        并入活跃集。只扩展可见集,不自动执行(安全边界:写工具仍需模型显式调用)。
        返回本轮新增的工具名。
        """
        if not self.enable_discovery or self._intent_index is None:
            return []
        if not query_text or not query_text.strip():
            return []
        hits = self._intent_index.search(
            query_text, top_k=self.intent_top_k, min_score=self.intent_min_score
        )
        active = self._active_names()
        added: List[str] = []
        for score, name in hits:
            if name not in active and name in self._all_tools_by_name:
                self._discovered.append(name)
                self._intent_retrieved.append(name)
                added.append(name)
                logger.warning(
                    f"Intent discovery [{reason}]: '{name}' (score={score:.4f}) "
                    f"retrieved from full pool"
                )
        return added

    @staticmethod
    def _intent_query_from_call(
        tool_name: str, tool_args: Dict[str, Any]
    ) -> str:
        """从"失败的调用"构造检索 query:请求的工具名 + 参数键(参数名往往就是
        真实工具 schema 里的属性名,能精确命中)。
        """
        parts = [tool_name]
        arg_keys = list((tool_args or {}).keys())
        if arg_keys:
            parts.append(" ".join(arg_keys))
        return " | ".join(parts)

    def _has_gap_signal(self, text: str) -> bool:
        """文本是否表达了"缺工具/做不到"的能力缺口(小写子串匹配)。"""
        low = (text or "").lower()
        return any(sig in low for sig in self._GAP_SIGNALS)

    async def execute(self) -> Dict[str, Any]:
        """Route -> ReAct loop with progressive discovery."""
        # PASS 0: route the tool space
        self._route_result = await self._run_routing()
        logger.info(
            f"[ROUTER] method={self._route_result.method} "
            f"full={self._route_result.full_tool_count} -> "
            f"selected={len(self._route_result.selected)} "
            f"(top_k={self.router_top_k})"
        )

        messages = [
            SystemMessage(content=self.config.system_prompt),
            HumanMessage(content=self.config.user_prompt),
        ]
        conversation_flow = [
            {"type": "system_message", "content": self.config.system_prompt},
            {"type": "user_message", "content": self.config.user_prompt},
        ]
        tools_used = []
        tool_results = []

        for iteration in range(self.max_iterations):
            logger.info(f"\n--- Iteration {iteration + 1} ---")

            response = await self.llm_client.invoke_with_tools(
                messages, self._active_tools()
            )
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

            if not response.tool_calls or len(response.tool_calls) == 0:
                # 触发点 C:模型没调用工具但文本表达"缺工具/做不到"的能力缺口。
                # 若能从全池检索到新工具,注入说明并再给一轮,而不是直接收尾。
                if self._has_gap_signal(assistant_text):
                    added = self._intent_discover(
                        assistant_text, reason="text_gap"
                    )
                    if added:
                        note = (
                            "[system] The following tools were not in your "
                            f"original set but exist and are now available: "
                            f"{', '.join(added)}. Call them if needed, then "
                            "finish the task."
                        )
                        messages.append(HumanMessage(content=note))
                        conversation_flow.append(
                            {"type": "system_message", "content": note}
                        )
                        logger.warning(
                            f"Intent discovery [text_gap]: added {added}; "
                            f"continuing loop"
                        )
                        continue
                logger.info("No tool calls requested. Task complete.")
                break

            # Progressive discovery BEFORE executing, so the tool definition
            # is expanded in time for the actual call.
            self._discover_if_needed(response.tool_calls)

            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]
                tool_call_id = tool_call.get("id", "")

                # 触发点 A/B:调用名不在全池 → 近似名/幻觉名。不直接执行也不只
                # 报错,而是用 名字+参数键+本轮推理文本 检索全池,把真实工具并入
                # 活跃集,再回喂"不存在 + 已新增候选"错误,让模型下一轮以真实名重试。
                if tool_name not in self._all_tools_by_name:
                    query = self._intent_query_from_call(tool_name, tool_args)
                    if assistant_text:
                        query = f"{query} | {assistant_text[:400]}"
                    added = self._intent_discover(
                        query, reason="unknown_call", caller=tool_name
                    )
                    tool_result: Dict[str, Any] = {
                        "success": False,
                        "error": (
                            f"Tool '{tool_name}' does not exist in the tool pool."
                        ),
                    }
                    if added:
                        tool_result["available_tools_added"] = added
                    target_gym = None
                else:
                    try:
                        exec_result = await self._execute_tool_call(tool_name, tool_args)
                        tool_result = exec_result["result"]
                        target_gym = exec_result["gym_server"]
                        logger.info(f"Tool result success: {tool_result.get('success')}")
                        if tool_name not in tools_used:
                            tools_used.append(tool_name)
                    except Exception as e:  # noqa: BLE001 — 工具调用失败不中断整个 run
                        logger.error(f"Tool '{tool_name}' execution failed: {e}")
                        tool_result = {
                            "success": False,
                            "error": f"{type(e).__name__}: {e}",
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

        return {
            "final_response": get_text_content(messages[-1].content) if messages else "",
            "conversation_flow": conversation_flow,
            "tools_used": tools_used,
            "tool_results": tool_results,
            "messages": messages,
        }
