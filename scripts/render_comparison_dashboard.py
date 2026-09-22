"""多版本结果对比看板:baseline 与各次修改的统计对比 + 逐任务跨版本对比。

背景与定位
----------
服务端原有 `scripts/render_results_dashboard.py` 只渲染**单卷**案例检查台
(输入 / 轨迹 / 输出 / 验证器 / 元数据),看不出"这次修改到底改好了哪些任务"。
本脚本在它的视觉语言与 markdown 渲染器基础上重写,输入改为**多个带标签的结果目录**,
输出一张自包含 HTML,回答三类问题:

  ① 各版本整体统计对比(baseline vs 每次修改):成功率 / 验证器通过率 / 耗时 / 遥测,
     并给出相对 baseline 的 pp 差;
  ② 逐任务跨版本对比:同一 task_* 在不同版本下 pass/fail/error 的矩阵,
     自动判定"修复 / 回退 / 抖动 / 完全稳定",支持只看有差异的任务;
  ③ 配对显著性:baseline↔各修改的 saved / regressed 明细与 McNemar 精确双侧 p 值。

额外内置的、本项目踩过坑因而值得自动化的检查
--------------------------------------------
  * **修复生效自检(fix signature)**:统计 tool_results 信封里有没有 `isError` 键、
    内层 isError=True 却被记成 success=True 的"掩蔽错误"条数、`meta_tool_name_repairs`、
    包装名失败用的是旧文案还是新文案 `Malformed tool name`、有没有 `executed` 字段。
    用于一眼确认"这一卷跑的到底是不是修好的代码"(09-18 那次判读就靠这套判据)。
  * **跨批可比性守卫**:两卷 error 数差异过大、或检索/注入遥测量级差 >1.5x 时给出告警。
    本项目已证实检索暴涨 / 工具发现暴跌属批次级环境漂移,跨批配对结论不可采信。
  * **噪声地板提示**:两卷成功率差 < 6.0pp 时标注"在运行间噪声内"(同代码两卷实测差 6.0pp)。
  * **验证器失败构成**:把失败验证器按 MISSING_STATE(actual==0)/ OVER_PROVISION /
    VALUE_MISMATCH 分类,判断"副作用没落地"占比是否变化。
  * 任务×版本矩阵与案例详情可互相跳转;矩阵可导出 CSV,整包 payload 可导出 JSON。

用法
----
**推荐:写一个 YAML 对比配置,一条命令出报告。** 产物名字跟着配置文件名走
(`out/email_修复前后对比.yaml` → `out/email_修复前后对比.html`)。

    # ① 先扫一遍现有结果目录,生成一份带注释的 YAML 模板(会把域与任务数打出来)
    python scripts/render_comparison_dashboard.py --init-config out/email_对比.yaml --scan out

    # ② 编辑它:给每个版本写清 name(标签)与 description(这次改了什么),
    #    并检查 baseline 标在哪一项;文件末尾附了"怎么加版本 / 怎么加一组"的写法

    # ③ 出报告
    python scripts/render_comparison_dashboard.py --config out/email_对比.yaml

配置长这样(单对比;`#` 后面是注释,可以随便加):

    title: "电子邮件域:修复前后对比"
    description: "一句话说明这组对比"
    domain: "email"                # 留空 "" 表示不按域过滤
    trajectory: "trim"             # full / trim / off
    trajectory_limit: 1200
    versions:
      - name: "修复前"
        dir: "out/email_fix_nomem/run_1"
        domain: "email"
        description: "未打补丁的原始实现"
        baseline: true
      - name: "修复后"
        dir: "out/email_fixed/run_1"
        description: "映射内层 isError + 修复包装名退化"
        expect_fixed: true         # 声明这卷应已带修复,没生效会告警

多域一起跑就换成 `comparisons: [...]`,每组各出一张看板,再额外生成一个
`<配置名>_index.html` 索引导航。`--init-config` 扫到多个域时会**自动按域分组** ——
不同域的题目集合不重叠,混成一个 versions: 再比,逐题矩阵会全是空的。
命令行参数(如 `--trajectory`)可覆盖配置里的同名项;多组模式下 `--out` 会自动
补上组名后缀,不会让各组互相覆盖。

**JSON 同样支持**(扩展名用 .json 即可,且不需要 PyYAML)。YAML 需要 `pip install pyyaml`
(项目依赖里的 langchain 已间接带上它,通常无需另装)。

**兼容手写模式**(不写配置文件):

    python scripts/render_comparison_dashboard.py \
        --version 修复前=out/email_fix_nomem/run_1 \
        --version 修复后=out/email_fixed/run_1 --domain email --out out/x.html

未指定 `--config` 时,若当前目录存在 `compare.yaml` / `compare.yml` / `compare.json` 会自动使用。

关键开关:`--trajectory full|trim|off`(轨迹体积)、`--trajectory-limit N`(trim 上限)、
`--report <前缀>`(摘要路径)、`--expect-fixed <标签>`(声明该卷应已带修复,否则告警)。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html as _html
import json
import math
import os
import re
import statistics
import sys
import unicodedata
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # YAML 配置需要 PyYAML;没有也能用 JSON 配置(见 load_config)
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

TREND_ORDER = {
    "完全稳定(失败)": 0,
    "完全稳定(错误)": 1,
    "回退": 2,
    "抖动": 3,
    "状态变动": 4,
    "修复": 5,
    "完全稳定(通过)": 6,
}

# 运行间噪声下限:同一份代码两次运行的 email 全量成功率差实测 6.0pp。
NOISE_FLOOR_PP = 6.0

VL_KEYS = (
    "vl_gate_rounds", "vl_read_calls_gate", "vl_no_read_reminders",
    "vl_correction_turns", "vl_plan_calls",
)
MEM_KEYS = (
    "mem_enabled", "mem_folds", "mem_facts", "mem_recap_chars",
    "mem_fold_rounds", "mem_invalid_facts",
)
# 参与均值统计的记忆层数值字段(布尔类如 mem_enabled 只进计数)
MEM_NUM_KEYS = ("mem_folds", "mem_facts", "mem_recap_chars",
                "mem_fold_rounds", "mem_invalid_facts")
META_KEYS = (
    "meta_tool_search_calls", "meta_tool_searches", "meta_tool_cache_hits",
    "meta_tool_hits_avg", "meta_tool_zero_hits", "meta_tool_injected",
)

# 修复前/后的判据文案(见 docs/fix_effect_review_0918.md)
OLD_WRAPPER_MSG = "does not exist in the tool pool"
NEW_WRAPPER_MSG = "Malformed tool name"

_TASK_RE = re.compile(r"task_[\w]+")
_DOMAIN_RE = re.compile(r"__(?P<dom>[a-z]+)__task_")


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _num(x: Any) -> Optional[float]:
    """把 int/float/bool/单元素 list/数字字符串统一成 float,取不到返回 None。"""
    if isinstance(x, (list, tuple)):
        return _num(x[0]) if x else None
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.strip())
        except ValueError:
            return None
    return None


def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def _dwidth(text: Any) -> int:
    """终端显示宽度(东亚全角字符算 2 列),用于对齐含中文的表格。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in str(text))


def _pad(text: Any, width: int, right: bool = False) -> str:
    text = str(text)
    gap = " " * max(0, width - _dwidth(text))
    return (gap + text) if right else (text + gap)


def _mean(vals: Sequence[float]) -> float:
    return float(sum(vals)) / len(vals) if vals else 0.0


def _median(vals: Sequence[float]) -> float:
    return float(statistics.median(vals)) if vals else 0.0


def _mcnemar_exact_p(b: int, n: int) -> float:
    """McNemar 精确双侧:翻转对总数 n,其中利于 treatment 的 b 个。
    p = 2 * P(X <= min(b, n-b)) under Binomial(n, 0.5),封顶 1.0。
    与 scripts/analyze_vloop_runs.py 保持同一实现。"""
    if n == 0:
        return 1.0
    k = min(b, n - b)
    p = 0.0
    for i in range(k + 1):
        p += math.comb(n, i) * (0.5 ** n)
    return min(1.0, 2.0 * p)


