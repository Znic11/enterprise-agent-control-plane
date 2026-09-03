# EnterpriseOps-Gym 项目交接文档(Handoff)

> 交接日期:2026-09-03 · 交接人:上一会话 · 接收人:新会话模型(用户将开新对话"重新优化方案")
> 阅读顺序:本文档 → `docs/tool_router_design.md`(路由详细设计,Phase-1 权威)→ `docs/agent_design_plan.md`(总方案)→ `.workbuddy/memory/`(按日日志,09-02/09-03 最新)
> 版本说明:本版取代 2026-08-29 旧 HANDOFF(其"非 git 仓库 / 两套路由未统一 / 从未运行"等描述均已被后续工作推翻);旧版可在 git 历史取回。历史执行记录与新进展的关系见 §2.3 commit 链。

---

## ⚡ 0. 三句话摘要(先读这里)

1. **目标**:在 ServiceNow 开源的 EnterpriseOps-Gym 基准上自研"企业级 LLM Agent",核心卖点 = 用 TF-IDF+LLM 的工具路由逼近 oracle 模式(把"答案泄露"变成"能力预测"),以可复现实验证明有效性,作为面试核心项目;代码同步沉淀在 GitHub 作品集 `enterprise-agent-control-plane`(= 本仓库 origin)。
2. **现状**:Phase-1 路由(统一 `benchmark/tool_router.py` 三级漏斗 + `react_router.py` + 离线评估)已在真实工具池(tools_dump.json,512→458 工具/7 域)上跑出指标与归因;随后三个会话落地**执行期鲁棒性修复(B 异常不炸 run)+ 意图级检索(触发点 A/B/C)+ 元工具(Meta-Tool)模式**,单测 **51/51 通过**(§2),提交状态见 §3.3/§4。
3. **当前状态(Meta-Tool 已实现;端到端价值验证是最大空白)**:`orchestrators/meta_tool_router.py::MetaToolOrchestrator` 已按 Spring AI Alibaba 思路落地——首轮只 bind `_tool_search`,LLM 显式调用 → 拦截 → `ToolRouter.search()`(TF-IDF)→ 命中工具 schema 动态并入后续轮次 bind 集 → 模型显式调用真实工具走 `_execute_tool_call`;`evaluate.py` 已注册、`eval_router.py` 已加离线仿真指标并跑出数字(§4.5.5)。**待办**:服务端端到端对照、提交本次 Meta-Tool diff、README 回填(§6)。

---

## 1. 项目背景核心事实(新模型必读)

1. **评分看终态,不看路径**:任务跑在真实 MCP server 上、操作真实写入数据库,评分用 SQL verifier 检查最终数据库状态。agent 只需稳定达成正确终态。
2. **oracle 模式 = 答案泄露**:`benchmark/executor.py:305-320` 读任务配置 `selected_tools`(人工标注的真实所需工具,平均 12.8 个)当白名单 → **工具路由器的学术目标 = 从任务描述预测该子集,逼近 oracle**。
3. **域信息免费**:任务 `gym_servers_config[].mcp_server_name` 给出域;单域任务工具池只有 60–80 个(非 512)。路由先锁域再域内路由。
4. **内置 agent(react/planner_react/decomposing)四大缺陷**:全量工具直灌上下文、无终态验证、历史无压缩(89k 上下文稀释)、政策只写在 prompt。→ react_router 针对 ①②做路由减负。
5. **诚实性红线**:`selected_tools` **只能用于离线评估**;执行时路由器只输入 `user_prompt`/`system_prompt`。面试追问即穿帮,不可违反。
6. **基准体量**:1150 任务 / 8 域 / 512 工具 / 平均 9.15 步 / 89k 上下文;最强模型 Claude Opus 4.6 平均成功率 45.9%,开源最佳 DeepSeek-V3.2 24.2%——提升空间大。

---

## 2. 运行环境与协作约定(防坑,必读)

