# EnterpriseOps-Gym 自研 Agent 设计方案与落地路线图

> 目标:在 ServiceNow 开源的 EnterpriseOps-Gym 基准上,设计并实现一个真正"企业级"的 LLM Agent,
> 用可复现的实验证明其有效性,并以此作为实习简历的核心项目。
> 本文档 = 对 benchmark 出题逻辑的解读 + 内置 agent 的解剖 + 六个可落地的设计方向 + 分阶段路线图 + 简历包装指南。

---

## 0. 一句话理解这个 benchmark(所有设计的出发点)

**它评分的是"数据库最终状态",不是"动作路径"。**

每个任务跑在真实 MCP server 上,agent 的操作会真实写入数据库;评分时由 SQL verifier 检查终态
(外加少量 response_check / tool_execution 校验)。这意味着:

- 你不需要模仿"人类操作员"的步骤,**只需要稳定地达成正确终态**;
- 达成终态的唯一途径是**正确地调用工具、正确地写入数据**;
- 而难点在于:512 个工具、89k 平均上下文、164 张表、平均 9.15 步/任务(最多 34 步)、5.3 个验证条件。

参考坐标系:当前最佳模型 Claude Opus 4.6 平均成功率 **45.9%**,开源最佳 DeepSeek-V3.2 仅 **24.2%**。
**头部闭源模型都只有不到一半的成功率 → 这不是"提示词工程"能解决的差距,是 agent 架构的问题。**
这正是你可以做出差异化贡献的空间。

另外注意官方设计了 `oracle` / `plus_5_tools` / `plus_10_tools` / `plus_15_tools` 四种模式:
在 oracle 基础上给 agent 塞入 5/10/15 个无关干扰工具。分数随干扰工具数量下降 ——
**官方自己就在用实验证明"工具数量是 agent 性能的变量",这直接支撑下面第一个设计方向。**

---

## 1. 内置 agent 解剖:它们为什么"简单"

项目自带三个 orchestrator,全部位于 `orchestrators/`,接入点:

```python
# evaluate.py:41-43
ORCHESTRATOR_MAP = {
    "react": ReactOrchestrator,               # 单循环 ReAct
    "planner_react": PlannerReactOrchestrator,  # 一次计划 + 执行
    "decomposing": DecomposingPlannerOrchestrator,  # 拆子任务 + 文本记忆
}
```

### 1.1 react(4.5KB)
- 循环:LLM 推理 → 调工具 → 观察 → 重复,直到 LLM 不再请求工具或达到 `max_iterations=50`。
- **没有**:工具选择(512 个全量给 LLM)、上下文压缩、终态验证、错误恢复策略、记忆。
- "任务完成"完全由 LLM 说了算 —— LLM 说停就停,不管数据库终态对不对。

### 1.2 planner_react(10.9KB)
- 两遍式:pass 1 用独立 planner LLM 生成一份散文式策略计划;pass 2 executor 拿着计划跑 ReAct。
- **计划是一次性的**:执行过程中永远不回头修订计划。计划与实际环境冲突时,executor 只能硬撑。
- planner 提示词其实写得很细(意图分析/政策合规/风险评估),说明官方知道问题在哪,但只做到"一次计划"。

### 1.3 decomposing(40KB,最复杂)
- 三阶段:planner 把任务拆成 2–5 个顺序子任务 → executor 子代理逐个子任务跑独立 ReAct → 汇总。
- 引入了 WorkingMemory,但只是**把前面子任务的结果文本拼进下一个子任务的 prompt**。
- 没有:子任务并行(依赖 DAG 被退化成顺序链)、跨子任务验证、失败子任务的局部重试。

### 1.4 四个共性结构缺陷(设计自研 agent 的四个切入点)

| # | 缺陷 | 后果 | 对应设计方向 |
|---|------|------|--------------|
| 1 | 512 个工具全量直灌上下文 | 选择噪音、注意力稀释、89k 上下文 | 工具路由层 |
| 2 | "完成"由 LLM 自说自话,从不回查终态 | 未达标就收工,白跑 | 验证驱动的自纠正 |
| 3 | 历史只增不减 | 早期关键事实(工单号/客户名/ID)被遗忘 | 分层记忆 |
| 4 | 企业政策只写在 system prompt 里 | 合规靠模型自觉,违反是概率事件 | 政策合规引擎 |

