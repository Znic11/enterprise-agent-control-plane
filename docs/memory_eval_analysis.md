# Phase 3 记忆对照实验 诊断与正式全量结果(2026-09-08 ~ 09-09)

> 触发:服务端 run_mem 卷跑到 34/67(3 error,compute_score 含 err 口径 52.94%)时,用户观察到
> "比印象中同进度 70–80% 大幅下降",怀疑记忆机制损伤成功率。
> 本文档 = **§0 正式全量结果(09-09,run_mem 67/67)** + §1 interim 数据复盘(过程记录)+ 机制级
> 潜在失败模式 + 外部成熟方案调研 + 改进设计 V2 建议。权威设计仍以 docs/memory_design.md 为基。

---

## 0. 正式全量结果(run_mem 67/67,2026-09-09;对照 = email_vloop/run_1 67,verify_loop on 无 memory)

**结论:记忆 V1(email 域)与对照持平 —— clean 成功率差 -1.76pp、配对 McNemar p=1.0000(无显著
损伤也无显著收益);记忆真正生效样本仅 7/67(folds>0),检验力仍不足,收益待 P0 调整后再验。**

### 0.1 总体与配对

| 卷 | err | clean | success | clean 成功率 | 含 err 成功率 |
|---|---|---|---|---|---|
| run_mem(+memory) | 6(全 timeout) | 61 | 38 | **62.30%** | 56.72% |
| ctl(email_vloop/run_1) | 3(全 timeout) | 64 | 41 | **64.06%** | 61.19% |

- 干净配对 67:saved **3** / regressed **4** / both_pass 35 / both_fail 18 → **McNemar p = 1.0000**。
- saved 3:083406(折叠 2 次,ctl fail→mem pass,长任务演示素材)、120537、120643;regressed 4:
  150451、af32832e(折叠 1 次,单例已诊断 = 检索噪声非记忆,§1.1)、419d673、fe1068d。
- 含 err 口径差 -4.47pp 主要由 err 差异驱动(mem 6 vs ctl 3,**全部 timeout**,非记忆质量);
  clean 口径差仅 -1.76pp。

### 0.2 记忆激活与子集

- mem_folds 分布:**{0: 60, 1: 5, 2: 1, 6: 1}** → 67 任务仅 **7 个(10%)触发折叠**,60 个"空转"
  (无折叠 = 无注入 = 与无记忆逐字节一致);mem_facts mean 2.0(抽取保守,读工具跳过)。
- folds>0 干净配对(记忆真正作用子集,7 个):saved 1 / regressed 1 / bp 2 / bf 2 → p=1.0,样本不足。
  - 演示素材(记忆可能受益):083406(folds=2、facts=12、tools 15,ctl 失败/mem 成功的长任务);
  - 记忆压力最大样本:20260105(folds=6、rounds=50、tools 3)→ mem 失败但 **ctl 同任务也失败**,
    无法归因记忆(任务本身难 + 长上下文压力;记忆未救场也未证损伤)。

### 0.3 成本口径

| 指标(clean 均值) | run_mem | ctl | 比 |
|---|---|---|---|
| 执行耗时 | 738s | 898s | **0.82x(降 18%)** |
| LLM 轮次(ai_message) | 15.6 | 12.7 | 1.23x(+23%) |

- 耗时下降但轮次略升:折叠压缩后单轮上下文更短(LLM 更快),但 mem 卷含更多长/空转任务;
  两卷 error 全 timeout(verify_loop 拉长任务的已知代价)。成本口径初步支持"记忆不劣化耗时、
  甚至略降",但轮次 +23% 需在 P0 调整后复核。

### 0.4 结论(汇报口径)

1. **无损伤证据**:clean -1.76pp、配对 p=1.0、folds>0 子集 p=1.0、唯一 regressed 折叠任务系检索
   噪声 —— 记忆 V1 未显著伤害成功率;"记忆导致大幅下降"不成立(interim 阶段已证,全量复核一致)。
2. **无收益证据(检验力不足)**:fold 只触发 7/67(10%),记忆在 90% 任务上未生效 → 无法检验
   "长任务分层增益"。要验证收益必须先让折叠在更多任务上发生(P0:fold_after 12→8 / 按 token
   触发 / 换 teams/csm 长任务域)。