| 事项 | 事实 |
|---|---|
| **真实 Python 环境** | 项目 `.venv`(Python 3.14.3,含 langchain_core 等全部依赖)。⚠️ 受管 python 3.13 与系统 anaconda **均无 langchain_core**;凡 import orchestrators / benchmark.llm_client 的代码只能用 `.\.venv\Scripts\python.exe` |
| **单测命令** | `./.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"` → **51 passed**(test_tool_router 33 + test_react_router 7 + test_meta_tool_router 11);路由模块本身纯 stdlib,任意 py3 可跑 |
| **LLM key / 容器** | 本地 `conf/llm/` 不存在 → 端到端被 key 阻塞;容器/udocker 全套指令见 `RUN_GUIDE.md`(服务端已验证;本地 docker daemon 曾静默失败) |
| **真实工具池** | `tools_dump.json`(仓库根,512 原始条目含 `_domain`,按 (name,_domain) 去重后 458)**已入库**(2c2b62f);⚠️ MCP `tools/list` 真实 schema 字段是 `inputSchema`(驼峰),dump/测试用 `input_schema`(小写)——`build_tool_signature` 已双兼容 |
| **F 盘 git 陷阱** | 沙箱 `git rm` 会连带删除同目录其他工作树文件(多次踩坑)。规避:普通 `rm` + `git add -A`,或 `git checkout <branch> -- <file>` 取回;批量操作后必须 `ls` 核对 |
| **git 拓扑** | `origin` = GitHub `Znic11/enterprise-agent-control-plane`(作品集,ssh.github.com:443);本地分支 `main`(当前)/ `remote`(服务端适配快照 8de4053)/ `local-tests`;origin 另有 main、remote。身份 znic |
| **用户偏好** | 中文沟通、结构化表格摘要;**选择性 commit**:只提交功能有效变更,无关改动不混入;沙箱内可 py_compile/单测,重构建/容器验证由用户在自己环境做;README 对实现状态如实标注(防面试深挖翻车) |

---

## 3. 路由管线现状(Phase-1 定型;设计权威见 `docs/tool_router_design.md`)

### 3.1 唯一实现与三级漏斗
- **`benchmark/tool_router.py`**(唯一路由实现,纯 stdlib):TF-IDF 粗筛(k_candidate=30)→ 可选 LLM 精排(`llm_call_fn` 注入,provider 无关;rerank_mode=strict|union)→ 两层兜底(置信度低→回退全量;LLM 失败/选中<5→回退 TF-IDF 子集)。
- 关键组件:`_tokenize`(snake_case 拆分+停用词与 <3 长度过滤+保守复数词干化)、`build_tool_signature`(名称×2+desc 前两句+参数名列表)、`LOOKUP_FLOOR=0.08` 只读工具地板分(find_/list_/get_/search_/retrieve_/check_ 前缀)、`TFIDFIndex`。
- 入口:`route()`(整条任务粗筛+可选精排)、`batch_route()`(并发精排)、`route_keywords()`(零成本兜底/消融)、`expand_tool()`。

### 3.2 离线评估数字(真实池,13 任务小样本,口径=域内池+reachable-GT;详见 memory/2026-09-02)
- 基线(默认 top_k=20 / floor=0.08):recall@20 **43.8%** / precision@20 **26.9%**。(旧 63.7% 作废:伪造 desc=name×3 的近似池虚高,别写简历。)
- 参数扫描:top_k=30 + floor=**0.15** → recall **57.5%**;top_k=25 + floor=0.15 → 50.0%。
- **漏检归因**(analyze_router_misses.py 修复键值倒置 bug 74e7207 后):90 漏检 = outside_candidates 44(49%,rank 31-50 被同族挤出)+ zero_overlap 32(36%)+ below_cutoff 14(16%)。
- **误报归因**:190 误报 = family_overlap **173(91%)** + weak_match 17 → **同实体动作族(add/update/delete×…)是噪音主源 = LLM 精排的核心价值场景**。
- `LOOKUP_FLOOR=0.08` 在真实池过低:GT 的 find_/list_ 全压 0.0800 一条线、rank 30-46 → **是否上调默认值 0.15 未决策**。
- full_cov 坑:32/160 GT 引用其他域容器工具(不可达),须按 reachable 口径排除(999c15d),否则 full_cov=0 误报。

