# 跑完流程的失败案例：根因分析

- 分析日期：2026-09-14
- 数据来源（本地 `out/`，均为已完成全部流程、给出 final 回答、但被 verifier 判定失败的 run）

| 域 | run | 任务数 | 通过 | 跑完但失败 | 超时/报错 |
|---|---|---|---|---|---|
| email | `out/email_vloop/run_1` | 67 | 41 | 23 | 3 |
| email | `out/email_vloop/run_2` | 67 | 43 | 19 | 5 |
| email | `out/full_exec_email/run_1` | 67 | 36 | 29 | 2 |
| hr | `out/meta_hybrid/run_1` | 102 | 31 | 69 | 2 |
| hr | `out/full_tfidf/run_1` | 102 | 24 | 74 | 4 |

- 分析脚本（本次新增，可复跑）
  - `scripts/extract_failure_traces.py` 单卷失败案例的紧凑 trace 抽取
  - `scripts/digest_stable_failures.py` 跨卷稳定失败案例的合并摘要
  - `scripts/classify_stable_failures.py` 按失败机制特征分类
  - `scripts/quantify_failure_mechanisms.py` 机制规模量化

---

## 0. 结论摘要

抽查层面最要紧的一条。**框架层把 MCP 的工具执行错误（`isError: true`）折叠成了 `success: true`**，位置在 `benchmark/mcp_client.py:334-342`。历史卷离线重放确认，5 个主卷里共 **675 次**调用处于这种状态；把范围放宽到本地全部 7 个可重放卷是 **780 次**，其中 **361 次是只读工具调用**，也就是被 verify 门禁误当成"有效回读证据"的那一批。单卷最多 **67/102 任务**受影响。这意味着环境反馈在约一成到两成的调用上是错的，agent 拿到的"调用成功"信号并不可信。

> **状态：已修复（09-14）。** 详见 §7 P0-1。修复后重放，780/780 条内层错误全部正确映射为 `success=False`；成功调用 9391 条无误伤。`isError-masked` 归零。

失败机制不是一种，而是七种，并且主次分明。

| 编号 | 机制 | 跨卷稳定失败中的规模 | 性质 |
|---|---|---|---|
| M1 | 工具执行错误被静默（isError → success） | 675 次调用 / 5 卷（全量 780 / 7 卷） | 框架 bug，**已修复** |
| M2 | 多目标题只完成前半，或只侦察就收工 | email 1/15、hr 6/61 完全零写入；大量后半段遗漏 | 任务分解与覆盖 |
| M3 | 字面值、枚举、格式知识缺口 | 失败验证器要求的业务字面值约 50% 从未被产出 | 领域知识 |
| M4 | 实体作用域与 ID 解析错误 | 具体案例：`me` 与 `bob@company.com` 混用、`user_id` 与 `hr_profile_id` 混用 | 标识符解析 |
| M5 | 复杂载荷结构靠猜 | email draft/message 的 `payload`/`raw` 反复变形 | 接口契约 |
| M6 | 破坏性抖动，终态错误 | 失败案例中 16/23、14/19、13/29、7/69、21/74 出现同一写工具 ≥3 次 | 收敛控制 |
| M7 | 援引政策拒绝执行 | hr 域 8/69、10/74 | 提示与任务冲突 |
| M0 | 包装工具名退化 `_execute_ttool` 等 | 66 次调用，单卷 25/67 任务触达 | dispatch=exec 专属，**已修复** |

主导失败形态是 **MISSING_STATE**，也就是"该落地的副作用没落地"。email 稳定失败里 22 个失败验证器有 17 个属此类（77%），hr 是 126 个里的 121 个（96%）。

还有一个反直觉但重要的发现。email 卷 `email_vloop/run_1` 里，**31 条判失败的要求中有 25 条（81%）本来就写在 agent 自己生成的验收清单里**。它列出来了，然后用自己的产物"核对"通过，宣布完成。问题不在清单覆盖，在于自证的可信度。（该项为保守关键词匹配，实际覆盖只会更高。）

---

## 1. 口径与方法

### 1.1 什么算"跑完流程的失败案例"

三条同时满足：

1. `runs[0].model_response` 非空，也就是 agent 给出了 final 回答；
2. `runs[0].overall_success == False`；
3. 回答中不含 `upstream connect error`、`reset reason` 等上游中断文本。

超时与上游报错单独计数，不进分析集。这样切分的原因是，超时属于基础设施噪声，混进来会污染机制判断。

### 1.2 为什么要跨卷取交集

单卷失败里混着运行期随机性。例如 email 67 个任务在三卷中的模式分布是：

```
OK/OK/OK        33
FAIL/FAIL/FAIL  15   <- 稳定失败，机制性
OK/OK/FAIL       7
FAIL/FAIL/OK     2
FAIL/OK/FAIL     2
（其余为含 ERR 的混合） 7
```

因此本报告的主分析集取 **跨卷全败** 的样本，email 15 个（三卷交集），hr 61 个（两卷交集）。这批指标复现性最好，能代表机制而不是噪声。单卷指标另作参照。

### 1.3 verifier 失败类型的归并规则

从 `verification_results` 里每条失败的 `comparison_type` 与 `expected`/`actual` 推算：

| 归并类型 | 判据 | 含义 |
|---|---|---|
| MISSING_STATE | `equals` 且 `actual < expected`；或 `greater_than` 且 `actual = 0` | 应存在的实体或事件没有出现 |
| EXTRA_STATE | `equals` 且 `actual > expected` | 多造了实体 |
| NOT_INCREASED | `greater_than` 且 `0 < actual <= expected` | 基线是期望值，实际没增长，即增量操作没发生 |
| VALUE_MISMATCH | 期望值是字符串且不匹配 | 字段取值错 |

关于 `greater_than` 要先说清语义，这里容易看反。它的 `expected` 字段存的是**基线计数**（绝大多数为 0），通过条件是 `actual >= 1`。所以 `expected=0, actual=0` 表示"要求存在的实体不存在"，归 MISSING_STATE；只有 `expected` 本身大于 0 时才是增量校验。

---

## 2. 失败面分布

### 2.1 稳定失败集的失败验证器构成

