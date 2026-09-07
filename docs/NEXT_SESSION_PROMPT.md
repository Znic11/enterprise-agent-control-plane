# 新会话启动提示词(复制到新对话第一句)

将下面整段复制到新对话,即可让新模型无缝接手。用途:上会话(09-06/09-07)已把**工具检索/分发主线收尾**(dispatch=exec 入库 e797cc0,email 域全量池配对闭环);**本会话进入 Phase 2 —— verifier-in-the-loop 验证闭环自纠正**(取代"LLM 自评是否合规"),核心约束见【本阶段关键事实与注意点】。

---

```
你正在接手一个企业级 LLM Agent 项目(基于 ServiceNow 开源的 EnterpriseOps-Gym benchmark,本地路径 F:\Project\EnterpriseOps-Gym-main)。目标:自研"工具路由逼近 oracle 模式"的 agent 方案并持续优化,用可复现实验证明其有效性,作为面试核心项目(作品集 GitHub:enterprise-agent-control-plane)。

【第一步:先读交接文档,再动手】
1. 必读 F:\Project\EnterpriseOps-Gym-main\docs\HANDOFF.md(2026-09-07 补记版;先读 §0/§2/§3.3/§4.6(4.6.5 oracle 口径、4.6.6 email 配对)/§5/§6)
2. 必读 docs\agent_design_plan.md **§3.2(验证驱动的自纠正)与 §5 Phase 2(验证闭环,约 1 周,含验收标准)**、§6(实验设计与诚实标注);tool_router_design.md 按需
3. 查看 .workbuddy\memory\ 日志(重点 2026-09-07、2026-09-06)

【本阶段课题 = verifier-in-the-loop(Phase 2),先理解评分机制再设计】
- 评分只认数据库/SQL 终态:benchmark/verifier.py 的 VerifierEngine 三类判据 ——
  ① database_state(SQL query + expected_value + comparison_type,占绝大多数)
  ② response_check(LLM-as-judge 比 SQL 结果与模型回复)
  ③ tool_execution(检查是否调过指定工具)。
  verifiers 是**人类专家在造数据集时离线编写**的(如 CSM 一条:name=update_entitlement,
  query=SELECT coverage_hours FROM entitlement WHERE entitlement_id=73, expected=h24x7)。
- 判据在 executor 层评分时才读:executor._run_verifiers(executor.py L471-533)拿 task_result
  后逐条执行,overall_success = 全部 passed;**执行中的 agent 从头到尾看不到 verifiers**。
- ⚠️ **用户注意点(本会话最重要约束)**:验证指标由人类专家设定、**任务不会提前告知 agent**。
  agent 运行时只拥有 system_prompt(域政策,如 CSM Agent Policy)+ user_prompt(任务描述),
  不知道也不应知道 verifier 的 SQL/expected_value。
- ⚠️ **更尖锐的代码事实**:executor 把完整 BenchmarkConfig(含 verifiers 字段,models.py L64)
  传给了 orchestrator(base.py L29 self.config=config)→ **orchestrator 代码上能触达
  self.config.verifiers** —— 设计时"把验收清单注入 prompt/自查"是极其自然的诱惑,但这是
  比 selected_tools 泄露更重的判据泄露(不仅给工具名,还给答案值),**绝对禁止**。
- **失败模式(为什么不能靠模型自评)**:agent 说"完成"就收工,从不回查终态 → 半途而废型失败
  (design_plan §3.2 痛点);LLM 自评"是否合规"不可靠(自说自话 + 无终态证据)。
  verifier-in-loop 的价值 = 用**外部确定性检查(数据库真实状态)**替换模型内省式自评。

【本阶段要解决的设计问题(先与用户逐项对齐,别急着写代码)】
1. 自查信号源:agent 用什么看终态?首选**现有 MCP 只读工具**(list/get/search 查询类,
   检索+工具治理已铺路);慎用 /api/sql-runner(那是 verifier 的 SQL 通道,agent 直连 =
   借用评分基础设施,需讨论是否可接受)。哪些域/任务的只读工具能覆盖自查需求?
2. 自查 SQL 谁写:模板化只读 SQL(design_plan §3.2 明确:不依赖 LLM 写 SQL,否则引入新错误源)
   + 白名单(只允许 SELECT/只读,禁止任何 DML;执行前校验)。
3. 自查标准从哪来(不读 verifiers 的前提下):从 user_prompt 显式目标 + 域政策推导
   ("把 case INC123 状态改为 open" → 自查 = 回查该 case 的 state);域内隐含验收
   (专家额外查的字段,任务描述没写)无法推导 → 只能靠域表结构/政策惯例自查,做不到如实标注。
4. 触发点:关键写操作后(建/改/删)即查 vs 宣布完成前终态核对 vs 两者?与现有 meta_tool
   的 exec-loop 在哪一层插(checkpoint 在 orchestrator 内 vs executor 层)?
5. "完成"判定:agent 自查通过才允许收工(自报 done 前强制一轮核对)?run 内重试预算?
   注意区分已有机制:execute_sample max_num_attempts=5 是 **error 重试**(整个 sample 重跑),
   verifier-in-loop 是 **run 内判定失败→纠错→续跑**,两者不同,别混。
6. 失败信号边界:verifier pass/fail 若回喂 agent = 评分者信息参与执行。设计文档 §6.5 认为
   "verifier 公开、执行中自查合理,但要写明方法"。建议从严:agent 自查全部走自己的只读通道,
   **不读取 verifiers 字段、不把 verifier 的 expected/SQL 当纠错信号**;若确需 verifier
   反馈做重试,只允许二元 pass/fail 且实验记录里如实写明(面试被问不翻车)。
7. 对照实验:baseline(现 meta_tool + hybrid + dispatch=exec,email 域 53.73%)vs +验证闭环;
   同域(建议 email/teams,写操作多、成功率基线高)同 split 同模型同 concurrency,单变量;
   成功判据 = compute_score.py 的 Avg Success/Verifier Pass 提升 + 半途而废类失败占比下降;
   顺带录 2-3 个"自查纠错"演示样例(Phase 2 验收要求,简历素材)。

【项目核心事实(继承,勿忘)】
- 主通道 = orchestrators/meta_tool_router.py:检索可插拔 retrieval=tfidf|dense|hybrid(α=0.5,
  bge-small-en-v1.5);分发 --tool_dispatch inject|exec(默认 inject;exec = bind 恒为
  [_tool_search,_execute_tool],真实工具经 _execute_tool 统一分发,保前缀缓存,e797cc0)
- ORCHESTRATOR_MAP = react/planner_react/decomposing/meta_tool;react_router 已删
- 工具路由红线:selected_tools 只用于离线评估,执行时路由器只输入 user_prompt/system_prompt
- oracle 口径无区分度(executor L330-350 过滤 GT 白名单);email 域全量池配对(§4.6.6):
  exec 53.73% ≈ inject ≈ react-oracle 59.70% 的 90%,仅 email 域单 run,域间不可比
- 单测 61/61(test_tool_router 33 + test_meta_tool_router 17 + test_dense_retriever 11,
  tests/ 不入库);功能 HEAD = e797cc0(a46ddab = docs 回填)
- ⚠️ 运行环境:必须用项目 .\.venv\Scripts\python.exe(Python 3.14.3,含 langchain_core);本地
  无 conf/llm(key)与容器 → 端到端需服务端(RUN_GUIDE.md);numpy ≥2.3(cp314);dense/hybrid
  依赖 `uv sync --extra dense`;HF_ENDPOINT=https://hf-mirror.com

【已完成(截至 09-07,勿重复劳动)】
- 工具检索/分发主线收尾:意图级检索(2c2b62f)→ MetaToolOrchestrator(7edee85)→ dense/hybrid
  + react_router 移除(6c308bb)→ dispatch=exec 方案 Y(e797cc0)→ email 域全量池配对闭环
  (a46ddab 回填 §4.6.6);oracle e2e 100 runs/31.0% 与口径红线已归档(435c530)
- verifier 引擎本身已存在且可直接复用:VerifierEngine(_run_verifiers 通道)的 SQL 执行能力;
  evaluate.py --hf_dataset/--configs_folder 全量池派生脚本、compute_score.py 聚合均已就绪

【红线,不可违反】
- verifier 判据(verifiers 字段的 SQL/expected_value/description)禁止注入 prompt、禁止作为
  自查依据、禁止当纠错信号 —— 与 selected_tools 同级甚至更重的答案泄露(§6.5 灰色地带从严处理)
- 自查 SQL 只读:任何 DML/绕过只读白名单一律拦截;agent 自写 SQL 须过校验器
- 对照实验同模型/同 split/同 concurrency/同池;报告写明口径与 verifier 信息使用边界
- 项目在 Windows(F 盘),git rm 有连带删除同目录文件的坑;文件操作后确认落盘
```

---

**使用说明**:
- 粘贴上面代码块整段内容作为新对话的第一条消息即可。
- 接手后建议:先读文件 + 跑单测(零成本),再与用户**逐项对齐【设计问题】1-7**(尤其自查信号源与失败信号边界),再动代码;实现/实验全程记入 .workbuddy\memory\ 并按惯例回填 HANDOFF。