3. **成本初步友好**:耗时 -18%(0.82x)与"记忆零新增 LLM 调用"的设计一致方向;轮次 +23% 与
   timeout 6 个需在更可控批次复核。

---

## 1. interim 数据复盘(2026-09-08,34/67 部分卷 —— 过程记录,已被 §0 全量结果取代)

**结论先行:这批数据不支持"记忆导致成功率大幅下降"。**

| 口径 | 数字 |
|---|---|
| run_mem 34 files | error 3;clean 31;**clean success 18/31 = 58.1%**;compute_score(含 err)= 52.94%(与用户表格一致) |
| **对照卷(email_vloop/run_1)同 34 任务** | clean success **19/33 = 57.6%**(差 **+0.5pp**) |
| 干净配对翻转 | saved **3** / regressed **3** / both_pass 15 / both_fail 10 → **McNemar p = 1.0000(纯噪声)** |
| mem_folds 分布 | **{0: 31, 1: 2, 2: 1}** —— 34 个任务只有 **3 个真正触发了折叠** |
| folds>0 子集 | 3 个任务:成功 2/3 = 66.7%(2 个 saved/保留,1 个 regressed) |
| folds=0 子集 | 31 个任务(记忆开启但**从未折叠、未注入任何 recap**):clean 16/28 = 57.1% |

要点:
1. **31/34 任务在记忆开启下行为与无记忆逐字节一致**(无折叠 = 无注入 = 上下文完全相同),它们贡献了
   绝大多数成功率(57.1%);这 31 个任务若失败,**与记忆机制无关**。
2. **真正检验记忆的样本只有 3 个**(2 成 1 败),统计上无任何结论力。
3. 用户"之前同进度 70–80%"的基线不成立:email_vloop/run_2 的 70.83% 是 48 任务**选择偏差子集**
   (中断/部分下载,早完成/易下载任务偏简单);本卷 34 任务与全集按文件名序前 34 只重叠 18 个
   (并发乱序完成)。**同任务正确对照 = 57.6%**,记忆卷 58.1%,基本一致。
4. 唯一 regressed 的折叠任务(af32832e,mem folds=1 失败 / ctl 成功,工具数 4 vs 7):单样本,不能归因;
   但其模式(对话更长 56 vs 27 条、业务工具反而少)可作 P1 个案复查素材。

### 1.1 单例深挖:af32832e(regressed,mem 失败 / ctl 成功)—— 结论:非记忆所致

对双卷完整对话轨迹逐条对比(2026-09-08,用户指定"先深挖单例"):

- **任务**:检查/创建发给 bob@company.com、subject="Offer Confirmation" 的 draft;检查/创建黑色 "Bob" label;把 label 应用到 draft;不发送。
- **CTL(成功,7 工具)**:`_tool_search` 命中并注入 `update_draft/get_draft/get_label/delete_draft/patch_label` 等(轨迹 [8][9])→ 用 `update_draft` 应用 label → gate 用 `get_draft/get_label` 回读 → verifier **3/3**。
- **MEM(失败,4 工具)**:`_tool_search` **始终只注入 `list_drafts/create_draft/list_labels/create_label`**(轨迹 [7][8][19]),`update_draft/get_draft` 等**从未被检索命中**;模型 4 次明说"pool 里没有 modify/get draft 工具"([29][51][55])→ 无法应用 label、gate 无法回读 → 只能凭创建响应断言 → verifier **1/3**。
- **因果判定**:MEM 卷的检索失败路径在**折叠发生之前**(第 12 轮前空转检索)就已注定;fold 在轨迹第 [41] 步注入一条 label 事实(无害但无帮助),此时败局已定。**失败根因 = LLM 生成的 `_tool_search` 查询差异 → 检索覆盖不同(run 噪声),与记忆折叠无因果关系**;与 Phase 2 已观察的 run 级噪声一致。
- **顺带洞察**:记忆 V1 只记"已确认事实",不记"哪些工具可用/已试过/正确调法"(程序性记忆) → 对"检索路径已错"的任务帮不上忙;若未来要做,属 CoALA 程序记忆层(procedural),超出 V1 范围。

**实验设计层面真正的教训**:fold 阈值 12 轮对 email 域偏高 → 折叠几乎不触发 → **对照实验没有
检验到记忆层**(检验力 = 3 个样本)。要验证"记忆对长任务有益"或"记忆有害",必须先让实验里
**记忆真的发生**:按任务轮次分布调阈值,或换更长任务域(teams/itsm),或按 token 触发。