```
email（15 任务 / 22 个失败验证器）
  MISSING_STATE    17   (77%)
  VALUE_MISMATCH    2   (9%)
  NOT_INCREASED     2   (9%)
  EXTRA_STATE       1   (5%)

hr（61 任务 / 126 个失败验证器）
  MISSING_STATE   121   (96%)
  OTHER             4   (3%)
  EXTRA_STATE       1   (1%)
```

两域一致指向同一个结论，绝大多数失败是"要存在的实体不存在"，不是"存在但值微差"。hr 域这一比例高达 96%，意味着几乎不存在"做对了大半、差一点"的情形，要么实体在，要么整个动作没发生。

### 2.2 每个任务的失败验证器条数

```
email: 1 个 -> 9 任务 | 2 个 -> 5 | 3 个 -> 1
hr:    1 个 -> 30    | 2 个 -> 16 | 3 个 -> 7 | 4 个 -> 3 | 5~8 个 -> 6
```

hr 有 5 个任务一次挂 5 条以上验证器（5 条 2 个、6/7/8 条各 1 个），这些全部是长链条任务（5 到 8 个子目标），典型形态是在前半段就停下了。

### 2.3 写入调用占比

稳定失败案例的业务工具调用里，写操作占比均值只有 email 0.377、hr 0.391。也就是说超过六成的动作花在读取上。读本身不是问题，但结合 §3 的 M2 可以看出，一部分任务的读取之后没有对应的写入。

---

## 3. 失败机制逐条拆解

### M1 工具执行错误被静默为成功（框架级）

**现象。** MCP 协议把工具自身的失败（参数校验不通过、业务逻辑拒绝、资源不存在）放在 `result.isError = true` 里返回，HTTP 层依然是 200。客户端取到 200 就判定成功。

**代码位置。** `benchmark/mcp_client.py:334-342`

```python
result = await self._send_request("tools/call", params, extra_headers)
if result.get("success"):
    data = result.get("data", {})
    return {
        "success": True,          # <- 没有看 data["result"]["isError"]
        "result": data.get("result"),
        "error": data.get("error"),
    }
```

**实证。** 统计 `result.result.isError is True` 而外层 `success is True` 的调用：

| run | 此类调用次数 | 受影响任务数 |
|---|---|---|
| `email_vloop/run_1` | 135 | 39 / 67 |
| `email_vloop/run_2` | 155 | 43 / 67 |
| `full_exec_email/run_1` | 113 | 39 / 67 |
| `meta_hybrid/run_1` | 89 | 41 / 102 |
| `full_tfidf/run_1` | 183 | 67 / 102 |

具体样本，任务 `20260106_071510_179`（建模器标注 create_filter）：

```
create_filter -> {"success": true,
                  "result": {"content":[{"text":"❌ Invalid Tool Arguments:
                             ['criteria.to: is required', 'criteria.subject: is required', ...]"}],
                             "isError": true}}
```

外层 `success: true`，内层明确 `isError: true`。

**它为什么会致败。** `success` 在编排层有三个消费点（下列行号为**修复前**的代码位置）：

- `orchestrators/meta_tool_router.py:638-645` `_vl_note_real_tool(real_name, success)`。函数内部写的是"`success` 为假就跳过计数"，但**两个调用点都把 `success` 硬编码成了 `True`**（原代码 `self._vl_note_real_tool(real_name, True)`），所以这个分支从来没有生效过。折叠之后，**一次失败的只读调用会被算作一次有效回读**，门禁因此可以在没有真实证据的情况下放行。离线重放量到这类"失败只读调用"共 **361 次**。
- `orchestrators/meta_tool_router.py:969` 与 `1001` 记忆事实抽取的入口判断。失败调用的"成功"参数同样被写进事实抽取。
- `orchestrators/meta_tool_router.py:1039` 兜底分支，以及异常路径里对 `tools_used` 的登记口径。

对比之下，`orchestrators/episodic_memory.py:112-115` 已经显式守了 `isError is True` 就直接返回 `None`。记忆层在 Phase 3 就把这个坑补上了，主循环没补上，两边口径不一致。

**修法（一行级别）。**

```python
result = await self._send_request("tools/call", params, extra_headers)
if result.get("success"):
    data = result.get("data", {})
    inner = data.get("result")
    is_err = isinstance(inner, dict) and inner.get("isError") is True
    return {
        "success": not is_err,
        "result": inner,
        "error": data.get("error"),
        "isError": is_err,
    }
```

MCP 规范对此有明确表述，工具执行错误必须放在 `isError` 里返回而不是抛协议错误，理由是要让模型看得见并能自我修正。客户端把 `isError` 抹平，等于取消了这条自救通道。

---

### M2 多目标题只完成前半，或只侦察就收工

**现象一，零写入。** 稳定失败里完全没有写操作调用的任务，email 1 个、hr 6 个。最典型的是 `20251212_194942_087`（Maria Garcia 合规离职，6 个子目标）：

```
共 4 条 AI 消息、9 次调用，全部是读
get_user_using_name > find_group_by_name > list_group_members > get_users_hr_profile
6 条验证器全部失败
```

同类还有 `20251216_123530_079`（David Kim 工资单路由），4 条 AI 消息、10 次调用，清一色读工具，5 条验证器全败。

**现象二，后半段消失。** 有写入但不覆盖全部子目标。例如 email `20260106_071510_179`：

```
要求：建 label "Manager Important" → 找出 manager@company.com 的历史邮件并打上该 label
      → 建 filter 让后续邮件自动打 label

实际：create_label 成功
      list_messages 查了 from:manager@company.com
      从未调用 modify_message / batch_modify  ->  label 没有打到任何邮件
      create_filter 连续失败 4 次不同形态
验证器：ManagerLabelCreatedAndApplied exp 1 act 0
        ManagerEmailFilterCreated      exp 1 act 0
```

"给历史邮件打标签"这一步唯一对应 `modify_message`，整个 run 里没有出现过。

**现象三，把"看清楚"当成"做完了"。** M2 与 M1 叠加时会放大。agent 侦察完，认为信息齐备，直接给结论。

**共性。** 这类任务的自然语言指令是**并列的子目标列表**，模型对第一个子目标的执行很完整，越往后越容易掉。挂 5 条以上验证器的 6 个任务全部属于这一形态。

---

### M3 字面值、枚举、格式知识缺口

这条直接回答"模型遗漏了什么关键知识"。

