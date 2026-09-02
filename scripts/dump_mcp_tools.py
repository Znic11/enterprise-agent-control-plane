"""从 MCP server dump 真实工具池,生成 eval_router.py --tools 所需的 JSON。

在远程(udocker MCP server 在线)运行,不需要本地起 docker:

    # 一次一域(RUN_GUIDE 流程,轮流启动,自动合并进同一个文件):
    python3 scripts/dump_mcp_tools.py --url http://localhost:8005 --domain teams
    python3 scripts/dump_mcp_tools.py --url http://localhost:8005 --domain itsm
    python3 scripts/dump_mcp_tools.py --url http://localhost:8003 --domain calendar

    # 输出: tools_dump.json (list[{name, description, input_schema, _domain}])
    # 之后: python eval_router.py --tools tools_dump.json --top_k 20

按 name upsert 合并:重复跑同一域会覆盖旧条目,不同域依次累积,
最终得到全量 512 工具池。_domain 字段会被 eval_router 用于域内分池。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def load_existing(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "tools" in data:
        data = data["tools"]
    return data if isinstance(data, list) else []


def merge_tools(existing: list, new_tools: list, domain: str) -> list:
    """合并新 dump 的工具进已有列表。

    合并键 = (name, _domain):同域重复跑覆盖本域旧条目;
    不同域的同名工具各自保留(官方各域容器确实存在重名工具,
    按 name 全局去重会覆盖先 dump 域的条目并污染 _domain 标签)。
    """
    merged = {(t.get("_domain"), t["name"]): t
              for t in existing if isinstance(t, dict) and t.get("name")}
    for t in new_tools:
        entry = {
            "name": t.get("name"),
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema") or t.get("input_schema") or {},
            "_domain": domain,
        }
        merged[(domain, entry["name"])] = entry
    return sorted(merged.values(), key=lambda x: (x.get("_domain", ""), x["name"]))


def dump_one(url: str, domain: str, out_path: str) -> int:
    # 惰性导入:httpx 仅在真正连服务时才需要(merge/统计逻辑可无依赖单独测试)
    from benchmark.mcp_client import MCPClient

    async def _run() -> list:
        client = MCPClient(url)
        tools = await client.list_tools()
        return tools or []

    tools = asyncio.run(_run())
    if not tools:
        print(f"[FAIL] {domain}: {url} 未返回任何工具(服务未启动/端口不对?)")
        return 1

    out = merge_tools(load_existing(out_path), tools, domain)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    dom_count = sum(1 for t in out if t.get("_domain") == domain)
    dup = len({t["name"] for t in out}) and len(out) - len({t["name"] for t in out})
    print(f"[OK] {domain}: 本次 dump {len(tools)} 个工具;文件现有 {len(out)} 个工具"
          f"(本域累计 {dom_count},跨域重名 {dup} 个) -> {out_path}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Dump MCP tools for offline router eval")
    ap.add_argument("--url", required=True, help="MCP server 地址,如 http://localhost:8005")
    ap.add_argument("--domain", required=True, help="域名(teams/csm/email/itsm/calendar/hr/drive)")
    ap.add_argument("--out", default="tools_dump.json", help="输出文件(默认 tools_dump.json,自动合并)")
    args = ap.parse_args()

    sys.exit(dump_one(args.url, args.domain, args.out))


if __name__ == "__main__":
    main()
