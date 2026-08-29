# EnterpriseOps-Gym 项目交接文档(Handoff)

> 交接日期:2026-08-29 · 交接人:上一会话 · 接收人:新会话模型
> 阅读顺序:本文档 → `docs/agent_design_plan.md`(总方案)→ `docs/tool_router_design.md`(路由详细设计)→ 记忆日志 `.workbuddy/memory/`

---

## ⚡ 2026-08-29 执行记录(P0 完成情况,先读这里)

### 已完成
1. **git 基线建立**:仓库已初始化,两个 commit:
   - `9f3edb4` Baseline(统一前状态,含重复路由器)
   - `6f1ff39` Unify tool router(统一后,见下)
2. **路由器统一完成**(原 2.2 节的结构问题已解决):
   - **唯一实现 = `benchmark/tool_router.py`**:TF-IDF 粗筛(k_candidate=30)→ 可选 LLM 精排(注入 `llm_call_fn`,provider 无关)→ 两层兜底(置信度低→回退全量;LLM 失败/选中<5→回退 TF-IDF 子集)。
   - Tokenizer 修复:snake_case 拆分后**过滤停用词与 <3 长度 token**(否则 `post_to_feed` 靠 "to" 虚词冲进 rank 2)+ **保守复数词干化**(`entitlements→entitlement`)。
   - **只读工具地板分**(`LOOKUP_FLOOR=0.08`):任务文本只写实体不写 "find/list",约 1/4 必要查找工具得 0 分落榜;给 `find_/list_/get_/search_/retrieve_/check_` 前缀工具小地板分(通用启发式,不读 selected_tools,无泄露)。
   - `orchestrators/react_router.py` 已改 import 统一模块;`orchestrators/tool_router.py` 与 `scripts/eval_router_offline.py` 已删除(git rm)。
   - **唯一评估入口 = `eval_router.py`**:`--pool_mode domain|cross`、`--tools tools_dump.json`(真实池)。
3. **离线评估已跑通**(13 任务本地小样本,名字近似池):

   | 指标 | 统一前 | 统一后(tokenizer 修复+地板分) |
   |---|---|---|
   | micro recall@20 | 52.5% | **63.7%**(macro 67.6%) |
   | micro precision@20 | 33.2% | 40.3% |
   | itsm 单任务 | 100% full coverage | 100% |

   ⚠️ **数字口径**:池 = 域内 selected_tools 并集 + 7 个干扰工具,**工具只有名字没有真实 description**(近似池,双向失真)。跨域池(cross 模拟)recall 基本持平 63.7%。
4. **依赖未装成**:`uv sync --extra openai` 运行 23 分钟无任何产物(.venv 未创建),疑似网络/代理被墙,已终止。**离线评估不受影响**(路由器纯 stdlib)。待网络恢复后重试 `uv sync --extra openai`,完成后验证 `python -c "import evaluate; print(evaluate.ORCHESTRATOR_MAP)"`。

### 遇到的坑(重要)
- **F 盘路径异常再次出现**:`git rm` 两个文件时 `orchestrators/` 下其余 5 个文件从工作区消失(git 索引完好),用 `git checkout -- <files>` 恢复。**在 F 盘上批量文件操作后必须 `ls` 核对**。
- Docker Desktop 已装但 daemon 启动失败/被拒(静默退出),本次未能 dump 真实工具池。

### 剩余缺口与下一步(按序)
1. **【阻塞:需 LLM key】端到端对照**(P0-4 未做):`conf/llm/` 不存在。用户配好 key 后:
   ```bash
   unzip gym_dbs.zip   # 已解压,可跳过
   docker pull shivakrishnareddyma225/enterpriseops-gym-mcp-teams:latest
   docker run -d -p 8002:8002 shivakrishnareddyma225/enterpriseops-gym-mcp-teams:latest
   cp -r conf.example conf && vi conf/llm/my-model.json
   # baseline
   python evaluate.py --hf_dataset ServiceNow-AI/EnterpriseOps-Gym --domain teams --mode oracle \
       --llm_config conf/llm/my-model.json --orchestrator react --output_folder results/react/<model>/teams/oracle --num_runs 1
   # router 版(同模型同 split 同 concurrency,只改 orchestrator)
   python evaluate.py --hf_dataset ServiceNow-AI/EnterpriseOps-Gym --domain teams --mode oracle \
       --llm_config conf/llm/my-model.json --orchestrator react_router --router_top_k 20 \
       --output_folder results/react_router/<model>/teams/oracle --num_runs 1
   ```
2. **dump 真实工具池,重跑离线评估**(起 docker 后,MCP `tools/list` 结果存 `tools_dump.json`,然后 `python eval_router.py --tools tools_dump.json`):真实 description 下 recall 预期显著高于 63.7%(词面缺口会被描述补上),这是**口径修正**,旧数字别写简历。
3. recall 若仍 <90%:候选保留率不足 → 上调 `router_top_k` 或上 LLM 精排;漏检模式分析脚本在本会话历史(全池打分 + GT 标记)。
4. 之后进入 Phase 2(verifier-in-the-loop,见 agent_design_plan.md 3.2)。

