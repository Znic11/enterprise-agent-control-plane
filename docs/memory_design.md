# Phase 3 设计定稿:分层记忆 V1(Episodic 事实层)

> 定稿日期:2026-09-08 · 对齐人:用户 × 会话模型(七项设计问题逐项确认)
> 前置:Phase 2 verifier-in-the-loop V1(a68dde3)已收口 —— email 配对 clean +8.7pp、
> saved 8/regressed 3、McNemar p=0.227 **未显著**(单卷);本地新发现 `out/email_vloop/run_2`
> (48 任务**部分卷**,中断/部分下载,0 error,clean 70.83%),与 run_1 共享同一 baseline 卷,
> 合并统计仅作**佐证**:任务级 saved 10/regressed 3、p=0.0923;观测级 p=0.0636 —— **均未达
> 显著**,Phase 2 结论维持"方向证据、收益在噪声内",不得写"显著提升"。
> 本文档 = Phase 3 V1 唯一权威设计;实现后按例回填 HANDOFF §4.7。

---

## 0. 三句话摘要

1. **做什么**:在 MetaToolOrchestrator 内加一个 **Episodic 记忆层**(已确认事实)+ **历史压缩**
   (摘要替换 + 保留最近 N 轮原始消息),只对治"长任务早期事实被 89k 上下文稀释/遗忘"这一类失败,
   不做动态计划(DAG/replan 后置)。
2. **不做什么(红线)**:记忆/压缩内容**零 verifier 触达**(与 selected_tools 同级);事实只来自
   **成功业务工具结果**与系统确认;压缩不破坏 tool_call↔tool_result 配对;gate/checklist 消息
   永不折叠;`--memory` 默认关,旧口径零污染。
3. **怎么证明**:对照 = 当前完整链(meta_tool + exec + hybrid + verify_loop,email 67 全量池,
   clean 64.06%)vs 同链 +memory —— 同池同模型单变量;按真实业务工具调用数分桶看长任务分层增益;
   成本记"省下 vs 开销"总账(记忆不新增 LLM 调用是本方案相对 Phase 2 的关键卖点)。

---

## 1. 已对齐的七项设计决策(定稿口径)

| # | 决策点 | 定稿口径 | 备选(已否决/后置) |
|---|---|---|---|
| 1 | 记忆形态 | **Episodic 一层**:存 `已确认事实(实体/字段/值/来源工具/轮次ts)`;Working 由现有上下文 + gate checklist 承担;Semantic 域知识后置 | 三层全做 / Working+Episodic 双结构(V2) |
| 2 | 事实来源 | **code 确定性抽取**(成功业务工具结果 → 规则抓取),零 LLM、零虚构,抽取不到不记 | 轻量 LLM 摘要(有虚构风险、+LLM 轮次,备选实验臂) |
| 3 | 压缩/注入 | **摘要替换 + 保留最近 N 轮**:按"完整轮次"折叠,原位注入单条 `[system] memory` recap | 只加不删(不降上下文)/ 裸截断(丢事实) |
| 4 | verify_loop 接口 | checklist = **Working 目标态,不入 episodic**;事实层独立存"已确认事实";gate/checklist/remind 消息**永不折叠** | checklist 条目化进 episodic(≈动态计划,后置) |
| 5 | 动态计划 | V1 **只做记忆**,不做 sub-goal DAG/replan | checklist 结构化 + 打勾(Phase 3.5) |
| 6 | 评估口径 | 对照 = verify_loop on vs **+memory**;email 67 同池同模型;按真实业务工具数分桶(≤5 / 6–10 / ≥11);配对 McNemar;成本 = 省下 token vs 开销 | teams 域第二对照(成本高,可选) |
| 7 | 红线 | 记忆只来自工具结果/系统确认;verifier 零触达;摘要宁缺毋滥;报告写明口径 | — |

---

## 2. 实现规格(meta_tool_router.py + evaluate.py)

### 2.1 事实抽取(纯 code、确定性、零 LLM)

- **触发点**:execute 循环内每次**成功真实业务工具调用**后即时抽 —— exec 分发(`_execute_tool`)
  与 inject/直调两条成功路径都覆盖(与 `_vl_note_real_tool` 同位置,单点收口)。
- **原始素材**:MCP 包裹格式(已实测 email 域):
  `tool_result = {"success": True, "result": {"content": [{"type": "text", "text": "<json字符串>"}], "isError": false}}`。
  抽取器先解包 `content[0].text` → `json.loads`;解析失败或非 dict → **跳过**(宁缺毋滥)。
- **规则**(`extract_facts(tool_name, args, result_payload) -> List[Fact]`):
  1. **实体标识**:从调用参数取键命中白名单的短标量
     (`id|*_id|key|email|name|number|sys_id|guid|uuid`,如 `message_id=msg_002`、`draft_id=…`);取不到再从结果 JSON 取顶层/单元素容器的 id 键;
  2. **状态/确认字段**:结果 JSON 中键命中状态白名单的短标量
     (`status|state|enabled|active|verified|disposition|accessWindow|purpose|type`);
  3. **产出上限**:单次调用最多 3 条 Fact,字段值非空短标量(截断 200 chars);结果含列表(list_*/search_* 返回数组)时**不逐条记**,仅当容器含上述单实体 id/状态时记 ≤1 条汇总;
  4. **不记**:失败结果、零命中、非 JSON、参数与结果均无白名单键。
- **Fact 结构**(dataclass):`entity, attribute, value, source_tool, ts`(ts = 轮次序号);
  渲染为域无关文本:`Confirmed (from <source_tool>): <entity> <attribute>=<value>`。