---

## 2. 机制级潜在失败模式(即使本批未显性,review 代码后列出,供 V2 修复)

V1 设计(memory_design.md)把"事实库 + recap 注入"当作安全压缩,但以下四类风险在原理上存在:

1. **过期事实未失效(最危险)**。事实库只 append、按 ts 倒序渲染,`update/modify/delete/send` 等
   后续写操作**不会使同实体旧事实失效**。例:create_draft 记 "draft D subject=S";随后 delete_draft D
   —— recap 仍渲染 "draft D subject=S"(无墓碑)。若模型基于 recap 断言 D 仍存在 → 与真实验证冲突,
   gate 回读时穿帮;更糟的是模型可能引用已删实体做后续写操作。
2. **recap 过薄,丢"后续必须引用的精确值"**。折叠把早期工具结果压成 (entity, attr, value) 摘要,
   recap≤1500 字符、事实≤40、值截断 120/200。长任务后期 gate 需要核对"我创建的 draft 的 subject/
   收件人/label 颜色",若关键 id/subject 在折叠点未被抽到或被截断 → 模型只能凭 recap 猜,或被迫
   重新 list 全量(浪费轮次) → verify_loop 的"逐项回读"被削弱。
3. **删除型验收需要"曾存在"证据**。Graphiti 核心语义:superseded ≠ deleted。验收"把 draft X 永久删除"
   需要证明 X **曾存在且现已不在**;若折叠把 X 的创建事实当作噪声扔掉,gate 阶段无从核对"确实删了
   对的实体"。
4. **头部全量 recap 的位置与权威性**。V1 把 recap 作为早期 [system] 消息固定放在头部;模型(尤其
   flash 级)可能把 recap 当成"当前系统状态权威",而非"可能已过期的过程摘要"。Anthropic context
   engineering 明确:长上下文有 context rot,压缩后模型对早期细节的检索精度下降 —— 头部 recap 若
   不带"以实时回读为准"的护栏,会放大 1/2 的风险。
5. **与 verify_loop 的非正交交互**。gate 需要"回读早期实体当前状态",而折叠拿走了早期原始结果 →
   两模块叠加后 gate 的核对基线变薄(详见 §4 改进 4/5)。

---

## 3. 外部成熟方案调研(2026-09-08 检索;选择对项目有直接启发者)

### 3.1 记忆管理哲学:Mem0(ECAI 2025,~44k★)
- 机制:提取 + 写入时与最相似旧记忆比对,**LLM 从 ADD / UPDATE / DELETE / NOOP 中选操作**;
  冲突在**写时解决**而非取回时;另有 Memory Decay(取回时对久未访问事实降权,0.3x–1.5x)。
- v3 转向 ADD-only + 保留双版本(旧事实加时间语境),取回时多信号排序。
- **对本项目启发**:① 事实必须可被新事实**更新/作废**,不是只 append —— 我们的域有确定性
  create/update/delete 动词,可做**零 LLM 的规则版四操作**(§4-1);② "干净的小索引胜过嘈杂的大索引"。

### 3.2 分层与自主编辑:MemGPT / Letta(2023,OS 虚拟内存隐喻)
- 机制:core memory(常驻可编辑块)+ recall(全历史可检索)+ archival(向量库);agent 通过工具
  self-edit memory;Sleeptime 后台整理。
- 明确承认的局限:**让 LLM 自己决定记什么/忘什么是不可预测的**,错误摘要可致关键信息永久丢失。
- **对本项目启发**:我们坚持 **code 确定性抽取(零 LLM)是对的** —— 与其让模型写摘要,不如让
  规则层保证"id/状态/主体"这类结构化事实可靠;LLM 层只做策略。记忆修改应**可控可审计**。

### 3.3 压缩工程:Anthropic(context engineering + Claude Code compaction)
- 关键事实:context rot —— token 越多,模型从上下文取回早期信息的精度越低;压缩是手段不是目的。
- 官方 cookbook 明说 compaction **会丢失细节**,并给出 ✅/❌ 清单:
  ✅ 必须保留:业务实体 ID、类别、状态、进度、结果;❌ 可丢:全文、详细推理。