---

## 2. 我的核心观点(简历里"自己的见解"部分)

1. **工具治理 > 模型能力。** 任务平均只需要 5–15 个工具,把 512 个工具全量暴露给 LLM 不是"能力",是"噪音"。工具选择的信噪比决定了 agent 能力的上限 —— 与其换更强模型,不如先让模型"看得更少、看得更准"。
2. **验证是一等公民,不是事后评分。** benchmark 的 verifier 是评分工具,但真正的 agent 应该把它搬进执行循环,作为"终态自查"。能自我验证的 agent,错误会被当场纠偏而不是累计放大。
3. **记忆要结构化,不是聊天记录。** 89k 上下文里塞聊天历史,本质是让模型在噪音里捞事实。企业 agent 的记忆应该是"已确认事实 + 待办 + 约束"的结构化状态,历史可以压缩、可以遗忘。
4. **计划要可修订,策略要可编译。** 静态计划在真实环境中必然过时;同样,把 prompt 里的政策写成一段话,不如编译成可执行的 pre-flight 检查规则 —— "合规"应该是保证,不是概率。
5. **失败要分类,迭代要数据驱动。** 跑完 benchmark 只报一个 success rate 没有意义。给每个失败样本建 taxonomy(工具选错 / 参数错误 / 政策违反 / 过早终止 / 上下文遗忘),按类别占比排序、逐个修复 —— 这才是工程化的迭代方式,也是面试官最想看到的工作方法。

---

## 3. 六个可落地的设计方向(按 ROI 排序)

### 3.1 工具路由层(Tool Router)⭐ 性价比最高,建议第一个做

**痛点**:512 个工具全量给 LLM → 上下文爆炸 + 选择歧义。

**方案**:
- 入口用**轻量模型**(或 embedding + 关键词规则)根据用户任务判断目标域与意图,从 512 个工具中筛出 **top-k(建议 15–30)** 工具,只把这部分 schema 暴露给执行 LLM。
- 工具 schema 本身要压缩:只给参数名 + 类型 + 一句话说明,不给完整描述。
- 执行过程中允许"渐进式发现":LLM 请求一个不在当前子集中的工具时,再临时展开它的 schema(而不是一开始全给)。

**实现要点**:工具选择器本身用 few-shot 固化(从训练集挑 10–20 个任务示例);路由失败时兜底回退全量工具。

**预期收益**:直接复用官方 oracle vs plus_15_tools 的观测 —— 减少工具噪音本身就能提分,同时显著降低 token 成本(89k 上下文中工具 schema 占大头)。

### 3.2 验证驱动的自纠正(Self-Verification Loop)⭐ 直击评分机制,建议第二个做

**痛点**:LLM 说"完成"就完成,从不检查数据库终态;评分却只看终态。

**方案**:
- 在每个**关键写操作后**(建 case、更新状态、创建记录),用 SQL 回查确认写入成功且字段正确(把 verifier 的 `database_state` 逻辑搬进执行循环)。
- 增加"终止前检查":LLM 宣布完成时,强制对任务相关实体做一轮终态核对(该创建的创建了?该关闭的关闭了?状态对了?)。不达标 → 生成纠错指令继续执行,而不是直接收工。
- 给每个失败的工具调用配**结构化错误恢复**:重试 → 换参数重试 → 换工具 → 上报,而不是把原始错误堆回上下文让 LLM 自己猜。

**实现要点**:复用 `benchmark/verifier.py` 的 `VerifierEngine`,在 orchestrator 里直接调用它的 SQL 执行能力;自省查询用模板化 SQL,不依赖 LLM 写 SQL(否则引入新错误源)。

**预期收益**:直接吃下一大部分"半途而废"型失败;这也是把 benchmark 的评分逻辑变成 agent 能力的最好演示 —— 简历面试时最容易被问到的亮点。

### 3.3 分层记忆架构(Hierarchical Memory)

**痛点**:89k 上下文,ReAct 一路 append,早期事实被稀释;decomposing 的 WorkingMemory 只是文本拼接。