**度量。** 从每条失败验证器的 SQL 里抽出业务字面值（滤掉 SQL 片段、内部 `user_00X`、UUID），检查它是否在 agent 的任何工具调用参数里原样出现过。

| run | 原样出现过 | 比例 |
|---|---|---|
| `meta_hybrid/run_1` | 218 / 453 | 48% |
| `full_tfidf/run_1` | 243 / 480 | 51% |
| `email_vloop/run_1` | 37 / 67 | 55% |

约一半被要求的值，agent 从头到尾没有产出过。以下是从未产出的高频项，按类别看规律非常整齐：

**（a）枚举词汇。** `critical`、`moderate`、`ready`、`work_in_progress`、`awaiting_approval`。这些是 hr 库的 priority / status 取值。agent 要么没设这个字段，要么用了别的词。验证器 `20251216_123530_079` 要求 `status = 'draft'`、`source = 'virtual-agent'`，`20251217_073807_153` 要求通知 `type = 'reminder'` 且 `status = 'draft'`，都属于这一类。

**（b）既有目录名。** `Workforce Administration`、`Total Rewards`。前者是 COE 名称，出现在多条验证器里（`20251212_114043_514`、`20251218_234518_453`）。agent 建模板时用的 COE 名与库里既有值不一致，表里查不到。

**（c）取值格式。** `-5 minutes`（时长格式）、`TASK-0001`（任务编号规则）、`%Eric Rodriguez%`（姓名匹配）、`<p>A.J.<br>Project Lead</p>`（HTML 签名）、`#7a2e0b` 等色值。email 侧 4 次 `$.backgroundColor` 说明 label 颜色的 JSON 结构也没对上。

**（d）必填字段义务。** 更麻烦的一类。`create_filter` 的 schema 把 `to`/`subject`/`query`/`negatedQuery` 全部标成 required，agent 依次试探：

```
{from: manager@company.com}                     -> criteria.to: is required
{... to:"", subject:"", query:"", negatedQuery:""} -> required field cannot be an empty string
{... to:null, subject:null, ...}                 -> expected type 'string', got 'null'
```

非空字符串是硬约束，工具描述里没有说，agent 三次都没摸到，之后开始用空格和一条不存在的 `negatedQuery` 硬凑，最终 filter 被删掉，终态为无。

---

### M4 实体作用域与 ID 解析错误

**案例 A，`me` 与显式地址混用。** `20260105_122123_327`：

```
读操作一律 userId = "me"
写操作一律 userId = "bob@company.com"
create_label(Executive) / create_label(Product News) 都返回 success

验证器：SELECT ... FROM labels WHERE user_id = 'user_002' ...  -> exp 1 act 0
```

读写用了两个不同的用户标识。更值得注意的是它的自证方式。agent 随后用**同一个 `bob@company.com`** 回读 `list_labels`，看到两个 label 都在，于是确认完成。**用同一个错误参数做回读，回读再成功也不构成验证。**这是 §4 里"自证不可信"的一个干净样本。

**案例 B，ID 类型混用。** hr 域验证器反复出现 `users = '[6]'`、`based_on_skills = '[20]'`、`opened_for = <hr_profile_id>` 这类关系字段。`20251217_211723_514` 里 agent 建 assignment rule 时给了 `user_group: 4`，**没有给 `users`**，规则因此没有路由到 Stephanie Todd（user 6），而它上一步刚刚成功调用 `add_new_skill_to_user(skill=20, user=6)`，说明它是知道这个 ID 的。知道但没落到该落的字段上。

**案例 C，名字照抄 vs 改写。** 同一个任务的技能名，验证器要 `US Payroll Bank Change`，agent 建的是 `US Payroll Bank Account Change`。名称与 ID 一起构成定位键，改写一处整条验证链就断。这一条同时暴露了任务描述本身的欠定义，见 §5。

---

### M5 复杂载荷结构靠猜

集中在 email 域，因为 Gmail 系的 draft 与 message 都有 `payload`（结构化）和 `raw`（base64）两条路径，且能带附件与多段 body。

**`20251203_104951_949`** 要求更新一封已存在的草稿。`update_draft` 被调用 5 次，每次换一种形态：

```
{message:{payload:{mimeType,headers}}}                      -> 不匹配
{message:{payload:{partId:"0",mimeType,headers}}}           -> 不匹配
{message:{raw:"U3ViamVjdD..."}}                             -> 不匹配
```

验证器查的是 `message_part_bodies` 里的正文，5 次都没写进去。`exp 1 act 0`。

**`20251204_101148_724`** 要求发信并打标签。`send_message` 返回了消息 ID，看起来成功：

```
send_message -> {"id":"cf1bd2fd-...", "snippet": "", ...}
```

`snippet` 为空是关键线索。snippet 由主题与正文派生，为空说明解析器没在消息里找到可索引的头与正文。随后 agent 自己也发现这封信搜不到，连续 `list_messages` 十余次都没找回来，也始终没有去修 `raw` 的构造。验证器 `mh.value = 'carol@university.edu'` 与 `m.snippet = 'My university trip'` 双双落空。

**`20251203_093009_456`** 更极端，`create_draft` 被调用 7 次以上，`payload.filename` 从缺省一路试到 `""` 再到 `"message.txt"`，验证器要 3 条匹配记录，最终只有 2 条。

**共性。** 这类失败的形态是"同一个写工具被反复调用，参数在猜"。它不是选错工具，是不知道工具的载荷契约长什么样，靠试错逼近，而试错预算有限。

---

### M6 破坏性抖动，终态错误

失败案例中同一写工具被调用 3 次及以上的比例：

```
email_vloop/run_1  16/23      email_vloop/run_2  14/19
full_exec_email    13/29      meta_hybrid         7/69      full_tfidf  21/74
```

最干净的样本仍然是 `20260106_071510_179` 的 filter 序列：

```
create_filter -> get_filter -> delete_filter -> create_filter -> get_filter
-> delete_filter -> create_filter(带 negatedQuery: "from:thisaddressdoesnotexist@...")
-> get_filter -> list_filters
```

agent 每次读回发现不对，就删掉重建，第三次为了同时满足"from 必须是 manager@company.com"和"非空字段必须有值"两条约束，塞了一条自造的 `negatedQuery`。终态是这套 filter 没有一条满足验证器要求。

