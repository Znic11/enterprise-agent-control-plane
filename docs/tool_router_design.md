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

---

## 7. 执行期补充:意图级检索 + Meta-Tool(元工具)模式 + Hybrid 检索(2026-09-03/09-04 落地)

> ⚠️ 本章补充 Phase-1 之后新增的执行期机制与检索后端演进。正文 §3/§4 的 BM25 骨架是早期设计稿;
> **当前唯一实现**是 `benchmark/tool_router.py`(稀疏 TF-IDF 余弦 / 稠密向量 / Hybrid 融合三后端可插拔,
> 接口/语义以此为准)。落地详情见 `docs/HANDOFF.md` §4.5(09-03 Meta-Tool)与 §4.6(09-04 dense/hybrid + react_router 移除)。

### 7.1 第一层(历史,已随 react_router 移除):执行期意图级检索(触发点 A/B/C,已提交 9a04a3d/2c2b62f)

> ⚠️ **2026-09-04 起 `orchestrators/react_router.py` 已删除**(用户判定双轨复杂化、不优雅,评估叙事收敛为
> meta_tool 主通道)。本节保留作设计历史:触发点 A(unknown_call)与 C(text_gap)在 meta_tool 下已被
> 显式 `_tool_search` 取代(检索时机完全交给 LLM);**B(exec_error try/except)被 meta_tool 继承保留**。

路由"事前给子集"只能覆盖粗筛已召回的 15–20 个工具。执行中模型会遇到三缺口,由
`orchestrators/react_router.py` 处理(**命中工具只进可见集,绝不自动执行**):

| 触发点 | 场景 | 行为 |
|---|---|---|
| A unknown_call | 模型点名全池不存在的工具(拼错/幻觉名) | `query = 名字\|参数键\|本轮文本` → 全池检索 → 并入活跃集 → 回喂 `{success:False, available_tools_added}` |
| B exec_error | 真实工具执行抛异常 | try/except → 错误回喂 ToolMessage,不再中断整轮 run |
| C text_gap | 无 tool_call 但文本命中 `_GAP_SIGNALS`("no such tool"…) | 检索 → 注入 `[system]` 新工具可用提示 → 再给一轮 |

支撑 API:`ToolRouter.search(query, top_k, min_score, boost_lookup)`(复用 `__init__` 索引,零额外构建;
只打分返回 `(score, name)`,入不入活跃集由调用方决定)。安全边界:检索输入只来自模型上下文与调用,
**绝不读 selected_tools**(执行时读 = 答案泄露)。

### 7.2 第二层:Meta-Tool(元工具)模式(09-03 落地,7edee85/67714f6 提交;检索后端已 hybrid 化见 §7.3)

参考 Spring AI Alibaba / OpenAI tool search / Anthropic tool search:**系统只暴露一个只读元工具
`_tool_search`,由 LLM 显式决定"何时检索、搜什么"**,检索命中后把真实工具 schema 动态注入后续
轮次的 bind 集。与 §7.1 的"系统事后兜底"相反,这是"事前内置为合法调用"——严格 function-calling
下模型永远能合法调用 `_tool_search`(它在 bind 集内),天然不受"模型发不出子集外名字"的限制。

```
第 n 轮 bind 集 = [_tool_search] + 已注入真实工具 defs      (每轮 invoke 重新 bind,对 LLMClient 零改动)
   │
   ├─ LLM 调用 _tool_search(query, top_k?)
   │     → orchestrator 拦截(只读,不执行任何真实工具)
   │     → ToolRouter.search(query)   [复用 §3 唯一检索实现]
   │     → 命中工具 _admit() 入注入集(保序去重)+ 回喂 found 清单
   │     → 首注时追加 [system] "now bound and callable: …"(插在全部 ToolMessage 后)
   ▼
第 n+1 轮 bind 集 = [_tool_search] + 注入集   → 模型看到 schema 后显式调用真实工具
   → 真实调用走 base._execute_tool_call → gym MCP;异常 try/except 回喂(单次失败不炸 run)
```

关键机制与默认值(实现:`orchestrators/meta_tool_router.py::MetaToolOrchestrator`):

| 机制 | 默认 | 说明 |
|---|---|---|
| `tool_search_top_k` | 6 | 每次 `_tool_search` 返回工具数 |
| `tool_search_min_score` | 0.03 | TF-IDF 分数下限,低于视为零命中 |
| `cache_size` | 64 | 同会话 query→hits LRU 缓存(不跨任务,防重复检索) |
| `fallback_all_after_zero_hits` | 3 | 连续零命中达阈值 → 兜底 bind 全池防死锁;`None`=关闭(只回喂引导) |
| `warmup_top_k` | None | 预热模式:非 None 时 `__init__` 即注入 `route(user_prompt)` top-k(与元工具并存);None = 纯 Meta-Tool 单通道 |
| `boost_lookup` | False | 检索是否对 find_/list_/get_ 前缀加只读地板分(对齐 route() 启发式) |

