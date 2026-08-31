<div align="center">

<h1>Enterprise Agent Control Plane</h1>

<p><i>把研究级 agent 变成能在真实环境部署的企业级 agent —— 以 EnterpriseOps-Gym 为基准，构建分层控制平面。</i></p>

<p>
  <a href="https://arxiv.org/abs/2603.13594"><img src="https://img.shields.io/badge/paper-arXiv%202603.13594-blue?logo=arxiv" /></a>
  <a href="https://huggingface.co/datasets/ServiceNow-AI/EnterpriseOps-Gym"><img src="https://img.shields.io/badge/benchmark-EnterpriseOps--Gym-yellow" /></a>
  <a href="https://github.com/ServiceNow/EnterpriseOps-Gym"><img src="https://img.shields.io/badge/upstream-ServiceNow%2FEnterpriseOps--Gym-green" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202-blue" /></a>
</p>

</div>

---

## 这是什么

Agent 领域不缺能跑通 toy task 的 demo，缺的是**能部署到真实企业环境的 agent**。两者的分水岭不在单步工具调用，而在控制平面：

- 研究级 agent：一次性的 ReAct 循环，工具集固定，失败靠重试，无状态无审计，成本不可控。
- 企业级 agent：**意图识别 → 结构化规划 → 受控执行 → 状态验证**的闭环，配齐记忆、政策合规、可观测性、成本治理四根横向支柱。

本仓库以 **EnterpriseOps-Gym**（1,150 个专家任务、512 个工具、8 个企业域、SQL 状态验证器）为训练与评测基准，从零实现并持续演进一个企业级 agent 控制平面。

## 目标架构

```
                         ┌─────────────────────────────────────────┐
  用户请求 ──▶ 入口层      │  意图识别 · 域路由 · 任务分类            │
                         └───────────────┬─────────────────────────┘
                                         ▼
                         ┌─────────────────────────────────────────┐
                         │  规划层 结构化计划 · 动态重规划 · 子目标分解 │
                         └───────────────┬─────────────────────────┘
                                         ▼
                         ┌─────────────────────────────────────────┐
                         │  执行层 ReAct 内核 · 工具路由 · 幂等重试    │
                         └───────────────┬─────────────────────────┘
                                         ▼
                         ┌─────────────────────────────────────────┐
                         │  验证层 verifier-in-the-loop · 终态自查    │
                         └─────────────────────────────────────────┘
     ─────────────────────────────────────────────────────────────────
     横向支撑：分层记忆 │ 政策合规引擎 │ 可观测性(全链路审计) │ 成本控制
     ─────────────────────────────────────────────────────────────────
```

**设计理念：control plane 与 data plane 分离。** ReAct 循环只负责"干活"（data plane），意图路由、规划、验证、治理全部由上层控制平面接管——这是从云原生基础设施（Kubernetes / etcd 的 control plane 模式）借鉴来的架构语言。

## 当前实现状态（如实标注）

| 模块 | 状态 | 说明 |
|---|---|---|
| 工具路由 `benchmark/tool_router.py` | ✅ 已实现 | TF-IDF 漏斗 + 可选 LLM 精排，路由质量可离线评测 |
| 离线路由评测 `eval_router.py` | ✅ 已实现 | 零 LLM 成本，用任务自带 `selected_tools` 标注评估 `recall@k` / `precision@k` |
| 编排器 ×4 `orchestrators/` | ✅ 已实现 | `react` / `planner_react` / `decomposing` / `react_router` |
| 执行器与验证器 `benchmark/executor.py` `benchmark/verifier.py` | ✅ 已实现 | SQL 终态验证，非动作序列验证 |
| 离线评测闭环 `evaluate.py` + `compute_score.py` | ✅ 已实现 | 支持断点续跑、失败自动重试、多 run 统计 |
| 分层记忆 | 🚧 设计中 | 短期会话记忆已进规划层，长期记忆未落地 |
| 政策合规引擎 | 🚧 设计中 | 策略即代码 + 执行前检查钩子 |
| 可观测性 | 🟡 部分 | 全链路 tool 调用日志已有，trace 聚合未落地 |
| 成本控制 | 🚧 设计中 | 路由层已考虑小模型优先，细粒度配额未落地 |