这里要区分"重试"和"抖动"。重试是同一目标换参数，抖动是**先销毁再重建**，中间的删除步骤让状态在时间上出现空窗，最后一次失败就等于前功尽弃。

---

### M7 援引政策拒绝执行

hr 域的系统提示里注入了一份《HR Management Assistant Policy》，其中写明：

> Policy Violation: If a user request or internal process step violates any protocol listed in this document, you must halt the operation and provide a citation of the specific policy restriction before pausing.

并且给出了角色权限表。`Administrator` 才能 create HR Services / Workflows / Assignment Rules / Groups；`HR Specialist (Agent Role)` 只能管理 HR Case、Case Work Note、Fulfillment Task。

**`20251212_194942_087` 的 final 回答：**

```
Step 1 - Create HR Service "Employee Offboarding":
- Per policy Section 2, only an Administrator can create HR Services.
  My role is HR Specialist (Agent Role), which cannot create services.
- Additionally ... "lifecycle event" is not a valid value ...
- POLICY VIOLATION - I cannot create this service.
```

**而任务书第 1 条要求的正是**"Create a Critical Priority HR Case for Maria ... under a new service as - Employee Offboarding which is a lifecycle event"。验证器 `Verify HR Service Creation: SELECT count(*) FROM hr_service WHERE service_name = 'Employee Offboarding'` 要求这个服务必须存在。

**规模。** final 回答含政策拒绝措辞的：

```
meta_hybrid/run_1  FAIL 8/69（另 1 条 OK 属误命中）
full_tfidf/run_1   FAIL 10/74
email_vloop/run_1  FAIL 1/23
```

约占 hr 失败量的一成。要说明的是，这**不是**模型的单方面错误。模型按提示词的字面要求执行了 halt，与任务的期望直接冲突。这属于基准设计层面需要对齐的缺口，修法在 §7 里单列。

---

### M0 包装工具名退化（dispatch=exec 专属）

**现象。** email 三个 run 的模型输出里反复出现 `_execute_tool` 的损坏变体：

```
_execute_ttool    14 次（run_1）/ 13（run_2）/ 15（full_exec）
_execute_toke      9 / 5 / 3
_execute_trap, _execute_tort, _execute_trip, _execute_tort,
_execute_t tool, _execute_toke_tool   各 1~2 次
```

合计 66 次。返回一律是：

```
Tool '_execute_ttool' does not exist in the tool pool. Use _tool_search to discover the right tool.
```

**受影响面。** 单卷触达任务数 25/67、21/67、19/67，即三成上下的任务至少踩一次。

**为什么会发生。** 只有 email 三个 run 是这个形态，hr 两个 run 里同类调用为 0。原因在 dispatch 模式：email 三个 run 的 `meta_tool_dispatch = exec`，业务工具全部经由 `_execute_tool({name, args})` 这一层包装调用；hr 两个 run 的 `dispatch = None`，工具直接绑定，模型直接调 `create_new_hr_case` 之类，不经过包装名。所以这是**包装层引入的新失败面**，不是模型本身的能力问题。形态一律是 `_execute_t` 后接变异 token，属 FC 服务端在函数名位置上的解码畸变。

**伤害程度（已核对，比初判更精确）。** 关键事实：这 66 次调用**内层 `args.name` 全部完好**，模型明确知道该调哪个工具（`get_message`、`list_threads`、`delete_send_as_alias`、`email_obliterate_cse_keypair` …），只有外层函数名畸变。逐条回放后确认，**66 次全部在后续轮次重试成功**（同一工具名在该 run 内出现过成功调用）。因此这不是"副作用丢失"，真实代价是：

- 白烧一轮步数预算，少数任务就此错过修复窗口；
- 回喂的提示**方向是错的**——"`does not exist in the tool pool. Use _tool_search to discover the right tool.`" 让模型回头重新检索，而它其实早已知道工具名。这条误导在统计口径上属于"选了一个不存在的工具"，即 §6 分类里的 tool type hallucination。

所以 M0 的正确定位是**噪声与浪费**，不是主导失败源（对比 M1 的量级）。

> **状态：已修复（09-14）。** 详见 §7 P0-2。离线重放 66/66 命中修复判据且内层名全部可解析 → 修复覆盖率 100%。

---

## 4. 共性

把七条机制横过来看，有五个跨域、跨卷反复出现的共性。

**共性一，环境反馈的可信度是当前最短板。** M1 的 675 次静默错误（全量 780，已修复），加上 M2 里 agent 用同一错误参数自证、M6 里读回发现不对就删重建，指向同一件事：agent 拿不到可靠的、可区分的环境信号。MCP 规范要求客户端把工具执行错误交给模型，就是为了让模型能自查自纠，这一环在客户端被抹平了。修复之后这条共性只在"框架层"消失，agent 侧的"自证效力"问题（§2.4）仍然存在。

**共性二，验证动作存在，验证效力不足。** 无论 email 还是 hr，agent 都在最后做了回读。但回读的是自己的产物，用的是自己写入时的参数，因此只能确认"这个东西存在"，无法确认"这个东西符合要求"。`20260105_122123_327` 是最干净的样本：用错误的 userId 写入，再用同一个 userId 读出，读出成功，于是判定正确。Phase 2 的 verify_loop 强制了"至少一次业务只读回读"，从数据看它确实被执行了，但门禁统计回读证据的入口（`_vl_note_real_tool`）用 `success` 判断，被 M1 影响。

**共性三，字面值保真度是长尾杀手。** 一半被要求的值从未原样产出，集中在枚举、既有目录名、取值格式三处。这类错误不会让 agent 觉得"我做错了"——所有调用都返回成功，实体也确实建出来了，只是名字、状态、枚举对不上。它属于"高置信度的错"，最难被 agent 自己发现。

**共性四，多目标题的衰减是单调的。** 子目标越多，越靠后的越容易掉。挂 5 条以上验证器的 5 个任务全部是 5 到 8 个子目标的长链条。衰减出现在"侦察完成、开始执行"这一步之后，第一到第二个子目标执行完整，之后开始丢。

**共性五，失败机制会叠加，且相互掩盖。** `20260106_071510_179` 同时踩了 M0（`_execute_toke` 打错名字）、M3（必填字段义务）、M5（载荷形态）、M6（删除重建）、M2（标签应用步骤整体缺失），五条。`20251204_101148_724` 踩了 M0 与 M5，`send_message` 的 `snippet` 为空又让 agent 的自查失效，于是 M2 的"以为做完了"随之出现。单条机制单独看都不致命，叠加之后 agent 每一步都拿到"成功"，直到最后。