- **何时不要压缩**:短任务(<50–100k token 会加开销)、需完整审计轨迹、**每步高度依赖前文精确细节**、
  高迭代精修 —— 这与"需要逐项回读实体终态"的 gate 任务高度相关。
- 四大模式:just-in-time context(按需取)、server-side compaction(超阈值总结)、prompt caching、
  memory tool(记忆目录 + 系统注入"先查记忆")。
- **对本项目启发**:① 我们的 recap 必须显式保留"实体 id + 状态 + 关键属性值"这类**后续必引用值**
  (对照 ✅ 列),丢弃的只能是过程性文本 —— 这正是 §2-2 指出的 V1 薄弱处;② 压缩触发应看 **token/
  轮次预算**,而非拍脑袋固定轮数;③ 压缩边界应选"自然节点"(检索/写入块),不是硬切轮。

### 3.4 摘要注入与取舍:LangGraph(short-term 记忆 + SummarizationMiddleware)
- 机制:checkpointer(短期,存消息状态)+ store(长期,跨会话事实);SummarizationMiddleware
  (trigger=上下文占用分数,如 0.75;keep=最近 N 条消息)将老消息 LLM 总结后放 system;
  trim_messages 是"删",summarize 是"压缩"。
- 明确警示:**trim 假设最老消息最不重要 —— 常错;早期用户目标/约束往往是关键**。
- **对本项目启发**:任务型 agent 的早期信息(user_prompt 目标、第一批写操作)恰恰最不可丢;
  我们的折叠从"最老完整业务轮"开始,方向与 LangGraph 的 keep-recent 一致,但要保证**压缩摘要能
  回答后续问题**(summary chain 测试:第 N 轮摘要是否仍携带第 1 轮关键信息)。

### 3.5 事实时效:Zep / Graphiti(时序知识图谱,arXiv 2501.13956)
- 机制:bi-temporal edge(valid_from/valid_to + created_at/invalidated_at);新事实与旧事实冲突时
  **把旧事实 valid_to 闭合(invalidate),不删除**;agent 可回答"现在真 / 当时真";声称
  "两矛盾的旧事实让 agent 自己挑,是 agent 幻觉最常见成因之一"。
- **对本项目启发**:§2-1 过期事实问题的直接解药 —— 对同实体(entity)做**失效而非删除**:
  保留历史(支撑"曾存在/已删除"类验收),渲染只输出当前有效事实(支撑"现在真")。

### 3.6 认知分层:CoALA(2309.02427)
- 机制:工作记忆(当前观测/目标/中间推理)+ 情景(过去事件)+ 语义(事实)+ 程序(技能/规则);
  internal actions = 取回/写入/更新记忆。
- **对本项目启发**:把记忆按"角色"分离比按"时间"分离更有用 —— 任务中真正该被压缩的是
  **过程噪声(检索往返、试探性读)**,而"已确认事实(语义)"与"验收清单/子目标(工作记忆)"应始终
  高可用。V1 把语义事实当唯一记忆,verify_loop checklist 当工作记忆 —— 方向对,但两者需打通
  (gate 引用实体要能在语义层找到历史与当前态,§4-5)。

---

## 4. 改进设计 V2(建议;实现前先与用户对齐,按项目惯例先落盘设计)

> **实施状态(09-09,用户决策:P0 暂停实验只做代码 / P1 现在全做 / 补回归测试 ✅)**
> 已落地为 **V1.1(commit a09c4a6,单测 92/92)**:P0-2 **行为等价性回归测试**(memory=True
> 未达 fold 阈值 → 与 memory=False 消息流逐位一致,`TestMemoryZeroDiffRegression`)、P1-4 **事实
> 生命周期规则版**(`reconcile_facts` 写时协调:destructive 动词 delete/remove/trash/purge/revoke/send
> 命中实体 → 有效事实失效不删除保审计、不 append 新状态;同实体同属性不同值 supersede 留痕;同值
> 去重;预算裁剪先丢最旧 invalid 再丢最旧 valid)、P1-5 **Active 关键值带 + may-be-stale 护栏 +
> 失效审计区**(`active_keys` 只出有效实体最新值;recap 自带 "may be stale - re-read before acting"
> 头部;失效分区限量渲染,支撑删除型验收)。metadata 增 `mem_valid_facts`/`mem_invalid_facts`
> (离线可看记忆库新鲜度)。未做:P0-1 折叠触发策略、P1-6 折叠单元细化(待服务端新卷)、P2 全项。
> 服务器如需复跑:代码 git pull 后按 §0.3 命令 + 调整 fold 触发即可。

