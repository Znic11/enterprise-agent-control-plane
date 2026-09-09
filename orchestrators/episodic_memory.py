"""Episodic 记忆层(Phase 3 V1):确定性事实抽取 + recap 渲染。

设计权威: docs/memory_design.md。本模块只含**纯函数/数据结构**(无 LLM、无
消息循环耦合),MetaToolOrchestrator 持有记忆状态并在执行循环中调用本模块。

红线(硬性):
  * 事实只来自**成功业务工具的结果**(MCP 包裹 text 里的 JSON)与调用参数,
    绝不读取任务配置的 verifiers/selected_tools —— 与答案泄露同级禁止;
  * 抽取规则宁缺毋滥:非 JSON / 无实体 id / 列表容器不逐条记;
  * 确定性(纯规则,零 LLM),同一输入必得同一输出。

工具结果结构(实测 email 域, MCP 标准):
    tool_result = {"content": [{"type": "text", "text": "<json 字符串>"}],
                   "isError": false}
事实 = 系统确认的实体状态快照:
    (entity, attribute, value, source_tool, ts)
例如 get_message 返回含 id=msg_002、labelIds=[...] 的单实体:
    Fact(entity="msg_002", attribute="labels", ...) —— 列表值不记(非标量)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence

# ---------------------------------------------------------------------------
# 白名单(规则引擎的唯一知识;域无关,宁缺毋滥)
# ---------------------------------------------------------------------------

# 实体标识键:参数/结果 JSON 中这些键的值(短标量)被视为"实体 id"候选。
# 匹配规则:键完全等于候选,或以 "_<key>" 结尾(如 message_id -> id)。
_ID_KEY_CANDIDATES = (
    "id", "key", "key_id", "email", "account_id", "user_id", "sys_id",
    "guid", "uuid", "number", "case_id", "draft_id", "thread_id",
    "message_id", "label_id", "identity_id", "alias_id", "keypair_id",
    "attachment_id", "cse_keypair_id", "send_as_alias_id", "contact_id",
)
_ID_SUFFIX_SET = frozenset(_ID_KEY_CANDIDATES)

# 状态/确认属性键:结果 JSON 中这些键的标量值会被记录为实体属性。
_ATTR_KEYS = frozenset({
    "status", "state", "enabled", "active", "verified", "verifiedAt",
    "disposition", "accessWindow", "purpose", "type", "archived", "read",
})

# 写动作动词(工具名分词含这些词 -> 成功即"实体被系统确认写入")。
_WRITE_VERBS = frozenset({
    "create", "update", "patch", "delete", "add", "remove", "modify",
    "set", "enable", "disable", "send", "assign", "rename", "batch",
    "restore", "trash", "insert", "save", "post", "put", "verify",
})

# 终态(破坏性/生命周期终结)动词:命中同实体 -> 该实体全部有效事实作废
# (失效不删除,保审计;Graphiti "superseded ≠ deleted" 语义的规则版)。
# send 也视为终态:草稿被发出后不再以草稿状态存在。
_DESTRUCTIVE_VERBS = frozenset({
    "delete", "remove", "trash", "purge", "revoke", "send",
})

# recap 顶部护栏:提醒模型"早期结果可能已过期,行动前须回读"。
_RECAP_HEADER = ("[memory recap - earlier tool results; "
                 "may be stale - re-read before acting]")

# 渲染预算:失效(审计)区最多渲染条数;总长仍受 max_chars 约束。
_MAX_INVALID_RENDERED = 5

_SCALAR_TYPES = (str, int, float, bool)
_ENTITY_VALUE_MAX = 120      # 实体 id 值截断
_ATTR_VALUE_MAX = 200        # 属性值截断


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    """一条"系统确认事实":在轮次 ts,工具 source_tool 成功返回了
    entity 的 attribute=value。

    invalid=False 表示该事实**当前有效**;invalid=True 表示已被后续写操作
    覆盖/作废(失效不删除,保留审计 —— 支撑"曾存在/已删除"类验收)。
    """
    entity: str
    attribute: str
    value: str
    source_tool: str
    ts: int
    invalid: bool = False
    invalid_reason: str = ""

    def render(self) -> str:
        tag = "Invalidated" if self.invalid else "Confirmed"
        suffix = f" ({self.invalid_reason})" if self.invalid and self.invalid_reason else ""
        return f"{tag} (from {self.source_tool}): {self.entity} {self.attribute}={self.value}{suffix}"


# ---------------------------------------------------------------------------
# 纯函数:解包 / 抽取 / 渲染
# ---------------------------------------------------------------------------

def unwrap_mcp_result(result: Any) -> Optional[Dict[str, Any]]:
    """从 MCP 工具结果解出数据 dict。

    兼容两种形态:
      * MCP 包裹 {"content":[{"type":"text","text":"<json>"}], "isError":...}
      * 直接 dict(单测 / 未来 API 直接返回结构)
    返回 dict;非 dict / text 非 JSON / 空 -> None(宁缺毋滥)。
    """
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        # MCP 工具自身失败(isError=True):不抽(错误文本即使可解析也非确认事实)
        if result.get("isError") is True:
            return None
        for part in result["content"]:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    obj = json.loads(text)
                except (ValueError, TypeError):
                    return None
                return obj if isinstance(obj, dict) else None
        return None
    if isinstance(result, dict):
        return result
    return None


def _looks_like_id_key(key: str) -> bool:
    k = str(key).strip().lower()
    if k in _ID_SUFFIX_SET:
        return True
    # 支持 message_id / msg_id 等后缀形态
    return any(k.endswith(f"_{c}") for c in _ID_SUFFIX_SET)


def _find_entity_in_args(args: Optional[Dict[str, Any]]) -> Optional[str]:
    """从调用参数里找实体 id(键命中 id 白名单且值为短标量)。"""
    if not isinstance(args, dict):
        return None
    for key, val in args.items():
        if not isinstance(val, _SCALAR_TYPES) or val is True or val is False:
            continue
        s = str(val).strip()
        if not s or len(s) > _ENTITY_VALUE_MAX or s.startswith(("{", "[")):
            continue
        if _looks_like_id_key(key):
            return s
    return None


def _find_entity_in_result(obj: Dict[str, Any]) -> Optional[str]:
    """从结果 dict 找实体 id:顶层 id 键优先;否则单实体容器(值 dict 含 id)。"""
    for key, val in obj.items():
        if not isinstance(val, _SCALAR_TYPES) or val is True or val is False:
            continue
        s = str(val).strip()
        if not s or len(s) > _ENTITY_VALUE_MAX:
            continue
        if _looks_like_id_key(key):
            return s
    # 单实体容器:{message: {id: ...}} 等 —— 只找"值是 dict 且内含 id"的键
    for key, val in obj.items():
        if isinstance(val, dict) and not isinstance(val, list):
            inner = _find_entity_in_result(val)
            if inner:
                return inner
    return None


def _collect_attrs(obj: Dict[str, Any], limit: int = 3) -> List[tuple]:
    """收集实体自身的状态属性(顶层标量,键命中 _ATTR_KEYS)。"""
    out: List[tuple] = []
    for key, val in obj.items():
        if key not in _ATTR_KEYS:
            continue
        if not isinstance(val, _SCALAR_TYPES):
            continue
        if val is True:
            s = "true"
        elif val is False:
            s = "false"
        else:
            s = str(val).strip()
        if not s or len(s) > _ATTR_VALUE_MAX:
            continue
        out.append((key, s))
        if len(out) >= limit:
            break
    return out


def _token_pos(name: str, verbs: frozenset) -> int:
    """返回工具名分词中第一个命中动词集的 token 下标;无则 -1。
    支持域前缀(email_create_*):扫描全部 token 而非只看首 token。"""
    toks = str(name or "").lower().split("_")
    for i, tk in enumerate(toks):
        if tk in verbs:
            return i
    return -1


def _tool_has_write_verb(tool_name: str) -> bool:
    return _token_pos(tool_name, _WRITE_VERBS) >= 0


def _is_destructive_tool(tool_name: str) -> bool:
    """终态动词(delete/remove/trash/purge/revoke/send):命中实体 = 实体
    生命周期终结/转移,旧状态不再为真。"""
    return _token_pos(tool_name, _DESTRUCTIVE_VERBS) >= 0


def _is_create_like(tool_name: str) -> bool:
    """新建类动词(create/add/insert/new):无目标实体 id,应记"返回的新 id"。"""
    return _token_pos(tool_name, frozenset(("create", "add", "insert", "new"))) >= 0


def extract_facts(
    tool_name: str,
    args: Optional[Dict[str, Any]],
    result: Any,
    ts: int,
    max_per_call: int = 3,
) -> List[Fact]:
    """从一次成功业务工具调用中抽取事实(确定性、零 LLM)。

    规则(宁缺毋滥,见 docs/memory_design.md §2.1):
      1. 解包 MCP text -> dict;失败/非 dict -> 空;
      2. 实体 id:优先参数(键命中 id 白名单),否则结果顶层/单实体容器;
      3. 属性:结果 dict 中命中状态白名单的标量键(至多 limit 条);
      4. 仅当结果含列表容器(list_*/search_* 形态)时:顶层若只有数组 -> 不记
         (不逐条记);单实体容器(值 dict)不受影响;
      5. 产出上限 max_per_call 条;参数与结果均无实体 id -> 空(宁缺毋滥)。
    """
    obj = unwrap_mcp_result(result)
    if obj is None:
        return []
    # 顶层是"纯数组容器"(如 {"messages":[...]})且无单实体 id -> 不记。
    # 实体优先序:新建类(create/add/insert)记"返回的新 id";其余记参数目标
    # 实体(id 键命中);都没有再从结果单实体容器取。
    if _is_create_like(tool_name):
        entity = _find_entity_in_result(obj) or _find_entity_in_args(args)
    else:
        entity = _find_entity_in_args(args) or _find_entity_in_result(obj)
    if not entity:
        return []

    facts: List[Fact] = []
    for attr, val in _collect_attrs(obj):
        facts.append(Fact(entity=entity, attribute=attr, value=val,
                          source_tool=tool_name, ts=ts))
        if len(facts) >= max_per_call:
            break
    # 写动作成功且没有可记录的状态属性:确认实体已被系统写入
    if not facts and _tool_has_write_verb(tool_name):
        facts.append(Fact(entity=entity, attribute="confirmed", value="true",
                          source_tool=tool_name, ts=ts))
    return facts[:max_per_call]


def reconcile_facts(
    facts: Sequence[Fact],
    new_fact: Fact,
    max_facts: int = 40,
) -> List[Fact]:
    """规则版"写时协调"(零 LLM;Mem0 ADD/UPDATE/DELETE 的确定性四操作子集):

      1. **destructive**(delete/remove/trash/purge/revoke/send)命中实体
         -> 该实体所有有效事实**失效不删除**(保审计,支撑删除型验收);
         破坏性调用本身不产生新状态事实 -> 不 append;
      2. 同 entity + 同 attribute + **不同值** -> 旧事实失效
         (reason=superseded by <tool>),append 新值;
      3. 同 entity + 同 attribute + **同值** -> 去重,不 append;
      4. 其余(新实体/新属性/回读确认) -> append。

    超限裁剪(总量 <= max_facts):**先丢最旧 invalid,再丢最旧 valid**
    (审计区保留近期失效,有效事实优先保新)。纯函数,不改入参,返回新列表。
    """
    out = list(facts)

    if _is_destructive_tool(new_fact.source_tool):
        out = [
            replace(f, invalid=True,
                    invalid_reason=f"{f.entity} removed/changed by "
                                   f"{new_fact.source_tool} @ts{new_fact.ts}")
            if (not f.invalid and f.entity == new_fact.entity) else f
            for f in out
        ]
        # 破坏性结果无新状态可记(delete 成功 ≠ 实体仍存在)
    else:
        duplicate = any(
            not f.invalid
            and f.entity == new_fact.entity
            and f.attribute == new_fact.attribute
            and f.value == new_fact.value
            for f in out
        )
        if duplicate:
            return list(out)
        out = [
            replace(f, invalid=True,
                    invalid_reason=f"superseded by {new_fact.source_tool} "
                                   f"@ts{new_fact.ts}")
            if (not f.invalid
                and f.entity == new_fact.entity
                and f.attribute == new_fact.attribute)
            else f
            for f in out
        ]
        out.append(new_fact)

    # 预算裁剪:先丢最旧 invalid(审计),仍超再丢最旧 valid
    if len(out) > max_facts:
        overflow = len(out) - max_facts
        invalid = sorted((f for f in out if f.invalid), key=lambda f: f.ts)
        valid = sorted((f for f in out if not f.invalid), key=lambda f: f.ts)
        while overflow > 0 and invalid:
            invalid.pop(0)
            overflow -= 1
        while overflow > 0 and valid:
            valid.pop(0)
            overflow -= 1
        out = sorted(valid + invalid, key=lambda f: f.ts)
    return out


def active_keys(facts: Sequence[Fact], limit: int = 8) -> str:
    """当前有效实体的关键值带:每个实体取最新一条事实 -> 'entity (attr=value)'。

    作用:折叠后模型仍需引用精确 id/状态,摘要可丢过程文本但**后续必引用值
    显式保留**(对照 Anthropic compaction 的 ✅/❌ 清单)。
    """
    latest: Dict[str, Fact] = {}
    for f in sorted((x for x in facts if not x.invalid), key=lambda x: x.ts):
        latest[f.entity] = f          # 同 entity 保留 ts 最新
    if not latest:
        return ""
    ordered = sorted(latest.values(), key=lambda x: x.ts, reverse=True)
    seg = "; ".join(
        f"{f.entity} ({f.attribute}={f.value})" for f in ordered[:limit]
    )
    return f"Active: {seg}"


def render_recap(
    facts: Sequence[Fact],
    max_chars: int = 1500,
    max_invalid_rendered: int = _MAX_INVALID_RENDERED,
) -> str:
    """把事实库渲染成单条 recap(整条快照语义,供原位替换旧 recap)。

    分区(预算内):
      * 顶部护栏(可能过期 + 需回读);
      * Active 关键值带(仅有效实体);
      * 有效事实(ts 倒序,原行格式);
      * 失效/审计区(限量,标注原因 —— 支撑"曾存在/已删除"核对)。
    全空 -> 返回空串(调用方据此跳过折叠)。
    """
    valid = sorted((f for f in facts if not f.invalid),
                   key=lambda f: f.ts, reverse=True)
    invalid = sorted((f for f in facts if f.invalid),
                     key=lambda f: f.ts, reverse=True)
    if not valid and not invalid:
        return ""

    lines = [_RECAP_HEADER]
    ak = active_keys(facts)
    if ak:
        lines.append(ak)
    lines.extend(f.render() for f in valid)
    if invalid:
        lines.append("-- invalidated (audit; no longer current) --")
        lines.extend(f.render() for f in invalid[:max_invalid_rendered])
        if len(invalid) > max_invalid_rendered:
            lines.append(f"…({len(invalid) - max_invalid_rendered} more "
                         "invalidated)")
    recap = "\n".join(lines)
    if len(recap) > max_chars:
        recap = recap[:max_chars] + "\n…(truncated)"
    return recap