- **事实库**:`self._facts: List[Fact]`,上限 `MEMORY_MAX_FACTS`(默认 40,超出丢最旧,逻辑层,
  与消息无关,不破坏配对)。

### 2.2 折叠(压缩)与注入

- **轮次边界**:execute 循环维护每轮的 messages 区间(iteration 起点/终点),**折叠最小单元 =
  一个完整轮次**(assistant 回复 + 其触发的全部 ToolMessages + 该轮 system 注入说明),
  保证 tool_call↔tool_result 配对与顺序不被破坏。
- **触发**:轮次序号 > `MEMORY_FOLD_AFTER_ROUNDS`(默认 12)时执行一次折叠;折叠最旧的
  `MEMORY_FOLD_ROUNDS`(默认 6)轮(至少留 `MEMORY_KEEP_ROUNDS`(默认 6)轮原始消息在窗口尾)。
- **永不折叠**:`messages[0]`(system_prompt)/`messages[1]`(user_prompt)、verify_loop 的
  checklist/gate/remind 消息所在轮次。
- **原位替换**:删除最旧可折叠窗口 → 在删除位置(首条保留消息之前)插入一条
  `HumanMessage(content="[system] memory recap: …")`(与 `_append_system_note` 同构,
  conversation_flow 同步 `{"type": "system_message", "stage": "memory_recap"}`)。
- **recap 内容**:从 `self._facts` 取**最新**事实按 ts 倒序渲染(全量快照、自愈 —— 后折叠的
  recap 自然覆盖前一个),上限 `MEMORY_RECAP_CHARS`(默认 1500 chars,超出截断)。
- **幂等**:同一轮次区间只折叠一次(记录已折叠边界),不重复删/插。

### 2.3 CLI 与 metadata

- `MetaToolOrchestrator` 新参 `memory=False`(默认关保旧口径);常量可被参数覆盖:
  `MEMORY_FOLD_AFTER_ROUNDS / MEMORY_FOLD_ROUNDS / MEMORY_KEEP_ROUNDS / MEMORY_MAX_FACTS /
  MEMORY_RECAP_CHARS`。
- `evaluate.py`:`--memory`(store_true),仅 meta_tool 分支透传。
- `get_result_metadata()` 新增(`memory=False` 时零污染):
  `mem_enabled / mem_facts(条数) / mem_folds(折叠次数) / mem_recap_chars(末次 recap 长度) /
  mem_kept_rounds(最近保留轮数) / mem_rounds(总轮次)`。
- 红线审计字段:**记忆链路不新增任何 verifier 相关读取**,抽取函数签名不接收 config/verifiers。

### 2.4 单测(TestEpisodicMemory,tests/ 不入库)

1. 确定性抽取:构造 MCP 包裹成功结果 → 抽到预期 Fact(参数 id + 状态键);非 JSON/失败/无键 → 零 Fact;
2. 列表结果不逐条记(单实体汇总或零条);
3. 折叠完整:超阈值轮次折叠后 tool_call↔tool_result 配对不破(断言删除区间边界正确、recap 已插入、消息顺序合法);
4. gate/checklist 消息永不折叠(verify_loop on 场景);
5. `memory=False` 旧行为零污染(messages 逐轮全留、metadata 无 mem_* 或全默认);
6. **verifier 零触达**:抽取/折叠全程不触碰 `self.config.verifiers`(构造含 verifiers 的 config,断言调用不读取);
7. 事实库上限与 recap 截断。

---

## 3. 评估口径(服务端)

```bash
# 对照 = 现状完整链(verify_loop on)  —— email 67 全量池(顶层 pop selected_tools/restricted_tools),
# 与 §4.6.6/§4.6.7 同 split/同模型(deepseek-v4-flash)/同 concurrency
evaluate.py --configs_folder runtmp_full_email --orchestrator meta_tool \
    --retrieval hybrid --tool_dispatch exec --verify_loop --output_folder out/email_vloop_mem
# 实验 = 同链 + memory(单变量)
evaluate.py ... 同上 + --memory
```

- **主指标**:clean 成功率配对翻转(saved/regressed/both_*)+ McNemar 精确 p(vs verify_loop on 卷);
- **分桶**:按 run 的真实业务工具调用数(conversation_flow 中非 `_tool_search/_execute_tool` 的
  tool_result 计数)分 ≤5 / 6–10 / ≥11 三桶,看 saved/regressed 集中度 —— 预期收益集中在长桶;
- **成本总账**:省下 = Σ(被折叠消息 json 字符) − recap 字符;开销 = 抽取 CPU(≈0)+ recap 注入
  token;LLM 轮次应 ≈ verify_loop on(~12.7),**不新增模型调用**;若折叠让部分任务免于 gate 重复
  回读,耗时还有下降空间(待实测,不预设);
- **报告口径**:注明单域(email)、单模型、verify_loop on 基底、run_2 仅为部分卷佐证;
  Phase 2 的 p=0.227/合并 p≈0.06–0.09 维持"未显著"表述。

---

## 4. 待办链与后续

1. 本会话:实现(memory 抽取+折叠)+ 单测 + 全量回归 → docs 回填(HANDOFF §4.7)→ commit;
2. 服务端:email 67 `--verify_loop --memory` 对照卷;
3. 收口:analyze 脚本扩展 memory 维度;run_2(48 子集)按"佐证、注明共享 baseline + 部分卷"
   回填 Phase 2 报告;
4. Phase 3.5(待记忆卷有收益再定):checklist 结构化 + 打勾、失败触发 replan(§3.4);
   Semantic 域知识层(表结构/字段约束,与政策引擎联动)。

---

*本文档由 2026-09-08 会话撰写(Phase 3 启动;接手基线 = 66/66 单测通过,HEAD = 9ae8c50)。*
