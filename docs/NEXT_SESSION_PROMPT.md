# 新会话启动提示词(复制到新对话第一句)

将下面整段复制到新对话,即可让新模型无缝接手。用途:用户已选定 **Spring AI Alibaba 元工具 (Meta-Tool) 模式** 重新实现工具调用,本次会话负责落地与评估。

---

```
你正在接手一个企业级 LLM Agent 项目(基于 ServiceNow 开源的 EnterpriseOps-Gym benchmark,本地路径 F:\Project\EnterpriseOps-Gym-main)。目标:自研"工具路由逼近 oracle 模式"的 agent 方案并持续优化,用可复现实验证明其有效性,作为面试核心项目(作品集 GitHub:enterprise-agent-control-plane)。

【第一步:先读交接文档,再动手】
1. 必读 F:\Project\EnterpriseOps-Gym-main\docs\HANDOFF.md(2026-09-03 版,含未提交 diff/未决问题/下一步候选;先读它的 §0/§4.5/§5/§6)
2. 通读 docs\tool_router_design.md(路由详细设计)与 docs\agent_design_plan.md(总方案)
3. 查看 .workbuddy\memory\ 日志(重点 2026-09-03、2026-09-02)

【项目核心事实】
- benchmark 评分看数据库终态(SQL verifier);最强模型平均成功率仅 45.9%
- oracle 模式 = 把任务自带 selected_tools 当白名单 = "答案泄露";工具路由目标 = 从任务描述预测该子集
- 唯一路由实现 = benchmark/tool_router.py(TF-IDF 三级漏斗:粗筛→可选 LLM 精排 strict/union→两层兜底),react_router.py 已接入
- 最近已完成(未提交):执行循环 try/except 鲁棒化 + 意图级检索三触发点 A(未知名)/B(异常)/C(文本缺口),命中只入活跃集绝不自动执行;单测 40/40
- ⚠️ 运行环境:必须用项目 .\.venv\Scripts\python.exe(受管 python 无 langchain_core);本地无 conf/llm(key)与容器 → 端到端需服务端(RUN_GUIDE.md)

【本次会话的核心任务(用户已选定方向 → 元工具 Meta-Tool 模式)】

参考 Spring AI Alibaba 的"元工具 Meta-Tool"模式重做工具调用:系统只向 LLM 暴露一个 `_tool_search` 特殊工具,LLM 需要更多工具时显式调用它(传入关键词),系统拦截 → 检索全池 → 动态注入命中工具 schema → LLM 在下一轮选择并执行真实工具。完整 6 步流程与设计草案见 HANDOFF §4.5。

具体动作(按优先级,与用户对齐再动):
1. 先与用户核对 git status 未提交 diff(鲁棒性 + 意图检索)是否仍要提交;若 Meta-Tool 取代原意图检索主通道,可考虑 squash 或仅保留 B(异常 try/except)
2. 实现 orchestrators/meta_tool_router.py 的 MetaToolOrchestrator:
   a. 构造 `_tool_search` 工具 def(input_schema: {query:str, top_k?:int=6})
   c. 首轮 invoke_with_tools 仅 bind [tool_search_def]
   d. 拦截 tool_search 调用 → 调 ToolRouter.search(query, top_k, min_score) 复用既有检索实现
   e. V1 采用"[system] 下列工具已可用:…"文本注入(零侵入 LLMClient);若效果不够再升级到 bind_tools 动态扩
   f. 同会话 query→tools 缓存(LRU/dict);不跨任务
   g. 真实工具走 _execute_tool_call;零命中回退处理(注入"请明确功能"或回退 react_router 全集)
3. evaluate.py:ORCHESTRATOR_MAP 新增 "meta_tool": MetaToolOrchestrator
4. eval_router.py 增加 Meta-Tool 离线指标:meta_tool_searches / hits_avg / cache_hits / final_recall
5. 回归单测 40+ 例;新增 Meta-Tool 测试(mock LLM 序列验证拦截/注入/执行/回退路径)
6. 与用户对齐 §4.5.4 的 9 个设计问题,逐项定调(尤其:纯 Meta-Tool vs 混合 top_k 预热;回退路径;并行 tool_call)

【可选延伸(优先级 P1+）】
- 端到端对照 react_router vs meta_tool_router(同模型同 split;服务端,需 key+容器)
- 元数据埋点支撑后续 TF-IDF → embedding 升级决策
- 文档回填 docs/tool_router_design.md 的"Meta-Tool 模式"章节

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
- 若只想聚焦 Meta-Tool 实现,删掉"可选延伸"段。
- 接手后建议先只读文件 + 跑单测(零成本),再与用户对齐 §4.5.4 的 9 个设计问题,然后进入实现。