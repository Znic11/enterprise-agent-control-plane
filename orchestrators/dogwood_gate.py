"""Dogwood policy enforcement at the MCP tool-call boundary.

This adapter intentionally uses the official ``dogwood`` CLI.  It is a small
prototype bridge for the Python benchmark runner; Dogwood remains the policy
engine and receives the complete request history on every decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class DogwoodConfigurationError(RuntimeError):
    """Raised when an enabled Dogwood gate cannot be initialized safely."""


# trace 里 principal / resource 的实体 UID。这两个名字来自 Dogwood 的模板,而
# 模板随 CLI 版本变:
#   * 指南版(较新)  namespace Drupe { entity OAuthUser { id: String } tags String;
#                                    entity Gateway; ... }
#   * 1.0.0 (commit c6237c8 那版二进制) 内置模板是 entity User / entity Gateway,
#     二进制里搜不到 OAuthUser 这个串。
# 策略一般写 bare `principal`,不约束实体类型,所以不影响 validate;但 trace 里
# 的 principal 若指向 schema 未声明的实体类型,某些构建可能在 replay 阶段报错 ->
# fail-closed deny -> 所有工具调用被拒(任务不可达)。故做成可配置,默认值保持
# 历史行为不变。
DEFAULT_PRINCIPAL = 'Drupe::OAuthUser::"enterpriseops-agent"'
DEFAULT_RESOURCE = 'Drupe::Gateway::"enterpriseops-gym"'


@dataclass(frozen=True)
class DogwoodDecision:
    allowed: bool
    verdict: str
    determining_rules: List[int]
    errors: List[str]
    request_id: str
    timestamp: int
    latency_ms: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _quote(value: str) -> str:
    """Render a Dogwood/Cedar-compatible quoted string."""
    out: List[str] = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            out.append(r'\"')
        elif char == "\\":
            out.append(r"\\")
        elif char == "\n":
            out.append(r"\n")
        elif char == "\r":
            out.append(r"\r")
        elif char == "\t":
            out.append(r"\t")
        elif code == 0:
            out.append(r"\0")
        elif code < 32 or code == 127:
            out.append(f"\\u{{{code:x}}}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _value(value: Any, depth: int = 0) -> str:
    """Serialize Python JSON-like values into Dogwood's trace syntax."""
    if depth > 64:
        raise ValueError("tool arguments are nested deeper than 64 levels")
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not valid Dogwood values")
        rendered = repr(value)
        return rendered if "." in rendered else f"{rendered}.0"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(item, depth + 1) for item in value) + "]"
    if isinstance(value, dict):
        fields = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Dogwood record keys must be strings")
            fields.append(f"{_quote(key)}: {_value(item, depth + 1)}")
        return "{" + ", ".join(fields) + "}"
    raise ValueError(f"unsupported Dogwood value type: {type(value).__name__}")


