"""Base class for agent orchestrators."""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, TYPE_CHECKING

from benchmark.mcp_client import MCPClient
from benchmark.llm_client import LLMClient
from benchmark.models import BenchmarkConfig
from .dogwood_gate import DogwoodSafetyGate

logger = logging.getLogger(__name__)


class AgentOrchestrator(ABC):

    def __init__(
        self,
        llm_client: "LLMClient",
        mcp_clients: Dict[str, "MCPClient"],
        tool_to_server_mapping: Dict[str, str],
        available_tools: List[Dict[str, Any]],
        config: "BenchmarkConfig",
        max_iterations: int = 50,
        dogwood_policy: str | None = None,
        dogwood_schema: str | None = None,
        dogwood_bin: str = "dogwood",
        dogwood_timeout_seconds: float = 10.0,
    ):
        self.llm_client = llm_client
        self.mcp_clients = mcp_clients
        self.tool_to_server_mapping = tool_to_server_mapping
        self.available_tools = available_tools
        self.config = config
        self.max_iterations = max_iterations
        self._dogwood_gate = (
            DogwoodSafetyGate(
                dogwood_policy,
                available_tools,
                binary=dogwood_bin,
                schema_path=dogwood_schema,
                timeout_seconds=dogwood_timeout_seconds,
            )
            if dogwood_policy
            else None
        )

    @abstractmethod
    async def execute(self) -> Dict[str, Any]:
        """Execute the task and return results dict with keys:
        final_response, conversation_flow, tools_used, tool_results, messages
        """
        pass

    def get_result_metadata(self) -> Dict[str, Any]:
        """Return extra metadata to merge into the run result.
        Override in subclasses to surface orchestrator-specific telemetry
        (e.g. token usage, plan metadata).
        """
        if self._dogwood_gate is None:
            return {}
        return {"dogwood": self._dogwood_gate.metadata()}

    async def _execute_tool_call(
        self, tool_name: str, tool_args: Dict[str, Any]
    ) -> Dict[str, Any]:
        target_gym = self.tool_to_server_mapping.get(tool_name)

        if not target_gym:
            logger.error(f"Tool '{tool_name}' not in any gym's tool mapping")
            raise ValueError(
                f"Tool '{tool_name}' not found in available tool pool"
            ) from None

        client = self.mcp_clients[target_gym]

        if self._dogwood_gate is not None:
            decision = await asyncio.to_thread(
                self._dogwood_gate.authorize, tool_name, tool_args
            )
            if not decision.allowed:
                dogwood = decision.to_dict()
                error = (
                    f"DOGWOOD_DENIED: policy blocked tool '{tool_name}'. "
                    "Choose a policy-compliant action or explain why the request "
                    "cannot be completed."
                )
                if decision.errors:
                    error += f" Gate error: {decision.errors[0]}"
                logger.warning(
                    "Dogwood denied '%s' (rules=%s, errors=%s)",
                    tool_name,
                    decision.determining_rules,
                    decision.errors,
                )
                return {
                    "result": {
                        "success": False,
                        "error": error,
                        "result": {"error": error, "dogwood": dogwood},
                        "dogwood": dogwood,
                    },
                    "gym_server": target_gym,
                    "executed": False,
                    "dogwood": dogwood,
                }

        logger.info(f"Executing '{tool_name}' on '{target_gym}'")

        result = await client.call_tool(tool_name, tool_args)

        return {
            "result": result,
            "gym_server": target_gym,
            "executed": True,
            "dogwood": decision.to_dict() if self._dogwood_gate is not None else None,
        }