---

## 5. 一个被否证的假设

分析中途有个假设很自然：既然 M3 是知识缺口，那成功任务应该是先查库拿到了既有命名，失败任务则是凭想象直造。测一下。

统计任务是否调用了目录发现类只读工具（`get_service_templates`、`find_hr_services`、`get_topic_categories`、`get_topic_details`、`get_hr_service_by_name`、`list_skills`、`list_hr_criteria`、`retrieve_knowledge_articles` 等），按调用种类数分档看成功率。

```
meta_hybrid/run_1    发现工具 0 种 -> 8/19 = 42%
                     1~2 种      -> 17/63 = 27%
                     3+ 种       -> 6/18  = 33%

full_tfidf/run_1     0 种   -> 2/6  = 33%
                     1~2 种 -> 8/44 = 18%
                     3+ 种  -> 14/48 = 29%
```

**没有正相关，假设不成立。** 盲目增加读取并不能救回成功率，这与"读得更多就更准"的直觉相反。它反过来印证了共性二：问题不在获取信息的多寡，在验证效力与字面值保真度。这一点避免了一个错误的优化方向。

---

## 6. 外部项目的对应解法

以下项目的做法与本项目七条机制可以一一对上。写法上区分"该借的机制"与"它具体解决了什么"。

### 6.1 环境反馈与错误可读性

**MCP 规范（Model Context Protocol，Tools / Error Handling）。** 规范明确规定工具执行错误（API 失败、输入校验错误、业务逻辑错误）必须以 `isError: true` 随结果返回，而不是抛协议级错误，原文给出的理由是"otherwise, the LLM would not be able to see that an error occurred and self-correct"。并且客户端 SHOULD 把工具执行错误交给模型以启用自我修正；恢复提示要写在 text 里，因为那是模型唯一能拿到的东西。

对应本项目：M1。`benchmark/mcp_client.py` 的映射需要补上 `isError`，且在把结果渲染给模型时保留明确的错误标记（当前内层文本里有 ❌，外层标志却是成功，两者矛盾）。

**SWE-agent（NeurIPS 2024，Agent-Computer Interface）。** 四条 ACI 设计原则里，与本项目直接相关的两条。一是"环境反馈应当信息充分但简洁"，他们观察到**静默成功的命令会让模型困惑，模型会额外花动作去确认文件是否真的删掉、编辑是否真的生效**，因此他们让命令在无输出时也回一句明确成功文案。二是"guardrails 抑制错误传播并加速恢复"，他们给编辑动作配了语法检查器，改坏就拒绝并给出诊断，而不是等到测试阶段才暴露。

对应本项目：M1、M5、M6。静默成功、载荷写不进去、删除重建，本质都是缺少即时、明确、可据以修正的反馈。

**Anthropic《Writing effective tools for agents》。** 两条要点。错误响应要写成"具体且可操作"，不要抛裸的 traceback 或错误码，他们的示例里把格式错误连同正确范例一起给出。工具描述按"跟新同事交接"的标准写，参数名要无歧义（`user` 改叫 `user_id`），并明确哪些工具不该用。

对应本项目：M3、M5。`create_filter` 那条"required field cannot be an empty string"已经算合格提示，缺的是它从来没出现在工具描述里，agent 只能靠失败去撞。

**Anthropic《Building Effective Agents》附录二，Poka-yoke。** 他们在 SWE-bench 上把工具从接受相对路径改成只接受绝对路径，直接消掉了一整类错误。要点是"改参数，让错误更难发生"，而不是事后校验。

对应本项目：M4，以及 M0 的"方案 A"（把包装层去掉，让畸形名无从产生）。至于 M0 实际落地的修复（§7 P0-2），更贴近上面第 442 行那条"错误响应要具体且可操作"——原本回喂的"去 `_tool_search` 找正确工具"把模型引向了错误方向，改成指明函数名拼写才是对症的。

### 6.2 工具名与参数的结构约束

**OpenAI Structured Outputs / strict mode（2024-08）。** `strict: true` 下用约束解码，把 JSON Schema 编译成文法在采样时屏蔽非法 token。他们的评测里复杂 schema 遵从率从不足 40% 提到 100%。硬性要求是每个 object 都要 `additionalProperties: false`，且 **properties 里的所有字段必须列进 required**（可选字段用 `["string","null"]` 表达）。

对应本项目：M0、M3(d)。两处直接可用。其一，`create_filter` 那类"看起来可选、实际必填"的 schema 就是 strict 模式的形态，把 schema 完整暴露给模型即可免掉三轮试错。其二，`_execute_tool` 的 `name` 参数（内层真实工具名）改成已绑定工具名的 enum 并施加约束解码，可以免掉名字拼错类的问题。

> 更正（09-14）：M0 的退化实际发生在**外层函数名**上，不是 `name` 参数——内层 `args.name` 在 66 次里全部正确。外层函数名能不能受 schema 约束取决于 FC 服务端是否支持 function-name 的 constrained decoding，客户端约束不到；因此 P0-2 走的是客户端侧的确定性修复（§7 P0-2），而非 enum 约束。

**工具调用失败分类研究（arXiv 2412.04141《Reducing Tool Hallucination via Reliability Alignment》，及后续工作）。** 把工具幻觉分成 selection 与 usage 两支。selection 又分 tool type（调用不存在的工具或无关工具）与 tool timing（同参数重复调用）；usage 分 tool format（JSON 非法、参数名错、漏必填）与 tool content（参数值凭空捏造）。

对应本项目：`_execute_ttool` 归 selection/tool type；漏 `users` 字段归 usage/tool format；把 `US Payroll Bank Change` 改写成 `US Payroll Bank Account Change` 归 usage/tool content；抖动归 selection/tool timing。分类的意义在于**三者修法不同**，用一套办法打不中。

**Google Cloud Agent Evaluation 的失败模式表。** 其中一条与本项目主导失败形态几乎同名，`Omission of Required Tool Call`，描述为"agent 失败于执行某个必要函数，无论是直接作答、**跳过了复合请求的一部分**，还是跳过了前置步骤"。另有 `Failure to Set Parameter`（漏掉用户约束所必需的一个参数，落到默认值）与 `Semantic Parameter Error`（语法合法但取值语义错误）。

