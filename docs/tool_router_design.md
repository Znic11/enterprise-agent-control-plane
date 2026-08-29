# 工具路由层(Tool Router)详细设计

> 对应方案文档 3.1。目标:在不降低"召回率"(任务真正需要的工具一个都不能丢)的前提下,
> 把暴露给执行 LLM 的工具从域内全量(60–80 个)压缩到 15–20 个,降低上下文与选择噪音。
> 本文档包含:设计前提、三级漏斗、两层兜底、代码骨架、离线/在线评估方案。

---

## 1. 设计前提:三个被验证的事实

在设计前我确认了项目里的三个关键事实,它们决定了整个方案:

**事实 1:官方 oracle 模式 = 提前把"正确答案工具"给 agent。**
`benchmark/executor.py:305-320` 会读取任务配置里的 `selected_tools` 字段,直接把它作为工具白名单过滤。
也就是说 oracle 模式下 agent 看到的工具就是该任务真实需要的工具(平均 12.8 个,见本地 CSM 数据)。

> 推论:工具路由器的学术目标 = **在没有 selected_tools 的情况下,从任务描述预测出这个子集,逼近 oracle 模式**。
> 这是你在简历里可以讲的核心叙事:把"答案泄露"变成"能力预测"。

**事实 2:域信息是免费的。**
每个任务 JSON 的 `gym_servers_config[].mcp_server_name` 直接给出域(如 `sn-csm-server`)。
单域任务只连 1 个 gym → 工具池就是该域的工具(约 60–80 个),不是 512 个。
**路由必须先锁定域,域内路由** —— 这一步零成本,却把搜索空间缩小一个数量级。
(只有 hybrid 任务跨多域,此时域锁定退化为"多域合并池",漏斗依然适用。)

**事实 3:召回率是生死线,精确率是锦上添花。**
丢一个必要工具 = 任务必败;多给几个无关工具 = 只是增加噪音。
所以整个设计的原则是:**召回优先,宁多勿少;靠机制(渐进式扩展)保证 100% 召回,而不是赌路由器预测 100% 准。**

**本地数据统计**(data/revised,CSM 12 个任务 + ITSM 1 个):
- CSM 任务平均需要 12.8 个工具(min 8 / max 19)→ 目标子集 15–20 个工具,足以覆盖。

---

## 2. 方案总览:三级漏斗 + 两层兜底

```
任务输入(user_prompt + system_prompt)
   │
   ▼
① 域锁定(免费)     gym_servers_config → 域工具池 60–80 个
   │
   ▼
② 粗筛             检索器(BM25 → 可升级 embedding)取 top-30
   │
   ▼
③ 精排             轻量 LLM(mini 模型)从 30 个中选 15–20 个,输出 JSON
   │
   ▼
执行引擎(ReAct)     只看到 15–20 个工具的 schema
   │
   ├─ ④ 渐进式扩展:执行中请求子集外工具 → 按需加载放行 + 记录漏检
   └─ ⑤ 置信度回退:粗筛/精排分数低于阈值 → 回退全量工具
```

**为什么是"两段式粗筛+精排"而不是只用其中一种:**
- 只用 BM25/embedding:零成本、召回稳定,但"更新 entitlement"这种动词抽象的任务检索不准;
- 只用 LLM 选择:60–80 个工具的 schema 全塞给 LLM,又回到了"上下文爆炸"的老路;
- 两段式:检索先免费砍掉一半以上,LLM 只在小候选集上做语义判断,成本低、准确高。

---

## 3. 各级实现细节

### ① 域锁定(零成本,必做)
- 从 `config.gym_servers_config[*].mcp_server_name` 提取域 → 每个域维护一个工具索引。
- 工具池来源:`benchmark/executor.py` 的 `_discover_and_merge_tools()` 已把所有 gym 的工具合并进
  `self.available_tools`,并给出 `tool_to_server_mapping`。构建索引时按 `tool_to_server_mapping` 分组即可。
- hybrid 任务:合并多个域的索引做检索,不做特殊处理。

### ② 粗筛:检索器(先 BM25,后升级 embedding)
**工具语义签名**(每个工具构建一个检索文档,这是召回质量的关键):

```
签名 = tool_name + " | " + description(截前 2 句) + " | params: " + 参数名列表
```

例:
```
create_new_case | Registers a new customer case with short_description, product, contact |
params: account_id, contact_id, short_description, channel, priority
```

**实现选择(渐进路径)**:
- **V1(BM25)**:用 `rank_bm25` 或 sklearn `TfidfVectorizer` 构建按域的倒排索引。零成本、零依赖、
  可离线评估。候选:top-30。