### 3.3 相关 commit 链(main,新→旧)
`(待提交: Meta-Tool 落地 diff = tool_router.py +5 / evaluate.py +34 / eval_router.py +256 / meta_tool_router.py 新,§4.1 末)` ← `2c2b62f`(feat(router): intent-level retrieval ToolRouter.search + exec-loop 触发点 A/C;含 react_router、base.py、docs、tools_dump.json 入库)← `9a04a3d`(fix(orchestrators): exec-loop robustness —— 工具失败不再中断 run)← `6386af2`(--candidate_k 解耦 LLM 候选宽度与 top_k 保底)← `7323b1c`(rerank_mode=union,recall ≥ 粗筛)← `6052625`(k_candidate/k_final 解耦,粗筛不再被候选池截断)← `94e979c`(RouteResult.candidate_names+ROUTER_PROMPT 6-20 引导)← `0690b43`(dump 合并键 name,_domain)← `999c15d`(reachable-GT 口径)← `74e7207`(归因脚本修复)← `0d31b51/2d258f2`(dump/归因脚本)← `eee7bc5`(batch_route 并发)。

---

## 4. 最近两个会话的工作(2026-08-31 ~ 09-03,★ 新对话"重新优化方案"的直接原料)

### 4.1 执行期鲁棒性 + 意图级检索(✅ 已提交;diff 拆 2 commit,用户确认全提交)