### P0 —— 先把实验跑对(检验力问题,不修这个后面都是空谈)
1. **让折叠真的发生**:统计 email 67 任务轮次分布,把 `--memory_fold_after` 从 12 降到覆盖多数
   中长任务(如 8)或改**按 token 预算触发**(仿 Anthropic/LangGraph trigger-fraction);
   或先换到**长任务更多**的域(teams/csm/itsm)验证记忆收益,email 域作为对照保留。
2. **行为等价性单测**:memory=True 但未达折叠阈值时,消息流必须与 memory=False **逐位一致**
   (V1 设计上满足,但缺回归测试) → 未来 A/B 才能干净归因,避免再出现"34 任务 31 个空转"说不清。
   ✅ **已落地(V1.1 a09c4a6)**:`tests/test_episodic_memory.py::TestMemoryZeroDiffRegression`。
3. **配对口径写进报告**:对照 = 同任务子集(57.6%),严禁拿 run_2(选择偏差子集)或全卷均值当基线。

### P1 —— 记忆机制修正(吸收 3.1/3.3/3.5/3.6)
4. **事实生命周期(规则版四操作,零 LLM,红线不变)**:
   - entity 维度索引;同实体出现**新写事实**(create/update 返回的新状态)或 **delete/send 类动词**
     命中该实体 → 旧事实标 invalid(失效而非删除,保留审计);
   - `render_recap` 只渲染当前有效事实,并可选附加"已失效实体清单(含删除时间)"以支撑删除型验收;
   - 保留 ts 单调、来源工具、max 预算 —— 与 V1 兼容。
   ✅ **已落地(V1.1 a09c4a6)**:`reconcile_facts`(supersede/dedup/destructive 作废/预算裁剪)
   + `Fact.invalid/invalid_reason` + recap 失效审计区(限量 + "N more invalidated" 计数)。
5. **关键值带 + recap 护栏提示**:折叠时对"曾出现的实体 id/关键属性"建**活跃键映射**注入 recap
   首部;recap 文案显式声明 "earlier tool results (may be stale) — re-read before relying on it",
   防止模型把 recap 当权威(§2-4)。gate/checklist 生成时若引用早期实体,确保 recap 或键带可支撑
   "它曾存在/现在状态"双向核对。
   ✅ **已落地(V1.1 a09c4a6)**:`active_keys`(仅有效实体最新值)+ recap 头部护栏
   "[memory recap - earlier tool results; may be stale - re-read before acting]"。
6. **折叠单元细化**:先折叠 `_tool_search` 检索往返与 schema 回喂块(噪声主源),仍超预算再折叠
   已闭环业务轮;每折叠点保留最近 N 轮原始消息(现 keep=6 保留,可保留)。
   ⏳ **未做**:需服务端新卷验证,与 P0-1 触发策略联动设计。

### P2 —— 实验设计 V2
7. ablation:memory-on-but-never-fold 作为第二个对照,分离"折叠压缩"与"抽取(不进上下文)"两变量;
8. 记录 token/轮次成本账:折叠发生后实验卷 token 应下降 —— 与成功率同报,验证"记忆省上下文且不伤
   成功率"的总账;
9. 按轮次/工具数分桶 + 单独统计 folds>0 任务与对照同任务配对。

---

## 5. 下一步(建议与用户对齐项)
1. 是否接受"当前无损伤证据、实验检验力不足"的结论(报告 §1);
2. P0-1 折叠触发策略(fold_after 8 vs token 触发 vs 换 teams 域)选哪个先做 —— ⏳ **已暂停实验,
   等代码 V1.1 审阅后由服务器新卷再验**;
3. P1-4/P1-5 已按"P1 现在全做"落地为 V1.1(a09c4a6);P1-6 折叠单元细化待 P0-1 定策后联动;
4. 等 run_mem 全 67 跑完后用同任务配对出正式报告再定 V2 实验 —— ✅ 已收口(§0,09-09)。

*本文档由 2026-09-08 会话生成、09-09 会话补记(§0 正式全量结果 + V1.1 实施状态);数据源
out/email_memory/run_mem/run_1(34→67 files)与 out/email_vloop/run_1(对照)本地直读。*
