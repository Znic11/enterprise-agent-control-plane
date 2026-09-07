# 新会话启动提示词(复制到新对话第一句)

将下面整段复制到新对话,即可让新模型无缝接手。用途:上会话(09-07 深夜)已完成 **Phase 2 verifier-in-the-loop V1 收口**(a68dde3 落地 + email 配对方向证据:saved 8/regressed 3,McNemar p=0.227 **未显著**,成本 2.33x;docs 回填 731eb07,分析脚本 3c2e67e);**本会话进入 Phase 3 —— 分层记忆(Hierarchical Memory)± 动态计划**,核心约束见【本阶段要解决的设计问题】。

---

```
你正在接手一个企业级 LLM Agent 项目(基于 ServiceNow 开源的 EnterpriseOps-Gym benchmark,
本地路径 F:\Project\EnterpriseOps-Gym-main)。目标:自研"工具路由逼近 oracle 模式"的 agent
方案并持续优化,用可复现实验证明其有效性,作为面试核心项目(作品集 GitHub:
enterprise-agent-control-plane)。

【第一步:先读交接文档,再动手】
1. 必读 F:\Project\EnterpriseOps-Gym-main\docs\HANDOFF.md(2026-09-07 深夜版;先读
   §0/§2/§3.3/§4.6.6/§4.6.7(Phase 2 结果与口径)/§5 #8/§6 #10)
2. 必读 docs\agent_design_plan.md §3.3(分层记忆)/§3.4(计划-执行-反思)/§5 Phase 3
   (验收:长任务成功率提升、多域稳定复现)/§6(实验设计与诚实标注)
3. 查看 .workbuddy\memory\2026-09-07.md(Phase 2 落地与配对全记录)与 MEMORY.md
4. 本地产物:out/email_vloop/run_1(vloop 卷)、out/full_exec_email/run_1(baseline 卷)
   已下载,analyze 报告在 out/vloop_report.{json,md} —— 分析脚本 scripts/analyze_vloop_runs.py

【本阶段课题 = Phase 3 分层记忆(±动态计划),先理解 Phase 2 现状再设计】
- Phase 2(verifier-in-the-loop,V1 已入库 a68dde3):meta_tool 收到"模型无 tool_call 声称
  完成"时不再直接 break,而是:① 首轮专用规划调用让模型自列验收 checklist(user_prompt+
  域政策推导,零 verifier 触达);② gate 阶段要求 ≥1 次成功业务只读工具调用才放行 FINAL;
  ③ 无证据反复声称 → 有界提醒(--verify_max_rounds=3)后强制收尾(vl_forced_done)。
- Phase 2 服务端结果(email 67 全量池配对,deepseek-v4-flash,单卷;本地已复算):
  clean 成功率 baseline 36/65=55.38% → verify_loop 41/64=64.06%(+8.7pp);Verifier Pass
  73.67%→81.17%;配对 63 干净对 saved 8 / regressed 3 / both_pass 33 / both_fail 19,
  **McNemar p=0.227 未达显著(单卷)**;成本 = 执行耗时 385s→898s(2.33x)、LLM 轮次
  8.5→12.7(1.49x,+1 规划调用);3 个 error 全 timeout;FINAL 标记率 0%(模型不遵守前缀,
  不强校验仅统计)。定性:8 saved 的 final 清一色按 checklist 逐项回读 ✅,救回验收字段
  漏写/写错型失败;3 regressed 中 1 例 FK 后端报错(环境性)、2 例疑似噪声。
- **第二卷可能已跑或待跑**(email_vloop_2 / full_exec_email_2):若本地/服务器已有,
  用 scripts/analyze_vloop_runs.py 配对 + 会话内合并两卷 discordant 算总 McNemar;
  若显著则 Phase 2 可写"验证闭环有效",不显著则如实写"方向证据、收益在噪声内"。

【本阶段要解决的设计问题(先与用户逐项对齐,别急着写代码)——Phase 3 清单】
1. 记忆形态:三层(Working/Episodic/Semantic,design_plan §3.3)全做 vs V1 只做
   **Episodic 事实摘要**("已确认事实:case INC123 state=open,归属账户 A42")?
   建议 V1 = episodic 一层,Working 由现有上下文承担、Semantic 域知识后置。
2. 事实来源:code 从工具成功结果做结构化抽取(确定性、零 LLM)vs 轻量 LLM 摘要?
   红线:只记工具结果/系统确认过的事实,禁止虚构;原始 ToolMessage 是否仍保留在历史。
3. 压缩与注入策略:触发时机(每 N 轮 / 关键写操作后)?注入形态(摘要替换旧事实 +
   保留最近 N 轮原始消息)?上下文预算上限(token)?
4. 与 verify_loop 的接口:gate 的 checklist 是否进 episodic("验收目标/已完成子目标")→
   支撑执行中状态机?两模块同在 meta_tool orchestrator 内如何不打架(gate 已是轻量 replan)。
5. 动态计划范围:V1 只做记忆、不做显式 sub-goal DAG/replan(§3.4 后置)?还是把 checklist
   升级成"结构化子目标列表 + 打勾"的轻量计划?replan 触发(工具连续失败/自查 fail/目标变化)
   哪个先做?
6. 评估口径:收益应集中在长任务 → 按 steps/工具数分桶看分层增益,不只看平均;对照 =
   当前完整链(meta_tool+exec+hybrid+verify_loop)vs +memory,同池同模型单变量;
   成本口径 = 记忆压缩省下的 token/时间 vs 压缩本身开销,要能算总账。
7. 红线:记忆内容不得来自 verifiers(与 selected_tools 同级);摘要宁缺毋滥;对照实验
   同池同模型;报告写明口径与 verifier 信息使用边界。

【项目核心事实(继承,勿忘)】
- 主通道 = orchestrators/meta_tool_router.py MetaToolOrchestrator:检索可插拔
  retrieval=tfidf|dense|hybrid(α=0.5,bge-small-en-v1.5);分发 --tool_dispatch
  inject|exec(默认 inject;exec = bind 恒为 [_tool_search,_execute_tool],真实工具经
  _execute_tool 统一分发,保前缀缓存,e797cc0);verify_loop(Phase 2,a68dde3,默认关)。
- ORCHESTRATOR_MAP = react/planner_react/decomposing/meta_tool;react_router 已删。
- 单测 66/66(test_tool_router 33 + test_meta_tool_router 22 + test_dense_retriever 11,
  tests/ 不入库);HEAD = c47a5b9(.gitignore out/);功能链 a68dde3(verify_loop)← e797cc0;
  分析脚本 scripts/analyze_vloop_runs.py(3c2e67e)。
- ⚠️ 运行环境:必须用项目 .\.venv\Scripts\python.exe(Python 3.14.3,含 langchain_core);
  本地无 conf/llm(key)与容器 → 端到端需服务端(RUN_GUIDE.md);numpy ≥2.3(cp314);
  dense/hybrid 依赖 `uv sync --extra dense`;HF_ENDPOINT=https://hf-mirror.com。
- 本地 out/ 已 gitignore(c47a5b9),评测结果放 out/ 不入库;产物可随会话下载。

【红线,不可违反】
- verifier 判据(verifiers 字段的 SQL/expected_value/description)禁止注入 prompt、禁止
  作为自查依据、禁止当纠错信号 —— 与 selected_tools 同级甚至更重的答案泄露;
  Phase 3 记忆/压缩同样不得写入 verifier 内容。
- 自查 SQL 只读(Phase 2 未用 SQL,纯业务只读工具通道);记忆事实只来自工具结果/系统确认。
- 对照实验同模型/同 split/同 concurrency/同池;报告写明口径与 verifier 信息使用边界
  (Phase 2 结论如实:p=0.227 未显著 + 成本 2.33x,勿写成"显著提升")。
- 项目在 Windows(F 盘),git rm 有连带删除同目录文件的坑;文件操作后确认落盘;
  提交用选择性 add,`git add .` 前确认 out/ 等产物不混入。
```

---

**使用说明**:
- 粘贴上面代码块整段内容作为新对话的第一条消息即可。
- 接手后建议:先读文件 + 跑单测(零成本),再与用户**逐项对齐【设计问题】1-7**(记忆形态与
  verify_loop 接口最关键),再动代码;实现/实验全程记入 .workbuddy\memory\ 并按惯例回填
  HANDOFF(新增 §4.7 小节)与本文档。