下述 diff 已在 2026-09-03 会话按用户选择拆为两个 commit 提交(`9a04a3d` 鲁棒性 + `2c2b62f` 意图检索,含 docs 与 tools_dump.json 入库),提交后 worktree clean:
```
9a04a3d fix(orchestrators): exec-loop robustness - tool failures no longer abort a run
        (base.py 未知名 raise 带工具名;react_router 执行循环 try/except 回喂)
2c2b62f feat(router): intent-level retrieval (ToolRouter.search) + exec-loop triggers A/C
        (benchmark/tool_router.py +47;orchestrators/react_router.py +178;
         tests/test_react_router.py 7 例 + test_tool_router.py TestIntentSearch 6 例;tools_dump.json)
```
⚠️ 本会话后续的 **Meta-Tool 落地 diff 尚未提交**:`M benchmark/tool_router.py`(+5,inputSchema 双兼容)+ `M evaluate.py`(+34)+ `M eval_router.py`(+256)+ `?? orchestrators/meta_tool_router.py`(新)。`tests/` 目录按既有策略仍在 `.git/info/exclude`(不入库)。功能有效、单测 51/51,建议一个功能 commit(§6 #1)。

### 4.2 背景与动机(意图级检索是用户亲自推动的设计质疑)
- **名字级发现(旧 `_discover_if_needed`)的缺陷链**:`llm_client.py` ~L415 `bind_tools` 只绑定活跃子集 → 严格 function-calling 下模型**发不出子集外名字** → 名字级发现永不触发;旧 `base._execute_tool_call` 未知名静默回退第一个 gym 或 raise → 异常冒泡炸整轮 run。
- **用户核心论点**:基准任务所需工具必然在全池 → 与其赌模型"精确拼出真实工具名"再精确匹配(命中率天然低),不如把模型**已表达的能力需求/上下文**(调用名残片、参数键、本轮推理文本)作 query,对全池近似检索(快、便宜),把真实工具并入活跃集供模型显式重选。检索不到就回喂错误而非中断。
- **ToolLLM 学术锚点**(2307.16789,详见 memory/2026-09-03):retriever(top-5)与 solver 分离、DFSDT 回溯 > ReACT(63.8 vs 35.3 pass rate)、检索器偶优于 GT → 支撑"候选宽于输出 + union 保底 + 执行期可补检索"的叙事。

### 4.3 意图级检索实现(触发点 + 安全边界)
| 触发点 | 场景 | 行为 |
|---|---|---|
| **A unknown_call** | 模型点名全池不存在的工具(拼错/近似/幻觉名) | query = `名字 \| 参数键 \| assistant_text[:400]` → `_intent_discover` 全池检索 top-k → 命中并入活跃集 → 回喂 `{success:False, error, available_tools_added:[...]}` 让模型下轮用真实名重试 |
| **B exec_error** | 真实工具执行抛异常 | try/except 捕获 → 错误回喂 ToolMessage,**不再冒泡中断整轮 run** |
| **C text_gap** | 模型没发 tool_call,但文本命中 `_GAP_SIGNALS`("no such tool"/"cannot find"/"unavailable"…) | 检索 → 注入"[system] 以下新工具已可用…"并 continue 再给一轮;正常收尾文本("Task complete.")零命中,不会空转 |

- 参数:`enable_discovery=True`、`intent_top_k=6`、`intent_min_score=0.03`(**拍脑袋初值,未调参**)。`_GAP_SIGNALS` 为类级常量,大小写不敏感子串匹配。
- **安全边界(硬性)**:意图检索命中工具**只进可见集(_discovered),绝不自动执行**——写工具副作用必须仍由模型显式调用触发;审计:`get_result_metadata()["router_intent_retrieved"]` 单列意图检索来源(与精确名发现 `router_discovered` 区分)。
- `ToolRouter.search()` 语义:复用 __init__ 索引、零额外构建;只打分返回 (score,name),不做截断/回退/精排——是否入活跃集由调用方决定;`boost_lookup` 可选加只读地板分。
- 实测探针(TF-IDF,确定性):拼错名 `updat_entitlement` → `update_entitlement` top1 **0.91**;GAP 文本 → **0.84**;幻觉名 → 零命中;收尾文本 → 零命中。
- 与旧 `_discover_if_needed` 关系:保留(便宜,精确命中直接执行);意图级检索负责"模型无法点名"的补盲。

### 4.4 测试与验证状态
- 全量 **51/51 通过**(意图级检索相关 40 例 + Meta-Tool 11 例,§4.5.5):触发点 A/B/C 各场景、正常收尾不误触、幻觉名零命中不致命、关闭 discovery 退化不炸、**安全"只加不执行"实证(mcp.calls==[])**、search API 排序/top_k/min_score/boost_lookup 一致性、便捷函数与实例同源;Meta-Tool 闭环/兜底/缓存/并行/预热见 §4.5.5。
- 无 LLM key/容器 → **对任务成功率的端到端增益仍未量化**(最大验证空白,服务端可跑)。

---

## 4.5 新方向(✅ 已实现落地,2026-09-03):元工具 (Meta-Tool) 模式 — 参考 Spring AI Alibaba

> 状态:本节 4.5.1-4.5.4 为原始设计(保留供对照);**实施结果、9 个设计问题的定调、测试与离线数字见 §4.5.5**。

### 4.5.1 Spring AI Alibaba 做法(来源:用户提供的元工具介绍截图)

这是当前业界最主流的工具调用实现模式,核心 = **用一个特殊工具去发现和加载其他工具**:

1. **用户提问**:用户向 Agent 提问。
2. **LLM 决策**:Agent 的 LLM 判断需要调用工具,但它"看到"的**唯一选项** = `ToolSearchTool`。
3. **调用元工具**:LLM 调用 `ToolSearchTool`,传入从用户问题中提取的关键词。
4. **检索相关工具**:系统**拦截**这次调用,用内置检索引擎(Lucene `LuceneToolSearcher` 或向量库)搜索。
5. **动态注入与执行**:系统将检索到的、最相关的几个业务工具(如 `get_weather`)的**完整 Schema 动态注入到当前对话上下文**。
6. **最终调用**:LLM 现在"看到"了这些具体的业务工具,选择并执行正确的那一个。

### 4.5.2 为什么这个方案对我们"更靠谱"

- **直接解决 §5.1(已升级为"已决策")**:`bind_tools` 只暴露 `ToolSearchTool`,LLM **任何时候都能合法调用**(因为它在绑定集里)→ 严格 function-calling 模式下不再受限。检索命中后通过 Schema 注入把真实工具送进下一轮 LLM 的可调用集。
- **LLM 显式控制"何时检索"**:与"事前默默扩绑定集"相比,LLM 自己决定是否需要更多工具,语义更对齐、误扩面更小。
- **架构统一、移除三条触发点分散逻辑**(`unknown_call`/`exec_error`/`text_gap`):检索变成显式动作,鲁棒性修复(`exec_error` try/except)仍保留。
- **业界已验证**:Spring AI Alibaba(阿里云)、OpenAI 工具检索、Anthropic tool search 都用同类思路,可写进简历叙事("对齐主流业界方案")。

### 4.5.3 映射到本仓库(实施草案)

| Spring AI Alibaba 步骤 | 在 EnterpriseOps-Gym 里的落点 |
|---|---|
| ① 用户提问 | `BenchmarkConfig.user_prompt` |
| ② LLM 决策 | `LLMClient.invoke_with_tools(messages, tools=[_tool_search_def])` |
| ③ 调用元工具 | LLM 输出 `tool_calls=[{name: "_tool_search", args:{query, top_k?}}]` |
| ④ 检索相关工具 | `ToolRouter.search(query, top_k=intent_top_k, min_score=..., boost_lookup=...)`(复用 §3.1 唯一路由实现,**零新代码**) |
| ⑤ 动态注入 Schema | 新增 `bind_tools` 动态扩绑定集机制(读 `benchmark/llm_client.py` ~L415 `bind_tools` 流,**最小入侵改造**);缓存 query→tools 避免重复检索 |
| ⑥ 最终调用 | LLM 在下一轮 `invoke_with_tools(messages, [tool_search_def] + 缓存中已注入的工具 schemas)` 输出真实工具调用 → 走 `_execute_tool_call` |

**新类建议**:`orchestrators/meta_tool_router.py` 的 `MetaToolOrchestrator`,**不替代** `react_router.py`(先并行,后对比);`evaluate.py:ORCHESTRATOR_MAP` 新增 `"meta_tool": MetaToolOrchestrator`。

### 4.5.4 关键设计问题(新对话需逐项定调)

1. **注入方式**:命中后直接把真实工具 defs 加进 bind_tools(需 LLMClient 支持 mid-conversation 增量绑定) vs 用一段 `[system]` 文本告知 LLM"下列工具已可用,直接用文字描述调用"(**后者零架构侵入,推荐先用此做 V1**)。
2. **检索后端**:V1 = `ToolRouter.search`(TF-IDF,§3.1 已实现,零成本);V2 = 换 sentence-transformers embedding(只换 `TFIDFIndex`,接口不变,§5.3 已有路径)。
3. **缓存策略**:同会话内 `query → retrieved_names` 缓存(LRU/单纯 dict),避免重复检索;**不跨任务**避免泄漏。
4. **并发调用**:与原 `_discover_if_needed` 共存的并行 tool_call 边界(§5.6)同样适用,新代码一并处理。
5. **回退路径**:`ToolSearchTool` 检索零命中时 → 注入 `[system]` "当前可用工具集为空,请说明你需要的具体功能" → 让模型继续;**或**直接回退到全池 bind(react_router 当前行为)避免死锁。
6. **预热/混合模式(可选)**:`ToolSearchTool` + 上层路由 top_k 双暴露,LLM 自选;或纯 Meta-Tool 单通道。**建议从纯 Meta-Tool 起步**,对比后再决定是否混 top_k。
7. **任务规模兼容:大池 vs 小池**:本项目单域 60–80 个工具,纯 Meta-Tool 每次检索毫秒级,延迟可忽略;确认对真实 512 池仍毫秒级。
8. **评估口径**:`eval_router.py` 离线 + `evaluate.py` 端到端双跑;新增元数据字段 `meta_tool_searches`(检索次数)、`meta_tool_hits_avg`(平均命中数)、`meta_tool_cache_hits`。
9. **意图级检索(原触发点 A/B/C)的去留**:建议 **保留 B(异常 try/except)**,**简化为单点 C 兜底**(零命中时回喂"还有别的工具吗")而非三类分散触发;或作为 Meta-Tool 的 fallback 子模块,逐步收敛。

### 4.5.5 实施落地(2026-09-03 本会话;§4.5.4 九个问题的定调与结果)

**交付清单**(未提交,见 §4.1 末):
- `orchestrators/meta_tool_router.py`(新,~520 行):`MetaToolOrchestrator`。核心机制 = 首轮 `_visible_tools()` 只 bind `[_tool_search]`;LLM 显式调用 `_tool_search(query, top_k?)` → `_handle_tool_search` 拦截 → `ToolRouter.search()`(复用 __init__ 索引,零新检索代码)→ 命中工具 `_admit()` 并入注入集 → **下一轮 bind `[_tool_search] + 已注入 defs`** → 模型显式调用真实工具 → `base._execute_tool_call` 执行。`_tool_search` 定义走 MCP 标准 `inputSchema`(驼峰)。
- `evaluate.py`:`ORCHESTRATOR_MAP["meta_tool"] = MetaToolOrchestrator`;CLI `--meta_tool_top_k`(默认 6)/`--meta_tool_min_score`(默认 0.03)/`--meta_warmup_top_k`(默认 None)。
- `eval_router.py`:`simulate_meta_tool()`(任务文本切段模拟逐轮 `_tool_search`,统计 final_recall/first_recall/hits_avg/zero%/full_cov/precision)+ `analyze_meta_tool_runs()`(聚合 evaluate.py `results_*.json` 的 run 级 `meta_tool_*`);CLI `--meta_sim`/`--meta_sim_top_k`/`--meta_sim_min_score`/`--meta_sim_max_searches`/`--analyze_meta_runs`。
- `benchmark/tool_router.py`(+5):`build_tool_signature` 对 `inputSchema`/`input_schema` 双兼容(小写优先、驼峰 fallback)。
- `tests/test_meta_tool_router.py`(新,11 例,全量 51/51 通过)。

**§4.5.4 九个问题 → 落地口径**:

| # | 设计问题 | 落地口径 |
|---|---|---|
| 1 | 注入方式 | **bind 动态扩 + [system] 文本说明双管齐下**。每轮 `invoke_with_tools(messages, visible)` 重新 bind → orchestrator 传 `[_tool_search]+已注入 defs` 即实现 mid-conversation 动态扩,**对 LLMClient 零改动**;新工具首注时追加一条 `[system] "now bound and callable…"` HumanMessage(插在全部 ToolMessage 之后),重复检索零噪音 |
| 2 | 检索后端 | V1 = `ToolRouter.search`(TF-IDF,零新代码);V2 换 embedding 只改 `TFIDFIndex`,路径不变 |
| 3 | 缓存策略 | `OrderedDict` LRU(默认 64 条)同会话 query→hits;**不跨任务**;命中计数 `cache_hits` 入元数据 |
| 4 | 并发 tool_call | 一轮内多个 tool_call 依序处理(`_tool_search` 拦截 + 真实工具执行),注入说明统一追加在本轮全部 ToolMessage 之后;并行场景有单测 |
| 5 | 回退路径 | 零命中 → 回喂 `count=0 + "Rephrase…"` 引导,**绝不中断**;连续零命中达 `fallback_all_after_zero_hits`(默认 3)→ 兜底 bind 全池防死锁(`None`=关闭兜底,只回喂) |
| 6 | 预热/混合 | 预留 `warmup_top_k`(默认 **None = 纯 Meta-Tool 单通道**,先验证假设);非 None 时 `__init__` 即注入 `route(user_prompt)` 粗筛 top-k,与 `_tool_search` 并存 |
| 7 | 大池/小池 | search 复用 `__init__` TF-IDF 索引,域内池 60-90 工具毫秒级;512 全池同构可用 |
| 8 | 评估口径 | orchestrator `get_result_metadata()` 输出 `meta_tool_search_calls / searches / cache_hits / hits_avg / zero_hits / injected / fallback_all / warmup_names`,evaluate.py 按 run 落盘;eval_router.py `--meta_sim`(离线仿真)+ `--analyze_meta_runs`(端到端产物聚合)双通道 |
| 9 | A/B/C 去留 | Meta-Tool 自身保留 **exec_error try/except**(=原 B,单次失败不炸 run);池外点名(=原 A)改为**非致命回错误 + 引导回 `_tool_search`**;text_gap(=原 C)被显式 `_tool_search` 取代(何时检索完全交给 LLM)。`react_router` 双轨保留,供同模型同 split 端到端对照 |

**安全边界(硬性,代码注释 + 单测双锁)**:`_tool_search` 只读——拦截后只打分/返回/注入可见集,**绝不自动执行**真实工具(写副作用必须由模型后续显式调用);检索输入仅本轮 LLM 调用参数(query),**不读** `selected_tools`(执行时读 = 答案泄露)。

**离线仿真数字**(真实池 13 任务 csm12+itsm1,top_k=6,min_score=0.03,≤4 轮;`eval_router.py --meta_sim`):final_recall **27.5%** / first_recall 6.9% / hits_avg 1.20 / zero% 6.0% / full_cov 0.0% / precision 20.5%。⚠️ **诚实口径**:仿真 query 由任务文本自动切段(非 LLM 生成)→ 数字是检索器对"分批能力片段"query 的覆盖**下界/诊断**,不是端到端增益;后者需服务端 `evaluate.py --orchestrator meta_tool`。

---

## 5. 已知边界与未决问题(新对话"重新优化方案"的着力点,按影响排序)

1. **【已落地】工具调用主通道采用元工具(Meta-Tool)模式**(详见 §4.5):`MetaToolOrchestrator` 已实现并注册;9 个设计子问题的定调见 §4.5.5 表格。剩余 = 端到端增益验证(服务端)与双轨收敛决策(§6)。
2. **【待决策】LOOKUP_FLOOR 默认值**:0.08 → 0.15?(真实池扫描 0.15 显著提 recall;暴露数 20→30,靠 LLM 精排兜截断)。Meta-Tool 的 `boost_lookup` 默认 False(与 react_router 只读启发式对齐,是否默认开待数据)。
3. **【待决策】TF-IDF 是否替换为向量检索**:上会话结论 = **暂不换**(本节事实未变);Meta-Tool 模式下 query 改为 LLM 自生成的关键词,词法重叠可能更不稳定,替换优先级比之前略升。决策仍须先有错误归因数据;廉价中间态:字符级模糊 + 领域同义词表。V2 升级仍仅换 `TFIDFIndex`。
4. **【初值未调,已有默认+仿真诊断】** `_tool_search` top_k=6/min_score=0.03/缓存 64/零命中兜底 3 均为拍脑袋初值;`eval_router.py --meta_sim` 已能离线诊断检索器覆盖,**真实 LLM 触发率/误触发率需服务端端到端统计**。
5. **【状态机已实现】** `meta_tool_router.py` 已管理 query→tools 缓存(LRU)、注入集合(保序)、回退集合/兜底标志三类状态,风格同 `react_router.py`。
6. **【潜在 bug,已单测覆盖】** LLM 一次发多个 tool_call(含 `_tool_search` 与真实工具)的并行处理、注入说明在 ToolMessage 之后插入 —— 已由 test_meta_tool_router.py 覆盖;tool_call_id 真实端到端匹配仍需服务端验证。
7. **【文档欠账】** 意图级检索 + 元工具模式均未写进 `docs/tool_router_design.md`;新方案定稿后应回填一节"Meta-Tool 模式"。

---

## 6. 下一步候选(按 ROI;2026-09-03 后,Meta-Tool 已实现)

| # | 任务 | 前置 | 状态/价值 |
|---|---|---|---|
| 1 | **提交本次 Meta-Tool 落地 diff**(单功能 commit:tool_router.py +5 / evaluate.py +34 / eval_router.py +256 / meta_tool_router.py 新) | 无 | 仓库卫生,防丢失 |
| 2 | **【P0】服务端端到端对照** react_router vs meta_tool_router(同模型同 split/同 concurrency,口径写明) | conf/llm key + 容器 | **新方案增益量化**(最大空白) |
| 3 | **【P1】README / 作品集回填**:如实标注 Meta-Tool 模式(实现 + 设计问题定调 + 离线下界数字,防面试深挖翻车) | #1 | 面试呈现 |
| 4 | **【P1】`_tool_search` 参数调优**:top_k / min_score / 缓存 / 零命中兜底阈值的触发率与误触发率统计(可先用 `--meta_sim` 扫描检索器,再服务端验证 LLM 行为) | #2 数据 | 稳定性 + 延迟 |
| 5 | **【P2】TF-IDF → embedding 升级实验**:Meta-Tool 模式下 query 是 LLM 自生成关键词,词法重叠更不稳定;错误归因三分(词法/语义/池外) + rapidfuzz 对比 | #2 数据 | V2 是否值得换 |
| 6 | **【P2】双轨收敛**:react_router vs meta_tool_router 对照后决定去留;意图级检索 A/C 在 meta_tool 下已被显式 `_tool_search` 取代,B(异常)两者均保留 | #2 数据 | 减少双轨维护 |
| 7 | LOOKUP_FLOOR 0.15 默认化(仅在仍保留 top_k 兜底路径时需要) | #2 数据 | 即得 recall 增益 |

✅ 已完成(2026-08-31~09-03):执行期鲁棒性 + 意图级检索(A/B/C)拆 2 commit 提交(9a04a3d/2c2b62f);`MetaToolOrchestrator` 实现 + evaluate.py 注册 + eval_router.py 离线指标(埋点/meta_sim/analyze_meta_runs);tests/test_meta_tool_router.py 11 例,全量 **51/51**;tools_dump.json 入库。

长期主线(Phase 2 起,见 agent_design_plan.md 3-6 节):verifier-in-the-loop 自纠正 → 分层记忆+动态计划 → 政策合规引擎。每完成一阶段按惯例更新本文档与 memory 日志。

---

## 7. 参考资料与入口速查

- 设计:`docs/agent_design_plan.md`(总方案)、`docs/tool_router_design.md`(路由详细设计)
- 代码:`benchmark/tool_router.py`(路由/检索唯一实现)、`orchestrators/react_router.py`(ReAct+路由+意图发现)、`benchmark/llm_client.py`(bind_tools ~L415)、`orchestrators/base.py`(_execute_tool_call)
- 评估:`eval_router.py`(离线,`--tools tools_dump.json` 真实池)、`evaluate.py`(端到端;ORCHESTRATOR_MAP 含 react/planner_react/decomposing/react_router,`--router_top_k` 等)、`compute_score.py`、`RUN_GUIDE.md`(服务端容器/评测全流程)、`scripts/analyze_router_misses.py`(漏检归因)
- 数据:`tools_dump.json`(512→458,_domain 分域)、`gym_dbs.zip`
- 作品集:GitHub `enterprise-agent-control-plane`(README:实现状态如实标注)
- 记忆:`.workbuddy/memory/2026-09-03.md`(ToolLLM 调研+意图检索落地+TF-IDF 结论)、09-02(真实池评估与归因)、09-01(分支合并与环境坑)、08-31(作品集推送)

---
*本文档由 2026-09-03 会话生成并更新(Meta-Tool 模式定稿落地后第三次更新:意图级检索已提交、MetaToolOrchestrator 已实现、51/51 单测通过),供新会话无缝接手。实现状态均已如实标注;服务端端到端验证完成后请回填数字。*