对应本项目：M2、M4(B)。这套词表可直接借用为本项目的失败标签体系，便于后续统计口径统一。

### 6.3 多子目标覆盖与收工判定

**Todo / 外部化计划模式（Claude Code 的 TodoWrite、Manus 的 todo.md recitation，以及 Agent Patterns Catalog 收录的 Todo-List-Driven Agent）。** 做法是让 agent 在开跑早期就把计划写成落盘的清单文件，每轮读取、推进未勾选项、回写、把剩余计划重新注入上下文末尾。报告的效果是长任务成功率提升两到三成，且能做到中断恢复。要点有三：**计划落在磁盘而不是上下文里**（上下文压缩时计划最先消失）、**每轮把未完成项重新注入**（对抗注意力漂移）、**完成判定与清单项一一对应**。

对应本项目：M2。当前 email 域的 verify_loop 会引导模型先生成一份验收清单，方向是对的，但清单只用于收尾核查，没有承担"逐项推进"的职责。

**Anthropic《Effective harnesses for long-running agents》。** 三条经验直接命中。其一，用 **JSON 而不是 Markdown** 记录特性清单与状态，避免模型误读或污染。其二，明确禁止删测试，因为"agents will delete failing tests to mark features complete"，与本项目 M6 的删除重建是同一类自利行为。其三，每个会话开头先做验证再写新代码。

**LangChain / AgentPatterns 的 Pre-Completion Checklist 中间件与 Loop Detection 中间件。** 前者在 agent 退出前拦截，强制逐条对照任务规格确认，任何一条不确认就不允许收工；后者用工具调用钩子统计重复行为（连续失败的测试、重复的相同调用），超阈值就注入纠正上下文或强制换策略。

对应本项目：共性二、M6。Loop Detection 对"create → delete → create"这种抖动是直接的解。Pre-Completion Checklist 对 M2 是直接的解，且它比当前 verify_loop 更硬，当前门禁只要求"有一次回读"，不要求"每一条子目标都有独立的确认"。

**Evaluator-Optimizer / Reflexion 类自证改进。** Anthropic 在《Building Effective Agents》里列的第五种工作流模式，生成器与评估器分设，评估器基于独立证据，迭代到满足为止。要点是评估器不应复用生成器的参数与路径。

对应本项目：共性二。`20260105_122123_327` 用同一个 `bob@company.com` 回读就是典型的自证失效。

---

## 7. 落地建议

按性价比排序，并标注与机制的对应关系。前三项属于"改了就不该继续失分"的类型。

> **进度（09-14）：环境反馈层的两项已修复 —— P0-1（M1）与 P0-2（M0）**，均通过离线重放 + 全量单测 105/105 验证。P0-3 起为后续项。所有修复都**尚未在真实评测上重跑**，因此只声明"缺陷已消除"，不声明"成功率提升"。

### P0-1 修 `isError` 映射（对应 M1）—— ✅ 已修复（09-14）

`benchmark/mcp_client.py:334-342` 增加内层 `isError` 检查，返回值中同时带上 `isError` 字段。同步检查 `meta_tool_router.py:1039` 附近的兜底分支，以及任何以 `success` 作为分支条件的编排逻辑。

顺带把 `_vl_note_real_tool` 的口径改成"`success` 为真 **且** `isError` 不为真"。

**实际改动。**

1. `benchmark/mcp_client.py`：新增模块级 `mcp_error_text(result)` / `is_tool_error(result)` 两个纯函数；`call_tool()` 尾部重写，三条出口统一为 `{success, result, error, isError}` 四字段形状。工具执行错误（内层 `result.isError=true`）映射为 `success=False` **且** `error=` 可读文本；协议级错误（`data.error`）映射为 `success=False, isError=True`；传输失败映射为 `success=False, isError=False`。
2. `orchestrators/meta_tool_router.py`：新增统一判定函数 `_call_failed(tool_result)`（`success` 为假 **或** `isError` 为真即失败），替换原先各处不一致的 `tool_result.get("success", True)`。两个执行点（`_execute_tool` dispatch 分支、直绑分支）共用它来驱动 `tools_used` 登记、verify 门禁证据计数、记忆事实抽取。`_vl_note_real_tool` 增加 `is_error` 形参，失败回读不再计为证据。回灌给模型的内容在失败时改为整个信封（含可读 `error`），成功时仍是内层 payload。
3. `tests/test_meta_tool_router.py`：`_FakeMCP` 增加 `tool_error` 模式；新增 `TestVerifyLoop.test_failed_readback_is_not_counted_as_verify_evidence` 与 `TestMcpToolErrorSemantics` 五个用例（统一口径、失败调用不进 `tools_used`、信封含可读文本、成功路径不受影响）。

**验证方式与实测结果。**

- 离线重放 `scripts/verify_iserror_fix.py`，对本地 7 个可重放卷逐条用新判据重新裁决历史记录：
  - `inner.isError=True` 的调用共 **780** 次（其中 5 个主卷 = 675，与诊断口径一致）；
  - 这 780 次在旧记录里**全部**是 `success=true`，新映射下 **780/780 全部翻转为 `success=False`**，即旧代码对内层错误零识别；
  - 其中 **361 次是只读工具**（`get_*`/`list_*`/`find_*` 等），属于原先被误当 verify 证据的调用；
  - 反向校验：记录为 `success=true` 且内层无错误的 9391 条在新映射下仍为成功，无误伤；记录为 `success=false` 的 66 条属传输层失败，新映射下依旧判失败。
- 单元测试：全量 **98/98 通过**（原基线 92，新增 6 个）。

**待实测确认（需重跑才能定论）。** 历史卷里 `vl_read_calls_gate` 的数值无法离线重算（门禁是运行期状态机），需要在修复后的新卷上观察。预期方向：`isError-masked` 归零（已由离线重放证实），`vl_read_calls_gate` 下降（因为失败的读不再计数）。这个下降本身是修好了的证据，不是变差。**在重跑之前，不得据此声称成功率有提升。**

### P0-2 消除包装工具名退化（对应 M0）—— ✅ 已修复（09-14）

诊断时给过两条路：