def _clean_manifest(tools: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only MCP manifest fields understood by Dogwood's generator."""
    by_name: Dict[str, Dict[str, Any]] = {}
    for tool in tools:
        name = str(tool.get("name", "")).strip()
        if not name:
            continue
        item: Dict[str, Any] = {
            "name": name,
            "description": str(tool.get("description", "")),
            "inputSchema": tool.get("inputSchema")
            or tool.get("input_schema")
            or {"type": "object", "properties": {}},
        }
        output_schema = tool.get("outputSchema") or tool.get("output_schema")
        if output_schema:
            item["outputSchema"] = output_schema
        by_name[name] = item
    return list(by_name.values())


def build_dogwood_manifest(tools: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """公开入口:把工具清单整理成 ``dogwood schema mcp`` 能吃的 manifest。

    与门禁内部生成 schema 时用的是同一个函数,保证"离线预生成 schema"与
    "运行时自动生成 schema"的形状完全一致(否则 --dogwood_schema 与本模块
    会各说各话)。
    """
    return _clean_manifest(tools)


class DogwoodSafetyGate:
    """Fail-closed authorization gate backed by the official Dogwood CLI."""

    def __init__(
        self,
        policy_path: str,
        available_tools: Iterable[Dict[str, Any]],
        *,
        binary: str = "dogwood",
        schema_path: Optional[str] = None,
        timeout_seconds: float = 10.0,
        principal: str = DEFAULT_PRINCIPAL,
        resource: str = DEFAULT_RESOURCE,
    ) -> None:
        """``available_tools`` 应当是**整域工具池**,不是单任务可见子集。

        策略是域级产物,动作词表必须覆盖整个域。若用单任务可见集(oracle 模式
        实测 email 一题仅 6 个工具)生成 schema,策略里引用到的其他动作会被
        Cedar 判为 unrecognized action,校验失败 -> fail-closed -> 整卷每个
        任务都在构造期报错。
        """
        self.policy_path = Path(policy_path).expanduser().resolve()
        if not self.policy_path.is_file():
            raise DogwoodConfigurationError(
                f"Dogwood policy does not exist: {self.policy_path}"
            )
        self.binary = self._resolve_binary(binary)
        self.timeout_seconds = timeout_seconds
        if timeout_seconds <= 0:
            raise DogwoodConfigurationError("Dogwood timeout must be greater than zero")
        self.principal = str(principal).strip()
        self.resource = str(resource).strip()
        if not self.principal or not self.resource:
            raise DogwoodConfigurationError(
                "Dogwood principal and resource must be non-empty entity UIDs"
            )

        self._tmp = tempfile.TemporaryDirectory(prefix="enterpriseops-dogwood-")
        self._workdir = Path(self._tmp.name)
        self._trace_path = self._workdir / "trace.log"
        self._history: List[str] = []
        self._session_id = uuid.uuid4().hex
        self._checks = 0
        self._allowed = 0
        self._denied = 0
        self._failures = 0
        self._latency_ms = 0
        self._audit: List[Dict[str, Any]] = []

        # 整域动作词表(清洗后的 MCP 清单形状)。同时用于生成 schema 与审计遥测。
        self._schema_tools = _clean_manifest(available_tools)

        if schema_path:
            self.schema_path = Path(schema_path).expanduser().resolve()
            if not self.schema_path.is_file():
                raise DogwoodConfigurationError(
                    f"Dogwood action schema does not exist: {self.schema_path}"
                )
            self._generated_schema = False
        else:
            if not self._schema_tools:
                raise DogwoodConfigurationError(
                    "Dogwood cannot generate an action schema from an empty tool list"
                )
            manifest_path = self._workdir / "tools.json"
            manifest_path.write_text(
                json.dumps(self._schema_tools, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self.schema_path = self._workdir / "tools.cedarschema"
            self._run(
                [
                    "schema",
                    "mcp",
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(self.schema_path),
                ],
                purpose="generate the MCP action schema",
            )
            self._generated_schema = True

        validation = self._run(
            [
                "validate",
                str(self.policy_path),
                "--policy-schema",
                str(self.schema_path),
                "--format",
                "json",
            ],
            purpose="validate the policy",
        )
        try:
            report = json.loads(validation.stdout)
        except json.JSONDecodeError as exc:
            raise DogwoodConfigurationError(
                f"Dogwood returned invalid validation JSON: {validation.stdout[:500]}"
            ) from exc
        if not report.get("passed", False):
            raise DogwoodConfigurationError(
                f"Dogwood policy validation failed: {json.dumps(report, ensure_ascii=False)}"
            )

        self.policy_sha256 = hashlib.sha256(self.policy_path.read_bytes()).hexdigest()

    @staticmethod
    def _resolve_binary(binary: str) -> str:
        candidate = Path(binary).expanduser()
        if candidate.parent != Path(".") or candidate.is_absolute():
            resolved = candidate.resolve()
            if resolved.is_file():
                return str(resolved)
        found = shutil.which(binary)
        if found:
            return found
        raise DogwoodConfigurationError(f"Dogwood executable was not found: {binary}")

    def _run(self, args: List[str], *, purpose: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DogwoodConfigurationError(f"Dogwood failed to {purpose}: {exc}") from exc
        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout).strip()
            raise DogwoodConfigurationError(
                f"Dogwood failed to {purpose} (exit {completed.returncode}): "
                f"{details[:2000]}"
            )
        return completed

    def _request_line(
        self, tool_name: str, tool_args: Dict[str, Any], request_id: str, timestamp: int
    ) -> str:
        principal = self.principal
        resource = self.resource
        input_record = _value(tool_args)
        return (
            f"@{timestamp} scope(principal: {principal}, resource: {resource}) "
            f"request_context(input: {input_record}) "
            f"Drupe::Action::{_quote(tool_name)}::request("
            f"input: {input_record}, callerPrincipal: {principal}, "
            f"callerResource: {resource}, requestId: {_quote(request_id)}, "
            f"sessionId: {_quote(self._session_id)})"
        )

    def authorize(self, tool_name: str, tool_args: Dict[str, Any]) -> DogwoodDecision:
        """Authorize and record one proposed tool call.

        Any adapter, trace, or engine error is converted into a deny decision.
        A successfully evaluated request is retained in history, including a
        denied request, so temporal policies can count attempted actions.
        """
        started = time.perf_counter()
        request_id = f"tool-{self._checks + 1}-{uuid.uuid4().hex[:10]}"
        timestamp = int(time.time())
        verdict = "deny"
        rules: List[int] = []
        errors: List[str] = []
        evaluated = False
        try:
            if not isinstance(tool_args, dict):
                raise ValueError("tool arguments must be a JSON object")
            request_line = self._request_line(
                str(tool_name), tool_args, request_id, timestamp
            )
            self._trace_path.write_text(
                "\n".join([*self._history, request_line]) + "\n", encoding="utf-8"
            )
            completed = self._run(
                [
                    "replay",
                    str(self.policy_path),
                    "--policy-schema",
                    str(self.schema_path),
                    "--trace",
                    str(self._trace_path),
                    "--format",
                    "json",
                ],
                purpose="authorize a tool call",
            )
            report = json.loads(completed.stdout)
            verdicts = report.get("verdicts") or []
            if not verdicts:
                raise ValueError("Dogwood replay returned no decision")
            current = verdicts[-1]
            verdict = str(current.get("verdict", "deny")).lower()
            rules = [int(item) for item in current.get("determining_rules", [])]
            errors = [str(item) for item in current.get("errors", [])]
            if verdict not in {"allow", "deny"}:
                raise ValueError(f"unknown Dogwood verdict: {verdict!r}")
            self._history.append(request_line)
            evaluated = True
        except Exception as exc:  # fail closed at the security boundary
            errors = [f"{type(exc).__name__}: {exc}"]
            self._failures += 1

        latency_ms = int((time.perf_counter() - started) * 1000)
        allowed = evaluated and verdict == "allow" and not errors
        self._checks += 1
        self._latency_ms += latency_ms
        if allowed:
            self._allowed += 1
        else:
            self._denied += 1

        decision = DogwoodDecision(
            allowed=allowed,
            verdict="allow" if allowed else "deny",
            determining_rules=rules,
            errors=errors,
            request_id=request_id,
            timestamp=timestamp,
            latency_ms=latency_ms,
        )
        self._audit.append({"tool_name": tool_name, **decision.to_dict()})
        return decision

    def metadata(self) -> Dict[str, Any]:
        return {
            "enabled": True,
            "adapter": "official-cli-replay",
            "policy_path": str(self.policy_path),
            "policy_sha256": self.policy_sha256,
            "schema_path": str(self.schema_path),
            "schema_generated_from_mcp": self._generated_schema,
            "principal": self.principal,
            "resource": self.resource,
            "action_scope_tools": len(self._schema_tools),
            "checks": self._checks,
            "allowed": self._allowed,
            "denied": self._denied,
            "engine_failures": self._failures,
            "latency_ms_total": self._latency_ms,
            "latency_ms_avg": (
                round(self._latency_ms / self._checks, 2) if self._checks else 0.0
            ),
            "decisions": list(self._audit),
        }