---

## 0. 交接摘要(三句话)

1. **目标**:在 ServiceNow 开源的 EnterpriseOps-Gym 基准上自研一个"企业级"LLM Agent,用可复现实验证明其有效性,作为实习简历的核心项目。
2. **已完成**:两份设计文档(`docs/agent_design_plan.md` 总方案、`docs/tool_router_design.md` 路由设计)+ Phase 1 路由功能代码 + 两个离线评估脚本。
3. **当前卡点**:路由代码**已写好但从未运行验证**(无 results/logs 目录),且存在**两套重复实现**需要先统一 —— 这是新会话的第一优先级。

---

## 1. 项目背景(新模型必须知道的 5 个核心事实)

1. **评分看终态,不看路径**:任务跑在真实 MCP server 上,操作真实写入数据库,评分时用 SQL verifier 检查最终数据库状态。agent 不需要模仿人类操作步骤,只需要稳定达成正确终态。
2. **oracle 模式 = 答案泄露**:`benchmark/executor.py:305-320` 直接读任务配置的 `selected_tools` 字段(该任务真实所需工具的人工标注,CSM 平均 12.8 个)作为工具白名单。→ 工具路由器的学术目标 = **从任务描述预测这个子集,逼近 oracle**。
3. **域信息是免费的**:任务 JSON 的 `gym_servers_config[].mcp_server_name` 直接给出域,单域任务工具池只有 60–80 个(不是 512 个),路由先锁域。
4. **内置 agent 四大缺陷**(react / planner_react / decomposing 三个内置 orchestrator):全量工具直灌上下文、无终态验证(LLM 说停就停)、历史无压缩(89k 上下文稀释)、政策只写在 prompt 里。
5. **诚实性边界**:`selected_tools` **只能用于离线评估路由质量**;执行时路由器只输入 `user_prompt`/`system_prompt`,禁止读 `selected_tools` —— 否则是答案泄露,面试一问就穿帮。

**基准数据**:1150 任务 / 8 域 / 512 工具 / 平均 9.15 步 / 89k 上下文;最强模型 Claude Opus 4.6 平均成功率仅 45.9%,开源最佳 DeepSeek-V3.2 24.2%。有大量提升空间。

---

## 2. 当前代码状态(务必先读这里)

### 2.1 已实现文件(均为 8/27 新增或修改)

| 文件 | 内容 | 状态 |
|------|------|------|
| `benchmark/tool_router.py` | **TF-IDF 版路由器**(类 API:`ToolRouter`/`TFIDFIndex`/`expand_tool`,带 ⑤置信度回退,纯 stdlib+math,零依赖) | 17:25 最后修改,⚠️ 目前**无 orchestrator 使用** |
| `orchestrators/tool_router.py` | **关键词重叠 + LLM 版路由器**(函数 API:`route`/`route_keywords`/`route_llm`/`RouteResult`,LLM 失败自动降级关键词,含 STOPWORDS 与 ROUTER_PROMPT) | 16:28,`react_router` 正在使用 |
| `orchestrators/react_router.py` | **路由 ReAct orchestrator**:路由 → ReAct 循环 + ④渐进式发现(子集外工具按需加载)+ 路由元数据进 `get_result_metadata()` | 已注册进 `evaluate.py` 的 `ORCHESTRATOR_MAP`(`--orchestrator react_router` 可用),支持 `router_top_k` / `router_llm_client` / `enable_discovery` 参数 |
| `scripts/eval_router_offline.py` | 离线评估脚本(import `orchestrators.tool_router.route_keywords`) | 未运行 |
| `eval_router.py`(根目录) | 离线评估脚本(import `benchmark.tool_router.ToolRouter`,TF-IDF 版,含域工具池近似 + 噪声工具注入) | 未运行 |
| `benchmark/executor.py` | 16:27 修改:新增 `router_llm_config` → `router_llm_client` 初始化(135-180 行),供 react_router 的 LLM 路由使用 | 已改 |

### 2.2 ⚠️ 结构问题(新会话第一优先级)

1. **两套路由实现重复**:`benchmark/tool_router.py`(TF-IDF,类 API,17:25 最新)vs `orchestrators/tool_router.py`(关键词/LLM,函数 API)。**需要二选一统一**,推荐保留 `benchmark/tool_router.py`(TF-IDF 语义更合理、带置信度回退),把 `orchestrators/tool_router.py` 的 LLM 路由能力(route_llm + 降级)合并进去。
2. **两个评估脚本各用各的路由器**:`eval_router.py` 用 TF-IDF 版,`scripts/eval_router_offline.py` 用关键词版 → 数字不可直接对比。统一后只留一个。
3. **`react_router.py` 目前用的是关键词版**:统一实现后需同步修改其 import。
4. **项目不是 git 仓库**(`git status` 报 fatal):动手前先 `git init` 并提交当前状态,否则所有探索不可回滚。

