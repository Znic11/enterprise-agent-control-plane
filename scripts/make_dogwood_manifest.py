#!/usr/bin/env python
"""生成 Dogwood 的**整域**工具清单 / Cedar action schema,并预检策略。

为什么需要它
------------
``orchestrators/dogwood_gate.py`` 默认用"运行时发现到的工具清单"现场生成
Cedar action schema。但基准在 ``--mode oracle`` / ``+N_tools`` 下只把**单题
需要的工具子集**交给 orchestrator(实测 email 一题仅 6 个工具),而策略是
**域级**产物 —— 它引用的动作(如 ``delete_label``)不在这 6 个里面时,Cedar
会判 ``unrecognized action`` 并让策略校验失败;门禁是 fail-closed 的,于是
整卷每个任务都在构造期报错、成功率恒为 0。

用法
----
    # 1) 只生成整域清单(不需要 dogwood CLI,离线可跑)
    python scripts/make_dogwood_manifest.py --domain email

    # 2) 顺手用 CLI 生成 action schema(需要 dogwood 可执行文件)
    python scripts/make_dogwood_manifest.py --domain email \
        --dogwood-bin .codex/bin/dogwood

    # 3) 生成后立刻预检策略(在跑评测之前就能确认策略能通过)
    python scripts/make_dogwood_manifest.py --domain email \
        --dogwood-bin .codex/bin/dogwood \
        --validate policies/dogwood/email_protect_system_labels.dw

    # 4) 一次把所有域都备好
    python scripts/make_dogwood_manifest.py --domain all --dogwood-bin .codex/bin/dogwood

产物(默认落在 ``policies/dogwood/``):
    <domain>.tools.json        整域工具清单(整域动作词表)
    <domain>.cedarschema       Cedar action schema(--dogwood-bin 给出时才生成)

评测时即可用预生成的 schema 完全绕开"运行时清单不完整"这条路:
    python evaluate.py ... --dogwood_policy policies/dogwood/<p>.dw \
        --dogwood_schema policies/dogwood/email.cedarschema
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrators.dogwood_gate import build_dogwood_manifest  # noqa: E402

DEFAULT_DUMP = REPO_ROOT / "tools_dump.json"
DEFAULT_OUT_DIR = REPO_ROOT / "policies" / "dogwood"


def load_dump(path: Path):
    """读工具 dump。既支持 ``[{...,_domain}]`` 列表,也支持 ``{domain: [...]}`` 字典。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        rows = []
        for domain, tools in data.items():
            for tool in tools:
                if isinstance(tool, dict):
                    rows.append({**tool, "_domain": tool.get("_domain", domain)})
        return rows
    if isinstance(data, list):
        return data
    raise ValueError(f"unsupported tool dump shape in {path}: {type(data).__name__}")


def domains_in(rows):
    return sorted({str(r.get("_domain", "")) for r in rows if isinstance(r, dict)} - {""})


def collect(rows, domains):
    picked = [r for r in rows if isinstance(r, dict) and str(r.get("_domain", "")) in domains]
    if not picked:
        raise SystemExit(
            f"no tools found for domain(s) {sorted(domains)}; "
            f"available: {domains_in(rows)}"
        )
    return picked


def run_cli(binary, args):
    completed = subprocess.run(
        [binary, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"dogwood {' '.join(args[:1])} failed ({completed.returncode}):\n{detail}")
    return completed.stdout


def main():
    parser = argparse.ArgumentParser(
        description="生成整域工具清单 / Cedar action schema,并预检策略。"
    )
    parser.add_argument("--domain", nargs="+", default=["all"],
                        help="一个或多个域(如 email teams),或 'all'(默认)。")
    parser.add_argument("--dump", type=Path, default=DEFAULT_DUMP,
                        help=f"工具 dump 路径(默认 {DEFAULT_DUMP.name})。")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                        help=f"产物输出目录(默认 {DEFAULT_OUT_DIR})。")
    parser.add_argument("--dogwood-bin", default=None,
                        help="dogwood 可执行文件;给出则生成 .cedarschema 与预检策略。")
    parser.add_argument("--policy-schema-dir", type=Path, default=None,
                        help="复用已存在的 .cedarschema 目录(默认与 --out-dir 相同)。")
    parser.add_argument("--validate", nargs="+", default=None,
                        help="生成 schema 后立刻校验这些 .dw 策略文件。")
    args = parser.parse_args()

    rows = load_dump(args.dump)
    available = domains_in(rows)
    domains = available if "all" in args.domain else args.domain
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"dump       : {args.dump}")
    print(f"domains    : {', '.join(domains)}")
    print(f"out dir    : {args.out_dir}")
    print()

    for domain in domains:
        picked = collect(rows, {domain})
        manifest = build_dogwood_manifest(picked)
        manifest_path = args.out_dir / f"{domain}.tools.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[{domain}] 清单 {len(manifest)} 个动作 -> {manifest_path}")

        if not args.dogwood_bin:
            continue

        schema_path = args.out_dir / f"{domain}.cedarschema"
        run_cli(
            args.dogwood_bin,
            ["schema", "mcp", "--manifest", str(manifest_path), "--output", str(schema_path)],
        )
        # 把 schema 里真实登记的动作数报出来 —— 这次事故的判别特征就是这个数
        # 远小于整域工具数。
        schema_text = schema_path.read_text(encoding="utf-8", errors="replace")
        registered = schema_text.count('action "') + schema_text.count("action '")
        print(f"[{domain}] schema -> {schema_path}"
              f"(登记 action 字面量 {registered} 个,清单 {len(manifest)} 个;"
              f"整域工具数 {len(picked)})")

        for policy in args.validate or []:
            report = run_cli(
                args.dogwood_bin,
                ["validate", policy, "--policy-schema", str(schema_path), "--format", "json"],
            )
            try:
                passed = json.loads(report).get("passed", False)
            except json.JSONDecodeError:
                print(f"[{domain}] {policy}: 无法解析 validate 输出:\n{report[:500]}")
                continue
            flag = "✅ passed" if passed else "❌ failed"
            print(f"[{domain}] {flag} {policy}")

    if not args.dogwood_bin:
        print()
        print("未给 --dogwood-bin,只生成了清单。要生成 schema 与预检策略请再加:")
        print("  --dogwood-bin <dogwood> [--validate <policy.dw> ...]")


if __name__ == "__main__":
    main()
