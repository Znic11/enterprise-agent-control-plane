# 新会话启动提示词(复制到新对话第一句)

将下面整段复制到新对话,即可让新模型无缝接手。用途:上会话(09-09)已完成 **Phase 3 记忆 V1.1 写时协调代码落地(a09c4a6,单测 92/92)** + **run_mem 全量首卷收口(62.30% vs 64.06%,p=1.0,检验力不足)**;**用户决策 = P0 暂停实验只做代码 / P1 全做 / 补零差异回归(已全部执行)**,当前处于"等用户审阅 V1.1 代码"状态;**本会话核心任务 = 用户审阅通过后服务端按新 fold 触发策略复跑对照 + 呈现/README 回填**。

---

```
你正在接手一个企业级 LLM Agent 项目(基于 ServiceNow 开源的 EnterpriseOps-Gym benchmark,
本地路径 F:\Project\EnterpriseOps-Gym-main)。目标:自研"工具路由逼近 oracle 模式"的 agent
方案并持续优化,用可复现实验证明其有效性,作为面试核心项目(作品集 GitHub:
enterprise-agent-control-plane)。

【第一步:先读交接文档,再动手】
1. 必读 F:\Project\EnterpriseOps-Gym-main\docs\HANDOFF.md(2026-09-09 版;先读
   §0/§2/§3.3/§4.6.6/§4.6.7(Phase 2 结果与口径)/§4.7(Phase 3 记忆 V1+V1.1 落地、
   run_mem 全量结果、服务端复跑命令)/§5/§6)
2. 必读 docs\memory_design.md(Phase 3 设计权威:决策表/实现规格/评估口径/红线)与
   docs\memory_eval_analysis.md(§0 正式全量结果 + §1 单例深挖 + §3 六框架调研 +
   §4 改进设计 V2 含实施状态)与 docs\agent_design_plan.md
3. 查看 .workbuddy\memory\2026-09-09.md(本会话工作日志)与 MEMORY.md(项目长期记忆)
4. 本地产物:out/email_memory/run_mem/run_1(记忆实验卷,67 files)、out/email_vloop/run_1
   + run_2(Phase 2 两卷=对照 baseline)、out/full_exec_email/run_1

【当前状态(09-09,如实呈现)】
- Phase 3 记忆 V1(020b583)+ V1.1 写时协调(a09c4a6)均已落地,默认关零污染;单测 92/92。
- run_mem 全量首卷(67/67,对照 email_vloop/run_1):clean 62.30% vs 64.06%(-1.76pp);
  干净配对 67 saved 3/regressed 4 → McNemar p=1.0000 无显著;err 6 vs 3 全 timeout。
  **记忆激活率低:fold 触发仅 7/67(10%),60 任务空转(无折叠=与无记忆逐字节一致)→
  检验力不足:无损伤证据也无收益证据**(结论:不是"记忆无效",是"没检验到")。
  成本:执行耗时 0.82x(-18%)、LLM 轮次 +23% —— 记忆未劣化耗时。
- 唯一 regressed 折叠任务(af32832e)已深挖:败局发生在折叠之前,根因 = _tool_search 检索
  覆盖差异(run 噪声)非记忆。
- V1.1 写时协调:reconcile_facts(destructive 动词 delete/remove/trash/purge/revoke/send
  命中实体 → 有效事实失效不删除保审计、不 append;同实体同属性不同值 supersede;同值
  去重;预算裁剪先丢最旧 invalid)+ active_keys 关键值带 + recap may-be-stale 护栏 +
  失效审计区;metadata 增 mem_valid_facts/mem_invalid_facts;零差异回归已补。
- 用户已决策:实验暂停,先代码(已完) → 下一步需用户审阅 + 服务器复跑。

【本会话主任务(按用户安排优先级)】
A. 用户审阅 V1.1 代码(或直接指示复跑)→ 服务器 git pull 后**换 fold 触发策略复跑对照**:
   候选 P0-1:fold_after 12→8(覆盖更多中长任务)/ 按 token 触发 / 换 teams/csm 长任务域;
   基线仍复用 email_vloop/run_1(同模型同 concurrency,report 注明历史批次);
   对照命令模板见 HANDOFF §4.7(注意 evaluate.py 无 --samples,用 --hf_dataset +
   --domain email --mode oracle + --num_runs 1)。
B. 若用户转向呈现:README/作品集回填(Phase 3 记忆如实标注"方向无损伤、检验力不足、
   待新触发策略复跑";数字分层写口径),逐项与用户对齐。

【项目核心事实(继承,勿忘)】
- 主通道 = orchestrators/meta_tool_router.py MetaToolOrchestrator:retrieval=tfidf|dense|
  hybrid(α=0.5,bge-small-en-v1.5);dispatch inject|exec(e797cc0);verify_loop(a68dde3);
  episodic memory V1.1(020b583 + a09c4a6,默认关)。ORCHESTRATOR_MAP =
  react/planner_react/decomposing/meta_tool;react_router 已删。
- 单测 92/92(tests/ 不入库);HEAD = a09c4a6(09-09);main 链 a09c4a6 ← 6b2150d ←
  2fed0f2 ← b68a534 ← f93fe0d ← 020b583(memory V1)← 81745fa(设计)← a68dde3 ← e797cc0…
- ⚠️ 运行环境:必须用项目 .\.venv\Scripts\python.exe(Python 3.14.3,含 langchain_core);
  本地无 conf/llm(key)与容器 → 端到端需服务端(RUN_GUIDE.md);numpy ≥2.3(cp314);
  dense/hybrid 依赖 `uv sync --extra dense`;HF_ENDPOINT=https://hf-mirror.com。
- 本地 out/ 与 .workbuddy/ 已 gitignore,评测结果/记忆日志不入库。

【红线,不可违反】
- verifier 判据(verifiers 字段 SQL/expected_value/description)禁止注入 prompt、禁止作为
  自查依据、禁止当纠错信号 —— 与 selected_tools 同级;Phase 3 记忆/压缩同样零触达
  (代码已守:只在成功业务工具结果上抽事实,gate 激活期停折)。
- 记忆事实只来自工具结果/系统确认(宁缺毋滥);压缩不破坏 tool_call<->tool_result 配对;
  V1.1 起事实"失效不删除",recap 自带 may-be-stale 护栏(勿把 recap 当权威,需回读)。
- 对照实验同模型/同 split/同 concurrency/同池;报告写明口径与 verifier 信息使用边界
  (Phase 2:p=0.227 单卷未显著 + run_2 三档 0.2891/0.0923/0.0636 均未显著 + 成本 2.33x;
  Phase 3 记忆首卷:无损伤/无收益证据、检验力不足、成本 0.82x —— 未复跑前不得写
  "记忆提升成功率")。
- 项目在 Windows(F 盘),git rm 有连带删除同目录文件的坑;文件操作后确认落盘;
  提交用选择性 add,`git add .` 前确认 out/、.workbuddy/ 等不混入。
```

---

**使用说明**:
- 粘贴上面代码块整段内容作为新对话的第一条消息即可。
- 接手后建议:先读文件 + 跑单测(零成本,应 92/92),再与用户确认是"审阅代码/指示复跑"
  还是"转向呈现",随后按上面对应路线执行 + 回填 HANDOFF/memory 日志;
  无论哪条路线,数字口径一律按"当前状态"节如实标注。
