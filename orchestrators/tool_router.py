"""Tool Router — Phase 1 core module.

Route the top-k most relevant tools from the FULL tool set before they are
exposed to the executing LLM. Goals:

1. Cut context noise: the average task only needs 5-15 tools out of ~512.
2. Cut selection ambiguity: fewer tools => less hallucinated/irrelevant calls.
3. Cheap: keyword matching is free; LLM routing is optional and pluggable.

Design notes
------------
* This module intentionally has ZERO third-party dependencies (stdlib only),
  so it can be unit-tested / offline-evaluated without installing langchain
  or touching the benchmark stack.
* LLM routing is injected as a plain callable ``llm_call_fn(prompt) -> str``.
  The orchestrator wires it to an LLMClient; the offline eval script wires
  it to a mock or skips it entirely (keyword fallback only).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 20

# Words too generic to be useful signals for keyword routing.
# NOTE: keep enterprise entity/action nouns OUT of this list (case, record,
# create, update, find, ...) — they are exactly the tokens tool names are
# built from. Only grammatical words and task-side imperatives go here.
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


def tokenize(text: str) -> List[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords & short tokens."""
    words = re.split(r"[^a-zA-Z0-9]+", text.lower())
    return [w for w in words if w and w not in STOPWORDS and len(w) >= 3]


def tool_signature(tool: Dict[str, Any]) -> str:
    """A compact, routing-oriented signature of a tool."""
    name = str(tool.get("name", ""))
    desc = str(tool.get("description", ""))
    return f"{name}: {desc}"


def keyword_score(tool: Dict[str, Any], task_tokens: set) -> float:
    """Score a tool by keyword overlap between its name/description and the task.

    The tool NAME is the strongest signal (tool names are domain-specific,
    e.g. create_new_case, update_entitlement), so it is weighted more.
    """
    name_tokens = set(tokenize(tool.get("name", "")))
    desc_tokens = set(tokenize(tool.get("description", "")))
    name_hits = len(name_tokens & task_tokens)
    desc_hits = len(desc_tokens & task_tokens)
    # Name hits count double; normalize slightly to prefer exact coverage.
    return 2.0 * name_hits + 0.8 * desc_hits


def route_keywords(
    task_text: str,
    all_tools: List[Dict[str, Any]],
    top_k: int = DEFAULT_TOP_K,
) -> List[str]:
    """Zero-cost routing: rank tools by keyword overlap with the task."""
    task_tokens = set(tokenize(task_text))
    if not task_tokens or not all_tools:
        return []
    scored = sorted(
        all_tools,
        key=lambda t: keyword_score(t, task_tokens),
        reverse=True,
    )
    # Only keep tools with at least one signal hit, so we never route
    # purely-random tools; if fewer than top_k remain, that is fine
    # (the orchestrator still gets a meaningful subset).
    hits = [t["name"] for t in scored if keyword_score(t, task_tokens) > 0]
    return hits[:top_k]


ROUTER_PROMPT = """You are a tool router for an enterprise agent. Given a user task and a list of available tools, select the most relevant tools needed to complete the task.

Rules:
- Select tools you would actually call for this task, including read-only lookups.
- Prefer precision over recall, but do not miss obviously required tools.
- Return ONLY a JSON array of tool names, no explanation, no markdown.

Task:
{task}

Tools:
{tools}

Return: ["tool_a", "tool_b", ...]"""


def parse_llm_route_response(text: str) -> Optional[List[str]]:
    """Extract a tool-name list from an LLM routing response (best effort)."""
    if not text:
        return None
    # 1. Try strict JSON parse of a top-level array.
    try:
        data = json.loads(text.strip())
        if isinstance(data, list):
            return [str(x) for x in data]
    except json.JSONDecodeError:
        pass
    # 2. Try to locate a [...] block inside the text.
    match = re.search(r"\[.*?\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                return [str(x) for x in data]
        except json.JSONDecodeError:
            pass
    # 3. Fall back to quoted strings found anywhere.
    names = re.findall(r'"([^"]+)"', text)
    if names:
        return names
    return None


def route_llm(
    task_text: str,
    all_tools: List[Dict[str, Any]],
    llm_call_fn: Callable[[str], str],
    top_k: int = DEFAULT_TOP_K,
) -> List[str]:
    """LLM-based routing with graceful degradation to keyword routing.

    ``llm_call_fn(prompt) -> str`` is injected by the caller; the router stays
    provider-agnostic. On any parse failure we fall back to route_keywords so
    a flaky LLM never bricks the run.
    """
    lines = []
    for tool in all_tools:
        name = str(tool.get("name", ""))
        desc = str(tool.get("description", "")).replace("\n", " ")[:120]
        lines.append(f"- {name}: {desc}")
    prompt = ROUTER_PROMPT.format(task=task_text, tools="\n".join(lines))
    try:
        raw = llm_call_fn(prompt)
        names = parse_llm_route_response(raw)
        if names is None:
            logger.warning("LLM router returned unparseable output; falling back to keywords")
            return route_keywords(task_text, all_tools, top_k)
        valid = {str(t.get("name")) for t in all_tools}
        names = [n for n in names if n in valid][:top_k]
        if not names:
            logger.warning("LLM router returned no valid tool names; falling back to keywords")
            return route_keywords(task_text, all_tools, top_k)
        return names
    except Exception as e:  # noqa: BLE001 — router must never crash the run
        logger.warning(f"LLM router failed ({e}); falling back to keywords")
        return route_keywords(task_text, all_tools, top_k)


@dataclass
class RouteResult:
    """Outcome of a routing pass + diagnostics used for audit & experiments."""

    selected: List[str] = field(default_factory=list)
    method: str = "keyword"  # "keyword" | "llm"
    full_tool_count: int = 0
    task_text: str = ""

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "router_method": self.method,
            "router_selected_count": len(self.selected),
            "router_selected": self.selected,
            "router_full_tool_count": self.full_tool_count,
        }


def route(
    task_text: str,
    all_tools: List[Dict[str, Any]],
    top_k: int = DEFAULT_TOP_K,
    llm_call_fn: Optional[Callable[[str], str]] = None,
    prefer_llm: bool = False,
) -> RouteResult:
    """Public entry point. Use keyword routing unless an LLM is preferred."""
    if prefer_llm and llm_call_fn is not None:
        selected = route_llm(task_text, all_tools, llm_call_fn, top_k)
        return RouteResult(
            selected=selected,
            method="llm",
            full_tool_count=len(all_tools),
            task_text=task_text,
        )
    selected = route_keywords(task_text, all_tools, top_k)
    return RouteResult(
        selected=selected,
        method="keyword",
        full_tool_count=len(all_tools),
        task_text=task_text,
    )
