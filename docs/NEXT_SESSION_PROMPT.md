# 新会话启动提示词(复制到新对话第一句)

将下面整段复制到新对话,即可让新模型无缝接手。用途:上会话已按用户选定方向落地 **Spring AI Alibaba 元工具 (Meta-Tool) 模式** 并全部单测通过(51/51);本会话负责**收尾验证与对外呈现**(端到端对照 / 参数调优 / README 回填)。

---

```
你正在接手一个企业级 LLM Agent 项目(基于 ServiceNow 开源的 EnterpriseOps-Gym benchmark,本地路径 F:\Project\EnterpriseOps-Gym-main)。目标:自研"工具路由逼近 oracle 模式"的 agent 方案并持续优化,用可复现实验证明其有效性,作为面试核心项目(作品集 GitHub:enterprise-agent-control-plane)。

【第一步:先读交接文档,再动手】
1. 必读 F:\Project\EnterpriseOps-Gym-main\docs\HANDOFF.md(2026-09-03 晚版,Meta-Tool 定稿;先读它的 §0/§3.3/§4.1/§4.5.5/§5/§6)
2. 通读 docs\tool_router_design.md(路由详细设计,§7 = 意图级检索 + Meta-Tool 模式)与 docs\agent_design_plan.md(总方案)
3. 查看 .workbuddy\memory\ 日志(重点 2026-09-03、2026-09-02)

【项目核心事实】
- benchmark 评分看数据库终态(SQL verifier);最强模型平均成功率仅 45.9%
- oracle 模式 = 把任务自带 selected_tools 当白名单 = "答案泄露";工具路由目标 = 从任务描述预测该子集
- 唯一路由实现 = benchmark/tool_router.py(TF-IDF:三级漏斗 粗筛→可选 LLM 精排 strict/union→两层兜底;执行期 ToolRouter.search 意图检索),react_router.py 与 meta_tool_router.py 双轨并行供对照
- ⚠️ 运行环境:必须用项目 .\.venv\Scripts\python.exe(受管 python 无 langchain_core);本地无 conf/llm(key)与容器 → 端到端需服务端(RUN_GUIDE.md)

【已完成(上会话)】
- 执行循环鲁棒化 + 意图级检索触发点 A/B/C(已提交 9a04a3d + 2c2b62f,含 tools_dump.json 入库);命中只入可见集绝不自动执行
- MetaToolOrchestrator(orchestrators/meta_tool_router.py)已实现:首轮只 bind _tool_search → LLM 显式调用 → 拦截 → ToolRouter.search(TF-IDF)→ 命中 schema 动态并入后续轮次 bind 集(每轮重新 bind,对 LLMClient 零改动)→ 模型显式调用真实工具走 _execute_tool_call;零命中回喂引导、连续达阈值(默认3)兜底 bind 全池;query→hits LRU 缓存(64);warmup_top_k 预热模式(默认 None=纯 Meta-Tool);§4.5.4 九个设计问题已逐项定调(见 HANDOFF §4.5.5)
- evaluate.py 注册 "meta_tool"(CLI --meta_tool_top_k/--meta_tool_min_score/--meta_warmup_top_k);eval_router.py 新增 Meta-Tool 离线指标(--meta_sim 仿真 + --analyze_meta_runs 聚合),真实池冒烟数字:final_recall 27.5%(诚实口径:query 任务文本切段模拟,是检索器覆盖下界,非端到端)
- 单测 51/51 通过(test_tool_router 33 + test_react_router 7 + test_meta_tool_router 11);docs/HANDOFF.md、docs/tool_router_design.md §7、memory 日志均已回填
- ⚠️ 尚未提交(Meta-Tool diff):M benchmark/tool_router.py(+5,inputSchema 双兼容)/ evaluate.py(+34)/ eval_router.py(+256) + ?? orchestrators/meta_tool_router.py;tests/ 在 .git/info/exclude(不入库,既有策略)

【本次会话的核心任务(按 ROI,与用户对齐再动)】
1. 提交 Meta-Tool 落地 diff(单功能 commit,保持"选择性提交"原则)
2. 【P0,最大空白】服务端端到端对照 react_router vs meta_tool_router(同模型/同 split/同 concurrency,口径写明;conf/llm key + 容器见 RUN_GUIDE.md);跑完用 eval_router.py --analyze_meta_runs 聚合 run 级 meta_tool_* 指标
3. 【P1】README / 作品集(enterprise-agent-control-plane)回填:如实标注 Meta-Tool 模式(机制、九个设计问题定调、离线下界数字,防面试深挖翻车)
4. 【P1】_tool_search 参数调优(top_k/min_score/缓存/零命中兜底阈值):可先用 --meta_sim 扫描检索器,再服务端验证 LLM 触发率
5. 【P2】TF-IDF → embedding 升级实验(需先有端到端错误归因数据);双轨(react_router/meta_tool)对照后决定去留

【红线,不可违反】
- selected_tools 只能用于离线评估路由质量,执行时路由器只输入 user_prompt/system_prompt,禁止读取(答案泄露)
- 对照实验必须同模型、同 split、同 concurrency;报告写明口径
- 检索命中工具只进可见集/可调用集,绝不自动执行(写工具副作用由模型显式调用)
- 元工具本身是只读操作(检索);不要在 tool_search 拦截里偷偷执行真实工具
- 不删除其他文件仍在引用的代码;改动前确认引用关系
- 项目在 Windows(F 盘),git rm 有连带删除同目录文件的坑(用普通 rm + git add -A);文件操作后确认落盘
```

---

**使用说明**:
- 粘贴上面代码块整段内容作为新对话的第一条消息即可。
- 接手后建议先只读文件 + 跑单测(零成本:`.\.venv\Scripts\python.exe -m unittest discover -s tests`),再与用户对齐提交与端到端优先级。