> 诚实声明：架构蓝图覆盖四层四支柱，但**记忆 / 政策 / 可观测性 / 成本治理尚在设计中**。本仓库的可运行部分（路由 + 编排 + 验证 + 评测闭环）均已实现并可通过下方流程复现结果。

## 快速开始

### 1. 环境准备

```bash
# 依赖（Python 3.11+，按需选 provider）
uv sync --extra deepseek    # 或 --extra openai / anthropic / all

# 数据库快照
unzip gym_dbs.zip

# LLM 配置（API key 放在 conf/llm/*.local.json，已被 gitignore）
cp -r conf.example/ conf/
```

### 2. 启动域 MCP 服务器（Docker）

```bash
docker run -d -p 8001:8001 shivakrishnareddyma225/enterpriseops-gym-mcp-csm:latest
# 各域默认端口见 upstream README：teams 8002 / calendar 8003 / email 8004 / itsm 8006 / hr 8008 / drive 8009
```

### 3. 跑评测

```bash
# 单个域（从 HuggingFace 拉任务配置）
python evaluate.py \
    --hf_dataset ServiceNow-AI/EnterpriseOps-Gym \
    --domain teams --mode oracle \
    --llm_config conf/llm/my-model.local.json \
    --output_folder results/react/my-model/teams/oracle \
    --orchestrator react \
    --concurrency 4 --num_runs 1

# 离线工具路由评测（零 LLM 成本，迭代期首选）
python eval_router.py --data_dir data/revised --top_k 20

# 汇总通过率（论文口径：任务全 verifier 通过 = success）
python compute_score.py --results_folder results/react/my-model/teams
```

支持 `--orchestrator planner_react / decomposing / react_router`，以及 Ray 分布式编排 `ray_experiment_queue.py`。

## 低预算验证方法

LLM provider 成本敏感时的推荐评测节奏（详见 `docs/HANDOFF.md`）：

1. **第 0 层**：`eval_router.py` 离线路由评测，不烧 token；
2. **第 1 层**：单域固定子集（20~30 任务，oracle 模式，`--num_runs 1`）做日常迭代，`evaluate.py` 自带断点续跑，中断不浪费；
3. **第 2 层**：跨域小样本回归；
4. **第 3 层**：全量 + `--num_runs 3`，只在最终报告时跑一次。

## 目录结构

```
├── benchmark/            # 执行器 / MCP 客户端 / LLM 客户端 / 验证器 / 工具路由器
├── orchestrators/        # react / planner_react / decomposing / react_router
├── eval_router.py        # 离线路由质量评测（零成本）
├── evaluate.py           # 评测执行器（断点续跑 · 失败重试）
├── compute_score.py      # 通过率汇总（论文口径）
├── ray_experiment_queue.py  # Ray 分布式批量评测
├── data/revised/         # 本地任务样本（csm / itsm）
├── docs/                 # 设计文档与迭代记录
└── Domain Wise DBs and Task-DB Mappings/  # SQL 快照（从 gym_dbs.zip 恢复）
```

## 评测基线对照

任务通过判定与上游一致：**仅当全部 SQL 验证条件满足才算成功**（非动作序列匹配）。各模型在上游全量基准的成绩见 [EnterpriseOps-Gym 论文](https://arxiv.org/abs/2603.13594) 与 [upstream README](https://github.com/ServiceNow/EnterpriseOps-Gym#leaderboard)——运行 `compute_score.py` 即可按相同口径得到本 agent 的结果并直接对比。

## 致谢与许可

- 基准环境、任务与 MCP 服务器来自 [ServiceNow/EnterpriseOps-Gym](https://github.com/ServiceNow/EnterpriseOps-Gym)（Apache 2.0），本仓库在其基础上演进，工具路由层与评测工作流为本仓库增量。
- 本仓库以 [Apache License 2.0](LICENSE) 发布。