**诚实性边界(硬性)**:`_tool_search` 只读——拦截后仅打分/返回/注入可见集,写副作用必须由模型
后续显式调用真实工具触发;检索输入仅本轮 LLM 调用参数;`selected_tools` 仅用于离线评估与
`eval_router.py --analyze_meta_runs` 聚合时的 GT 匹配。

**评估**:离线仿真 `eval_router.py --meta_sim`(任务文本切段模拟逐轮检索,统计 final_recall /
first_recall / hits_avg / zero% / full_cov / precision;诚实口径 = 检索器对"分批能力片段"query 的
覆盖下界,非端到端);端到端 `evaluate.py --orchestrator meta_tool --retrieval …`(服务端,
run 级 `meta_tool_*` 元数据落盘 → `--analyze_meta_runs` 聚合)。09-04 起对照口径不再是
react_router vs meta_tool_router(react_router 已删),而是 **meta_tool 内部检索后端对照**:
tfidf vs dense vs hybrid(同模型/同 split/同 concurrency)。

### 7.3 Hybrid 稠密检索后端(2026-09-04 落地,commit 6c308bb;参考 Spring AI Alibaba 工具检索)

**动机**:Meta-Tool 的 `_tool_search` query 是 LLM 自生成的能力描述,与工具名/描述经常"同义不同词"
(实测零词法重叠漏检约占 1/3)。稀疏 TF-IDF 只认词面重叠 → 换成 **稠密向量语义检索**,并用
**Hybrid 融合**兼顾语义(同义词)与词面(精确工具名/参数键,稠密对精确 ID 类不敏感)。

```
q ──► ToolRouter._rank_all(query)                  [retrieval = tfidf|dense|hybrid]
        ├─ 稀疏通道: TFIDFIndex.search_all  → sparse score(余弦,可能 0.9+ 尖峰)
        ├─ 稠密通道: DenseIndex.search_all   → dense score(L2 归一余弦,通常 0.2-0.6)
        └─ hybrid 融合: score = α·minmax01(dense) + (1-α)·sparse
             min-max 归一到 [0,1] 解决双通道量纲不可比 —— α 才是可解释的语义权重
```

| 组件 | 说明 |
|---|---|
| `benchmark/dense_retriever.py` | `TextEmbedder` 抽象(`embed_texts` 单一接口,`embed_query` 默认派生);`SentenceTransformerEmbedder`(默认 **BAAI/bge-small-en-v1.5**,384 维、CPU 毫秒级;`query_instruction` 默认空串,参数保留调优);`DenseIndex`(工具签名矩阵一次性编码,L2 归一后余弦=点积);`APIEmbedder` 预留(OpenAI 兼容 /embeddings);`get_embedder()` 模块级缓存(同 (model,device) 一次加载,e2e 并发共享) |
| `ToolRouter(retrieval=...)` | 构造参数 `retrieval=tfidf(default)|dense|hybrid` + `embedder`(可注入,测试用 FakeEmbedder)/`hybrid_alpha=0.5`;校验非法值/缺 embedder;`_rank_all()` 集中融合,`route()/search()/batch_route()/search_tools()` 与旧接口完全一致,仅 `method`/元数据打标 `<retrieval>+llm[+union]` |
| `meta_tool_router.py` | `_tool_search` 检索后端可配 `retrieval/embedding_model/embedding_device/hybrid_alpha`;运行时元数据 `meta_tool_retrieval`/`meta_tool_hybrid_alpha` 入 execute 结果与审计 |
| `evaluate.py` / `eval_router.py` | CLI `--retrieval(默认 hybrid)/--embedding_model/--embedding_device/--hybrid_alpha`;离线 `resolve_retrieval`:auto → 有 sentence-transformers 则 hybrid,否则 tfidf(无依赖用户无缝回退) |

**诚实性 / 环境边界**:
- dense/hybrid 依赖可选 extra `pip install '.[dense]'`(sentence-transformers + torch);缺依赖时
  **显式 ImportError 带安装提示,绝不静默降级**(实验口径必须真实可复现)。
- bge 首次下载走 HuggingFace,服务器设 `HF_ENDPOINT=https://hf-mirror.com` 加速。
- 测试用确定性 `FakeEmbedder/ConceptEmbedder`(概念词表把同义词映射到同方向低维向量)验证
  语义命中与融合退化(α=0 纯稀疏 / α=1 纯稠密),不依赖真实 torch;55/55 单测通过。
- 本地验证:hybrid 真实数字需服务端(见 HANDOFF §4.6.4 命令);离线 tfidf 冒烟 ALL recall@20 = 42.5%(零回归)。