def _run_of_file(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """取该任务文件的代表 run:优先第一个无 error 的 run,否则 runs[0]。
    与 scripts/analyze_vloop_runs.py 同口径。"""
    runs = data.get("runs") or []
    if not runs:
        return None
    for r in runs:
        if not r.get("error"):
            return r
    return runs[0]


def _classify_error(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("timeout", "readtimeout", "timed out")):
        return "timeout"
    if any(k in t for k in ("overflow", "upstream connect", "unavailable")):
        return "upstream"
    if any(k in t for k in ("connection", "refused", "network", "resolve",
                            "econnreset", "dns")):
        return "network"
    if any(k in t for k in ("rate", "429", "quota", "too many")):
        return "rate_limit"
    return "other"


# ---------------------------------------------------------------------------
# 载入一个版本目录
# ---------------------------------------------------------------------------

def _iter_result_files(folder: str) -> List[str]:
    """递归找出 evaluate 的 results_*.json;跳过 `_` 开头的汇总文件。"""
    found: List[str] = []
    for root, _dirs, files in os.walk(folder):
        for fn in sorted(files):
            if not fn.endswith(".json"):
                continue
            if fn.startswith("_"):
                continue  # _failure_traces.json / _digest_*.json 之类
            if not fn.startswith("results_"):
                continue
            found.append(os.path.join(root, fn))
    return sorted(found)


def _task_key(basename: str) -> str:
    m = _TASK_RE.search(basename)
    return m.group(0) if m else basename.replace(".json", "")


def _domain_of(basename: str, data: Dict[str, Any]) -> str:
    m = _DOMAIN_RE.search(basename)
    if m:
        return m.group("dom")
    try:
        name = (data.get("benchmark_config") or {}).get("gym_servers", [{}])[0].get("name", "")
    except Exception:
        return ""
    return name.replace("gym-", "").replace("-mcp", "")


def _verifier_items(vr: Any) -> List[Dict[str, Any]]:
    """verification_results 可能是 {name: {...}} 或 [{name/...}]。统一成 list。"""
    items: List[Dict[str, Any]] = []
    if isinstance(vr, dict):
        for name, body in vr.items():
            if isinstance(body, dict):
                items.append({"name": body.get("name") or name, **body})
            else:
                items.append({"name": name, "passed": bool(body)})
    elif isinstance(vr, list):
        for i, body in enumerate(vr):
            if isinstance(body, dict):
                items.append({"name": body.get("name") or f"verifier_{i + 1}", **body})
    return items


def _classify_verifier(v: Dict[str, Any]) -> str:
    """验证器结果 → 通过 / 未落地的副作用 / 多做了 / 值不符 / 无结果。

    关键规则(见 docs/failure_analysis.md):actual == 0 一律判 MISSING_STATE,
    与 comparison_type 无关 —— 基准里 expected 多为 1,gym 返回 count 为 0。
    """
    if v.get("passed"):
        return "PASS"
    actual = _num(v.get("actual"))
    expected = _num(v.get("expected"))
    if actual is None:
        return "NO_RESULT"
    if actual == 0:
        return "MISSING_STATE"
    ct = str(v.get("comparison_type") or "").lower()
    if expected is not None and actual > expected and ct in ("equals", "equal", "=="):
        return "OVER_PROVISION"
    return "VALUE_MISMATCH"


def _fix_signature(run: Dict[str, Any]) -> Dict[str, Any]:
    """从一卷的 tool_results 里读出"修复是否生效"的特征指纹。"""
    tool_results = run.get("tool_results") or []
    inner_errors = 0
    masked_errors = 0
    envelope_with_iserror = 0
    executed_false = 0
    has_executed_field = False
    old_hits = 0
    new_hits = 0
    malformed_examples: List[str] = []

    for tr in tool_results:
        if not isinstance(tr, dict):
            continue
        env = tr.get("result")
        if not isinstance(env, dict):
            continue
        if "executed" in tr or "executed" in env:
            has_executed_field = True
        if tr.get("executed") is False or env.get("executed") is False:
            executed_false += 1
        if "isError" in env:
            envelope_with_iserror += 1

        inner = env.get("result")
        is_err = False
        if isinstance(inner, dict) and inner.get("isError") is True:
            is_err = True
        if env.get("isError") is True:
            is_err = True
        if is_err:
            inner_errors += 1
            if env.get("success") is True:
                masked_errors += 1  # M1 缺陷:内层失败被记成成功

        blob = json.dumps(env, ensure_ascii=False)
        if OLD_WRAPPER_MSG in blob:
            old_hits += 1
        if NEW_WRAPPER_MSG in blob:
            new_hits += 1
            if len(malformed_examples) < 5:
                nm = tr.get("tool_name") or (tr.get("arguments") or {}).get("name")
                malformed_examples.append(str(nm))

    name_repairs = run.get("meta_tool_name_repairs")
    # 生效判据:有内层错误时,必须存在于 isError 键且不存在被掩蔽的错误。
    effective = bool(envelope_with_iserror > 0 and masked_errors == 0) if inner_errors else True
    return {
        "envelope_iserror_keys": envelope_with_iserror,
        "inner_errors": inner_errors,
        "masked_errors": masked_errors,
        "name_repairs": None if name_repairs is None else int(_num(name_repairs) or 0),
        "dogwood_denied": executed_false,
        "has_executed_field": has_executed_field,
        "old_message_hits": old_hits,
        "new_message_hits": new_hits,
        "malformed_examples": malformed_examples,
        "effective": effective,
    }


def _case_metadata(run: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """把 run 里的遥测字段整理成可展示的键值对(比原看板的白名单更全)。"""
    meta: "OrderedDict[str, Any]" = OrderedDict()
    for k in ("meta_tool", "meta_tool_retrieval", "meta_tool_dispatch",
              "meta_tool_warmup_names"):
        v = run.get(k)
        if v not in (None, "", [], {}):
            meta[k] = v
    for k in META_KEYS:
        v = run.get(k)
        if v not in (None, "", [], {}):
            meta[k] = v
    for k in ("vl_enabled", "vl_forced_done", "vl_final_marker"):
        v = run.get(k)
        if v not in (None,):
            meta[k] = v
    for k in VL_KEYS:
        v = run.get(k)
        if v not in (None, 0, [], {}):
            meta[k] = v
    for k in MEM_KEYS:
        v = run.get(k)
        if v not in (None, 0, [], {}):
            meta[k] = v
    for k, v in extra.items():
        meta[k] = v
    return dict(meta)


def load_version(name: str, folder: str, domain_filter: Optional[str],
                 trajectory: str, trajectory_limit: int,
                 description: str = "", expect_fixed: bool = False) -> Dict[str, Any]:
    """载入一个版本目录 → 统计 + 遥测 + 修复指纹 + 逐任务记录 + 案例明细。

    name 是给人看的标签(可以写中文);description 描述这次修改做了什么,
    会直接出现在看板的版本总览、配对标题与案例卡片上。
    """
    if not os.path.isdir(folder):
        sys.exit(f"[compare] 不是目录: {folder}")

    files = _iter_result_files(folder)
    if not files:
        sys.exit(f"[compare] {folder} 下没有 results_*.json(注意跳过 _ 开头的汇总文件)")

    tasks: Dict[str, Dict[str, Any]] = {}
    cases: List[Dict[str, Any]] = []
    tax_all: Counter = Counter()
    tax_failed: Counter = Counter()
    errors: List[Dict[str, Any]] = []
    models: Counter = Counter()
    dispatches: Counter = Counter()
    retrievals: Counter = Counter()
    runtimes: List[float] = []
    tool_calls: List[float] = []
    tele: Dict[str, List[float]] = {k: [] for k in VL_KEYS + META_KEYS + MEM_NUM_KEYS}
    counts: Dict[str, int] = {k: 0 for k in (
        "vl_on", "vl_forced_done", "vl_final_marker", "mem_on",
        "meta_tool_zero_hits", "meta_tool_fallback_all")}
    fix_totals = Counter()
    envelopes: Counter = Counter()
    cs_success: List[float] = []
    cs_verifier: List[float] = []

    for path in files:
        basename = os.path.basename(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[compare] 跳过损坏文件 {basename}: {exc}", file=sys.stderr)
            continue

        run = _run_of_file(data)
        if run is None:
            print(f"[compare] 跳过无 runs 的文件 {basename}", file=sys.stderr)
            continue

        domain = _domain_of(basename, data)
        if domain_filter and domain != domain_filter:
            continue
        key = _task_key(basename)
        n_runs = len(data.get("runs") or [])
        any_run_error = any(bool(r.get("error")) for r in (data.get("runs") or []))

        err_text = str(run.get("error") or "")
        if err_text:
            status = "error"
        elif run.get("overall_success"):
            status = "pass"
        else:
            status = "fail"

        vs = run.get("verification_summary") or {}
        v_passed = int(_num(vs.get("passed")) or 0)
        v_total = int(_num(vs.get("total")) or 0)
        if v_total == 0:
            v_items_all = _verifier_items(run.get("verification_results"))
            v_total = len(v_items_all)
            v_passed = sum(1 for v in v_items_all if v.get("passed"))
        else:
            v_items_all = _verifier_items(run.get("verification_results"))

        for v in v_items_all:
            cat = _classify_verifier(v)
            tax_all[cat] += 1
            if cat != "PASS":
                tax_failed[cat] += 1

        ms = _num(run.get("execution_time_ms")) or 0.0
        runtimes.append(ms)
        used = list(run.get("tools_used") or [])
        tool_calls.append(len(used))

        for k in VL_KEYS + META_KEYS + MEM_NUM_KEYS:
            n = _num(run.get(k))
            if n is not None:
                tele[k].append(n)
        if run.get("vl_enabled"):
            counts["vl_on"] += 1
        if run.get("vl_forced_done"):
            counts["vl_forced_done"] += 1
        if run.get("vl_final_marker"):
            counts["vl_final_marker"] += 1
        if run.get("mem_enabled"):
            counts["mem_on"] += 1
        if run.get("meta_tool_zero_hits"):
            counts["meta_tool_zero_hits"] += 1
        if run.get("meta_tool_fallback_all"):
            counts["meta_tool_fallback_all"] += 1

        sig = _fix_signature(run)
        for k in ("envelope_iserror_keys", "inner_errors", "masked_errors",
                  "dogwood_denied", "old_message_hits", "new_message_hits"):
            fix_totals[k] += sig[k]
        if sig["name_repairs"] is not None:
            fix_totals["name_repairs"] += sig["name_repairs"]
        fix_totals["has_executed_field"] += 1 if sig["has_executed_field"] else 0
        for tr in (run.get("tool_results") or []):
            env = tr.get("result") if isinstance(tr, dict) else None
            if isinstance(env, dict):
                envelopes[",".join(sorted(env.keys()))] += 1

        cfg = data.get("benchmark_config") or {}
        models[str(cfg.get("model", ""))] += 1
        if run.get("meta_tool_dispatch"):
            dispatches[str(run.get("meta_tool_dispatch"))] += 1
        if run.get("meta_tool_retrieval"):
            retrievals[str(run.get("meta_tool_retrieval"))] += 1

        # compute_score.py 的口径:逐文件 statistics 的均值(mean-of-ratios)。
        # 注意 error 文件的 verifier_level_pass_rate 恒为 0,会把该均值拉低。
        stats = data.get("statistics") or {}
        cs_success.append(float(_num(stats.get("overall_success_rate")) or 0.0))
        cs_verifier.append(float(_num(stats.get("verifier_level_pass_rate")) or 0.0))

        if err_text:
            errors.append({"task": key, "class": _classify_error(err_text),
                           "text": err_text[:400]})

        tasks[key] = {
            "key": key,
            "domain": domain,
            "status": status,
            "verifier_passed": v_passed,
            "verifier_total": v_total,
            "execution_time_ms": ms,
            "n_runs": n_runs,
            "any_run_error": any_run_error,
            "source": path,
        }

        prompt = str(cfg.get("user_prompt") or "")
        flow = run.get("conversation_flow") or []
        if trajectory == "off":
            flow_out: List[Any] = []
        elif trajectory == "trim":
            flow_out = [_trim_turn(t, trajectory_limit) for t in flow]
        else:
            flow_out = flow

        cases.append({
            "id": f"{domain} / {key}" if domain else key,
            "version": name,
            "version_description": description,
            "task": key,
            "domain": domain,
            "status": status,
            "source": path,
            "prompt": prompt,
            "output": str(run.get("model_response") or ""),
            "error": err_text,
            "conversation": flow_out,
            "verifiers": [
                {"name": v.get("name"), "passed": bool(v.get("passed")),
                 "expected": v.get("expected"), "actual": v.get("actual"),
                 "comparison_type": v.get("comparison_type"),
                 "details": v.get("details"), "query": v.get("query"),
                 "category": _classify_verifier(v)}
                for v in v_items_all
            ],
            "verifier_passed": v_passed,
            "verifier_total": v_total,
            "execution_time_ms": ms,
            "started_at": str(run.get("started_at") or ""),
            "model": str(cfg.get("model", "")),
            "tools_used": used,
            "checklist": str(run.get("vl_checklist") or ""),
            "metadata": _case_metadata(run, {
                "task": key,
                "domain": domain,
                "n_runs": n_runs,
                "fix_effective": sig["effective"],
                "masked_errors": sig["masked_errors"],
                "inner_errors": sig["inner_errors"],
            }),
        })

    n_all = len(tasks)
    if n_all == 0:
        sys.exit(f"[compare] 版本 {name}({folder})在该域下没有任何任务"
                 f"(domain 过滤 = {domain_filter!r})。"
                 "请检查配置里的 domain 是否写错,或目录里确实没有该域的结果。")
    n_pass = sum(1 for t in tasks.values() if t["status"] == "pass")
    n_fail = sum(1 for t in tasks.values() if t["status"] == "fail")
    n_err = sum(1 for t in tasks.values() if t["status"] == "error")
    clean = n_all - n_err
    vp = sum(t["verifier_passed"] for t in tasks.values())
    vt = sum(t["verifier_total"] for t in tasks.values())

    telemetry = {k: round(_mean(v), 3) for k, v in tele.items() if v}
    telemetry.update({f"{k}_count": v for k, v in counts.items()})

    return {
        "name": name,
        "dir": folder,
        "description": description,
        "expect_fixed": bool(expect_fixed),
        "n_tasks": n_all,
        "pass": n_pass, "fail": n_fail, "error": n_err,
        "clean": clean,
        "success_rate_raw": (n_pass / n_all) if n_all else 0.0,
        "success_rate_clean": (n_pass / clean) if clean else 0.0,
        "verifier_passed": vp, "verifier_total": vt,
        "verifier_rate": (vp / vt) if vt else 0.0,
        "cs_success_rate": _mean(cs_success),
        "cs_verifier_rate": _mean(cs_verifier),
        "cs_files": len(cs_success),
        "runtime_mean_ms": _mean(runtimes),
        "runtime_median_ms": _median(runtimes),
        "runtime_max_ms": max(runtimes) if runtimes else 0.0,
        "runtime_total_ms": sum(runtimes),
        "tool_calls_mean": _mean(tool_calls),
        "fix": dict(fix_totals),
        "envelopes": dict(envelopes.most_common(5)),
        "taxonomy_all": dict(tax_all),
        "taxonomy_failed": dict(tax_failed),
        "errors": errors,
        "error_classes": dict(Counter(e["class"] for e in errors)),
        "telemetry": telemetry,
        "configs": {
            "models": dict(models),
            "dispatch": dict(dispatches),
            "retrieval": dict(retrievals),
        },
        "tasks": tasks,
        "cases": cases,
    }


def _trim_turn(turn: Any, limit: int) -> Any:
    """轨迹瘦身:截断 content / result / tool_call args,并给单轮一个硬上限。

    体积主要由超长工具返回(list_messages、search、SQL 结果)贡献,截断它们
    对"看模型为什么做错"影响最小;硬上限再兜一层,防止某轮携带超长元数据。
    """
    if not isinstance(turn, dict):
        return turn
    out = dict(turn)
    if isinstance(out.get("content"), str) and len(out["content"]) > limit:
        out["content"] = out["content"][:limit] + f"\n…[截断,原 {len(turn['content'])} 字符]"
    res = out.get("result")
    if res is not None:
        blob = json.dumps(res, ensure_ascii=False)
        if len(blob) > limit:
            out["result"] = {"_截断": True, "预览": blob[:limit], "_原始字符数": len(blob)}
    calls = out.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            args = call.get("args")
            if args is None:
                continue
            blob = json.dumps(args, ensure_ascii=False)
            if len(blob) > limit:
                call["args"] = {"_截断": True, "预览": blob[:limit], "_原始字符数": len(blob)}
    if len(json.dumps(out, ensure_ascii=False)) > 4 * limit:
        out.pop("usage_metadata", None)
        out.pop("response_metadata", None)
    return out


# ---------------------------------------------------------------------------
# 跨版本对比
# ---------------------------------------------------------------------------

def build_pairs(versions: List[Dict[str, Any]], baseline: str) -> List[Dict[str, Any]]:
    base = next(v for v in versions if v["name"] == baseline)
    b_ok = {k: (t["status"] == "pass") for k, t in base["tasks"].items()
            if t["status"] != "error"}
    pairs: List[Dict[str, Any]] = []
    for v in versions:
        if v["name"] == baseline:
            continue
        o_ok = {k: (t["status"] == "pass") for k, t in v["tasks"].items()
                if t["status"] != "error"}
        common = sorted(set(b_ok) & set(o_ok))
        saved, regressed, both_pass, both_fail = [], [], [], []
        for k in common:
            a, b = b_ok[k], o_ok[k]
            if a and b:
                both_pass.append(k)
            elif not a and not b:
                both_fail.append(k)
            elif not a and b:
                saved.append(k)
            else:
                regressed.append(k)
        n_disc = len(saved) + len(regressed)
        p = _mcnemar_exact_p(len(saved), n_disc)

        notes: List[str] = []
        err_a, err_b = base["error"], v["error"]
        frac_a = err_a / base["n_tasks"] if base["n_tasks"] else 0.0
        frac_b = err_b / v["n_tasks"] if v["n_tasks"] else 0.0
        if abs(err_a - err_b) > 2 or abs(frac_a - frac_b) > 0.05:
            notes.append(
                f"两卷 error 数差异较大({baseline}={err_a} vs {v['name']}={err_b}),"
                "跨批配对结论不可采信,建议同批重跑基线。")
        for stat_key, label in (("meta_tool_search_calls", "检索次数"),
                                ("meta_tool_injected", "注入工具数")):
            a = base["telemetry"].get(stat_key)
            b = v["telemetry"].get(stat_key)
            if a and b:
                ratio = max(a, b) / min(a, b)
                if ratio >= 1.5:
                    notes.append(
                        f"{label}遥测相差 {ratio:.2f}x({baseline}={a:.2f} vs "
                        f"{v['name']}={b:.2f}),疑批次级环境漂移,非本次修改所致。")
        delta_pp = (v["success_rate_raw"] - base["success_rate_raw"]) * 100
        if abs(delta_pp) < NOISE_FLOOR_PP and n_disc > 0:
            notes.append(
                f"成功率差 {delta_pp:+.1f}pp,小于运行间噪声下限 {NOISE_FLOOR_PP:.1f}pp,"
                "单看总分不足以判定效果。")

        pairs.append({
            "baseline": baseline,
            "variant": v["name"],
            "baseline_desc": base.get("description", ""),
            "variant_desc": v.get("description", ""),
            "paired": len(common),
            "saved": len(saved), "regressed": len(regressed),
            "both_pass": len(both_pass), "both_fail": len(both_fail),
            "mcnemar_p": p,
            "mcnemar_sig": p < 0.05,
            "saved_tasks": saved,
            "regressed_tasks": regressed,
            "delta_pp": delta_pp,
            "comparable": not notes,
            "notes": notes,
        })
    return pairs


def build_matrix(versions: List[Dict[str, Any]], baseline: str) -> List[Dict[str, Any]]:
    """任务 × 版本 状态矩阵,附趋势判定与案例索引。"""
    names = [v["name"] for v in versions]
    all_keys = sorted({k for v in versions for k in v["tasks"]})
    base = next(v for v in versions if v["name"] == baseline)
    outline_versions = [n for n in names if n != baseline]

    case_index: Dict[Tuple[str, str], int] = {}
    for i, c in enumerate(c for v in versions for c in v["cases"]):
        case_index[(c["version"], c["task"])] = i

    rows: List[Dict[str, Any]] = []
    for key in all_keys:
        status: Dict[str, str] = {}
        verifier: Dict[str, List[int]] = {}
        runtime: Dict[str, float] = {}
        idx: Dict[str, int] = {}
        domain = ""
        prompt = ""
        for v in versions:
            t = v["tasks"].get(key)
            if not t:
                status[v["name"]] = "absent"
                continue
            status[v["name"]] = t["status"]
            verifier[v["name"]] = [t["verifier_passed"], t["verifier_total"]]
            runtime[v["name"]] = t["execution_time_ms"]
            if (v["name"], key) in case_index:
                idx[v["name"]] = case_index[(v["name"], key)]
            if not domain:
                domain = t["domain"]
        for c in base["cases"]:
            if c["task"] == key and c["prompt"]:
                prompt = c["prompt"]
                break

        present = [status[n] for n in names if status.get(n) != "absent"]
        if len(set(present)) == 1 and present:
            st = present[0]
            trend = {"pass": "完全稳定(通过)", "fail": "完全稳定(失败)",
                     "error": "完全稳定(错误)"}.get(st, "状态变动")
        else:
            b_st = status.get(baseline, "absent")
            saved = any(status.get(n) == "pass" and b_st in ("fail", "error")
                        for n in outline_versions)
            regressed = any(status.get(n) == "fail" and b_st == "pass"
                            for n in outline_versions)
            if saved and regressed:
                trend = "抖动"
            elif saved:
                trend = "修复"
            elif regressed:
                trend = "回退"
            else:
                trend = "状态变动"
        changed = len(set(present)) > 1

        rows.append({
            "key": key, "domain": domain, "prompt": prompt,
            "status": status, "verifier": verifier, "runtime": runtime,
            "case_index": idx, "trend": trend, "changed": changed,
        })

    rows.sort(key=lambda r: (TREND_ORDER.get(r["trend"], 9), r["key"]))
    return rows


def build_warnings(versions: List[Dict[str, Any]], pairs: List[Dict[str, Any]],
                   baseline: str, expect_fixed: Sequence[str]) -> List[str]:
    warns: List[str] = []
    base = next(v for v in versions if v["name"] == baseline)

    # ① 配置一致性
    cfg_sets = {v["name"]: v["configs"] for v in versions}
    models = {n: tuple(sorted(c["models"])) for n, c in cfg_sets.items()}
    if len(set(models.values())) > 1:
        warns.append("各卷模型不一致:" + "; ".join(
            f"{n}→{', '.join(m) or '—'}" for n, m in models.items()))
    disp = {n: tuple(sorted(c["dispatch"])) for n, c in cfg_sets.items()}
    if len(set(disp.values())) > 1:
        warns.append("各卷 meta_tool_dispatch 不一致:" + "; ".join(
            f"{n}→{', '.join(m) or '—'}" for n, m in disp.items()))
    retr = {n: tuple(sorted(c["retrieval"])) for n, c in cfg_sets.items()}
    if len(set(retr.values())) > 1:
        warns.append("各卷 meta_tool_retrieval 不一致:" + "; ".join(
            f"{n}→{', '.join(m) or '—'}" for n, m in retr.items()))

    # ② 任务集覆盖
    key_sets = {v["name"]: set(v["tasks"]) for v in versions}
    if len(set(map(len, key_sets.values()))) > 1:
        warns.append("各卷任务数不同:" + "; ".join(
            f"{n}={len(k)}" for n, k in key_sets.items())
            + "。配对只在共同任务上进行。")

    # ③ 修复生效自检
    for v in versions:
        f = v["fix"]
        masked = f.get("masked_errors", 0)
        env_keys = f.get("envelope_iserror_keys", 0)
        if masked > 0 or (f.get("inner_errors", 0) > 0 and env_keys == 0):
            warns.append(
                f"[修复未生效] {v['name']}:内层错误 {f.get('inner_errors', 0)} 次,"
                f"其中 {masked} 次仍被记为 success=True;tool_results 信封中"
                f"`isError` 键出现 {env_keys} 次。疑似跑的是修复前代码,"
                "请核对 git merge-base --is-ancestor 355b872 HEAD。")
        if f.get("old_message_hits", 0) and not f.get("new_message_hits", 0):
            warns.append(
                f"[旧文案] {v['name']}:包装名失败全部使用修复前文案 "
                f"`{OLD_WRAPPER_MSG}`({f['old_message_hits']} 次),"
                f"未见 `{NEW_WRAPPER_MSG}`。")

    declared = list(dict.fromkeys(
        list(expect_fixed) + [v["name"] for v in versions if v.get("expect_fixed")]))
    for name in declared:
        v = next((x for x in versions if x["name"] == name), None)
        if v is None:
            warns.append(f"[expect_fixed] 版本标签 {name} 不存在,已忽略该项声明。")
            continue
        if not v["fix"].get("effective", True):
            warns.append(
                f"[期望已修复但判定未生效] {name}:该卷声明了 expect_fixed,"
                "但修复指纹显示跑的是修复前代码,对比结论对它不适用。")
    # ④ 跨批可比性
    for p in pairs:
        for note in p["notes"]:
            warns.append(f"[可比性] {p['baseline']} vs {p['variant']}:{note}")

    # ⑤ 基线自身异常
    if base["error"] > 0:
        warns.append(
            f"基线 {baseline} 有 {base['error']}/{base['n_tasks']} 个 error,"
            "报数时优先看剔除 error 的成功率,并注意 error 分布是否两卷同向。")

    # ⑥ 口径分歧提示:compute_score.py 的验证器均值会被 error 文件拉低
    for v in versions:
        gap = (v["verifier_rate"] - v["cs_verifier_rate"]) * 100
        if abs(gap) > 2.0:
            warns.append(
                f"[口径] {v['name']}:验证器通过率合并口径 {_pct(v['verifier_rate'])} 与 "
                f"compute_score 口径 {_pct(v['cs_verifier_rate'])} 相差 {gap:+.1f}pp。"
                f"后者按逐文件 statistics 取均值,而 error 文件的验证器通过率恒为 0"
                f"(本卷 error {v['error']} 个),因此被拉低。对外引用时请注明用的是哪个口径。")
    return warns


# ---------------------------------------------------------------------------
# 图表数据(纯 CSS/SVG,不引外部库)
# ---------------------------------------------------------------------------

def build_chart(versions: List[Dict[str, Any]], baseline: str) -> Dict[str, Any]:
    """给前端画成功率对比柱状图用的数据。"""
    base = next(v for v in versions if v["name"] == baseline)
    bars = []
    for v in versions:
        bars.append({
            "name": v["name"],
            "raw": v["success_rate_raw"],
            "clean": v["success_rate_clean"],
            "verifier": v["verifier_rate"],
            "delta_pp": (v["success_rate_raw"] - base["success_rate_raw"]) * 100,
        })
    return {"bars": bars, "noise_floor_pp": NOISE_FLOOR_PP}


# ---------------------------------------------------------------------------
# HTML 生成
# ---------------------------------------------------------------------------

CSS = r"""
:root {
  color-scheme: light dark;
  --page: #f3f5f7; --surface: #ffffff; --surface-2: #f7f8fa;
  --text: #17202a; --muted: #5e6a75; --line: #cbd2d9; --focus: #1463ff;
  --pass-bg: #ccefd5; --pass-bg-2: #e9f9ed; --pass-strong: #137333; --pass-text: #0c351b;
  --fail-bg: #ffd6d6; --fail-bg-2: #fff0f0; --fail-strong: #b3261e; --fail-text: #4a1010;
  --error-bg: #ffe5b2; --error-bg-2: #fff5df; --error-strong: #9a4d00; --error-text: #462600;
  --neutral-bg: #e6e9ed; --neutral-strong: #63717e;
  --shadow: 0 8px 28px rgba(28, 39, 49, 0.10);
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #111417; --surface: #1d2227; --surface-2: #242a30;
    --text: #eef2f5; --muted: #b6c0c9; --line: #46515c; --focus: #8db1ff;
    --pass-bg: #123b22; --pass-bg-2: #16301f; --pass-strong: #65cf83; --pass-text: #e5ffec;
    --fail-bg: #521e1e; --fail-bg-2: #3b1e1e; --fail-strong: #ff8b83; --fail-text: #fff0ef;
    --error-bg: #4b3514; --error-bg-2: #382b18; --error-strong: #ffbd62; --error-text: #fff4df;
    --neutral-bg: #2c333a; --neutral-strong: #9aa6b1;
    --shadow: 0 8px 28px rgba(0, 0, 0, 0.28);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--text);
  font: 15px/1.6 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
        "Segoe UI", "Microsoft YaHei", sans-serif; }
button, input, select { font: inherit; }
button, select, input[type="search"] { min-height: 40px; border: 1px solid var(--line);
  border-radius: 9px; background: var(--surface); color: var(--text); }
button { padding: 6px 12px; cursor: pointer; }
button:hover { filter: brightness(0.97); }
button.active { background: var(--text); color: var(--page); border-color: var(--text); }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
.shell { width: min(1560px, 100%); margin: 0 auto; padding: 26px 22px 72px; }
h1 { margin: 0; font-size: clamp(24px, 3.4vw, 36px); line-height: 1.2; }
.subtitle { margin: 7px 0 0; color: var(--muted); }
h2.section { margin: 34px 0 12px; font-size: 19px; }
h2.section small { color: var(--muted); font-weight: 400; font-size: 13px; margin-left: 8px; }
.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 13px;
  padding: 15px 16px; box-shadow: var(--shadow); overflow: hidden; }
.panel + .panel { margin-top: 14px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(126px, 1fr));
  gap: 11px; margin: 20px 0 4px; }
.stat { background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
  padding: 12px 14px; }
.stat-label { display: block; color: var(--muted); font-size: 12px; }
.stat-value { display: block; font-size: 23px; font-weight: 700; line-height: 1.25; }
.stat-sub { display: block; color: var(--muted); font-size: 12px; }
.warnings { background: var(--error-bg); color: var(--error-text);
  border-left: 6px solid var(--error-strong); border-radius: 10px;
  padding: 11px 14px; margin: 16px 0; }
.warnings p { margin: 0 0 6px; }
.warnings p:last-child { margin-bottom: 0; }
.warn-ok { background: var(--pass-bg-2); color: var(--pass-text);
  border-left-color: var(--pass-strong); }
table { width: 100%; border-collapse: collapse; background: var(--surface); }
th, td { padding: 7px 9px; border: 1px solid var(--line); text-align: left;
  vertical-align: middle; }
th { background: var(--surface-2); font-weight: 700; font-size: 13px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }
.badge { display: inline-block; padding: 2px 9px; border-radius: 999px;
  font-size: 12px; font-weight: 700; color: #fff; }
.badge.pass { background: var(--pass-strong); }
.badge.fail { background: var(--fail-strong); }
.badge.error { background: var(--error-strong); }
.badge.absent { background: var(--neutral-strong); }
.badge.sig { background: var(--fail-strong); }
.badge.nosig { background: var(--neutral-strong); }
.chip { display: inline-block; padding: 2px 8px; border-radius: 999px;
  background: color-mix(in srgb, var(--surface-2) 86%, transparent);
  border: 1px solid var(--line); color: var(--text); font-size: 12px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.delta-up { color: var(--pass-strong); font-weight: 700; }
.delta-down { color: var(--fail-strong); font-weight: 700; }
.delta-flat { color: var(--muted); }
.bars { display: grid; gap: 9px; margin-top: 6px; }
.bar-row { display: grid; grid-template-columns: 132px minmax(0, 1fr) 168px;
  align-items: center; gap: 10px; }
.bar-track { position: relative; height: 22px; background: var(--surface-2);
  border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.bar-fill { position: absolute; inset: 0 auto 0 0; background: var(--neutral-strong); }
.bar-fill.pass { background: var(--pass-strong); }
.bar-fill.baseline { background: var(--focus); }
.bar-noise { position: absolute; inset: 0 auto 0 0; border-right: 2px dashed var(--muted); }
.bar-legend { color: var(--muted); font-size: 12px; }
.matrix-wrap { max-height: 640px; overflow: auto; }
.matrix td.cell { text-align: center; cursor: pointer; font-weight: 700;
  font-size: 12px; padding: 4px 6px; }
.matrix td.cell.pass { background: var(--pass-bg); color: var(--pass-text); }
.matrix td.cell.fail { background: var(--fail-bg); color: var(--fail-text); }
.matrix td.cell.error { background: var(--error-bg); color: var(--error-text); }
.matrix td.cell.absent { background: var(--neutral-bg); color: var(--muted); }
.matrix td.cell:hover { outline: 2px solid var(--focus); outline-offset: -2px; }
.matrix th.sticky, .matrix td.sticky { position: sticky; left: 0; z-index: 2;
  background: var(--surface); }
.matrix th { position: sticky; top: 0; z-index: 3; }
.matrix th.sticky { z-index: 4; }
.matrix td.task { font: 12px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace;
  white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
.toolbar { display: grid; grid-template-columns: minmax(220px, 2fr) repeat(3, minmax(130px, 1fr));
  gap: 10px; padding: 12px; background: var(--surface); border: 1px solid var(--line);
  border-radius: 12px; }
.field { display: flex; flex-direction: column; gap: 4px; }
.field label { color: var(--muted); font-size: 12px; }
.field input, .field select { width: 100%; padding: 6px 9px; }
.result-line { color: var(--muted); margin: 9px 0 13px; font-size: 13px; }
.case-list { display: grid; gap: 16px; }
.case-card { border-radius: 15px; border: 2px solid transparent; border-left-width: 9px;
  box-shadow: var(--shadow); overflow: hidden; }
.case-card.pass { background: linear-gradient(135deg, var(--pass-bg), var(--pass-bg-2));
  border-color: var(--pass-strong); color: var(--pass-text); }
.case-card.fail { background: linear-gradient(135deg, var(--fail-bg), var(--fail-bg-2));
  border-color: var(--fail-strong); color: var(--fail-text); }
.case-card.error { background: linear-gradient(135deg, var(--error-bg), var(--error-bg-2));
  border-color: var(--error-strong); color: var(--error-text); }
.case-head { display: grid; grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center; gap: 11px; padding: 14px 16px;
  border-bottom: 1px solid color-mix(in srgb, currentColor 22%, transparent); }
.status-badge { display: inline-flex; align-items: center; justify-content: center;
  min-width: 68px; padding: 4px 9px; border-radius: 999px; color: #fff;
  font-weight: 750; letter-spacing: .03em; }
.pass .status-badge { background: var(--pass-strong); }
.fail .status-badge { background: var(--fail-strong); }
.error .status-badge { background: var(--error-strong); }
.case-title { min-width: 0; }
.case-id { margin: 0; font: 700 15px/1.3 ui-monospace, SFMono-Regular, Consolas, monospace;
  overflow-wrap: anywhere; }
.case-group { font-size: 12px; opacity: .78; }
.case-score { text-align: right; white-space: nowrap; }
.case-score strong { display: block; font-size: 20px; line-height: 1.2; }
.case-body { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr);
  gap: 13px; padding: 14px 16px 16px; }
.content-panel { min-width: 0; background: color-mix(in srgb, var(--surface) 82%, transparent);
  border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
  border-radius: 11px; padding: 12px 13px; color: var(--text); }
.content-panel h3 { margin: 0 0 7px; font-size: 14px; }
.text-block { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere;
  max-height: 30rem; overflow: auto; font-family: inherit; }
.markdown-body { white-space: normal; }
.markdown-body > :first-child { margin-top: 0; }
.markdown-body > :last-child { margin-bottom: 0; }
.markdown-body h1, .markdown-body h2, .markdown-body h3,
.markdown-body h4, .markdown-body h5, .markdown-body h6 { margin: 1.1em 0 .5em; line-height: 1.3; }
.markdown-body h1 { font-size: 1.5em; } .markdown-body h2 { font-size: 1.32em; }
.markdown-body h3 { font-size: 1.16em; }
.markdown-body p { margin: .62em 0; }
.markdown-body ul, .markdown-body ol { margin: .52em 0; padding-left: 1.6em; }
.markdown-body li { margin: .2em 0; }
.markdown-body blockquote { margin: .7em 0; padding: .25em .9em;
  border-left: 4px solid var(--line); background: var(--surface-2); color: var(--muted); }
.markdown-body code { padding: .12em .38em; border-radius: 5px; background: var(--surface-2);
  font: .9em/1.5 ui-monospace, SFMono-Regular, Consolas, monospace; }
.markdown-body pre { margin: .72em 0; border: 1px solid var(--line); }
.markdown-body pre code { padding: 0; background: transparent; }
.markdown-body hr { border: 0; border-top: 1px solid var(--line); margin: 1em 0; }
.markdown-table-wrap { overflow-x: auto; margin: .8em 0; }
.markdown-body table { width: 100%; border-collapse: collapse; background: var(--surface); }
.markdown-body th, .markdown-body td { padding: 6px 8px; border: 1px solid var(--line);
  text-align: left; vertical-align: top; }
.markdown-body th { background: var(--surface-2); font-weight: 750; }
.markdown-source { margin-top: 9px; }
.error-message { font-weight: 650; }
.wide { grid-column: 1 / -1; }
.verifier-list { display: grid; gap: 8px; }
.verifier { border-radius: 9px; padding: 9px 11px; border-left: 6px solid;
  background: var(--surface-2); color: var(--text); }
.verifier.ok { border-color: var(--pass-strong); }
.verifier.bad { border-color: var(--fail-strong); }
.verifier-head { display: flex; justify-content: space-between; gap: 12px; }
.verifier-name { font-weight: 700; }
.verifier-state { font-weight: 750; white-space: nowrap; }
.verifier.ok .verifier-state { color: var(--pass-strong); }
.verifier.bad .verifier-state { color: var(--fail-strong); }
.verifier-values { margin-top: 5px; color: var(--muted); }
details { margin-top: 8px; }
summary { cursor: pointer; font-weight: 650; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; background: var(--surface-2);
  color: var(--text); border-radius: 8px; padding: 9px; max-height: 22rem; overflow: auto; }
.meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 7px 13px; }
.meta-item { min-width: 0; }
.meta-key { display: block; color: var(--muted); font-size: 12px; }
.meta-value { display: block; overflow-wrap: anywhere; }
.trajectory-details > summary { padding: 8px 10px; border-radius: 8px;
  background: var(--surface-2); border: 1px solid var(--line); }
.trajectory-list { display: grid; gap: 9px; margin-top: 11px; padding-left: 19px;
  border-left: 3px solid var(--line); }
.turn { position: relative; min-width: 0; padding: 9px 11px; border: 1px solid var(--line);
  border-radius: 10px; background: var(--surface-2); color: var(--text); }
.turn::before { content: ''; position: absolute; left: -27px; top: 16px; width: 12px;
  height: 12px; border-radius: 50%; background: var(--muted); border: 3px solid var(--surface); }
.turn.user_message::before { background: #1463ff; }
.turn.ai_message::before { background: #7b3fc6; }
.turn.tool_result::before { background: #d66b00; }
.turn.system_message::before { background: #63717e; }
.turn-head { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; margin-bottom: 6px; }
.turn-index { color: var(--muted); font: 12px/1.4 ui-monospace, SFMono-Regular, Consolas, monospace; }
.turn-role { font-weight: 750; }
.turn-stage { padding: 1px 7px; border-radius: 999px; background: var(--surface);
  border: 1px solid var(--line); color: var(--muted); font-size: 11px; }
.turn-content { margin: 0; max-height: 30rem; }
.tool-call-list { display: grid; gap: 7px; margin-top: 8px; }
.tool-call { padding: 7px 9px; border-radius: 8px; border-left: 4px solid #7b3fc6;
  background: var(--surface); }
.tool-call pre, .turn details pre { margin-bottom: 0; }
.turn-meta { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 7px; }
.turn-meta .chip { background: var(--surface); }
.empty { padding: 40px 18px; text-align: center; background: var(--surface);
  border: 1px solid var(--line); border-radius: 12px; color: var(--muted); }
.pagination { display: flex; justify-content: center; align-items: center;
  flex-wrap: wrap; gap: 10px; margin-top: 18px; }
.source { font: 12px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace;
  overflow-wrap: anywhere; opacity: .72; }
.hint { color: var(--muted); font-size: 13px; margin: 6px 0 0; }
@media (max-width: 1000px) {
  .case-body { grid-template-columns: 1fr; }
  .wide { grid-column: auto; }
  .toolbar { grid-template-columns: 1fr 1fr; }
  .bar-row { grid-template-columns: 100px minmax(0, 1fr) 132px; }
}
@media (max-width: 640px) {
  .shell { padding: 16px 11px 48px; }
  .toolbar { grid-template-columns: 1fr; }
  .case-head { grid-template-columns: auto minmax(0, 1fr); }
  .case-score { grid-column: 1 / -1; text-align: left; }
  .bar-row { grid-template-columns: 1fr; }
}
"""

JS = r"""
(() => {
  const payload = JSON.parse(document.getElementById('dashboard-data').textContent);
  const versions = payload.versions || [];
  const tasks = payload.tasks || [];
  const cases = payload.cases || [];
  const pairs = payload.pairs || [];
  const versionsByName = Object.fromEntries(versions.map(v => [v.name, v]));

  // ---------- 基础工具(与原看板一致的 markdown 渲染) ----------
  const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
  const inlineMarkdown = (value) => {
    const tokens = [];
    const stash = (html) => `\uE000MD${tokens.push(html) - 1}\uE001`;
    let source = String(value ?? '');
    source = source.replace(/`([^`\n]+)`/g, (_, code) => stash(`<code>${escapeHtml(code)}</code>`));
    source = source.replace(/\[([^\]]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/gi,
      (_, label, url) => stash(`<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`));
    let rendered = escapeHtml(source);
    rendered = rendered.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    rendered = rendered.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
    rendered = rendered.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
    rendered = rendered.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    rendered = rendered.replace(/(^|[\s(])_([^_\n]+)_/g, '$1<em>$2</em>');
    return rendered.replace(/\uE000MD(\d+)\uE001/g, (_, index) => tokens[Number(index)] || '');
  };
  const tableCells = (line) => {
    let value = line.trim();
    if (value.startsWith('|')) value = value.slice(1);
    if (value.endsWith('|')) value = value.slice(0, -1);
    return value.split('|').map(cell => cell.trim());
  };
  const isTableDivider = (line) => {
    const cells = tableCells(line);
    return cells.length > 1 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
  };
  const renderMarkdown = (value) => {
    const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let paragraph = [], listType = '';
    const flushParagraph = () => {
      if (!paragraph.length) return;
      out.push(`<p>${paragraph.map(inlineMarkdown).join('<br>')}</p>`);
      paragraph = [];
    };
    const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = ''; } };
    const flushBlocks = () => { flushParagraph(); closeList(); };
    for (let index = 0; index < lines.length;) {
      const line = lines[index];
      const fence = line.match(/^\s*```\s*([\w.+-]*)\s*$/);
      if (fence) {
        flushBlocks();
        const code = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) code.push(lines[index++]);
        if (index < lines.length) index += 1;
        const language = fence[1] ? ` class="language-${escapeHtml(fence[1])}"` : '';
        out.push(`<pre><code${language}>${escapeHtml(code.join('\n'))}</code></pre>`);
        continue;
      }
      if (!line.trim()) { flushBlocks(); index += 1; continue; }
      if (index + 1 < lines.length && line.includes('|') && isTableDivider(lines[index + 1])) {
        flushBlocks();
        const headers = tableCells(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
          rows.push(tableCells(lines[index++]));
        }
        const head = headers.map(cell => `<th>${inlineMarkdown(cell)}</th>`).join('');
        const body = rows.map(row => `<tr>${headers.map((_, i) => `<td>${inlineMarkdown(row[i] || '')}</td>`).join('')}</tr>`).join('');
        out.push(`<div class="markdown-table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        flushBlocks();
        const level = heading[1].length;
        out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        index += 1; continue;
      }
      if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushBlocks(); out.push('<hr>'); index += 1; continue; }
      if (/^\s*>/.test(line)) {
        flushBlocks();
        const quoted = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) quoted.push(lines[index++].replace(/^\s*>\s?/, ''));
        out.push(`<blockquote>${renderMarkdown(quoted.join('\n'))}</blockquote>`);
        continue;
      }
      const unordered = line.match(/^\s*[-+*]\s+(.+)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        flushParagraph();
        const nextType = unordered ? 'ul' : 'ol';
        if (listType && listType !== nextType) closeList();
        if (!listType) { listType = nextType; out.push(`<${listType}>`); }
        out.push(`<li>${inlineMarkdown((unordered || ordered)[1])}</li>`);
        index += 1; continue;
      }
      closeList();
      paragraph.push(line);
      index += 1;
    }
    flushBlocks();
    return out.join('');
  };

  const fmt = (value) => {
    if (value === null || value === undefined || value === '') return '—';
    if (typeof value === 'object') return JSON.stringify(value, null, 2);
    return String(value);
  };
  const pct = (value) => (Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '—');
  const duration = (ms) => {
    if (!Number.isFinite(ms) || ms <= 0) return '—';
    const seconds = ms / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
    if (seconds < 3600) return `${(seconds / 60).toFixed(1)} 分钟`;
    return `${(seconds / 3600).toFixed(1)} 小时`;
  };
  const statusLabel = { pass: '成功', fail: '失败', error: '错误', absent: '未跑' };
  const roleLabel = { system_message: '系统消息', user_message: '用户消息',
    ai_message: '模型消息', tool_result: '工具结果' };
  const deltaHtml = (pp) => {
    if (!Number.isFinite(pp)) return '<span class="delta-flat">—</span>';
    const cls = pp > 0.05 ? 'delta-up' : (pp < -0.05 ? 'delta-down' : 'delta-flat');
    const sign = pp > 0 ? '+' : '';
    return `<span class="${cls}">${sign}${pp.toFixed(1)}pp</span>`;
  };
  const $ = (id) => document.getElementById(id);

  document.title = payload.title;
  $('page-title').textContent = payload.title;
  $('page-subtitle').textContent = payload.subtitle;

  // ---------- 警告 ----------
  const warns = payload.warnings || [];
  if (warns.length) {
    $('warnings').innerHTML = `<div class="warnings">${warns.map(w => `<p>${escapeHtml(w)}</p>`).join('')}</div>`;
  } else {
    $('warnings').innerHTML = '<div class="warnings warn-ok"><p>未发现配置不一致、修复未生效或跨批不可比的告警。</p></div>';
  }

  // ---------- 版本总览表 ----------
  const base = payload.baseline;
  function renderSummary() {
    const head = ['版本', '目录', '任务', '成功', '失败', '错误', '成功率(原始)',
      '成功率(剔错)', 'Δ vs 基线', '验证器(合并)', '验证器(cs口径)', '均值耗时', '中位耗时', '超时/错误类'];
    const rows = versions.map(v => {
      const delta = (v.success_rate_raw - (versionsByName[base]?.success_rate_raw ?? 0)) * 100;
      const ec = Object.entries(v.error_classes || {}).map(([k, n]) => `${k}:${n}`).join(' · ') || '—';
      const gap = Math.abs((v.verifier_rate - v.cs_verifier_rate) * 100);
      const desc = v.description
        ? `<div class="case-group">${escapeHtml(v.description)}</div>` : '';
      return `<tr>
        <td><strong>${escapeHtml(v.name)}</strong>${v.name === base ? ' <span class="chip">基线</span>' : ''}${desc}</td>
        <td class="source">${escapeHtml(v.dir)}</td>
        <td class="num">${v.n_tasks}</td>
        <td class="num">${v.pass}</td>
        <td class="num">${v.fail}</td>
        <td class="num">${v.error}</td>
        <td class="num">${pct(v.success_rate_raw)}</td>
        <td class="num">${pct(v.success_rate_clean)}</td>
        <td class="num">${v.name === base ? '<span class="delta-flat">基线</span>' : deltaHtml(delta)}</td>
        <td class="num">${pct(v.verifier_rate)}
          <span class="stat-sub">${v.verifier_passed}/${v.verifier_total}</span></td>
        <td class="num" title="compute_score.py 口径:逐文件 statistics 取均值,error 文件按 0 计入">
          ${pct(v.cs_verifier_rate)}${gap > 2 ? ` <span class="chip">差${gap.toFixed(1)}pp</span>` : ''}</td>
        <td class="num">${duration(v.runtime_mean_ms)}</td>
        <td class="num">${duration(v.runtime_median_ms)}</td>
        <td>${escapeHtml(ec)}</td>
      </tr>`;
    }).join('');
    $('summary-table').innerHTML = `<div class="table-wrap"><table>
      <thead><tr>${head.map((h, i) => `<th class="${i >= 2 && i <= 12 ? 'num' : ''}">${h}</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="hint">成功率(原始)= 通过任务数 / 任务总数,与 <code>compute_score.py</code> 的输出一致。
        验证器通过率给两种口径:<strong>合并</strong>= Σ通过 / Σ验证器(项目平时引用的就是这个);
        <strong>cs 口径</strong>= <code>compute_score.py</code> 的逐文件均值,error 文件的验证器通过率恒为 0,
        因此有 error 的卷会被拉低。两者差值超过 2pp 时上方会给出告警。</p>`;

    // 柱状图
    $('bars').innerHTML = `<div class="bars">${versions.map(v => {
      const w = (v.success_rate_raw * 100).toFixed(1);
      const nw = (payload.chart.noise_floor_pp).toFixed(1);
      const cls = v.name === base ? 'baseline' : (v.success_rate_raw >= (versionsByName[base]?.success_rate_raw ?? 0) ? 'pass' : '');
      return `<div class="bar-row">
        <div>${escapeHtml(v.name)}</div>
        <div class="bar-track"><div class="bar-fill ${cls}" style="width:${w}%"></div>
          <div class="bar-noise" style="left:${nw}%" title="噪声地板 ${nw}pp"></div></div>
        <div class="bar-legend">${pct(v.success_rate_raw)} · 剔错 ${pct(v.success_rate_clean)}</div>
      </div>`;
    }).join('')}</div>
    <p class="hint">虚线为已知运行间噪声下限 ${payload.chart.noise_floor_pp.toFixed(1)}pp
      (同一份代码两卷 email 全量的实测差),柱长差小于它不构成效果证据。</p>`;
  }

  // ---------- 两两配对 ----------
  function renderPairs() {
    if (!pairs.length) { $('pairs').innerHTML = '<p class="hint">只有一个版本,无需配对对比。</p>'; return; }
    $('pairs').innerHTML = pairs.map(p => {
      const sig = p.mcnemar_sig
        ? '<span class="badge sig">显著 p&lt;0.05</span>'
        : '<span class="badge nosig">未达显著</span>';
      const notes = p.notes.length
        ? `<ul>${p.notes.map(n => `<li>${escapeHtml(n)}</li>`).join('')}</ul>` : '';
      const listOf = (arr, cls) => arr.length
        ? `<div class="chips">${arr.slice(0, 200).map(k => `<span class="chip" style="border-color:var(--${cls});">${escapeHtml(k)}</span>`).join('')}</div>`
        : '<span class="hint">无</span>';
      const desc = [
        p.baseline_desc ? `${p.baseline}:${p.baseline_desc}` : '',
        p.variant_desc ? `${p.variant}:${p.variant_desc}` : '',
      ].filter(Boolean).join('  |  ');
      return `<div class="panel">
        <h3 style="margin:0 0 4px;">${escapeHtml(p.baseline)} → ${escapeHtml(p.variant)} ${sig}</h3>
        ${desc ? `<p class="hint">${escapeHtml(desc)}</p>` : ''}
        <div class="table-wrap"><table>
          <thead><tr><th class="num">配对任务</th><th class="num">修复(saved)</th>
            <th class="num">回退(regressed)</th><th class="num">都通过</th><th class="num">都失败</th>
            <th class="num">Δ成功率</th><th class="num">McNemar p</th></tr></thead>
          <tbody><tr>
            <td class="num">${p.paired}</td>
            <td class="num">${p.saved}</td>
            <td class="num">${p.regressed}</td>
            <td class="num">${p.both_pass}</td>
            <td class="num">${p.both_fail}</td>
            <td class="num">${deltaHtml(p.delta_pp)}</td>
            <td class="num">${p.mcnemar_p.toFixed(4)}</td>
          </tr></tbody></table></div>
        ${notes}
        <details><summary>修复的任务(${p.saved})</summary>${listOf(p.saved_tasks, 'pass-strong')}</details>
        <details><summary>回退的任务(${p.regressed})</summary>${listOf(p.regressed_tasks, 'fail-strong')}</details>
      </div>`;
    }).join('');
  }

  // ---------- 修复生效自检 ----------
  function renderFix() {
    const head = ['版本', '信封含 isError 键', '内层错误数', '被掩蔽错误',
      '名修复', '门禁拒绝', '旧文案', '新文案 Malformed', '判定', '信封键组合'];
    const rows = versions.map(v => {
      const f = v.fix || {};
      const eff = f.effective
        ? '<span class="badge pass">生效</span>'
        : '<span class="badge fail">未生效</span>';
      const env = Object.entries(v.envelopes || {}).map(([k, n]) => `${k}×${n}`).join('<br>') || '—';
      return `<tr>
        <td><strong>${escapeHtml(v.name)}</strong></td>
        <td class="num">${f.envelope_iserror_keys ?? 0}</td>
        <td class="num">${f.inner_errors ?? 0}</td>
        <td class="num">${f.masked_errors ?? 0}</td>
        <td class="num">${f.name_repairs === undefined || f.name_repairs === null ? '—' : f.name_repairs}</td>
        <td class="num">${f.dogwood_denied ?? 0}</td>
        <td class="num">${f.old_message_hits ?? 0}</td>
        <td class="num">${f.new_message_hits ?? 0}</td>
        <td>${eff}</td>
        <td class="source">${env}</td>
      </tr>`;
    }).join('');
    $('fix-table').innerHTML = `<div class="table-wrap"><table>
      <thead><tr>${head.map((h, i) => `<th class="${i >= 1 && i <= 7 ? 'num' : ''}">${h}</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="hint">判据:内层 isError=True 的次数应全部记成 success=False(被掩蔽错误 = 0),
        且 tool_results 信封里应出现 <code>isError</code> 键。若某卷"内层错误 &gt; 0 但信封无 isError 键"
        或"被掩蔽错误 &gt; 0",说明该卷跑的是修复前代码。</p>`;
  }

  // ---------- 验证器失败构成 ----------
  function renderTaxonomy() {
    const cats = ['MISSING_STATE', 'VALUE_MISMATCH', 'OVER_PROVISION', 'NO_RESULT'];
    const cn = { MISSING_STATE: '副作用未落地', VALUE_MISMATCH: '值不符',
      OVER_PROVISION: '多做了', NO_RESULT: '无结果' };
    const head = ['版本', '验证器总数', '通过', ...cats.map(c => cn[c]),
      '未落地占比', '通过率'];
    const rows = versions.map(v => {
      const all = v.taxonomy_all || {};
      const failed = v.taxonomy_failed || {};
      const total = Object.values(all).reduce((a, b) => a + b, 0);
      const passed = all.PASS || 0;
      const miss = failed.MISSING_STATE || 0;
      const nFail = Object.values(failed).reduce((a, b) => a + b, 0);
      return `<tr>
        <td><strong>${escapeHtml(v.name)}</strong></td>
        <td class="num">${total}</td>
        <td class="num">${passed}</td>
        ${cats.map(c => `<td class="num">${failed[c] || 0}</td>`).join('')}
        <td class="num">${nFail ? pct(miss / nFail) : '—'}</td>
        <td class="num">${pct(total ? passed / total : 0)}</td>
      </tr>`;
    }).join('');
    $('taxonomy-table').innerHTML = `<div class="table-wrap"><table>
      <thead><tr>${head.map((h, i) => `<th class="${i >= 1 ? 'num' : ''}">${h}</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="hint">本项目基准里失败验证器绝大多数是"副作用未落地"(actual==0),
        基线口径 email ≈ 77%、hr ≈ 96%。该占比若在两卷间明显不同,说明失败形态被改变了。</p>`;
  }

  // ---------- 遥测 / 环境漂移 ----------
  const TELE_ROWS = [
    ['meta_tool_search_calls', '检索次数/任务', 'mean'],
    ['meta_tool_searches', '检索轮次/任务', 'mean'],
    ['meta_tool_injected', '注入工具数/任务', 'mean'],
    ['meta_tool_hits_avg', '平均命中数', 'mean'],
    ['meta_tool_zero_hits', '零命中任务占比', 'frac'],
    ['meta_tool_fallback_all', '全量回退任务占比', 'frac'],
    ['vl_gate_rounds', 'gate 轮次/任务', 'mean'],
    ['vl_correction_turns', '纠错回合/任务', 'mean'],
    ['vl_no_read_reminders', '无证据提醒/任务', 'mean'],
    ['vl_forced_done', '强制收尾任务占比', 'frac'],
    ['mem_folds', '记忆折叠/任务', 'mean'],
    ['mem_invalid_facts', '失效事实/任务', 'mean'],
    ['mem_recap_chars', 'recap 字符/任务', 'mean'],
    ['mem_on', '记忆层开启任务占比', 'frac'],
  ];
  function renderTelemetry() {
    const head = ['指标', ...versions.map(v => v.name)];
    const rows = TELE_ROWS.map(([key, label, kind]) => {
      const cells = versions.map(v => {
        const t = v.telemetry || {};
        if (kind === 'frac') {
          const n = t[key + '_count'] ?? 0;
          return `<td class="num">${v.n_tasks ? pct(n / v.n_tasks) : '—'}<span class="stat-sub">${n}</span></td>`;
        }
        const val = t[key];
        return `<td class="num">${val === undefined ? '—' : val.toFixed(2)}</td>`;
      }).join('');
      return `<tr><td>${label}</td>${cells}</tr>`;
    }).join('');
    $('tele-table').innerHTML = `<div class="table-wrap"><table>
      <thead><tr>${head.map((h, i) => `<th class="${i >= 1 ? 'num' : ''}">${h}</th>`).join('')}</tr></thead>
      <tbody>${rows}</tbody></table></div>
      <p class="hint">用于识别批次级环境漂移。本项目曾出现"检索次数 2.4x 且注入工具数 0.3x"的漂移,
        连逐字节相同的对照卷都受影响,因此这类指标差异不能归因于本次修改。</p>`;
  }

  // ---------- 任务矩阵 ----------
  const matrixState = { search: '', filter: 'changed', trend: 'all', page: 1, pageSize: 40 };
  const trendCls = { '修复': 'pass', '回退': 'fail', '抖动': 'error', '状态变动': 'error',
    '完全稳定(通过)': 'pass', '完全稳定(失败)': 'fail', '完全稳定(错误)': 'error' };
  function matrixRows() {
    let rows = tasks.filter(t => {
      if (matrixState.search && !(t.key + ' ' + t.prompt).toLowerCase().includes(matrixState.search)) return false;
      if (matrixState.filter === 'changed' && !t.changed) return false;
      if (matrixState.filter === 'improved' && !['修复', '完全稳定(通过)'].includes(t.trend)) return false;
      if (matrixState.filter === 'worsened' && !['回退', '完全稳定(失败)', '完全稳定(错误)'].includes(t.trend)) return false;
      if (matrixState.trend !== 'all' && t.trend !== matrixState.trend) return false;
      return true;
    });
    return rows;
  }
  function renderMatrix() {
    const rows = matrixRows();
    const size = matrixState.pageSize;
    const pageCount = Math.max(1, Math.ceil(rows.length / size));
    matrixState.page = Math.min(matrixState.page, pageCount);
    const start = (matrixState.page - 1) * size;
    const pageRows = rows.slice(start, start + size);
    const head = ['任务', ...versions.map(v => v.name), '趋势'];
    const body = pageRows.map(t => {
      const cells = versions.map(v => {
        const st = t.status[v.name] || 'absent';
        const ver = t.verifier[v.name];
        const tip = `${v.name} · ${statusLabel[st]}` +
          (ver ? ` · 验证器 ${ver[0]}/${ver[1]}` : '') +
          (t.runtime[v.name] ? ` · ${duration(t.runtime[v.name])}` : '');
        const clickable = t.case_index[v.name] !== undefined;
        return `<td class="cell ${st}" data-case="${clickable ? t.case_index[v.name] : ''}"
          title="${escapeHtml(tip)}">${statusLabel[st]}</td>`;
      }).join('');
      const cls = trendCls[t.trend] || '';
      const label = cls
        ? `<span class="badge ${cls}">${escapeHtml(t.trend)}</span>`
        : escapeHtml(t.trend);
      return `<tr><td class="task sticky" title="${escapeHtml(t.key)}">${escapeHtml(t.key)}</td>
        ${cells}<td>${label}</td></tr>`;
    }).join('');
    $('matrix-table').innerHTML = `<div class="matrix-wrap"><table class="matrix">
      <thead><tr>${head.map((h, i) => `<th class="${i === 0 ? 'sticky' : ''}">${escapeHtml(h)}</th>`).join('')}</tr></thead>
      <tbody>${body || `<tr><td colspan="${head.length}">没有符合筛选条件的任务。</td></tr>`}</tbody></table></div>
      <p class="result-line">匹配 ${rows.length} 个任务;当前显示 ${rows.length ? start + 1 : 0}–${Math.min(start + size, rows.length)}。
        点击状态格子可跳到该任务在该版本下的案例详情。</p>`;
    $('matrix-pagination').innerHTML = pageCount <= 1 ? '' : `
      <button type="button" id="mx-prev" ${matrixState.page <= 1 ? 'disabled' : ''}>上一页</button>
      <span>第 ${matrixState.page} / ${pageCount} 页</span>
      <button type="button" id="mx-next" ${matrixState.page >= pageCount ? 'disabled' : ''}>下一页</button>`;
    $('mx-prev')?.addEventListener('click', () => { matrixState.page -= 1; renderMatrix(); });
    $('mx-next')?.addEventListener('click', () => { matrixState.page += 1; renderMatrix(); });
    document.querySelectorAll('.matrix td.cell[data-case]').forEach(cell => {
      if (cell.dataset.case === '') return;
      cell.addEventListener('click', () => focusCase(Number(cell.dataset.case)));
    });
  }

  // ---------- 案例详情 ----------
  const caseState = { search: '', version: 'all', status: 'all', sort: 'status', page: 1, pageSize: 20 };
  const vrate = (c) => (c.verifier_total ? c.verifier_passed / c.verifier_total : -1);
  function visibleCases() {
    let result = cases.filter(c => {
      if (caseState.version !== 'all' && c.version !== caseState.version) return false;
      if (caseState.status !== 'all' && c.status !== caseState.status) return false;
      if (caseState.search) {
        const blob = [c.id, c.task, c.prompt, c.output, c.error, ...(c.tools_used || []),
          ...(c.verifiers || []).map(v => [v.name, v.details, v.expected, v.actual].join(' '))]
          .join('\n').toLowerCase();
        if (!blob.includes(caseState.search)) return false;
      }
      return true;
    });
    const w = { error: 0, fail: 1, pass: 2 };
    result.sort((a, b) => {
      if (caseState.sort === 'status') return w[a.status] - w[b.status] || a.id.localeCompare(b.id);
      if (caseState.sort === 'verifier') return vrate(a) - vrate(b) || a.id.localeCompare(b.id);
      if (caseState.sort === 'duration') return (b.execution_time_ms || 0) - (a.execution_time_ms || 0);
      return (a.version + a.id).localeCompare(b.version + b.id);
    });
    return result;
  }
  function renderVerifier(v) {
    const klass = v.passed ? 'ok' : 'bad';
    const extra = [
      v.category ? `归类：${escapeHtml(v.category)}` : '',
      v.comparison_type ? `比较：${escapeHtml(v.comparison_type)}` : '',
      `期望：${escapeHtml(fmt(v.expected))}`,
      `实际：${escapeHtml(fmt(v.actual))}`,
    ].filter(Boolean).join(' · ');
    const details = v.details ? `<div class="verifier-values">${escapeHtml(v.details)}</div>` : '';
    const query = v.query ? `<details><summary>查看验证查询</summary><pre>${escapeHtml(v.query)}</pre></details>` : '';
    return `<div class="verifier ${klass}">
      <div class="verifier-head"><span class="verifier-name">${escapeHtml(v.name)}</span>
        <span class="verifier-state">${v.passed ? '✓ 通过' : '✕ 未通过'}</span></div>
      <div class="verifier-values">${extra}</div>${details}${query}</div>`;
  }
  function renderToolCall(call, index) {
    const name = call?.name || `工具调用 ${index + 1}`;
    return `<div class="tool-call"><strong>调用：${escapeHtml(name)}</strong>
      <pre>${escapeHtml(fmt(call?.args ?? {}))}</pre></div>`;
  }
  function renderTurn(turn, index) {
    const type = turn?.type || 'unknown';
    const role = roleLabel[type] || type;
    const stage = turn?.stage ? `<span class="turn-stage">${escapeHtml(turn.stage)}</span>` : '';
    const content = turn?.content
      ? (type === 'ai_message'
        ? `<div class="turn-content markdown-body">${renderMarkdown(turn.content)}</div>`
        : `<pre class="turn-content">${escapeHtml(turn.content)}</pre>`)
      : '';
    const calls = Array.isArray(turn?.tool_calls) && turn.tool_calls.length
      ? `<div class="tool-call-list">${turn.tool_calls.map(renderToolCall).join('')}</div>` : '';
    const result = type === 'tool_result'
      ? `<details><summary>展开 ${escapeHtml(turn.tool_name || '工具')} 返回结果</summary><pre>${escapeHtml(fmt(turn.result))}</pre></details>` : '';
    const usage = turn?.usage_metadata || {};
    const response = turn?.response_metadata || {};
    const meta = [
      turn?.tool_name ? `工具：${turn.tool_name}` : '',
      response.model || response.model_name ? `模型：${response.model || response.model_name}` : '',
      response.stop_reason ? `停止：${response.stop_reason}` : '',
      Number.isFinite(usage.input_tokens) ? `输入 token：${usage.input_tokens}` : '',
      Number.isFinite(usage.output_tokens) ? `输出 token：${usage.output_tokens}` : '',
    ].filter(Boolean);
    const metaHtml = meta.length
      ? `<div class="turn-meta">${meta.map(v => `<span class="chip">${escapeHtml(v)}</span>`).join('')}</div>` : '';
    const fallback = !content && !calls && !result
      ? `<details><summary>查看原始记录</summary><pre>${escapeHtml(fmt(turn))}</pre></details>` : '';
    return `<article class="turn ${escapeHtml(type)}">
      <div class="turn-head"><span class="turn-index">#${index + 1}</span>
        <span class="turn-role">${escapeHtml(role)}</span>${stage}</div>
      ${content}${calls}${result}${fallback}${metaHtml}</article>`;
  }
  function renderCase(c) {
    const verifierText = c.verifier_total ? `${c.verifier_passed}/${c.verifier_total}` : '—';
    const output = c.error
      ? `<p class="text-block error-message">${escapeHtml(c.error)}</p>`
      : `<div class="text-block markdown-body">${renderMarkdown(c.output || '（无模型输出）')}</div>
         <details class="markdown-source"><summary>查看原始 Markdown</summary><pre>${escapeHtml(c.output || '（无模型输出）')}</pre></details>`;
    const verifiers = (c.verifiers || []).length
      ? c.verifiers.map(renderVerifier).join('') : '<p>没有可展示的验证器明细。</p>';
    const checklist = c.checklist
      ? `<details><summary>查看 Verify Loop 验收清单</summary><pre>${escapeHtml(c.checklist)}</pre></details>` : '';
    const tools = (c.tools_used || []).length
      ? `<div class="chips">${c.tools_used.map(t => `<span class="chip">${escapeHtml(t)}</span>`).join('')}</div>`
      : '<span>无工具记录</span>';
    const flow = c.conversation || [];
    // 轨迹按需渲染:多版本看板动辄十几万轮,首次展开时才生成 HTML。
    const trajectory = !flow.length
      ? '<p>该结果没有保存对话轨迹（--trajectory off,或运行在生成轨迹前中断）。</p>'
      : `<details class="trajectory-details" data-case="${c._index}">
           <summary>展开完整轨迹：${flow.length} 条记录 ·
             ${flow.filter(t => t.type === 'ai_message').length} 条模型消息 ·
             ${flow.filter(t => t.type === 'tool_result').length} 条工具结果</summary>
           <div class="trajectory-list"><p>正在载入轨迹…</p></div></details>`;
    const metaEntries = Object.entries(c.metadata || {});
    const metaHtml = metaEntries.map(([k, v]) =>
      `<div class="meta-item"><span class="meta-key">${escapeHtml(k)}</span>
        <span class="meta-value">${escapeHtml(fmt(v))}</span></div>`).join('');
    return `<article class="case-card ${c.status}" id="case-${c._index}">
      <header class="case-head">
        <span class="status-badge">${statusLabel[c.status]}</span>
        <div class="case-title"><h2 class="case-id">${escapeHtml(c.id)}</h2>
          <div class="case-group">版本 <strong>${escapeHtml(c.version)}</strong> · ${escapeHtml(c.domain || '')}${c.version_description ? ' · ' + escapeHtml(c.version_description) : ''}</div></div>
        <div class="case-score"><strong>${verifierText}</strong><span>验证器通过</span></div>
      </header>
      <div class="case-body">
        <section class="content-panel"><h3>输入</h3>
          <pre class="text-block">${escapeHtml(c.prompt || '（无输入内容）')}</pre></section>
        <section class="content-panel"><h3>${c.error ? '运行错误' : '最终模型输出'}</h3>${output}</section>
        <section class="content-panel wide"><h3>完整多轮轨迹</h3>${trajectory}</section>
        <section class="content-panel wide"><h3>验证器</h3><div class="verifier-list">${verifiers}</div></section>
        <section class="content-panel"><h3>工具</h3>${tools}</section>
        <section class="content-panel"><h3>运行元数据</h3>
          <div class="meta-grid">
            <div class="meta-item"><span class="meta-key">开始时间</span><span class="meta-value">${escapeHtml(c.started_at)}</span></div>
            <div class="meta-item"><span class="meta-key">模型</span><span class="meta-value">${escapeHtml(c.model)}</span></div>
            <div class="meta-item"><span class="meta-key">执行时间</span><span class="meta-value">${duration(c.execution_time_ms)}</span></div>
            ${metaHtml}</div>${checklist}</section>
        <div class="source wide">源文件：${escapeHtml(c.source)}</div>
      </div></article>`;
  }
  function bindTrajectories() {
    $('case-list').querySelectorAll('.trajectory-details').forEach(details => {
      details.addEventListener('toggle', () => {
        if (!details.open || details.dataset.loaded === 'true') return;
        const c = cases[Number(details.dataset.case)];
        const target = details.querySelector('.trajectory-list');
        if (target) target.innerHTML = ((c && c.conversation) || []).map(renderTurn).join('');
        details.dataset.loaded = 'true';
      });
    });
  }
  function renderCases() {
    const items = visibleCases();
    const size = caseState.pageSize;
    const pageCount = Math.max(1, Math.ceil(items.length / size));
    caseState.page = Math.min(caseState.page, pageCount);
    const start = (caseState.page - 1) * size;
    const pageItems = items.slice(start, start + size);
    const counts = { pass: 0, fail: 0, error: 0 };
    items.forEach(c => counts[c.status] += 1);
    $('case-stats').innerHTML = `
      <div class="stat"><span class="stat-label">案例</span><strong class="stat-value">${items.length}</strong></div>
      <div class="stat"><span class="stat-label">成功</span><strong class="stat-value">${counts.pass}</strong></div>
      <div class="stat"><span class="stat-label">失败</span><strong class="stat-value">${counts.fail}</strong></div>
      <div class="stat"><span class="stat-label">错误</span><strong class="stat-value">${counts.error}</strong></div>`;
    $('case-result-line').textContent =
      `匹配 ${items.length} 个案例；当前显示 ${items.length ? start + 1 : 0}–${Math.min(start + size, items.length)}`;
    $('case-list').innerHTML = pageItems.length
      ? pageItems.map(c => renderCase({ ...c, _index: c._index })).join('')
      : '<div class="empty">没有符合当前条件的案例。</div>';
    bindTrajectories();
    $('case-pagination').innerHTML = pageCount <= 1 ? '' : `
      <button type="button" id="cs-prev" ${caseState.page <= 1 ? 'disabled' : ''}>上一页</button>
      <span>第 ${caseState.page} / ${pageCount} 页</span>
      <button type="button" id="cs-next" ${caseState.page >= pageCount ? 'disabled' : ''}>下一页</button>`;
    $('cs-prev')?.addEventListener('click', () => { caseState.page -= 1; renderCases(); scrollTo({ top: 0, behavior: 'smooth' }); });
    $('cs-next')?.addEventListener('click', () => { caseState.page += 1; renderCases(); scrollTo({ top: 0, behavior: 'smooth' }); });
  }
  function focusCase(index) {
    const c = cases[index];
    if (!c) return;
    caseState.search = c.task.toLowerCase();
    caseState.version = c.version;
    caseState.status = 'all';
    caseState.page = 1;
    $('case-search').value = c.task;
    $('case-version').value = c.version;
    renderCases();
    $('case-section').scrollIntoView({ behavior: 'smooth' });
  }

  // ---------- 交互绑定 ----------
  function init() {
    renderSummary(); renderPairs(); renderFix(); renderTaxonomy(); renderTelemetry();
    // 矩阵筛选器
    const trendOptions = [...new Set(tasks.map(t => t.trend))].sort();
    trendOptions.forEach(t => {
      const o = document.createElement('option'); o.value = t; o.textContent = t;
      $('matrix-trend').appendChild(o);
    });
    $('matrix-search').addEventListener('input', e => {
      matrixState.search = e.target.value.trim().toLowerCase(); matrixState.page = 1; renderMatrix();
    });
    $('matrix-filter').addEventListener('change', e => {
      matrixState.filter = e.target.value; matrixState.page = 1; renderMatrix();
    });
    $('matrix-trend').addEventListener('change', e => {
      matrixState.trend = e.target.value; matrixState.page = 1; renderMatrix();
    });
    $('matrix-pagesize').addEventListener('change', e => {
      matrixState.pageSize = Number(e.target.value); matrixState.page = 1; renderMatrix();
    });
    // 案例筛选器
    versions.forEach(v => {
      const o = document.createElement('option'); o.value = v.name; o.textContent = v.name;
      $('case-version').appendChild(o);
    });
    cases.forEach((c, i) => { c._index = i; });
    $('case-search').addEventListener('input', e => {
      caseState.search = e.target.value.trim().toLowerCase(); caseState.page = 1; renderCases();
    });
    $('case-version').addEventListener('change', e => {
      caseState.version = e.target.value; caseState.page = 1; renderCases();
    });
    $('case-sort').addEventListener('change', e => {
      caseState.sort = e.target.value; caseState.page = 1; renderCases();
    });
    document.querySelectorAll('[data-cstatus]').forEach(btn => {
      btn.addEventListener('click', () => {
        document.querySelectorAll('[data-cstatus]').forEach(o => o.classList.toggle('active', o === btn));
        caseState.status = btn.dataset.cstatus; caseState.page = 1; renderCases();
      });
    });
    // 导出按钮
    $('btn-export-json').addEventListener('click', () => {
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'comparison_payload.json';
      a.click(); URL.revokeObjectURL(a.href);
    });
    $('btn-export-csv').addEventListener('click', () => {
      const names = versions.map(v => v.name);
      const header = ['task', 'domain', ...names, 'trend', 'changed'];
      const lines = [header.join(',')];
      tasks.forEach(t => {
        const row = [t.key, t.domain, ...names.map(n => t.status[n] || 'absent'), t.trend, t.changed ? 1 : 0];
        lines.push(row.map(x => `"${String(x).replaceAll('"', '""')}"`).join(','));
      });
      const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'task_version_matrix.csv';
      a.click(); URL.revokeObjectURL(a.href);
    });
    renderMatrix();
    renderCases();
  }
  init();
})();
"""

HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>__CSS__</style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <h1 id="page-title"></h1>
        <p class="subtitle" id="page-subtitle"></p>
      </div>
      <div class="chips">
        <button type="button" id="btn-export-json">导出对比 JSON</button>
        <button type="button" id="btn-export-csv">导出任务矩阵 CSV</button>
      </div>
    </header>

    <div id="warnings"></div>

    <h2 class="section">一、版本总览 <small>相对 __BASELINE__ 的差异</small></h2>
    <section class="panel" id="summary-table"></section>
    <section class="panel" id="bars"></section>

    <h2 class="section">二、两两配对对比 <small>saved / regressed 与 McNemar 精确检验</small></h2>
    <section id="pairs"></section>

    <h2 class="section">三、修复生效自检 <small>确认这一卷跑的是不是修复后的代码</small></h2>
    <section class="panel" id="fix-table"></section>

    <h2 class="section">四、验证器失败构成</h2>
    <section class="panel" id="taxonomy-table"></section>

    <h2 class="section">五、遥测与环境漂移</h2>
    <section class="panel" id="tele-table"></section>

    <h2 class="section">六、逐任务跨版本矩阵 <small>点击状态格跳到案例详情</small></h2>
    <section class="toolbar" aria-label="矩阵筛选">
      <div class="field"><label for="matrix-search">搜索任务 ID 或输入</label>
        <input id="matrix-search" type="search" placeholder="例如：task_20251125、POP、draft"></div>
      <div class="field"><label for="matrix-filter">范围</label>
        <select id="matrix-filter">
          <option value="changed" selected>只看有差异的任务</option>
          <option value="all">全部任务</option>
          <option value="improved">只看改好或稳定通过的</option>
          <option value="worsened">只看回退或稳定失败的</option>
        </select></div>
      <div class="field"><label for="matrix-trend">趋势</label>
        <select id="matrix-trend"><option value="all">全部趋势</option></select></div>
      <div class="field"><label for="matrix-pagesize">每页</label>
        <select id="matrix-pagesize">
          <option value="20">20</option><option value="40" selected>40</option>
          <option value="100">100</option><option value="1000">全部</option>
        </select></div>
    </section>
    <section class="panel" id="matrix-table" style="margin-top:14px;"></section>
    <nav class="pagination" id="matrix-pagination"></nav>

    <h2 class="section" id="case-section">七、案例详情 <small>输入 / 轨迹 / 输出 / 验证器 / 元数据</small></h2>
    <section class="toolbar" aria-label="案例筛选">
      <div class="field"><label for="case-search">搜索</label>
        <input id="case-search" type="search" placeholder="任务 ID、输入、输出、工具、验证器"></div>
      <div class="field"><label for="case-version">版本</label>
        <select id="case-version"><option value="all">全部版本</option></select></div>
      <div class="field"><label for="case-sort">排序</label>
        <select id="case-sort">
          <option value="status">失败优先</option>
          <option value="verifier">验证器通过率低优先</option>
          <option value="duration">耗时长优先</option>
          <option value="group">版本与任务 ID</option>
        </select></div>
      <div class="field" style="justify-content:end;">
        <div class="chips">
          <button type="button" class="active" data-cstatus="all">全部</button>
          <button type="button" data-cstatus="pass">成功</button>
          <button type="button" data-cstatus="fail">失败</button>
          <button type="button" data-cstatus="error">错误</button>
        </div>
      </div>
    </section>
    <section class="stats" id="case-stats"></section>
    <p class="result-line" id="case-result-line"></p>
    <section class="case-list" id="case-list"></section>
    <nav class="pagination" id="case-pagination"></nav>
  </main>
  <script id="dashboard-data" type="application/json">__DATA__</script>
  <script>__JS__</script>
</body>
</html>
"""


def _dumps_for_html(obj: Any) -> str:
    """把 payload 塞进 <script type="application/json"> 时,必须打断 `</script>`。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render_html(title: str, subtitle: str, payload: Dict[str, Any]) -> str:
    out = HTML_TEMPLATE
    out = out.replace("__CSS__", CSS)
    out = out.replace("__JS__", JS)
    out = out.replace("__DATA__", _dumps_for_html(payload))
    out = out.replace("__TITLE__", _html.escape(title))
    out = out.replace("__BASELINE__", _html.escape(payload["baseline"]))
    return out


# ---------------------------------------------------------------------------
# 文本 / Markdown / JSON 摘要
# ---------------------------------------------------------------------------

def print_summary(versions: List[Dict[str, Any]], pairs: List[Dict[str, Any]],
                  baseline: str) -> None:
    base = next(v for v in versions if v["name"] == baseline)
    name_w = max([_dwidth("版本")] + [_dwidth(v["name"]) for v in versions]) + 2
    print("=" * 78)
    print("版本总览")
    print("=" * 78)
    print(_pad("版本", name_w)
          + _pad("任务", 5, True) + _pad("成功", 6, True) + _pad("失败", 6, True)
          + _pad("错误", 6, True) + _pad("成功率", 9, True) + _pad("剔错", 9, True)
          + _pad("验证器", 9, True) + _pad("cs口径", 9, True) + _pad("Δpp", 9, True))
    for v in versions:
        delta = (v["success_rate_raw"] - base["success_rate_raw"]) * 100
        tag = " (基线)" if v["name"] == baseline else ""
        print(_pad(v["name"], name_w)
              + _pad(v["n_tasks"], 5, True) + _pad(v["pass"], 6, True)
              + _pad(v["fail"], 6, True) + _pad(v["error"], 6, True)
              + _pad(_pct(v["success_rate_raw"]), 9, True)
              + _pad(_pct(v["success_rate_clean"]), 9, True)
              + _pad(_pct(v["verifier_rate"]), 9, True)
              + _pad(_pct(v["cs_verifier_rate"]), 9, True)
              + _pad(("—" if v["name"] == baseline else f"{delta:+.1f}"), 9, True)
              + tag)
    print("  成功率 = 通过/总数(与 compute_score.py 一致);"
          "验证器 = 合并口径;cs口径 = compute_score 逐文件均值(error 按 0 计入)")
    for p in pairs:
        print("-" * 78)
        print(f"{p['baseline']} → {p['variant']}:配对 {p['paired']}  "
              f"saved {p['saved']} / regressed {p['regressed']}  "
              f"both_pass {p['both_pass']} / both_fail {p['both_fail']}  "
              f"Δ{p['delta_pp']:+.1f}pp  McNemar p={p['mcnemar_p']:.4f}"
              f"{' [显著]' if p['mcnemar_sig'] else ''}")
        if p["saved_tasks"]:
            print(f"  saved    : {', '.join(p['saved_tasks'][:15])}"
                  + (" …" if len(p["saved_tasks"]) > 15 else ""))
        if p["regressed_tasks"]:
            print(f"  regressed: {', '.join(p['regressed_tasks'][:15])}"
                  + (" …" if len(p["regressed_tasks"]) > 15 else ""))
        for note in p["notes"]:
            print(f"  ! {note}")


def write_report(prefix: str, payload: Dict[str, Any]) -> Tuple[str, str]:
    """写出机器可读摘要。JSON 里剥掉完整对话轨迹(HTML 已有),
    只留统计、配对、矩阵与案例层面的判定信息,便于二次分析。"""
    json_path, md_path = f"{prefix}.json", f"{prefix}.md"
    slim = dict(payload)
    slim["cases"] = [
        {k: v for k, v in c.items() if k != "conversation"}
        for c in payload.get("cases", [])
    ]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False, indent=2)

    versions = payload["versions"]
    base = payload["baseline"]
    lines = [f"# {payload['title']}", "",
             f"生成时间:{payload['generated_at']}", "",
             f"基线:**{base}**", "", "## 版本总览", "",
             "| 版本 | 任务 | 成功 | 失败 | 错误 | 成功率(原始) | 成功率(剔错) | 验证器(合并) | 验证器(cs口径) | Δ vs 基线 | 均值耗时 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    b = next(v for v in versions if v["name"] == base)
    for v in versions:
        delta = (v["success_rate_raw"] - b["success_rate_raw"]) * 100
        lines.append(
            f"| {v['name']} | {v['n_tasks']} | {v['pass']} | {v['fail']} | {v['error']} | "
            f"{_pct(v['success_rate_raw'])} | {_pct(v['success_rate_clean'])} | "
            f"{_pct(v['verifier_rate'])} | {_pct(v['cs_verifier_rate'])} | "
            f"{'—' if v['name'] == base else f'{delta:+.1f}pp'} | "
            f"{v['runtime_mean_ms'] / 1000:.0f}s |")
    lines += ["", "## 两两配对", ""]
    for p in payload["pairs"]:
        lines += [
            f"### {p['baseline']} → {p['variant']}",
            "",
            f"- 配对 {p['paired']}:saved {p['saved']} / regressed {p['regressed']} / "
            f"both_pass {p['both_pass']} / both_fail {p['both_fail']}",
            f"- Δ成功率 {p['delta_pp']:+.1f}pp;McNemar p = {p['mcnemar_p']:.4f}"
            f"{'(显著)' if p['mcnemar_sig'] else '(未达显著)'}",
            f"- saved: {', '.join(p['saved_tasks']) or '无'}",
            f"- regressed: {', '.join(p['regressed_tasks']) or '无'}",
        ]
        for note in p["notes"]:
            lines.append(f"- ⚠️ {note}")
        lines.append("")
    lines += ["## 修复生效自检", "",
              "| 版本 | 信封含 isError | 内层错误 | 被掩蔽 | 名修复 | 旧文案 | 新文案 | 判定 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for v in versions:
        f = v["fix"]
        lines.append(
            f"| {v['name']} | {f.get('envelope_iserror_keys', 0)} | {f.get('inner_errors', 0)} | "
            f"{f.get('masked_errors', 0)} | {f.get('name_repairs') if f.get('name_repairs') is not None else '—'} | "
            f"{f.get('old_message_hits', 0)} | {f.get('new_message_hits', 0)} | "
            f"{'生效' if f.get('effective') else '未生效'} |")
    warns = payload.get("warnings") or []
    lines += ["", "## 告警", ""]
    lines += [f"- {w}" for w in warns] or ["- 无"]
    lines.append("")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return json_path, md_path


# ---------------------------------------------------------------------------
# 对比配置清单(manifest)与 main
# ---------------------------------------------------------------------------

def _slug(text: str) -> str:
    """把标签转成安全的文件名片段(保留中文,只替换路径非法字符)。"""
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", str(text).strip())
    return s.strip("._") or "comparison"


def _direct_result_files(folder: str) -> List[str]:
    """只看该目录本身(不递归)的 results_*.json。"""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in sorted(names)
            if n.endswith(".json") and not n.startswith("_") and n.startswith("results_")]


def _discover_dirs(roots: Sequence[str]) -> List[Dict[str, Any]]:
    """扫描根目录,找出**直接**含 results_*.json 的目录(不把父目录也算一份)。"""
    found: List[Dict[str, Any]] = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for cur, _dirs, _files in os.walk(root):
            files = _direct_result_files(cur)
            if not files:
                continue
            domain = ""
            try:
                with open(files[0], "r", encoding="utf-8") as f:
                    data = json.load(f)
                domain = _domain_of(os.path.basename(files[0]), data)
            except (OSError, json.JSONDecodeError):
                pass
            found.append({"dir": cur.replace(os.sep, "/"),
                          "domain": domain, "n_tasks": len(files)})
    found.sort(key=lambda x: x["dir"])
    return found


def _suggest_name(dirpath: str, used: set) -> str:
    """从路径末两段拼一个可读且唯一的默认标签:out/a/run_1 → a_run_1。"""
    parts = [p for p in dirpath.replace("\\", "/").split("/") if p and p != "out"]
    base = "_".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else dirpath)
    name, i = base, 2
    while name in used:
        name = f"{base}_{i}"
        i += 1
    used.add(name)
    return name


def _yaml_scalar(value: Any) -> str:
    """把标量写成 YAML。字符串一律加双引号,避免中括号、冒号、`=` 等被误解析。"""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


YAML_TEMPLATE_TAIL = """
# ---------------------------------------------------------------- 怎么改
#
# ① 加一个版本(最常用):在 versions: 下面照抄一条,改这几处 ——
#
#   - name: "调了 XXX 的第 3 卷"      # 看板上的标签,随便写中文,组内不能重名
#     dir: "out/xxx/run_1"            # 含 results_*.json 的那一层目录
#     domain: "email"                 # 跟别的版本同域才能逐题对比
#     description: "把 dispatch 改成 exec"
#     baseline: false                 # 整组只能有一个 true
#     expect_fixed: false             # true = 这卷应该已带修复,实际没生效会告警
#
# ② 加一组对比(换域、或换一个话题):把顶层的 versions: 整段挪到 comparisons: 下面,
#    写成下面这样两组,每组各出一张看板,外加一个 <本文件名>_index.html 索引导航。
#    组内共用字段(name/description/domain/trajectory)写在组里,会盖过顶层的同名字段。
#    下面这段去掉每行开头的 "#" 就能直接用:
#
# comparisons:
#   - name: "email 三版本"
#     description: "email 域:修复前后"
#     domain: "email"
#     versions:
#       - {name: "修复前", dir: "out/email_fix_nomem/run_1", baseline: true}
#       - {name: "修复后", dir: "out/email_fixed/run_1", expect_fixed: true}
#   - name: "hr 检索后端对比"
#     description: "hr 域:检索后端对比"
#     domain: "hr"
#     versions:
#       - {name: "tfidf", dir: "out/full_tfidf/run_1", baseline: true}
#       - {name: "hybrid", dir: "out/full_hybrid/run_1"}
#
# ③ 只想临时换个口径就不用改本文件,命令行会盖过配置:
#    --trajectory off / --baseline "某版本" / --out other.html
#
# 字段速查:
#   title / description     这组对比的标题与说明(description 显示在看板副标题)
#   domain                  只纳入该域;留空 "" 表示不按域过滤(每个版本还能单独覆盖)
#   trajectory              full 全量轨迹 / trim 截断(推荐) / off 不带(文件最小)
#   trajectory_limit        trim 时单条内容的字符上限
#   versions[].name         看板上显示的标签,随便写中文,组内唯一
#   versions[].dir          结果目录(含 results_*.json 的那一层,如 out/xxx/run_1)
#   versions[].baseline     是否作为对比基线;每组只能标一个,不标则用第一个
#   versions[].expect_fixed true 表示这卷应该已经带上修复;若实际没生效会告警
"""


def _group_by_domain(dirs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按检测到的域把目录分组,保持扫描顺序;域没识别出来的归到最后。"""
    groups: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for d in dirs:
        key = d["domain"] or ""
        grp = seen.get(key)
        if grp is None:
            grp = {"domain": key, "label": key or "未识别域", "members": []}
            seen[key] = grp
            groups.append(grp)
        grp["members"].append(d)
    # 未识别域排最后,免得夹在中间干扰阅读
    groups.sort(key=lambda g: g["domain"] == "")
    return groups


def _yaml_version_lines(d: Dict[str, Any], index: int, indent: str = "  ") -> List[str]:
    """一条版本记录,连同它上面那三行说明注释(前面空一行,便于肉眼分隔)。"""
    bar = indent + "# " + "-" * 68
    head = ["", bar,
            f"{indent}# 版本 {index}:检测到域={d['domain'] or '?'},任务数={d['n_tasks']}"]
    if d["n_tasks"] <= 2:
        head.append(f"{indent}# ⚠ 只有 {d['n_tasks']} 个任务,像是试跑或中断卷,"
                    f"确认是否真要纳入对比")
    head.append(bar)
    return head + [
        f"{indent}- name: {_yaml_scalar(d['name'])}",
        f"{indent}  dir: {_yaml_scalar(d['dir'])}",
        f"{indent}  domain: {_yaml_scalar(d['domain'])}",
        f"{indent}  description: {_yaml_scalar('')}  # 写清这个版本做了什么改动",
        f"{indent}  baseline: {_yaml_scalar(bool(d['baseline']))}",
        f"{indent}  expect_fixed: {_yaml_scalar(bool(d['expect_fixed']))}",
    ]


def _dump_yaml_config(cfg: Dict[str, Any], dirs: List[Dict[str, Any]]) -> str:
    """手写 YAML 模板(不用 yaml.dump,因为要带中文注释与固定字段顺序)。

    扫到多个域时**自动按域分组**成 comparisons: —— 把不同域塞进同一个 versions:
    再比,题目集合不重叠,逐题矩阵会全是空的,是最容易踩的坑。
    """
    groups = _group_by_domain(dirs)
    lines = [
        "# EnterpriseOps-Gym 多版本对比配置",
        "#",
        "# 用法:python scripts/render_comparison_dashboard.py --config <本文件>",
        "#",
        "# 要改的就三处:① 给每个版本写清 name 标签;② 写 description 说明这次改了什么;",
        "# ③ 检查 baseline 标在哪一项上(true 的那个就是被拿来当参照的卷)。",
        "#",
        "# 目录有新增想重扫一遍:换个新路径再跑,或加 --force 覆盖本文件(会丢掉注释与你的改动)。",
        "",
        f"title: {_yaml_scalar(cfg['title'])}",
        f"description: {_yaml_scalar(cfg['description'])}",
        "",
        "# 轨迹体积:full 全量 / trim 截断(推荐) / off 不带",
        f"trajectory: {_yaml_scalar(cfg['trajectory'])}",
        f"trajectory_limit: {cfg['trajectory_limit']}",
        "",
    ]
    if len(groups) <= 1:
        lines += [
            '# 只纳入该域(留空 "" 表示不按域过滤 / 每个版本还能单独覆盖)',
            f"domain: {_yaml_scalar(cfg['domain'])}",
            "",
            "# 本次扫描只发现 1 个域,所以用单组写法(versions: 直接挂顶层)。",
            "# 以后要一次跑多个域,把下面整段挪到 comparisons: 下面(格式见文件末尾)。",
            "versions:",
        ]
        for i, d in enumerate(dirs, 1):
            lines += _yaml_version_lines(d, i)
    else:
        lines += [
            "# ---",
            f"# 本次扫描发现 {len(groups)} 个域,已按域分成 {len(groups)} 组。"
            f"不同域的题目集合不重叠,",
            "# 混在一组里逐题对比会全是空的,所以这里自动分开;每组各出一张看板,",
            "# 并额外生成 <本文件名>_index.html 索引导航页。",
            "# ---",
            "comparisons:",
        ]
        for gi, grp in enumerate(groups, 1):
            lines += [
                "",
                "  # " + "=" * 68,
                f"  # 组 {gi}/{len(groups)}:域 = {grp['label']}"
                f"({len(grp['members'])} 个版本)",
                "  # " + "=" * 68,
                f"  - name: {_yaml_scalar(grp['label'] + ' 对比')}",
                f"    description: {_yaml_scalar('')}  # 写清这组在比什么",
            ]
            if grp["domain"]:
                lines.append(f"    domain: {_yaml_scalar(grp['domain'])}")
            lines.append("    versions:")
            for i, d in enumerate(grp["members"], 1):
                lines += _yaml_version_lines(d, i, indent="      ")
    return "\n".join(lines) + "\n" + YAML_TEMPLATE_TAIL


def write_init_config(path: str, roots: Sequence[str], force: bool = False) -> List[Dict[str, Any]]:
    """扫描目录,生成一份待填的对比配置模板(YAML 或 JSON,看扩展名)。

    基线的默认位置:只扫到一个域时给整个列表的第一项;扫到多个域时**每组各自**
    给第一项,因为多域会自动分成多个 comparisons 块,每块都需要自己的基线。
    """
    if os.path.exists(path) and not force:
        sys.exit(f"[compare] {path} 已存在。它可能已经有你写好的 name/description,"
                 f"所以默认不覆盖。要重新生成请加 --force,或换个新路径。")
    dirs = _discover_dirs(roots)
    if not dirs:
        sys.exit(f"[compare] 在 {list(roots)} 下没找到含 results_*.json 的目录")
    used: set = set()
    for d in dirs:
        d["name"] = _suggest_name(d["dir"], used)
        d["baseline"] = False
        d["expect_fixed"] = False

    groups = _group_by_domain(dirs)
    if len(groups) <= 1:
        dirs[0]["baseline"] = True
    else:
        for grp in groups:
            grp["members"][0]["baseline"] = True

    def _version_obj(d: Dict[str, Any]) -> Dict[str, Any]:
        return {"name": d["name"], "dir": d["dir"], "domain": d["domain"],
                "description": "", "baseline": d["baseline"],
                "expect_fixed": d["expect_fixed"]}

    cfg = {
        "title": "EnterpriseOps-Gym 多版本对比",
        "description": "在 description 里写清这组对比的目的、每个版本做了什么改动",
        "domain": "",
        "trajectory": "trim",
        "trajectory_limit": 1200,
    }
    if os.path.splitext(path)[1].lower() == ".json":
        payload = dict(cfg)
        if len(groups) > 1:
            payload.pop("domain", None)
            payload["comparisons"] = [
                dict({"name": grp["label"] + " 对比", "description": "",
                      "versions": [_version_obj(d) for d in grp["members"]]},
                     **({"domain": grp["domain"]} if grp["domain"] else {}))
                for grp in groups]
        else:
            payload["versions"] = [_version_obj(d) for d in dirs]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_dump_yaml_config(cfg, dirs))
    return dirs


def load_config(path: str) -> Dict[str, Any]:
    """读配置。按扩展名分派:yaml/yml 走 YAML,json 走 JSON,其它先试 YAML。"""
    if not os.path.isfile(path):
        sys.exit(f"[compare] 配置文件不存在:{path}")
    try:
        # utf-8-sig:在 Windows 上用记事本/VSCode 存过的文件常带 BOM
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except OSError as exc:
        sys.exit(f"[compare] 读不了配置文件 {path}:{exc}")

    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        try:
            cfg = json.loads(text)
        except json.JSONDecodeError as exc:
            sys.exit(f"[compare] 配置文件不是合法 JSON({path}):{exc}")
    else:
        if yaml is None:
            sys.exit(
                f"[compare] 读 YAML 配置需要 PyYAML(缺依赖)。二选一:\n"
                f"          · pip install pyyaml\n"
                f"          · 或者把配置写成 .json(扩展名用 .json 即可,JSON 不需要额外依赖)")
        try:
            cfg = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" 第 {mark.line + 1} 行第 {mark.column + 1} 列" if mark else ""
            sys.exit(f"[compare] 配置文件不是合法 YAML({path}){where}:"
                     f"{getattr(exc, 'problem', exc)}")

    if cfg is None:
        sys.exit(f"[compare] 配置文件是空的:{path}")
    if not isinstance(cfg, dict):
        sys.exit(f"[compare] 配置文件顶层必须是映射(键值对),实际是 "
                 f"{type(cfg).__name__}:{path}")
    # 防呆:有人会把生成的报告当成配置传进来 —— 报告里有 generated_at 与 noise_floor_pp,
    # 而它的 versions 只是结果快照,不是配置语义。
    if "generated_at" in cfg and "noise_floor_pp" in cfg:
        sys.exit(f"[compare] {path} 看起来是本脚本生成的**报告**,不是对比配置。"
                 "配置需要 versions 数组(每项含 name/dir/description)。")
    return cfg


def _parse_version_arg(raw: str) -> Tuple[str, str]:
    """`NAME=DIR` 或裸 DIR(标签取目录名)。"""
    if "=" in raw:
        name, folder = raw.split("=", 1)
        return name.strip(), folder.strip()
    folder = raw
    name = os.path.basename(os.path.normpath(folder)) or folder
    return name, folder


def _build_specs(args: argparse.Namespace) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """把命令行参数与配置文件归一成一组 comparison spec。

    优先级:命令行 > 配置块 > 全局默认。配置文件两种写法都支持:
      · 单对比:顶层直接给 versions 数组;
      · 多对比:顶层给 comparisons 数组,每项是一个单对比结构(可继承顶层字段)。
    """
    cli = dict(title=args.title, domain=args.domain, baseline=args.baseline,
               out=args.out, report=args.report, trajectory=args.trajectory,
               trajectory_limit=args.trajectory_limit)

    cfg_path = args.config
    cfg: Optional[Dict[str, Any]] = None
    if cfg_path:
        cfg = load_config(cfg_path)
    elif not args.version and not args.dirs:
        for cand in ("compare.yaml", "compare.yml", "compare.json"):
            if os.path.isfile(cand):
                cfg_path, cfg = cand, load_config(cand)
                print(f"[compare] 未指定 --config,自动使用 {cand}")
                break

    blocks: List[Dict[str, Any]] = []
    if cfg is not None:
        globalowed = {k: v for k, v in cfg.items()
                      if k not in ("comparisons", "versions")}
        comps = cfg.get("comparisons")
        if comps is not None:
            if not isinstance(comps, list) or not comps:
                sys.exit("[compare] 配置里的 comparisons 必须是非空数组")
            for i, blk in enumerate(comps, 1):
                if not isinstance(blk, dict):
                    sys.exit(f"[compare] comparisons[{i}] 必须是对象")
                merged = dict(globalowed)
                merged.update(blk)
                merged.setdefault("name", f"comparison{i}")
                if "versions" not in merged:
                    sys.exit(f"[compare] comparisons[{i}] 缺少 versions 数组")
                blocks.append(merged)
        else:
            if not isinstance(cfg.get("versions"), list) or not cfg["versions"]:
                sys.exit("[compare] 配置里缺少非空的 versions 数组")
            blocks.append({k: v for k, v in cfg.items() if k != "comparisons"})
    else:
        raw_versions: List[Dict[str, str]] = []
        for raw in list(args.version) + list(args.dirs):
            n, d = _parse_version_arg(raw)
            raw_versions.append({"name": n, "dir": d})
        if not raw_versions:
            sys.exit("[compare] 至少给一个 --config,或 --version NAME=DIR,或一个结果目录")
        blocks.append({"versions": raw_versions})

    specs: List[Dict[str, Any]] = []
    for blk in blocks:
        versions: List[Dict[str, Any]] = []
        for i, item in enumerate(blk["versions"], 1):
            if isinstance(item, str):
                n, d = _parse_version_arg(item)
                item = {"name": n, "dir": d}
            if not isinstance(item, dict) or not item.get("dir"):
                sys.exit(f"[compare] 版本 #{i} 缺少 dir 字段")
            folder = str(item["dir"])
            versions.append({
                "name": str(item.get("name")
                            or os.path.basename(os.path.normpath(folder)) or folder),
                "dir": folder,
                # 优先级:命令行 > 该版本自己写的 domain > 配置块全局 domain
                "domain": (cli["domain"] or item.get("domain")
                           or blk.get("domain") or None),
                "description": str(item.get("description") or "").strip(),
                "baseline": bool(item.get("baseline")),
                "expect_fixed": bool(item.get("expect_fixed")),
            })

        names = [v["name"] for v in versions]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            sys.exit(f"[compare] 版本标签重复:{dup};每个 name 必须唯一")
        marked = [v["name"] for v in versions if v["baseline"]]
        if len(marked) > 1 and not cli["baseline"]:
            sys.exit(f"[compare] 配置里标记了多个 baseline:{marked};只能标一个")
        # 优先级:命令行 --baseline > 配置里 baseline:true 的那一项 > 配置块 baseline > 第一个
        baseline = (cli["baseline"] or (marked[0] if marked else None)
                    or blk.get("baseline") or names[0])
        if baseline not in names:
            sys.exit(f"[compare] baseline={baseline} 不在版本列表 {names}")

        # 命令行显式给了就盖过配置(否则配置里写 trim、命令行想临时看 full 会改不动)
        trajectory = cli["trajectory"] or blk.get("trajectory") or "full"
        if trajectory not in ("full", "trim", "off"):
            sys.exit(f"[compare] trajectory 只能是 full/trim/off,收到 {trajectory!r}")
        specs.append({
            "name": str(blk.get("name") or baseline),
            "title": str(cli["title"] or blk.get("title")
                         or "EnterpriseOps-Gym 多版本对比看板"),
            "description": str(args.subtitle or blk.get("description") or "").strip(),
            "baseline": baseline,
            "versions": versions,
            "trajectory": trajectory,
            "trajectory_limit": int(cli["trajectory_limit"]
                                    or blk.get("trajectory_limit") or 6000),
            "out": cli["out"] or blk.get("out") or None,
            "report": cli["report"] or blk.get("report") or None,
            "expect_fixed": list(args.expect_fixed or []),
        })

    if len(specs) > 1:
        # 多组模式下 --out/--report 是给所有组共用的,**必须给每组加组名后缀**,
        # 否则后一组会把前一组的产物直接覆盖掉,而且不报错(只在索引页上表现为
        # 好几个组指向同一个文件)。组名重名同样会导致覆盖,直接拦下。
        names = [s["name"] for s in specs]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            sys.exit(f"[compare] 多组模式下的组名重复:{dup};"
                     f"请给每个 comparisons[i].name 取唯一名字,否则产物会互相覆盖")
        for s in specs:
            if cli["out"] and s["out"]:
                stem_o, ext = os.path.splitext(s["out"])
                s["out"] = f"{stem_o}__{_slug(s['name'])}{ext}"
            if cli["report"] and s["report"]:
                s["report"] = f"{s['report']}__{_slug(s['name'])}"
    return cfg_path, specs


def _default_paths(cfg_path: Optional[str], spec: Dict[str, Any],
                   multi: bool) -> Tuple[str, Optional[str]]:
    """按配置文件的名字给产物命名:compare_email.json → compare_email.html。

    摘要默认加 `_摘要` 后缀 —— 配置本身也是 .json,同名前缀会把配置覆盖掉。
    """
    stem = os.path.splitext(cfg_path)[0] if cfg_path else None
    suffix = f"__{_slug(spec['name'])}" if multi else ""

    if spec.get("out"):
        out = spec["out"]
    elif stem:
        out = f"{stem}{suffix}.html"
    else:
        out = "out/comparison_dashboard.html"

    if spec.get("report"):
        report: Optional[str] = spec["report"]
    elif stem:
        report = f"{stem}{suffix}_摘要"
    else:
        report = None
    return out, report


def _guard_collision(cfg_path: Optional[str], out_path: str,
                     report_prefix: Optional[str]) -> None:
    """别让产物把配置文件本身覆盖掉(配置也是 .json)。"""
    if not cfg_path:
        return
    cfg_abs = os.path.abspath(cfg_path)
    if os.path.abspath(out_path) == cfg_abs:
        sys.exit(f"[compare] 输出路径会覆盖配置文件本身:{out_path};请改 --out")
    if report_prefix and os.path.abspath(report_prefix + ".json") == cfg_abs:
        sys.exit(f"[compare] 摘要路径会覆盖配置文件本身:{report_prefix}.json;请改 --report")


def run_one(spec: Dict[str, Any], cfg_path: Optional[str], multi: bool) -> Dict[str, Any]:
    """跑完一个对比:载入各版本 → 出 HTML/摘要 → 打印终端总览。"""
    # 预检:一次性把所有配错的地方报出来,不要载入到一半才炸
    problems: List[str] = []
    for item in spec["versions"]:
        if not os.path.isdir(item["dir"]):
            problems.append(f"  {item['name']}:目录不存在 {item['dir']}")
        elif not _iter_result_files(item["dir"]):
            problems.append(f"  {item['name']}:{item['dir']} 下没有 results_*.json")
    if problems:
        sys.exit("[compare] 以下版本无法载入,请检查配置:\n" + "\n".join(problems))

    versions: List[Dict[str, Any]] = []
    names: List[str] = []
    for item in spec["versions"]:
        print(f"[compare] 载入 {item['name']} ← {item['dir']}"
              + (f"  [{item['description']}]" if item["description"] else ""))
        v = load_version(item["name"], item["dir"], item["domain"],
                         spec["trajectory"], spec["trajectory_limit"],
                         description=item["description"],
                         expect_fixed=item["expect_fixed"])
        versions.append(v)
        names.append(item["name"])
        print(f"          任务 {v['n_tasks']}(成功 {v['pass']} / 失败 {v['fail']} / "
              f"错误 {v['error']})  成功率 {_pct(v['success_rate_raw'])}")

    baseline = spec["baseline"]
    pairs = build_pairs(versions, baseline)
    matrix = build_matrix(versions, baseline)
    warnings = build_warnings(versions, pairs, baseline, spec["expect_fixed"])
    chart = build_chart(versions, baseline)

    all_cases: List[Dict[str, Any]] = []
    for v in versions:
        all_cases.extend(v["cases"])

    subtitle = spec["description"] or (
        f"基线 {baseline};共 {len(versions)} 个版本 / {len(matrix)} 个任务。"
        "对比统计、逐任务矩阵与配对显著性,并自带修复生效自检与跨批可比性守卫。")
    payload = {
        "title": spec["title"],
        "subtitle": subtitle,
        "generated_at": _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
        "baseline": baseline,
        "noise_floor_pp": NOISE_FLOOR_PP,
        "versions": [{k: val for k, val in v.items() if k not in ("tasks", "cases")}
                     for v in versions],
        "pairs": pairs,
        "tasks": matrix,
        "cases": all_cases,
        "chart": chart,
        "warnings": warnings,
    }

    out_path, report_prefix = _default_paths(cfg_path, spec, multi)
    _guard_collision(cfg_path, out_path, report_prefix)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    html_text = render_html(spec["title"], subtitle, payload)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_text)

    print_summary(versions, pairs, baseline)
    print("=" * 78)
    if warnings:
        print("告警:")
        for w in warnings:
            print(f"  ! {w}")
    else:
        print("告警:无")
    print("=" * 78)
    print(f"看板已写:{out_path}  ({len(html_text) / 1024 / 1024:.2f} MB,"
          f"案例 {len(all_cases)} 条,轨迹={spec['trajectory']}"
          + (f"(上限 {spec['trajectory_limit']} 字符)" if spec["trajectory"] == "trim" else "")
          + ")")
    reports = None
    if report_prefix:
        reports = write_report(report_prefix, payload)
        print(f"摘要已写:{reports[0]} / {reports[1]}")
    return {"name": spec["name"], "title": spec["title"],
            "description": spec["description"], "base": baseline,
            "out": out_path, "report": reports,
            "versions": [v for v in payload["versions"]],
            "n_tasks": len(matrix), "warnings": warnings}


INDEX_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__
.entry { margin-top: 14px; }
.entry h3 { margin: 0 0 6px; }
.entry a { font-weight: 700; }
.entry .desc { color: var(--muted); margin: 4px 0 8px; }
</style>
</head>
<body>
<main class="shell">
  <h1>__TITLE__</h1>
  <p class="subtitle">生成时间 __AT__ |共 __N__ 组对比</p>
  <section class="panel" style="margin-top:16px;">
    <div class="table-wrap"><table>
      <thead><tr><th>对比</th><th>说明</th><th>版本</th><th class="num">任务</th>
        <th>各版本成功率</th><th>看板</th></tr></thead>
      <tbody>__ROWS__</tbody>
    </table></div>
  </section>
  <section>__ENTRIES__</section>
</main>
</body>
</html>
"""


def write_index(path: str, title: str, entries: List[Dict[str, Any]]) -> str:
    rows, cards = [], []
    for e in entries:
        rates = " · ".join(
            f"{v['name']} {_pct(v['success_rate_raw'])}" for v in e["versions"])
        rel = os.path.basename(e["out"])
        warns = len(e["warnings"])
        wtag = f' <span class="badge fail">{warns} 条告警</span>' if warns else ""
        rows.append(
            f"<tr><td><strong>{_html.escape(e['name'])}</strong>{wtag}</td>"
            f"<td>{_html.escape(e['description'] or '—')}</td>"
            f"<td>{len(e['versions'])}</td>"
            f"<td class='num'>{e['n_tasks']}</td>"
            f"<td>{_html.escape(rates)}</td>"
            f"<td><a href=\"{_html.escape(rel)}\">打开</a></td></tr>")
        vs = "".join(
            f"<li><strong>{_html.escape(v['name'])}</strong> "
            f"<span class='chip'>{_pct(v['success_rate_raw'])}</span>"
            f"{' <span class=\"chip\">基线</span>' if v['name'] == e['base'] else ''}"
            f"<div class='desc'>{_html.escape(v.get('description') or '')}</div>"
            f"<div class='source'>{_html.escape(v['dir'])}</div></li>"
            for v in e["versions"])
        cards.append(
            f"<div class='panel entry'><h3>{_html.escape(e['name'])}</h3>"
            f"<div class='desc'>{_html.escape(e['description'] or '')}</div>"
            f"<a href=\"{_html.escape(os.path.basename(e['out']))}\">打开对比看板</a>"
            f"<ul>{vs}</ul></div>")
    html_text = (INDEX_TEMPLATE
                 .replace("__CSS__", CSS)
                 .replace("__TITLE__", _html.escape(title))
                 .replace("__AT__", _dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"))
                 .replace("__N__", str(len(entries)))
                 .replace("__ROWS__", "".join(rows))
                 .replace("__ENTRIES__", "".join(cards)))
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, metavar="PATH",
                    help="对比配置(YAML 推荐,也支持 JSON);不指定时若存在 "
                         "compare.yaml/compare.yml/compare.json 会自动使用")
    ap.add_argument("--init-config", default=None, metavar="PATH",
                    help="扫描结果目录并生成一份带注释的 YAML 配置模板,然后退出"
                         "(扩展名写 .json 则生成 JSON)")
    ap.add_argument("--scan", action="append", default=[], metavar="DIR",
                    help="--init-config 的扫描根目录(可重复,默认 out)")
    ap.add_argument("--force", action="store_true",
                    help="--init-config 时允许覆盖已存在的配置文件")
    ap.add_argument("--version", action="append", default=[], metavar="NAME=DIR",
                    help="一个版本:标签=结果目录(可重复;与 --config 二选一)")
    ap.add_argument("dirs", nargs="*", help="未加标签的结果目录(标签取目录名)")
    ap.add_argument("--baseline", default=None, help="基线版本标签(默认配置里标 baseline 的,或第一个)")
    ap.add_argument("--out", default=None, help="HTML 输出路径(默认跟随配置文件名)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--subtitle", default=None,
                    help="这组对比的一句话说明(等价于配置里的 description)")
    ap.add_argument("--report", default=None,
                    help="摘要输出前缀(生成 .json/.md;默认跟随配置文件名)")
    ap.add_argument("--domain", default=None, help="只纳入该域(如 email / hr)")
    ap.add_argument("--trajectory", choices=("full", "trim", "off"), default=None,
                    help="轨迹嵌入方式:full 全量 / trim 截断 / off 不带(HTML 更小)")
    ap.add_argument("--trajectory-limit", type=int, default=None,
                    help="--trajectory trim 时单条 content/result 的字符上限")
    ap.add_argument("--expect-fixed", action="append", default=[],
                    help="期望已经带上修复的版本标签(可重复);未生效会告警")
    args = ap.parse_args()

    if args.init_config:
        roots = args.scan or ["out"]
        dirs = write_init_config(args.init_config, roots, force=args.force)
        print(f"[compare] 已扫描 {list(roots)},找到 {len(dirs)} 个结果目录:")
        for d in dirs:
            mark = "  ⚠ 任务数偏少,像试跑或中断卷" if d["n_tasks"] <= 2 else ""
            print(f"  - {d['dir']}  域={d['domain'] or '?'}  任务={d['n_tasks']}{mark}")
        groups = _group_by_domain(dirs)
        if len(groups) > 1:
            print(f"[compare] 扫到 {len(groups)} 个域,模板已按域分成 {len(groups)} 组"
                  f"(每组一张看板 + 一个索引导航;不同域题目不重叠,混着比没意义):")
            for gi, grp in enumerate(groups, 1):
                print(f"          组 {gi}:域={grp['label']}  版本={len(grp['members'])}")
        print(f"[compare] 模板已写:{args.init_config}")
        print("          请编辑它:给每个版本写清 name 与 description,"
              "确认 baseline 标在哪一项,然后运行")
        print(f"          python scripts/{os.path.basename(__file__)} "
              f"--config {args.init_config}")
        return

    cfg_path, specs = _build_specs(args)
    multi = len(specs) > 1
    entries: List[Dict[str, Any]] = []
    for i, spec in enumerate(specs):
        if multi:
            print("=" * 78)
            print(f"对比 {i + 1}/{len(specs)}:{spec['name']}"
                  + (f"  ({spec['description']})" if spec["description"] else ""))
            print("=" * 78)
        entries.append(run_one(spec, cfg_path, multi))

    if multi:
        assert cfg_path
        index_path = f"{os.path.splitext(cfg_path)[0]}_index.html"
        write_index(index_path, f"{specs[0]['title']}({len(specs)} 组对比)", entries)
        print("=" * 78)
        print(f"索引已写:{index_path}")
        for e in entries:
            print(f"  {e['name']}: {e['out']}")


if __name__ == "__main__":
    main()
