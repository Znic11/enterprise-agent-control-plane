# 新会话启动提示词(复制到新对话第一句)

将下面整段复制到新对话,即可让新模型无缝接手。用途:上会话(09-04)已把 Meta-Tool 的稀疏 TF-IDF 检索升级为 **Hybrid 稠密检索(参考 Spring AI Alibaba)并移除 react_router**,单测 55/55 全绿;本会话负责**服务端验证与对外呈现**(hybrid 端到端对照 / 参数调优 / README 回填)。

---

```
你正在接手一个企业级 LLM Agent 项目(基于 ServiceNow 开源的 EnterpriseOps-Gym benchmark,本地路径 F:\Project\EnterpriseOps-Gym-main)。目标:自研"工具路由逼近 oracle 模式"的 agent 方案并持续优化,用可复现实验证明其有效性,作为面试核心项目(作品集 GitHub:enterprise-agent-control-plane)。

【第一步:先读交接文档,再动手】
1. 必读 F:\Project\EnterpriseOps-Gym-main\docs\HANDOFF.md(2026-09-04 版,hybrid 检索定稿;先读它的 §0/§3.3/§4.6/§5/§6)
2. 通读 docs\tool_router_design.md(路由详细设计,§7.3 = Hybrid 稠密检索)与 docs\agent_design_plan.md(总方案)
3. 查看 .workbuddy\memory\ 日志(重点 2026-09-04、2026-09-03)

【项目核心事实】
- benchmark 评分看数据库终态(SQL verifier);最强模型平均成功率仅 45.9%
- oracle 模式 = 把任务自带 selected_tools 当白名单 = "答案泄露";工具路由目标 = 从任务描述预测该子集
- 主通道 = orchestrators/meta_tool_router.py(MetaToolOrchestrator):首轮只 bind _tool_search → LLM 显式
  调用 → 拦截 → ToolRouter.search() → 命中 schema 动态并入 bind 集 → 模型显式调用真实工具
- 唯一路由实现 = benchmark/tool_router.py,检索后端可插拔 retrieval=tfidf|dense|hybrid:
  hybrid = α×稠密(bge-small-en-v1.5,min-max 归一)+(1-α)×稀疏 TF-IDF(α=0.5);dense 后端在
  benchmark/dense_retriever.py(TextEmbedder 抽象 + SentenceTransformerEmbedder + DenseIndex + get_embedder 缓存)
- react_router 已于 09-04 彻底删除;ORCHESTRATOR_MAP = react/planner_react/decomposing/meta_tool
- ⚠️ 运行环境:必须用项目 .\.venv\Scripts\python.exe(Python 3.14.3,含 langchain_core);本地无 conf/llm(key)
  与容器 → 端到端需服务端(RUN_GUIDE.md);numpy 需 ≥2.3(cp314),装法见 HANDOFF §2;dense/hybrid 依赖用
  `uv sync --extra dense` 装(项目是非包仓库 [tool.uv] package=false,勿 `pip install -e '.[dense]'`;
  若坚持 -e,pyproject 已加 setuptools 包发现配置可正常构建);sentence-transformers/torch 建议清华镜像
  + HF_ENDPOINT=https://hf-mirror.com;服务端若报 "Multiple top-level packages",先删仓库根 ls/list 垃圾文件

【已完成(截至 09-04,commit 链 6c308bb ← 67714f6 ← 7edee85 ← 2c2b62f ← 9a04a3d …)】
- 执行循环鲁棒化 + 意图级检索(9a04a3d/2c2b62f,历史;react_router 已删,其 B 异常兜底被 meta_tool 继承)
- MetaToolOrchestrator(7edee85):_tool_search 显式检索 → schema 注入 → 真实执行;零命中回喂引导、连续
  3 次兜底 bind 全池;query→hits LRU(64);warmup_top_k 预热(默认 None=纯单通道);九个设计问题定调见
  HANDOFF §4.5.5;离线下界数字:meta_sim final_recall 27.5%(任务文本切段模拟,非端到端)
- 09-04 dense/hybrid(6c308bb):用户端到端实测 meta_tool 32.35% vs react(oracle) 30.39%(小样本非显著)→
  用户决策移除 react_router + 换稠密检索;evaluate.py --retrieval(默认 hybrid)/eval_router.py resolve_retrieval
  (auto 无依赖回退 tfidf);pyproject [dense] extra;单测 55/55(test_tool_router 33 + meta_tool 11 +
  dense 11,FakeEmbedder 确定性);离线 tfidf 冒烟 ALL recall@20 42.5% 零回归
- docs/HANDOFF.md(09-04 版)、tool_router_design.md §7.3 已回填;tests/ 在 .git/info/exclude(不入库)

【本次会话的核心任务(按 ROI,与用户对齐再动)】
1. 【P0,最大空白】服务端 hybrid 端到端对照:evaluate.py --orchestrator meta_tool --retrieval hybrid
   (同 split 对比用户已跑的 tfidf 版 32.35%);跑完 eval_router.py --analyze_meta_runs 聚合。命令见
   HANDOFF §4.6.4(先 `uv sync --extra dense` 或 pip 直装 sentence-transformers/torch,
   bge 首次下载设 HF_ENDPOINT=https://hf-mirror.com)
2. 【P1】服务端离线参数扫描(零 LLM 成本):eval_router.py --meta_sim 扫 retrieval ∈ {tfidf,dense,hybrid}
   × alpha ∈ {0.3,0.5,0.7},产出 recall/precision 对照表做调参依据与面试消融
3. 【P1】README / 作品集(enterprise-agent-control-plane)如实回填:hybrid 检索 + 55/55 + e2e 小样本数字
   (注明"小样本非显著"),防面试深挖翻车
4. 【P1】_tool_search 参数调优(top_k/min_score/缓存/零命中兜底阈值)的触发率统计
5. 【P2】bge 选型(bge-small/base/e5)与 query_instruction 开关实验;稠密同族噪音诊断

【红线,不可违反】
- selected_tools 只能用于离线评估路由质量,执行时路由器只输入 user_prompt/system_prompt,禁止读取(答案泄露)
- 对照实验必须同模型、同 split、同 concurrency;报告写明口径(retrieval 后端 + alpha)
- _tool_search 拦截只打分/返回/注入可见集,绝不自动执行真实工具(写副作用由模型显式调用)
- dense/hybrid 缺依赖时显式 ImportError,绝不静默降级成 tfidf 而让实验口径失真
- 项目在 Windows(F 盘),git rm 有连带删除同目录文件的坑(用普通 rm + git add -A);文件操作后确认落盘
```

---

**使用说明**:
- 粘贴上面代码块整段内容作为新对话的第一条消息即可。
- 接手后建议先只读文件 + 跑单测(零成本:`./.venv/Scripts/python.exe -m unittest discover -s tests`),再与用户对齐服务端验证与调参优先级。