- **V2(embedding)**:用开源 embedding 模型(如 `bge-small-zh` / `all-MiniLM-L6-v2`,本地跑)对
  工具签名向量化,任务文本向量化后余弦相似度取 top-30。用于对比消融。
- 两者都做,离线评估谁召回更高 —— 这是"数据驱动"的证据,简历可写。

**查询文本**:`user_prompt` 全文(任务描述本身就包含实体名、动作、域术语,与工具签名高度同构)。

### ③ 精排:轻量 LLM(mini 模型)
从粗筛的 30 个里选 15–20 个,输出结构化 JSON:

```json
{"selected_tools": ["create_new_case", "create_interaction", ...], "reason": "..."}
```

**Prompt 模板**(骨架):

```
你是企业工具选择器。给定用户任务与候选工具列表,选出完成任务【必要】的工具。

规则:
- 只选与任务直接相关的工具;拿不准的宁可多选(漏选=任务失败)。
- 输出 JSON:{"selected_tools": [...], "reason": "一句话理由"}

## 用户任务
{user_prompt}

## 候选工具(名称: 一句话描述)
{top30_tools}

## 输出
```

- 用 `--planner_llm_config` 同款机制传入一个便宜的 mini 模型(如 gpt-4.1-mini / deepseek-v3),复用
  executor 已有的 planner LLM 初始化逻辑,不新增基础设施。
- 精排候选上限 20,保底返回粗筛前 10(防止 LLM 抽风时子集过小)。

### ④ 渐进式扩展(召回兜底,必须做)
- 执行引擎的 `_execute_tool_call` 里拦截:LLM 请求的工具不在当前子集 → **放行并动态加载**该工具
  (从全量池取 schema 注入),同时把工具名记入 `router_miss_log`。
- 这一步把"路由器 100% 准确"的假设降级为"路由器 90% 准确 + 系统自动补齐",召回率从概率变保证。
- 漏检日志(任务id, 漏检工具, 当前子集)是迭代路由器的训练信号 —— **比任何调参都有用**。

### ⑤ 置信度回退
- 粗筛:BM25/相似度最高分 < 阈值(如 0.1,归一化后)→ 认为任务与工具池语义差异大,回退全量。
- 精排:LLM 输出解析失败或选出的工具 < 5 个 → 回退为粗筛 top-30。
- 回退的成本是暂时的,但能防止"路由决策错误导致任务全灭"。

---

## 4. 代码骨架

### 4.1 路由器本体 `benchmark/tool_router.py`

```python
"""ToolRouter: 三级漏斗工具路由."""
import json
from typing import Any, Dict, List


def build_tool_signature(tool: Dict[str, Any]) -> str:
    desc = (tool.get("description") or "").split(".")[:2]
    desc = ". ".join(desc)[:200]
    params = list((tool.get("input_schema") or {}).get("properties", {}).keys())
    return f"{tool['name']} | {desc} | params: {', '.join(params[:12])}"


class BM25Router:
    """V1: BM25/TF-IDF 粗筛 + 可选 LLM 精排。V2 用 embedding 替换 _score_fn。"""

    def __init__(self, tools: List[Dict[str, Any]], k_candidate: int = 30):
        # tools: 全量(或按域分组后)工具列表
        self.tools = tools
        self.signatures = {t["name"]: build_tool_signature(t) for t in tools}
        self.k_candidate = k_candidate
        self._build_index()  # TfidfVectorizer / rank_bm25 构建,见 4.4

    def _score_fn(self, query: str, sig: str) -> float:
        raise NotImplementedError  # V1: TF-IDF 余弦;V2: embedding 余弦

    def route(self, task_text: str, top_k: int = 20, min_score: float = 0.05):
        """返回 (子集工具列表, 元数据)。分数过低回退全量。"""
        scored = sorted(
            ((t, self._score_fn(task_text, self.signatures[t["name"]])) for t in self.tools),
            key=lambda x: x[1], reverse=True,
        )
        if not scored or scored[0][1] < min_score:
            return self.tools, {"fallback": "low_confidence", "top_score": scored[0][1] if scored else 0}
        cand = [t for t, s in scored[:self.k_candidate]]
        return cand, {"candidate_size": len(cand), "top_score": scored[0][1]}
```

### 4.2 精排(轻量 LLM)`rerank(tools, task_text, llm_client)`

```python
def rerank(candidates: List[Dict], task_text: str, llm_client) -> List[Dict]:
    """从候选里选 15–20 个。返回子集工具;解析失败则回退粗筛候选。"""
    # 组装 prompt(见 3.③ 模板)→ llm_client.llm.ainvoke → 解析 JSON
    # selected = set(json["selected_tools"])
    # 兜底:if len(selected) < 5: return candidates[:20]
    ...
```

