# 新会话启动提示词(复制到新对话第一句)

将下面整段复制到新对话,即可让新模型无缝接手。用途:上会话(09-08)已完成 **Phase 3 分层记忆 V1 的设计定稿(memory_design.md,81745fa)与实现落地(020b583)** + **Phase 2 第二卷佐证收口**(run_2 非独立卷,合并仍未显著);**本会话核心任务 = 服务端端到端对照实验(验证记忆层有效性)+ 呈现/README 回填**。

---

```
你正在接手一个企业级 LLM Agent 项目(基于 ServiceNow 开源的 EnterpriseOps-Gym benchmark,
本地路径 F:\Project\EnterpriseOps-Gym-main)。目标:自研"工具路由逼近 oracle 模式"的 agent
方案并持续优化,用可复现实验证明其有效性,作为面试核心项目(作品集 GitHub:
enterprise-agent-control-plane)。

【第一步:先读交接文档,再动手】
1. 必读 F:\Project\EnterpriseOps-Gym-main\docs\HANDOFF.md(2026-09-08 版;先读
   §0/§2/§3.3/§4.6.6/§4.6.7(Phase 2 结果与口径,含 run_2 佐证补记)/§4.7(Phase 3 记忆
   V1 落地,含服务端实验命令)/§5/§6)
2. 必读 docs\memory_design.md(Phase 3 V1 唯一权威设计:七项决策表/实现规格/评估口径/红线)
   与 docs\agent_design_plan.md §3.3/§3.4/§5/§6(总方案与诚实标注)
3. 查看 .workbuddy\memory\2026-09-08.md(本会话工作日志)与 MEMORY.md(项目长期记忆)
4. 本地产物:out/email_vloop/run_1 + run_2(Phase 2 两卷)、out/full_exec_email/run_1
   (baseline 卷)、out/vloop_report{,_run2}.{json,md}(analyze_vloop_runs.py 产物)

【Phase 2 现状(方向证据,勿写成显著)】
- verify_loop V1(a68dde3):claim-done 不再直接 break,首轮 checklist + gate 强制业务只读
  回读核对 + 有界提醒;email 67 全量池配对 clean 55.38% → 64.06%(+8.7pp),saved 8/
  regressed 3,p=0.227;成本 ~2.33x 耗时 / LLM 轮次 ~1.5x;3 error 全 timeout。
- run_2 佐证(09-08 收口):48 任务部分卷 ⊂ run_1 67(同批重跑,**非独立任务卷**,勿当
  独立复制实验);单卷 saved 6/regressed 2 p=0.2891;任务级并集去重 N=13 p=0.0923;
  观测级相加 N=19 p=0.0636 —— 均未达 0.05、无方向冲突翻转。定显著性仍需 teams 域或
  新 seed 独立任务卷。报告引用须注明"48 子集、同批重跑、p 均未达 0.05"。

【Phase 3 分层记忆 V1(已实现,默认关,待服务端对照)——本会话主任务】
- 七项决策已对齐(用户逐项确认,全选推荐):Episodic 一层 / code 确定性抽取(零 LLM 零虚构)/
  摘要替换+保最近 N 轮 / checklist=Working 目标态不入 episodic / V1 不做 DAG-replan /
  verify_loop on + memory 对照 / 记忆零 verifier 触达(红线)。
- 实现(020b583):orchestrators/episodic_memory.py(纯函数:Fact dataclass、
  unwrap_mcp_result 含 isError=True 守卫、extract_facts 含 _token_pos 域前缀动词、
  render_recap)+ meta_tool_router.py(常量 DEFAULT_MEMORY=False、FOLD_AFTER=12 轮、
  KEEP=6 轮、MAX_FACTS=40、RECAP_CHARS=1500;_maybe_fold 整轮原位替换 recap,
  保 tool_call<->result 配对;gate 激活停折;conversation_flow 全量+审计标记;
  tool_results 独立不折叠)+ evaluate.py(--memory/--memory_fold_after/
  --memory_keep_rounds)。单测 80/80(新增 test_episodic_memory 14 例)。
- 【服务端实验(本会话核心,对照命令见 HANDOFF §4.7)】:
  对照 = meta_tool + exec + hybrid + verify_loop(email 67 全量池,deepseek-v4-flash,
  同 §4.6.7 卷,可复用其 baseline 文件直接配对);
  实验 = 同链 + --memory --memory_fold_after 12 --memory_keep_rounds 6(单变量)。
  产出:任务级配对 saved/regressed + McNemar;按真实业务工具调用数分桶看长任务分层增益
  (记忆卖点 = 对治长任务事实遗忘,别只看平均);成本账 = 记忆零新增 LLM 调用(区别于
  Phase 2 的 +1 规划轮),同成功率下 token/耗时下降即"白拿"。
  分析口径沿用 scripts/analyze_vloop_runs.py(load_folder/pair_flip/_mcnemar_exact_p),
  记忆卷的 mem_* metadata 在 run 级 dict 里可读。
- 若服务端无资源/用户不安排,至少产出:README 回填(Phase 3 记忆如实标注"实现未验"或
  "默认关"),勿写任何未经实验支撑的提升结论。

【项目核心事实(继承,勿忘)】
- 主通道 = orchestrators/meta_tool_router.py MetaToolOrchestrator:retrieval=tfidf|dense|
  hybrid(α=0.5,bge-small-en-v1.5);dispatch inject|exec(e797cc0);verify_loop(a68dde3);
  episodic memory(020b583,默认关)。ORCHESTRATOR_MAP = react/planner_react/decomposing/
  meta_tool;react_router 已删。
- 单测 80/80(test_tool_router 33 + test_meta_tool_router 22 + test_dense_retriever 11 +
  test_episodic_memory 14;tests/ 不入库);HEAD = 020b583;功能链 020b583(memory)←
  81745fa(设计)← a68dde3(verify_loop)← e797cc0(exec)…;origin/main 已同步至 81745fa。
- ⚠️ 运行环境:必须用项目 .\.venv\Scripts\python.exe(Python 3.14.3,含 langchain_core);
  本地无 conf/llm(key)与容器 → 端到端需服务端(RUN_GUIDE.md);numpy ≥2.3(cp314);
  dense/hybrid 依赖 `uv sync --extra dense`;HF_ENDPOINT=https://hf-mirror.com。
- 本地 out/ 与 .workbuddy/ 已 gitignore,评测结果/记忆日志不入库。

【红线,不可违反】
- verifier 判据(verifiers 字段 SQL/expected_value/description)禁止注入 prompt、禁止作为
  自查依据、禁止当纠错信号 —— 与 selected_tools 同级;Phase 3 记忆/压缩同样零触达
  (代码已守:只在成功业务工具结果上抽事实,gate 激活期停折)。
- 记忆事实只来自工具结果/系统确认(宁缺毋滥);压缩不破坏 tool_call<->tool_result 配对。
- 对照实验同模型/同 split/同 concurrency/同池;报告写明口径与 verifier 信息使用边界
  (Phase 2 结论如实:p=0.227 单卷未显著 + run_2 三档 0.2891/0.0923/0.0636 均未显著 +
  成本 2.33x;Phase 3 未跑实验前不得写"记忆提升成功率")。
- 项目在 Windows(F 盘),git rm 有连带删除同目录文件的坑;文件操作后确认落盘;
  提交用选择性 add,`git add .` 前确认 out/、.workbuddy/ 等不混入。
```

---

**使用说明**:
- 粘贴上面代码块整段内容作为新对话的第一条消息即可。
- 接手后建议:先读文件 + 跑单测(零成本,应 80/80),再确认服务端实验安排(用户是否已跑/
  是否本会话跑),随后按 HANDOFF §4.7 命令执行对照 + 分析 + 回填 HANDOFF/memory 日志;
  若转向呈现,先改作品集 README(数字分层、口径注明),具体改动与用户逐项对齐。
