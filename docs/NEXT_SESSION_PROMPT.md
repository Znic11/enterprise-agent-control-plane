# 新会话启动提示词(复制到新对话第一句)

将下面整段复制到新对话,即可让新模型无缝接手:

---

```
你正在接手一个企业级 LLM Agent 项目(基于 ServiceNow 开源的 EnterpriseOps-Gym benchmark,本地路径 F:\Project\EnterpriseOps-Gym-main)。目标是自研一个能真实落地的企业级 agent,用可复现实验证明其有效性,作为实习简历的核心项目。

【第一步:先读交接文档,再动手】
1. 必读 F:\Project\EnterpriseOps-Gym-main\docs\HANDOFF.md(交接文档,含代码状态与待办)
2. 通读 F:\Project\EnterpriseOps-Gym-main\docs\agent_design_plan.md(总方案)与 docs\tool_router_design.md(路由详细设计)
3. 查看 .workbuddy\memory\ 下的工作日志

【项目核心事实(来自交接文档)】
- benchmark 评分看"数据库终态"不看动作路径(SQL verifier);最强模型平均成功率仅 45.9%
- 官方 oracle 模式 = 把任务自带 selected_tools(真实所需工具标注)直接当白名单给 agent,即"答案泄露";工具路由器的目标 = 从任务描述预测该子集
- 内置 agent(react/planner_react/decomposing)四大缺陷:全量工具直灌、无终态验证、历史无压缩、政策靠 prompt
- 已实现的 Phase 1 代码:benchmark/tool_router.py(TF-IDF 版路由)、orchestrators/tool_router.py(关键词+LLM 版)、orchestrators/react_router.py(已注册 evaluate.py 的 react_router)、两个离线评估脚本、executor 的 router_llm_config 支持
- ⚠️ 现状问题:两套路由实现重复未统一;所有代码从未运行过(无 results 目录);项目不是 git 仓库

【你的首要任务(P0,按顺序)】
1. git init 并提交当前代码状态(现在不是 git 仓库,先建立基线)
2. 统一两套路由实现(推荐保留 benchmark/tool_router.py 的 TF-IDF 版,合并 orchestrators/tool_router.py 的 LLM 路由与降级逻辑;同步修改 react_router.py 的 import;删除重复文件前先确认无引用)
3. 跑通离线评估(不需要 docker 和 LLM key):python eval_router.py --data_dir data/revised --top_k 20,记录 recall@k / precision@k(目标 recall@20 ≥ 90%),并把评估脚本统一成一个
4. 跑端到端对照 react vs react_router(需 docker MCP server + conf/llm 的 key;建议 teams 或 email 域 oracle 模式),记录成功率与 token 消耗对比
5. 完成后汇报:统一后的代码结构、离线评估数字、端到端对比数字、遇到的坑

【红线,不可违反】
- selected_tools 只能用于离线评估路由质量,执行时路由器只输入 user_prompt/system_prompt,禁止读取(答案泄露)
- 对照实验必须同模型、同 split、同 concurrency,只改 agent 架构变量;报告要写明口径
- 不要删除其他文件里仍在引用的代码;改动前先确认引用关系
- 项目在 Windows 上,文件操作后要确认落盘成功

【长期目标(按 ROI 排序,先做 P0 再往下)】
- Phase 2:验证驱动的自纠正(把 verifier 的 SQL 检查搬进执行循环)
- Phase 3:分层记忆 + 动态计划
- Phase 4:政策合规引擎(policy-as-code)
详细设计都在 docs/agent_design_plan.md 第 3-6 节。每完成一个阶段,更新 docs/HANDOFF.md 和 .workbuddy/memory/ 日志。
```

---

**使用说明**:
- 粘贴上面代码块整段内容作为新对话的第一条消息即可。
- 若想精简,可只保留【项目核心事实】【你的首要任务】【红线】三段,约压缩 40%。
- 新模型接手后建议先只读文件+git init+跑离线评估(零成本、无风险),确认环境无误再碰端到端。