- 方案 A，改 dispatch 默认值。hr 两卷走默认 `inject`（直绑，不经过包装名）退化 0 次；email 三卷显式传 `--tool_dispatch exec` 才有 66 次。**注意评测侧默认值本来就是 `inject`**（`evaluate.py:231` / `meta_tool_router.py:261`），`exec` 是显式 opt-in。所以方案 A 实际等价于"别传 `exec`"，代价是放弃方案 Y 的固定 bind 集（前缀缓存友好 + 统一执行入口）。
- 方案 B，保留包装，约束/修复名字。

**实际采用：方案 B 的一个有界变体**，保留方案 Y 不动。理由：内层 `args.name` 在 66 次里全部完好，说明信息没有丢，丢的只是外层字面量；为一个**服务端解码畸变**去改架构不划算，加一层防御更合适。

改动落在 `orchestrators/meta_tool_router.py`：

1. 模块级 `_looks_like_execute_wrapper(raw_name)`：判据是"带 `_execute_` 前缀且不等于 `_execute_tool`"。取前缀而非整串编辑距离，既覆盖观测到的全部变体（`_execute_ttool` / `_execute_toke` / `_execute_trap` / `_execute_trip` / `_execute_tool_tool` / `_execute_t tool`），又不会误伤任何真实业务工具名（真实名不带该前缀）。
2. 方法 `_repair_execute_wrapper_name(raw_name, args)`：要求**两个条件同时成立**才改判为 `_execute_tool` —— (a) 命中上面的前缀判据，(b) `args.name` 命中 `_all_tools_by_name` 池内真实工具。条件 (b) 是关键约束，保证修复只在"模型意图已经明确"时发生。只改外层包装名，内层工具名与参数原样透传，不改变任何执行语义。每次修复记一条 `warn` 并写入 `self.name_repairs` 审计。
3. 分发循环入口处（`for tool_call in response.tool_calls` 之后）调用修复，再进入原有分支。
4. 未修得动时的提示也改了方向。原先不管什么情况都回"`does not exist in the tool pool. Use _tool_search to discover the right tool.`"；若名字形似包装层，改为提示函数名拼写（`Malformed tool name '...'. The dispatcher tool is spelled exactly '_execute_tool' ...`），因为该问题的正解是改拼写而不是回去检索。
5. 遥测新增 `meta_tool_name_repairs` / `meta_tool_name_repair_detail`，便于在新卷上核对残余规模。注意 `conversation_flow` 里存的是模型**原始**输出的畸形名，留档以便离线统计。

**验证方式与实测结果。**

- 离线重放 `scripts/verify_wrapper_repair.py`，对历史卷逐条用新判据重算：退化调用 **66** 次，命中前缀判据 **66** 次，其中内层名可解析 **66** 次 → **修复覆盖率 100%**，无一条漏网。被挡住的真实工具去重 **26** 个，含 `delete_send_as_alias` / `email_obliterate_cse_keypair` / `modify_message` 等写工具与 `get_message` / `list_threads` 等读工具。
- 单元测试：新增 `TestWrapperNameRepair` 七个用例（判据边界、六种变体逐一修复、原始名留档、内层名不可解析时不修复且改提示、正常名不产生修复记录、非包装形态未知名维持原引导）。全量 **105/105 通过**（M1 的 6 个 + M0 的 7 个，基线 92）。

**边界（如实说明）。** 该修复只覆盖"前缀仍是 `_execute_`"的畸变。若服务端把名字烂得更彻底（例如前缀本身写错），修复不会触发，但此时回喂的提示已改为指明正确拼写，比原先的"回去检索"更准。真要在解码层杜绝，需要 FC 服务端支持 constrained decoding / strict function-name 约束（§6.2），那不在这套代码的控制范围内。

**两条路的关系。** 修完之后 `dispatch=exec` 与 `inject` 在"包装名退化"这一项上不再有差别，方案 Y 可以继续用；方案 A（少用 `exec`）不再是必需项。

### P0-3 把 schema 完整暴露给模型（对应 M3(d)、M5）

现在的工具描述只有一句话说明，参数级约束藏在服务端。至少对以下几类工具，把 JSON Schema 在绑定或注入时一并给出，注明必填、非空、枚举取值：

- 必填但非直观的：`create_filter`（`to`/`subject`/`query`/`negatedQuery` 需非空字符串）
- 载荷结构复杂的：`create_draft`、`update_draft`、`send_message`（`payload` 与 `raw` 二选一，header 与 body 的位置）
- 枚举取值的：hr 域的 status / priority / source / type

一次性投入，之后 M3 的"枚举词汇"与"必填义务"两个子类应有明显下降。

### P1-1 验收清单升级为逐项证据（对应 M2、共性二）

现有 verify_loop 的方向保留，把门禁从"至少一次只读回读"改成"原任务每一项子目标各有独立的确认"。具体做法：

- 在 checklist 生成阶段就把子目标编号化，与用户请求的条目对齐；
- 收工时要求逐项标注证据来源，也就是用于确认该子目标的那次调用是哪一个；
- 证据必须与写入路径不同源（避免 `20260105_122123_327` 那种同参数自证）。

可参照 §6.3 的落盘清单做法，用 JSON 存，勾选与动作一一对应。

### P1-2 加抖动检测（对应 M6）

对同一写工具在窗口内出现 `create → delete → create` 形态，或同一工具以不同参数连续调用 3 次以上，注入一次策略干预，要求先说明为什么前一次不成立，再允许重试。Google Cloud 的 `Tool timing hallucination`、LangChain 的 Loop Detection 都是这个做法。

### P1-3 明确 ID 与作用域的使用规范（对应 M4）

系统提示里加两条硬约束。其一，同一任务内实体标识符的解析结果一经确定就固定复用，禁止在读用 `me`、写用显式地址之间来回切。其二，关系字段必须用目标表的键类型（`opened_for` 取 `hr_profile_id`，`users` 取 `user_id`），并在工具描述里点明。

### P2-1 对齐政策提示与任务期望（对应 M7）

这是基准设计问题，需要与上游确认口径，三条路：

- 把 hr 任务的角色设定为 `Administrator`，与任务的权限需求一致；
- 在任务描述里显式写明本次执行的授权范围覆盖哪些管理员动作；
- 若确实要考察"识别越权并拒绝"，则这类任务的验证器不应要求越权实体存在。