### 2.3 验证状态

- ❌ **从未运行**:无 `results/`、无 `logs/` 目录,两个评估脚本和端到端实验都没跑过。
- 无运行结果、无失败样本、无 token 数据 —— 简历数字还完全空缺。

---

## 3. 环境与运行方式

- Python 3.11+(项目用 uv 管理:`uv sync --extra <provider>`)。
- 基础环境(README 要求):`unzip gym_dbs.zip`;docker 起各域 MCP server(`shivakrishnareddyma225/enterpriseops-gym-mcp-<domain>:latest`,端口 8001-8009);LLM key 配置在 `conf/llm/<name>.json`。
- **无需额外下载模型**:路由功能(TF-IDF)纯 stdlib,零第三方依赖;LLM 精排复用现有 key;只有 V2 embedding 对比才需要下载 embedding 模型。
- 离线评估(不需 docker / 不需 LLM key):
  ```bash
  python eval_router.py --data_dir data/revised --top_k 20
  python scripts/eval_router_offline.py --data_dir data/revised --top_k 20
  ```
- 端到端(需 docker + LLM key):
  ```bash
  python evaluate.py --hf_dataset ServiceNow-AI/EnterpriseOps-Gym --domain teams --mode oracle \
      --llm_config conf/llm/<model>.json --orchestrator react_router \
      --router_top_k 20 --output_folder results/react_router/<model>/teams/oracle --num_runs 1
  ```
- 注意:本地 `data/revised/` 只有 csm(12 任务)+ itsm(1 任务),小样本只够验证功能;出简历数字需用 `--hf_dataset` 全量。

---

## 4. 下一步计划(按优先级)

### P0 — 收尾 Phase 1(1-2 天)
1. `git init` 并提交当前状态。
2. **统一路由器实现**(推荐:保留 `benchmark/tool_router.py` 的 TF-IDF,合并 LLM 路由 + 降级逻辑),删掉重复文件,同步改 `react_router.py` 的 import。
3. 跑通离线评估,拿到 **recall@k / precision@k 初值**(目标 recall@20 ≥ 90%)。当前工具池是"该域 selected_tools 并集"的近似,数字偏乐观 —— 文档里要注明;要真实数字需连 MCP dump 全量工具池(见 `docs/tool_router_design.md` 4.4)。
4. 跑一次端到端对照:`react` vs `react_router`,建议 teams 或 email 域(成功率相对高),oracle 模式,同模型同 split。记录成功率 + **token 消耗对比**(成本下降是 Phase 1 的另一卖点)。

### P1 — 评估体系(可与 P0 并行)
5. 建失败样本收集管道(JSONL 日志 → 失败分类器:tool_selection / arg_error / policy_violation / premature_stop / context_loss)。
6. 消融矩阵:react / +域锁定 / +粗筛 / +精排 / +渐进式扩展(见 `docs/tool_router_design.md` 第 5 节)。

### P2 — 下一模块(Phase 2+)
7. 验证驱动的自纠正(verifier-in-the-loop):把 `benchmark/verifier.py` 的 SQL 检查搬进执行循环。
8. 分层记忆(working/episodic/semantic)。
9. 政策合规引擎(policy-as-code)。
10. 动态计划(JSON 子任务 DAG + 失败 replan)。
> 详细设计都在 `docs/agent_design_plan.md` 第 3-6 节,按 ROI 排序。

---

## 5. 约束与红线(不可违反)

1. **诚实性**:`selected_tools` 只用于离线评估,执行时禁止读取;实验报告必须写明模型/split/concurrency/是否用 verifier 信息。
2. **同口径对比**:所有对照实验同一模型、temperature=0、同一 split、同一 concurrency,只改 agent 架构变量。
3. **不删除他人代码**:统一实现时用 git 提交后操作,别直接 rm。
4. **文件路径**:项目在 `F:\Project\EnterpriseOps-Gym-main`(Windows,Git Bash 下用 `/f/Project/...` 更稳)。注意该盘曾出现过路径访问异常,写入后用 `ls` 确认落盘。

---

## 6. 文档索引

| 文档 | 内容 |
|------|------|
| `docs/agent_design_plan.md` | 总方案:benchmark 解读、四大缺陷、六个设计方向、4 阶段路线图、简历包装与面试问答 |
| `docs/tool_router_design.md` | 路由层详细设计:三级漏斗、两层兜底、代码骨架、评估与消融矩阵 |
| `.workbuddy/memory/2026-08-27.md` | 前两轮工作日志(设计决策与关键发现) |
| `README.md` | 官方文档:安装、运行、leaderboard |
| 论文 arXiv 2603.13594 / HF 数据集 `ServiceNow-AI/EnterpriseOps-Gym` | 官方资料 |
