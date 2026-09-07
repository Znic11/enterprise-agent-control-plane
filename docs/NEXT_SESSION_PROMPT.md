# 新会话启动提示词(复制到新对话第一句)

将下面整段复制到新对话,即可让新模型无缝接手。用途:上会话(09-06/09-07)已把**工具检索/分发主线收尾** —— dispatch=exec(方案 Y)入库 `e797cc0`,email 域全量池配对已闭环(exec 53.73% ≈ inject ≈ react-oracle 基线的 90%);本会话负责**对外呈现**(README/作品集如实回填、报告口径)与后续其它模块。

---

```
你正在接手一个企业级 LLM Agent 项目(基于 ServiceNow 开源的 EnterpriseOps-Gym benchmark,本地路径 F:\Project\EnterpriseOps-Gym-main)。目标:自研"工具路由逼近 oracle 模式"的 agent 方案并持续优化,用可复现实验证明其有效性,作为面试核心项目(作品集 GitHub:enterprise-agent-control-plane)。

【第一步:先读交接文档,再动手】
1. 必读 F:\Project\EnterpriseOps-Gym-main\docs\HANDOFF.md(2026-09-07 补记版;先读它的 §0/§2/§3.3/§4.6(含 4.6.5 oracle 口径与 4.6.6 email 全量池配对)/§5/§6)
2. 通读 docs\tool_router_design.md(路由详细设计,§7.3 = Hybrid 稠密检索)与 docs\agent_design_plan.md(总方案)
3. 查看 .workbuddy\memory\ 日志(重点 2026-09-07、2026-09-06、2026-09-05)

【项目核心事实】
- benchmark 评分看数据库终态(SQL verifier);最强模型平均成功率仅 45.9%
- oracle 模式 = 把任务自带 selected_tools 当白名单 = "答案泄露";工具路由目标 = 从任务描述预测该子集
- 主通道 = orchestrators/meta_tool_router.py(MetaToolOrchestrator):首轮只 bind _tool_search → LLM 显式
  调用 → 拦截 → ToolRouter.search() → 分发执行。检索后端可插拔 retrieval=tfidf|dense|hybrid:
  hybrid = α×稠密(bge-small-en-v1.5,min-max 归一)+(1-α)×稀疏 TF-IDF(α=0.5);dense 后端在
  benchmark/dense_retriever.py(TextEmbedder/SentenceTransformerEmbedder/DenseIndex/get_embedder 缓存)
- 分发双模式 --tool_dispatch inject|exec(默认 inject):inject=命中工具动态 bind(legacy,前缀逐轮变);
  exec(方案 Y,09-06 入库 e797cc0)= bind 恒为 [_tool_search,_execute_tool],命中工具完整 schema 在
  消息内回喂,真实工具经 _execute_tool(name,args) 统一分发 —— 真实工具名只是元工具字符串参数,任何
  FC 服务端(OpenAI/DeepSeek 严格校验)都接受,不依赖网关宽松;前缀稳定 → KV/前缀缓存友好
- react_router 已于 09-04 删除;ORCHESTRATOR_MAP = react/planner_react/decomposing/meta_tool
- ⚠️ 运行环境:必须用项目 .\.venv\Scripts\python.exe(Python 3.14.3,含 langchain_core);本地无 conf/llm(key)
  与容器 → 端到端需服务端(RUN_GUIDE.md);numpy 需 ≥2.3(cp314);dense/hybrid 依赖 `uv sync --extra dense`
  (非包仓库 [tool.uv] package=false);sentence-transformers/torch 建议清华镜像 + HF_ENDPOINT=https://hf-mirror.com

【已完成(截至 09-07,功能 HEAD = e797cc0;链:e797cc0 ← 435c530 ← 2449049 ← 21ee47b ← b66e82f ← 086bcea
← f8820d5 ← 25068d1 ← 6c308bb ← …)】
- 执行循环鲁棒化 + 意图级检索(9a04a3d/2c2b62f);MetaToolOrchestrator(7edee85);09-04 dense/hybrid(6c308bb,
  meta_tool e2e 32.35% vs react-oracle 30.39% 非显著 → 用户决策移除 react_router);单测 61/61
  (test_tool_router 33 + test_meta_tool_router 17 + test_dense_retriever 11;tests/ 不入库)
- 服务端真池 hybrid 离线(meta_sim 31.9% vs tfidf 27.5%,zero% 6→0);五个运维 hotfix(25068d1/f8820d5/
  086bcea/21ee47b/2449049)
- **09-05:hybrid e2e(hr/oracle)100 runs / 31.0% + oracle 口径红线**:oracle 模式 executor(L330-350)
  把池过滤成 selected_tools GT 白名单 → 检索对成功率无区分度 → 31.0% vs 32.35% 是噪声,不是增益也不是回退
- **09-06:dispatch=exec(方案 Y)入库 e797cc0**:固定 bind 保前缀缓存;analyze_meta_runs 行级
  meta_tool_dispatch(老结果缺省归 inject);方案 X(裸真实工具名直调)冒烟可行但依赖网关宽松,弃用
- **09-07:email 域全量池配对已闭环(§4.6.6)**:67 configs(email/oracle split,pop selected_tools/
  restricted_tools),compute_score.py 聚合 → dispatch=exec+hybrid **53.73%/71.47%**(2 error)≈
  dispatch=inject(同池对照,用户实测差异很小)≈ react-oracle 基线 59.70%/76.39% 的 90%。⚠️ 限定:
  仅 email 域单 run 配对,域间不可比(hr 全量池仅 19-24%);2 error 文件未归类
- docs/HANDOFF.md(09-07 版 §4.6.6)与 memory 已回填

【本次会话的核心任务(按 ROI,与用户对齐再动)】
1. 【P0】对外呈现 / README / 作品集(enterprise-agent-control-plane)如实回填:Meta-Tool 主通道 + hybrid
   检索 + dispatch=exec 三层机制;数字分层并注明口径 —— ① oracle e2e 31.0%(无区分度/噪声) ② email 全量池
   配对 53.73%(单域限定) ③ 离线下界 meta_sim 31.9%;cache_hits=0 如实说明;测试 61/61
2. 【P1】服务端离线参数扫描(零 LLM 成本):eval_router.py --meta_sim 扫 retrieval × alpha ∈ {0.3,0.5,0.7}
   × top_k,做调参依据与面试消融(仅当仍需调参时)
3. 【P1】分类 email 卷 2 个 error 文件(_execute_tool 未知名/参数错 vs 超时/网络)→ 判断 exec 提示是否需修
4. 【P1】若需跨域泛化证据:hr/csm 域全量池配对(成本换严谨,非必须;报告只做同域)
5. 【P2】_tool_search 参数调优(top_k/min_score/缓存阈值)触发率统计;bge 选型与 query_instruction 实验
6. 若用户推进其它模块(如 Phase-2 记忆/自纠正),按 agent_design_plan.md 进入新主线

【红线,不可违反】
- selected_tools 只能用于离线评估路由质量,执行时路由器只输入 user_prompt/system_prompt,禁止读取(答案泄露)
- 对照实验必须同模型、同 split、同 concurrency、同池;报告写明口径(retrieval 后端 + alpha + dispatch + 域限定)
- _tool_search 拦截只打分/返回/注入可见集,绝不自动执行真实工具(写副作用由模型显式调用)
- dense/hybrid 缺依赖时显式 ImportError,绝不静默降级成 tfidf 而让实验口径失真
- 项目在 Windows(F 盘),git rm 有连带删除同目录文件的坑(用普通 rm + git add -A);文件操作后确认落盘
```

---

**使用说明**:
- 粘贴上面代码块整段内容作为新对话的第一条消息即可。
- 接手后建议先只读文件 + 跑单测(零成本:`./.venv/Scripts/python.exe -m unittest discover -s tests`),再与用户对齐下一步(推荐先做 §6 #3 对外呈现,或用户指定的其它模块)。