**方案**(三层记忆,全部结构化):
- **Working Memory**:当前子任务状态机 —— 目标、已完成的步骤、当前待办(每次调用 LLM 前只注入这一层)。
- **Episodic Memory**:已完成子目标的结果摘要(如"已创建 case INC123,状态 new,关联联系人 A")。每次执行完一个关键操作,用一个小 LLM 把新事实抽取成 JSON 追加进去。
- **Semantic Memory**:领域知识(表结构、字段约束、工具使用惯例),构建一次,全局复用。
- 上下文组装:`working + episodic(压缩后)+ semantic(按需检索)+ 最近 N 轮原始消息`,丢弃中间噪音轮次。

**实现要点**:记忆条目带时间戳与来源工具;每次组装时由轻量模型做一次"事实去重 + 摘要"。

**预期收益**:长任务(10–34 步)不丢关键事实,是它最大的价值 —— 平均 89k 上下文中,大部分任务的失败根源是"忘了前面查到的 ID/状态"。

### 3.4 计划-执行-反思闭环(Plan-Execute-Reflect)

**痛点**:planner_react 的计划一次性生成、永不修订;实际执行 3–5 步后计划往往已失效。

**方案**:
- 计划以**结构化 JSON(子任务 DAG)** 输出,而不是散文:每个子任务含 id / 目标 / 依赖 / 所需工具 / 验收条件。
- 执行中每完成一个子任务(或遇到失败),触发**反思**:对比"预期结果 vs 实际结果",由 planner 决定继续 / 修正剩余计划 / 回滚。
- 关键:**验收条件写成可执行检查**,即每个子任务结束时用 3.2 的自查机制确认,而不是"计划说完成了就完成"。

**实现要点**:这是对官方 decomposing 的自然演进 —— 保留其子任务思想,但把顺序链升级为依赖 DAG + 动态 replan。可以先只做"线性 + 失败时重规划",DAG 并行是进阶项。

### 3.5 政策合规引擎(Policy-as-Code)⭐ 最能体现"企业级"三个字

**痛点**:system_prompt 里企业政策很长(如 CSM 域:注册 case 必须同时创建 interaction、默认 priority、必须验证产品归属客户账户、assignment 约束……),LLM 靠 prompt 记住这些必然出错。

**方案**:
- 把每一条政策编译成**规则检查器**(代码或 JSON 规则):在每次**写操作执行前**做 pre-flight 检查,违反则拦截并返回给 LLM 纠正指令。
- 例(CSM):`创建 case 前必须存在 interaction` → 规则:case 创建动作的 payload 中必须含 interaction 引用,否则拒绝并提示先调 create_interaction。
- 政策规则同时注入 system prompt(让 LLM 理解)+ 代码执行(让系统保证),双保险。

**实现要点**:从任务数据里人工提炼每个域 10–20 条高频规则;规则的触发条件是"工具名 + 参数模式"匹配,规则引擎是纯代码,不引入 LLM 判断(确定性保证)。

**预期收益**:政策类失败从"概率"变"零",还能作为安全审计的依据 —— 这是"真实落地的企业级 agent"与 demo 的分水岭,面试时讲这个故事非常有说服力。

### 3.6 专业化多智能体分工(可选,锦上添花)

**方案**:入口 Triage Agent(轻量模型)判断任务域 → 分发给各域 specialist agent(CSM Agent / ITSM Agent / Email Agent……),每个 specialist 持有该域工具子集 + 该域政策规则 + 该域 few-shot;复杂跨域任务(Hybrid 域)由 Coordinator 编排。

**预期收益**:每个 specialist 的上下文更干净、政策更聚焦,与 3.1/3.5 天然协同;Hybrid 域(当前最弱,最优仅 34%)是差异化战场。

---

## 4. 工程底座(真实落地必备,即使不做 benchmark 也必须建)