### 4.3 orchestrator 集成(改 `execute()` 入口 + 工具调用拦截)

```python
class RoutedReactOrchestrator(AgentOrchestrator):
    async def execute(self):
        # 1. 路由(只输入任务文本,绝不读 selected_tools)
        task_text = self.config.user_prompt
        subset, meta = self.router.route(task_text, top_k=20)
        self.active_tools = subset                     # 执行期间可见的工具
        # 2. 注入一条系统说明:如需其他工具可直接请求,会自动加载
        # 3. 标准 ReAct 循环,但 available_tools=self.active_tools
        # 4. _execute_tool_call 拦截子集外工具 → 动态加载 + 记录 miss
        ...
```

关键点:
- `self.available_tools` 换成 `self.active_tools`,循环逻辑复用 react.py;
- 在 `_execute_tool_call` 中判断 `tool_name not in active_names` → 从全量池补进 `active_tools`
  并记录 `miss_log`(重写基类方法或包装);
- 新增 `get_result_metadata()` 返回 `router_meta`(子集大小、是否回退、miss 列表)→ 自动进入结果日志,
  供评估脚本读取。

### 4.4 离线评估脚本 `eval_router.py`(不烧 API,立即出数)

```python
"""用任务自带的 selected_tools 标注评估路由器质量。"""
import json, glob
# 1. 从 data/revised 加载任务 → (user_prompt, selected_tools)
# 2. 构建域工具池:从任务的 gym_servers_config 关联到该域全量工具
#    (本地无 MCP 时可先用 HuggingFace 数据集或连一次 MCP dump 工具列表)
# 3. 对每个任务:pred = router.route(user_prompt, top_k=20)
# 4. 指标:
#    recall@20 = |pred ∩ selected| / |selected|        ← 生死线,必须 ≥ 0.9(靠渐进式扩展补到 1.0)
#    precision@20 = |pred ∩ selected| / |pred|
#    coverage@15 = 用 top-15 子集时 recall 多少
# 5. 输出:总体 + 分域表格;对比 V1(BM25) vs V2(embedding)
```

> ⚠️ **诚实性边界(必读)**:`selected_tools` 只能用于【离线评估路由质量】;
> 执行时路由器输入仅 `user_prompt`/`system_prompt`,**禁止**读 `selected_tools` 字段。
> 否则等于答案泄露,结果不可信,面试一问就穿帮。文档里写明这条边界,反而加分。

---

## 5. 评估与验收

### 离线指标(路由器本身,迭代快)
| 指标 | 含义 | 目标 |
|------|------|------|
| recall@20 | 任务所需工具被覆盖的比例 | ≥ 0.9(渐进式扩展兜底到 1.0) |
| precision@20 | 子集里真正用到的比例 | 0.5–0.7 即可(宁多勿少) |
| 上下文压缩率 | 子集 schema tokens / 全量 schema tokens | ≤ 40% |

### 在线指标(端到端,Phase 1 验收)
- 同域同模型同 split:react baseline vs react+router,oracle 模式成功率。
- 期望:**成功率持平或提升**(理论上路由减噪音应提升)+ **token 成本下降**(可量化写简历)。
- 若成功率持平但成本降 50%,同样是合格的 Phase 1 交付 —— 讲"效率"故事。

### 消融矩阵(面试会被问,提前准备)
| 配置 | 说明 |
|------|------|
| react(全量工具) | baseline |
| + 域锁定 | 只减搜索空间,不改上下文 |
| + 粗筛(BM25) | 压缩到 30 |
| + 精排(LLM) | 压缩到 15–20 |
| + 渐进式扩展 | 召回兜底 |
| 粗筛换 embedding | 对比检索器 |
| + 置信度回退 | 极端场景保护 |

---

## 6. 里程碑

1. **M1(半天)**:`build_tool_signature` + BM25 索引 + `route()`,跑通 4.4 离线评估,拿到 recall/precision 初值。
2. **M2(1 天)**:orchestrator 集成 + 渐进式扩展 + 结果元数据,跑一个小域(建议 teams 或 email)端到端对照。
3. **M3(1 天)**:精排 LLM + 置信度回退,重跑对照,记录 token 消耗对比。
4. **M4(可选)**:embedding 版检索器,离线对比 BM25,写进消融。

> 一个务实的提醒:先做 **M1 + M2 的 BM25 版**(零成本闭环),确认"工具治理"真的提分或省钱,
> 再上 LLM 精排。不要一开始就堆复杂度 —— 这也是简历上"渐进式工程方法"的体现。