现状是提示词要求 halt、验证器要求实体存在，二者不可同时满足。在口径统一之前，这批任务的失败不宜计入 agent 能力指标。

### 不建议做的事

**不要靠增加读取量来提升成功率。** §5 已经用数据否证了"多查库就能对"。发现工具的调用次数与成功率无正相关，盲目加检索轮次只会推高成本。

**不要用单纯的重试兜底。** §6.2 引用的那篇分类工作讲得很清楚，重试是在同一个未受约束的分布里重新采样，只降低概率不改变状态空间。M0 的工具名退化与 M3 的枚举取值都需要在生成前约束，事后重试治不了。

> 补充（09-14，M0 修复后）：P0-2 采用的不是重试，而是**确定性修复**——畸变的外层名由一个纯函数按前缀判据 + 内层名可解析这两个条件直接改判，不产生新的采样。它之所以可行，是因为畸变发生在**冗余的包装层**上、而模型真正的意图（`args.name`）完好无损；这是 M0 独有的有利条件。M3 的枚举取值没有这个条件（正确取值本来就不在模型输出里），所以那一类仍然只能在生成前约束，修法不能照搬。

---

## 8. 附录

### 8.1 稳定失败案例明细（email 15 例，三卷全败）

| task | 要求的核心动作 | 实际发生 | 失败验证器 exp/act | 机制 |
|---|---|---|---|---|
| 20251203_093009_456 | 开假期自动回复 + 建草稿 | create_draft 调 7 次以上，filename 反复试 | 3 / 2 | M5 |
| 20251203_104951_949 | 更新既有草稿正文 | update_draft 换 5 种载荷形态 | 1 / 0 | M5 |
| 20251203_120537_904 | 改别名显示名并完成验证 | 别名状态仍为 pending | accepted / pending | M2 |
| 20251204_101148_724 | 发信给 carol 并打标签 | send_message 返回 snippet 为空；`_execute_toke` 打错名 | 1 / 0, 2 / 0 | M0+M5 |
| 20260105_122123_327 | 建两个 label 等 7 项配置 | 写操作 userId 用 bob@company.com | 1 / 0 | M4 |
| 20260106_071510_179 | 建 label + 打标签 + 建 filter | 从未调 modify_message；filter 试错后删除 | 1 / 0 ×2 | M2+M3+M5+M6 |
| 20260106_085129_923 | 清 urgent 标签 + 假期设置 | 假期设置未生效 | 1 / 0 | M2 |
| 20260107_125943_886 | 安全审计 + 转接规则 | 审计转接未建 | 1 / 0 | M2 |
| 20260107_131200_705 | 别名替换 | 别名未替换 | 1 / 0 | M2 |
| 20260107_141655_644 | 更新既有 filter | filter 未建 | 1 / 0 | M2 |
| 20260107_160335_693 | 关假期回复并更新 | 假期设置行不存在 | 1 / 0 | M2 |
| 20260107_214637_627 | 导入密钥对并禁用 | 全程仅 3 次只读，零写入 | 98 / 98（未增长） | M2 |
| 20260108_031733_760 | 删转发与别名 | 消息未删除 | 0 / 1 | M2 |
| 20260108_101001_581 | 假期回复 + 发信 + filter | 主体动作未落地 | 3 / 0 等 3 条 | M2 |
| 20260109_160851_122 | 建 send-as 别名并试发 | 别名未建 | 1 / 0 ×2 | M2 |

### 8.2 稳定失败案例明细（hr，取 10 例代表，两卷全败）

| task | 要求的核心动作 | 失败验证器 | 机制 |
|---|---|---|---|
| 20251212_114043_514 | 建 topic + 模板（指定 COE） | 模板 count 0，coe_type 不匹配 | M3(b) |
| 20251212_175622_173 | 建 case task 且状态 ready | task count 0 | M3(a) |
| 20251212_194942_087 | 离职全流程 6 步 | 6 条全败，全程零写入 | M7+M2 |
| 20251216_123530_079 | David Kim 路由配置 5 步 | 5 条全败，全程零写入 | M2+M3(a) |
| 20251217_073807_153 | 部署案例与通知 | case / notification 均 0 | M3(a) |
| 20251217_211723_514 | 建技能 + 路由 + 审批 | 技能名改写；rule 漏 users 字段 | M4(C)+M4(B) |
| 20251218_234518_453 | 建保密调查模板与服务 | 模板 count 0 | M3(b) |
| 20251219_114935_971 | 医疗福利案例全流程 | 关联校验全败 | M4(B) |
| 20251226_010313_969 | 建调动案例（Employee Profile Update） | HR Case count 0 | M2 |
| 20260115_151312_690 | Travis Wood 调动案例与通知 | `source` 取值不在允许枚举内，验证器计数 0 | M3(a) |
| 20260116_095609_650 | Joanne 牙科福利案例与通知 | 通知缺 `type='alert'` / `status='draft'`，计数 0 | M3(a) |

完整清单在 `out/_classify_hr.json` 与 `out/_digest_hr_sig.md`。

### 8.3 复现命令

```bash
cd F:/Project/EnterpriseOps-Gym-main

# 1. 单卷失败案例 trace
python scripts/extract_failure_traces.py out/email_vloop/run_1

# 2. 跨卷稳定失败摘要
python scripts/digest_stable_failures.py \
  --runs "out/email_vloop/run_1,out/email_vloop/run_2,out/full_exec_email/run_1" \
  --out out/_digest_email_stable.md

# 3. 机制分类
python scripts/classify_stable_failures.py \
  --runs "out/meta_hybrid/run_1,out/full_tfidf/run_1" --label hr

# 4. 机制规模量化（含 isError 折叠与包装名退化计数）
python scripts/quantify_failure_mechanisms.py
```

### 8.4 待确认

- M7 的政策冲突需要有权限的一方给出基准设计口径，本报告只呈现冲突本身。
- M3 里"既有目录名"一类的正确取值是否可从 `Domain Wise DBs and Task-DB Mappings/` 的种子库里系统性枚举出来，值得单独查一次。若可枚举，把它做成工具描述的一部分即可批量消除该类失败。
- `isError` 修复已完成，离线重放证实 `isError-masked` 归零（780/780）。`vl_read_calls_gate` 的变化需要重跑一轮才能实测，本报告的预期是下降。