- **可观测性**:记录每步的工具名/参数/结果/token 消耗/耗时,输出成 JSONL 审计日志 —— 这是失败分析的原料,也是"企业级"的证据。
- **失败分类器**:对失败样本自动/半自动打标签(tool_selection / arg_error / policy_violation / premature_stop / context_loss / other),按类别统计占比。
- **幂等与重试**:写操作先读后写(如创建前先查重);工具调用失败按"重试 → 换参 → 换工具"分级恢复。
- **成本控制**:模型分级(路由/判断用便宜小模型,关键推理用大模型)+ 工具 schema 缓存 + 结果缓存。
- **安全审计**:所有写操作过政策引擎 + 落审计日志(谁、何时、调了什么、改了什么) —— 企业 agent 的准入标准。

---

## 5. 落地路线图(4 个阶段,每阶段可验收)

> 原则:先 baseline,再逐模块消融;每个阶段在**同一数据集 split、同一模型、同一 concurrency** 下对比,数字才可信。

### Phase 0 — 建立基线(1–2 天)
- [ ] 跑通环境:unzip `gym_dbs.zip`、起 1–2 个域 MCP server、配好 LLM key。
- [ ] 用 `react` + 你选的主模型,跑通 1 个域(建议先 teams 或 email,成功率相对高)的 oracle 模式,记录 baseline。
- [ ] 建好可观测日志 + 失败样本收集管道(为 Phase 1 的分析做准备)。
- **验收**:有可复现的 baseline 数字和一批失败样本。

### Phase 1 — 工具路由 + 上下文治理(约 1 周)
- [ ] 实现 3.1 工具路由层;顺手做工具 schema 压缩 + 渐进式发现。
- [ ] 对照实验:react(baseline) vs react+router,同域同模型。
- **验收**:同域 oracle 模式成功率提升 + token 成本明显下降;能讲清楚"为什么"。

### Phase 2 — 验证闭环(约 1 周)
- [ ] 实现 3.2 关键写操作后自查 + 终止前终态核对 + 结构化错误恢复。
- [ ] 对照实验:上阶段最优 vs +验证闭环。
- **验收**:半途而废类失败显著减少;这是简历主卖点,录 2–3 个"自查纠错"的演示样例。

### Phase 3 — 记忆 + 动态计划(1–2 周)
- [ ] 实现 3.3 分层记忆;再实现 3.4 结构化计划 + 失败触发 replan。
- [ ] 扩展到 3–4 个域(含一个写操作复杂的域,如 CSM),做跨域一致性验证。
- **验收**:长任务成功率提升;能在多个域稳定复现收益。

### Phase 4 — 政策引擎 + 全量评估(1–2 周)
- [ ] 实现 3.5 政策合规引擎;可选做 3.6 多智能体。
- [ ] 全量跑你选定的模型 + 全部域的 oracle 模式,产出最终数字,与 leaderboard 对比。
- **验收**:有完整的、可复现的、与官方同口径的对比结果 —— 这就是简历上的核心数据。

---

## 6. 实验设计(防止"自欺欺人",面试被问倒)

1. **固定对照**:所有对比实验用同一模型、同一温度(0.0)、同一 `num_runs`、同一数据集 split(用官方 public split 或固定 seed 采样),只改 agent 架构变量。
2. **消融实验**:从完整架构逐个移除模块(去 router / 去验证 / 去记忆),看每个模块的独立贡献 —— 这比只报一个总分可信得多,面试官大概率会问。
3. **分域看数**:平均分会被强域拉高,一定要按域报告(表格同 leaderboard),找出你的架构在哪些域增益最大、为什么。
4. **失败样本人工复盘**:每个阶段挑 20–30 个失败样本,人工归类写进文档 —— 这既是迭代依据,也是面试故事素材。
5. **诚实标注**:模型、split、concurrency、是否微调、是否用了 verifier 信息(这是 benchmark 设计上的灰色地带,verifier 是公开的,用它做执行中自查是合理的,但要在简历/文档里写明方法,别被问到时含糊)。

---

## 7. 简历包装与面试准备

### 7.1 项目一句话(中文/英文各一版)

> **中文**:在 ServiceNow 开源的 EnterpriseOps-Gym(1150 个企业任务、512 个 MCP 工具、SQL 终态评分)上,自主设计并实现了一套"工具路由 + 验证闭环 + 分层记忆 + 政策合规"的企业级 Agent 架构,将 xxx 域任务成功率从 A% 提升至 B%,平均 token 消耗下降 C%。(A/B/C 填你的真实数字)

