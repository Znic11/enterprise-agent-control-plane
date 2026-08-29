"""ReAct orchestrator with a Tool Router (Phase 1 deliverable).

Built on top of the official ``orchestrators/react.py`` loop, adding three
capabilities the stock ReAct agent lacks:

1. TOOL ROUTING
   Before execution starts, the full tool set (up to ~512 tools) is reduced to
   a top-k subset using ``orchestrators.tool_router``. Keyword routing is free
   and deterministic; optional LLM routing uses a cheap router model when
   ``router_llm_client`` is provided. The subset is logged so every run is
   auditable.

2. PROGRESSIVE DISCOVERY
   If the executing LLM calls a tool that was NOT in the routed subset (a
   legitimate miss — routing is lossy on purpose), the tool definition is
   expanded on the fly, added to the active set, and the call proceeds. This
   guarantees the router can never block a correct action, only reduce noise.

3. ROUTE METADATA
   The routing decision (method, selected names, full count) is merged into
   the run result via ``get_result_metadata()`` so experiments can attribute
   gains/losses to routing decisions and compute recall@k offline.

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
from .tool_router import route, RouteResult

logger = logging.getLogger(__name__)


class ReactRouterOrchestrator(AgentOrchestrator):
    """ReAct loop over a routed (top-k) tool subset with progressive discovery.

    Args:
        router_top_k:        Max tools exposed to the executor at any time.
        router_llm_client:   Optional LLMClient used for LLM-based routing.
                             If None, keyword routing is used (free).
        enable_discovery:    Allow out-of-subset tool calls to expand the set.
    """

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
        self._route_result: Optional[RouteResult] = None
        self._discovered: List[str] = []

        # name -> full tool def for progressive discovery lookups
        self._all_tools_by_name = {str(t["name"]): t for t in available_tools}

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

            conversation_flow.append(
                {
                    "type": "ai_message",
                    "content": get_text_content(response.content),
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
            logger.info(f"LLM Response: {get_text_content(response.content)}")

            if not response.tool_calls or len(response.tool_calls) == 0:
                logger.info("No tool calls requested. Task complete.")
                break

            # Progressive discovery BEFORE executing, so the tool definition
            # is expanded in time for the actual call.
            self._discover_if_needed(response.tool_calls)

            for tool_call in response.tool_calls:
                tool_name = tool_call["name"]
                tool_args = tool_call["args"]

                exec_result = await self._execute_tool_call(tool_name, tool_args)
                tool_result = exec_result["result"]
                target_gym = exec_result["gym_server"]

                logger.info(f"Tool result success: {tool_result.get('success')}")

                if tool_name not in tools_used:
                    tools_used.append(tool_name)
                tool_results.append(
                    {
                        "tool_name": tool_name,
                        "arguments": tool_args,
                        "result": tool_result,
                        "gym_server": target_gym,
                    }
                )

                messages.append(
                    ToolMessage(
                        content=json.dumps(tool_result.get("result", {})),
                        tool_call_id=tool_call.get("id", ""),
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