> **English**:Designed and implemented an enterprise-grade LLM agent architecture (tool routing, verifier-in-the-loop self-checking, hierarchical memory, policy-as-code) on ServiceNow's EnterpriseOps-Gym benchmark (1,150 tasks, 512 MCP tools, SQL state-based grading), improving task success rate from A% to B% on [domain] with C% lower token cost.

### 7.2 要点写法(参考)
- **背景一句带过**:基准本身有分量,不用解释太多。
- **洞察必须前置**:写"我发现 512 个工具全量暴露 + 无终态验证是内置 agent 的根因" —— 这是你和"跑了个 benchmark"的人的区别。
- **方法给架构不给代码**:用上面第 3 节的六个方向命名,面试官一听就知道你有体系。
- **结果给对比不给裸分**:baseline vs 你的架构 vs leaderboard 同模型,三列对比,一眼看出增量。

### 7.3 面试高频问题准备
1. **"为什么选这个 benchmark?"** → 它评分终态、跑真实 MCP、有 leaderboard,能客观证明 agent 能力,且官方发布较新(2026),没有太多人做过,是差异化机会。
2. **"你的 agent 和官方内置的有什么区别?"** → 背熟第 1.4 节四个缺陷 + 你补上的四个模块,一个缺陷对应一个模块。
3. **"验证自查不会泄露答案吗?"** → 诚实回答:verifier 是公开的(benchmark 设计如此),我用它做执行中的状态确认,相当于"系统自检",不是拿预期答案喂给模型;并说明若做纯学术评估会区分。
4. **"结果能复现吗?"** → 有固定实验配置 + JSONL 日志 + 分域报告,直接演示。
5. **"真实企业环境和这个 gym 有什么区别,你学到了什么?"** → 预置答案:gym 验证了"验证闭环、工具治理、政策引擎"在模拟环境有效;真实环境还要考虑鉴权、数据隐私、审批流、模型路由成本 —— 体现工程视野。

### 7.4 避坑
- ❌ 不要说"超越 GPT-5"除非你真在同一 split 同口径跑过且写明模型版本。
- ❌ 不要只报一个平均分,会被追问到细节。
- ❌ 不要伪造工具调用/结果,面试官一问日志就穿帮。
- ✅ 把 `docs/agent_design_plan.md` 和失败分析文档放进仓库,面试可展示工程素养。

---

## 8. 代码接入速览(怎么把自研 agent 挂进 benchmark)

```python
# orchestrators/my_agent.py
from orchestrators.base import AgentOrchestrator

class MyEnterpriseAgent(AgentOrchestrator):
    async def execute(self) -> dict:
        # 1. tool routing: 从 self.available_tools 筛出 top-k
        # 2. planner: 生成 JSON 子任务 DAG
        # 3. for each subtask: ReAct loop + 写操作后 SQL 自查(可用 self.mcp_clients 直连)
        # 4. 终止前终态核对,不达标继续纠错
        # 5. 返回 {final_response, conversation_flow, tools_used, tool_results, messages}
        ...
```

```python
# evaluate.py 第 41-43 行,注册即可用 --orchestrator my_agent 运行
ORCHESTRATOR_MAP["my_agent"] = MyEnterpriseAgent
```

可用资源:基类已提供 `self.llm_client`(LLM)、`self.mcp_clients`(各域 MCP,可直接调工具/查 SQL)、
`self.tool_to_server_mapping`、`self.available_tools`、`self.config`(含 system_prompt/user_prompt);
`benchmark.verifier.VerifierEngine` 可直接复用做终态自查。

---

## 9. 参考资料

- 官方仓库:https://github.com/ServiceNow/EnterpriseOps-Gym
- 论文:https://arxiv.org/abs/2603.13594(Stateful Agentic Planning and Tool Use in Enterprise Settings)
- 数据集:https://huggingface.co/datasets/ServiceNow-AI/EnterpriseOps-Gym
- Leaderboard 口径:oracle 模式,任务通过 = 全部 verifier 通过(与本方案第 2 点"验证是一等公民"互为印证)
