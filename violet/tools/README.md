# Violet 2.0 本地测试工具（Phase 7 B.1）

三个命令行工具，全部跑在**真实的 Violet 2.0 流水线**上（VioletRuntime → IntentAgent / ChatAgent / MemoryAgent、ContextEngine、
MemoryWorker、outbox、ProactiveScheduler），没有另写简化版聊天逻辑。

| 工具 | 用途 | 默认 |
|---|---|---|
| `python -m tools.violet_test_chat` | 在隔离环境里和 Violet 聊天 | 模拟模型，免费、离线 |
| `python -m tools.dialogue_eval` | 32 轮验收对话（虚构用户“小满”）+ 评分表 | 模拟模型 |
| `python -m tools.model_compat` | 检查模型对 Chat Completions 各项参数的兼容性 | 模拟服务商 |
| `python -m tools.free_chat` | 自由聊天体验测试：会话可以接着聊，每轮细节和标记都保存（§20，Phase 8.9.6） | 模拟模型 |
| `python -m tools.fake_mcp_server` | 虚构的课表 / 天气 / 搜索 MCP 服务器（stdio，不联网），用来试 Violet 的工具使用（§23，Phase 9.2） | 虚构数据 |
| `python -m tools.memory_conflicts` | 只读列出跨记忆的冲突（§22，Phase 9.1.2） | 只读 |

## 1. 隔离保证

- 每次启动都新建一个临时目录（`%TEMP%\violet-testchat-*`），数据库、数据、媒体都在里面；结束后自动删除（`--keep` 保留）。
  删除前先停止后台线程并关闭 SQLite 连接；只删除**本次运行创建**的目录（目录里有本次的归属标记），不按通配符删除。删不掉（例如 Windows 上文件仍被占用）会明确报告剩下哪些文件，退出码为 3；正常退出、Ctrl+C、异常退出都会清理，清理失败不会掩盖原来的错误。
- 以前遗留的测试目录只列出、不自动删除：`python -m tools.violet_test_chat --list-leftovers`。
  项目目录、`violet.db`、`data/` 会被拒绝作为测试目录。
- `VIOLET_ENV_FILE` 指向临时目录里的**空文件**：工具不会读取任何真实 `.env`，生产配置、微信凭据、生产数据库都碰不到。
- shell 里已有的 `VIOLET_*`、`LLM_*`、`MCP_*` 变量都会被清掉；模型名和提示词只从 `env_example`（或 `--config`）读取。
- 强制关闭：微信（换成内存里的假客户端）、MCP、Web 搜索与探索、图片、生活引擎、Dashboard、主动调度线程、启动消息、代理。
  主动消息只在你输入 `/proactive` 时判断一次。
- 每次模型调用（包括语义守卫等辅助模型）都经过 `BudgetedClient`：**先检查预算，再发请求**。
- 报告保存在 `tools/output/<时间>/`（已加入 `.gitignore`），写入前会把 Key 替换成 `<redacted>`。

### 报告里的调用明细（Phase 7 B.2）

`report.json` / `chat_transcript.json` 中每次模型调用一条记录（`call_details`），只含白名单里的结构化字段：
轮次、Agent、模型、全局序号 / 本轮序号、所属运行（run_id）及第几步、请求消息数、请求字符数、工具定义数量及字符数、调用了哪些工具、工具结果摘要（工具名、ok、action / reason 代码、返回记录数量和 id、档案 key）、延迟、`prompt_tokens` / `completion_tokens` / `total_tokens` / `cached_tokens` / `reasoning_tokens`、错误类型和 HTTP 状态码。

- 不写入：Key、提示词全文、用户消息、记忆原文、工具参数里的文字、错误信息原文。
- token 字段只照录上游返回的值；上游没有给的字段保持 `null`，不推算。没有 usage 的调用单独计数，预算的 token 上限对它们无效（此时只有调用次数上限）。
- `call_summary` 按 Agent 汇总调用数、token、延迟、工具使用和运行次数；`total_tokens` 与 prompt+completion 的差异只做统计。
- `fit_prompt_tokens_vs_request_chars` 是 prompt_tokens 对请求字符数的最小二乘拟合，**只是统计量**，截距不代表已确认的固定开销。
- `runs` 记录每次 Agent 运行的步数和是否在工具执行后直接结束（`finished_after_tools`）；Chat 运行附带上下文各段的字符数和 fact id。
- 对话原文（用户的话、Violet 的回复、最终档案状态）仍保存在轮次记录里，供人工评分；这些都是虚构数据。

## 2. 启动（Windows PowerShell）

```powershell
cd "D:\unity\violet\Violet"
..\.venv\Scripts\Activate.ps1

# 模拟模式聊天（不花钱、不联网）
python -m tools.violet_test_chat

# 模拟模式跑 32 轮（检查流程是否通畅；回复是“（模拟回复）……”，不能用来评自然度）
python -m tools.dialogue_eval --scripted-memory --quiet-hours off

# 模拟模式的兼容性检查
python -m tools.model_compat
```

聊天里的命令：

| 命令 | 作用 |
|---|---|
| `/advance 3` | 假装过去了 3 小时（把所有记录的时间往前挪） |
| `/proactive` | 让主动消息调度器判断一次“现在要不要发” |
| `/state` | 查看档案事实、情境、记忆、约定、Violet 的生活事件 |
| `/usage` | 调用次数、token、平均延迟、错误、估算费用 |
| `/score 2 备注` | 给上一轮打分（0/1/2） |
| `/quit` 或 Ctrl+C | 停止并保存记录 |

## 3. 真实模型：授权与退出

需要同时满足以下 **4 个条件**，缺一个都不会发出任何真实请求：

1. 当前 PowerShell 窗口里设置 `VIOLET_TEST_ALLOW_REAL_API=1`；
2. 命令行加 `--real`；
3. 提供**单独的测试 Key**：用 `VIOLET_TEST_API_KEY` 环境变量，或者在隐藏提示里输入（不回显、不保存）；
4. 看完屏幕上的“接口 / 模型 / 预算”说明后，输入 `yes`。

```powershell
$env:VIOLET_TEST_ALLOW_REAL_API = "1"
# Key 建议在提示时输入；如果用环境变量，只在这个窗口里设置：
# $env:VIOLET_TEST_API_KEY = "<测试 Key>"

# 先跑兼容性检查（每个模型约 11 个很小的请求，每个请求输出上限 16–64 token）
python -m tools.model_compat --real --api-url https://你的测试接口/v1 --model grok-4.2 --model gpt-5-mini

# 再聊天，或跑前 5 轮试试
python -m tools.violet_test_chat --real --api-url https://你的测试接口/v1 --max-calls 40
python -m tools.dialogue_eval --real --api-url https://你的测试接口/v1 --rounds 1-5 --max-calls 60
```

**预算**（任何一项达到就停止；价格未知时只按次数和 token 限制）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--max-calls-per-round` | 15 | 单轮上限；超过时这一轮由 Violet 的降级回复兜底，下一轮重新计数 |
| `--max-calls` | 120 | 全程上限；达到后整个会话停止调用 |
| `--max-tokens` | 300000 | 全程 token 上限 |
| `--price-per-1k` / `--max-cost` | 不设 | 知道价格时再填，按估算费用封顶 |

参考：一轮正常对话大约 3–8 次调用（意图 1–3、聊天 1–2、记忆 1–3）。32 轮按默认预算可能在中途停止，
需要完整跑完时请自己评估费用后再调高 `--max-calls`。

**退出**：随时按 Ctrl+C 或输入 `/quit`，已完成的轮次会保存。用完后关闭这个 PowerShell 窗口，或者执行：

```powershell
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API, Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
```

`--config 文件` 只允许写模型名、提示词、温度、超时之类的设置（例如 `CHAT_LLM_MODEL=...`）；
文件里如果出现 KEY / TOKEN / SECRET，会直接拒绝启动。

## 4. 32 轮验收（`tools.dialogue_eval`）

计划来自 `tests/samples/acceptance_dialogue_30.json`（32 轮，虚构用户小满），覆盖：时间推进、用户纠正事实、
已结束的事件不再被提起、主动消息选择不发、用户正在忙、Violet 生活经历的一致性、观点分歧、需要回忆的轮次。

| 选项 | 作用 |
|---|---|
| `--rounds 1-10` | 只跑部分轮次 |
| `--manual` | 手动模式：每轮显示计划的话，回车照发，也可以输入改写后的内容，`/skip` 跳过，`/quit` 停止 |
| `--manual-score` | 每轮结束后立即打分 |
| `--scripted-memory` | 用计划里写好的记忆动作代替记忆模型，只考察聊天模型 |
| `--no-memory` | 记忆模型什么都不存：看看 Violet 在没有记忆时怎么表现 |
| `--quiet-hours off` | 关闭静默时段（深夜测试时主动消息也能判断） |

输出：`report.json`（每轮回复、调用次数、token、延迟、错误、自动证据、最终状态）和 `score_sheet.csv`（评分表，可用 Excel 打开）。
填好分数后汇总：

```powershell
python -m tools.dialogue_eval --summarize tools\output\<时间>\report.json --scores tools\output\<时间>\score_sheet.csv
```

### 评分模板

每轮 0 / 1 / 2 分（2 = 好，1 = 能接受但有毛病，0 = 不合格），对照 `rubric` 列和下面的维度：

| 维度 | 2 分 | 0 分 |
|---|---|---|
| 自然度 | 像朋友在微信上回话，长度和语气合适 | 客服腔、说教、模板句、刻意的口头禅 |
| 人设贴合 | 符合 Violet 的性格和说话方式 | 像换了一个人，或者过分讨好 |
| 背景理解 | 用对了之前知道的事（住校、专业、最近的事） | 张冠李戴、把常住地说成此刻所在地 |
| 记忆正确 | 记得该记的，纠正后用新的 | 用了已被纠正的旧事实 |
| 不重复追问 | 已回答过的不再问 | 反复问同一件事 |
| 已结束的事 | 不再当成没结束 | 又问“考试准备得怎么样” |
| 不编造经历 | Violet 只提记录里有的经历 | 编出没发生过的经历 |
| 不过度提问 | 不是每条都以问句结尾 | 连续追问、查户口 |
| 主动消息 | 用户忙 / 刚聊完时选择不发 | 打扰正在忙的用户 |

**逐轮补充说明（Phase 7 B.5；只补充评分说明，验收剧本和已有评分都不变）：**

- **第 11 轮**（问“猜猜我现在在哪”，此刻位置未知）：自然地承认不知道并接住对方的话 → 2 分；明确说明只是猜测、
  没有把常住地当成已知事实（例如“在宿舍？我瞎猜的”）→ 1 分；把猜测当事实说出 → 0 分。
- **第 13 轮**（对方刚说过在家，又问在哪）：正确使用已知位置并自然回答（可以带玩笑）→ 2 分；
  无视明确、有效的位置而猜成别的地方 → 0 分。
- **第 14 轮**（面试前紧张）：允许一个针对对方当前困扰的关怀式追问（例如“最担心哪一部分”），
  不算了解类问题，不扣分；但仍计入全局问句比例。

报告里的 `location`（上下文中的位置、回复提到的地点和来源、是否用上已知位置、是否标明是猜测）和汇总里的
`reply_location_flags` 只是给评分人的证据，不会改变 verdict。已完成的报告可以用
`python -m tools.dialogue_eval --recheck <report.json>` 离线复核：数据库会先复制，不会改动测试目录。

**通过标准**（`summary.json` 的 `verdict`）：每轮都打过分；总分 ≥ 满分的 54/64；关键轮次（9、10、14、24、25、27）没有 0 分；
用户忙时没有发主动消息；以问句结尾的回复 ≤ 40%，且不连续 3 轮以问句结尾。

上下文检查按字面匹配；第 3 轮的“身份：大学生”另外接受结构化的等价档案：本轮 Chat 上下文里有已确认的身份（大学生 / 研究生……），或者有已确认的大学名称、同时有已确认且对 Violet 可见的年级（大一……）。只出现“学校”两个字、未确认的候选、被禁用的档案都不算。报告的 `matched_by` 说明是按哪条规则通过的。

自动字段只是**给评分人的证据**，不能代替人工判断自然度：以问句结尾、一致性守卫标记、提到已结束的事件、
需要回忆时意图模型有没有调用 `query_memory`（R‑M2 风险的观察指标）、每轮调用数 / token / 延迟 / 错误率。

第 16 轮的 `ended_event_not_allowed` 检查（Phase 7 B.6.1）除了“已结束的事件不在主动消息可提的列表里”，还要求记忆里
没有**同一件事的两条互相矛盾的有效记录**：一条已结束（done / cancelled），另一条仍是未结束（例如“进展未知”）。
发现时检查失败，`contradictory_events` 列出两条记录的 id 和判断依据（文本相似度或共同关键词）。

### 跨轮回放与继续测试（Phase 7 B.3）

`--replay-from <真实测试的 report.json>` 在隔离环境里**离线**重建所选轮次之前的轮次，然后接着跑真实（或模拟）轮次：

- 只接受真实模型测试的报告（`real: true`），而且必须正好包含要回放的轮次；每轮用户原话必须与剧本逐字一致。
- Violet 的回复逐字使用报告里记录的真实回复，经正常消息流水线发送和入库；回放阶段不创建真实模型客户端、不发网络请求、
  不计调用次数和 token、不参与评分。
- 档案和事件只按报告能确认的内容恢复（最终状态里的值 + 调用明细里的轮次和动作）；档案证据用值本身（必须逐字出现在
  当轮原话里）；事件日期只在记忆内容里写有日期时才用。报告里没有的元数据列在报告的 `replay.not_restored`，
  与剧本记忆步骤的差异列在 `replay.differences_vs_plan`。
- 剧本时钟：从原始运行的开始时刻（剧本预置生活事件的时间 + `hours_ago`）起算，每轮按报告记录的调用耗时推进；
  之后的轮次接着这个时间走，不使用宿主机当前日期。
- 回放后逐项核对档案、事件、Violet 生活（含分享次数）、情境、提醒，与报告不一致就停止（退出码 4），不会发起任何真实调用。

测试目录里的 `.violet-eval-state.json` 记录回放状态和已完成的轮次。加 `--keep` 保留目录后，下次用同一个 `--workdir`
加 `--rounds <下一轮起>` 就能继续（不会重复回放）。以下情况一律拒绝：再次回放、轮次重叠或不连续、回放未成功完成、
目录里有聊天记录但没有状态文件、在含模拟回复的目录上跑真实测试。

主动消息检查会给出拦截原因：未触发调度 / 静默时段 / 用户忙碌 / 冷却时间 / 用户偏好或每日上限 / 模型决定不发送 /
已发送 / 错误。第 9 轮只有在**本轮保存了忙碌状态、被忙碌规则拦下、且没有发送**时才通过；第 10 轮检查忙碌状态
已经不再有效。任何一项不通过，汇总结论都是 fail。

**多份报告连续回放（Phase 7 B.5）**：`--replay-from` 可以按顺序给多次，例如先给第 1–5 轮的报告，再给第 6–10 轮的报告。

- 轮次必须连续、不重复、不重叠；后一份报告必须记录过它回放的正是前一份报告（哈希一致），否则拒绝运行。
- 每一段回放完都与该段报告的最终状态逐项核对：聊天记录的内容和顺序，档案、事件、情境（含过期时间）、提醒，
  Violet 生活事件的时间和分享次数。
- 情境只按报告能确认的内容恢复：类型和内容来自最终状态；过期时间优先使用该轮忙碌检查记下的原始值，
  否则用最终值加上之后的旧式时间推进推算；证据用整句用户原话（报告里没有工具参数）。
- B.4 之前录制的报告（每轮没有 `scenario_time`）在原位置重放当时的旧式时间推进（整体后移时间戳）和第 9 轮的主动消息检查，
  并逐字核对拦截原因和第 10 轮结束时的情境；B.4 之后的报告直接使用记录的每轮场景时间。
- 报告的 `replay` 里有每段的核对结果、时钟偏差、重放的检查、回放结束时有效的情境，以及每条情境由哪一轮的记忆任务写入。

### 场景时间与剧本修正版（Phase 7 B.4）

- 时间推进只推进会话的**场景时钟**（`/advance`、剧本的 `advance_hours` / `time`、第 9 轮的 1.6 小时）；
  已保存的消息、档案证据、事件时间、生活事件和情境时间一律不改写，已保存的绝对日期保持不变。
  情境过期、事件到期（计划中 → 不确定）、提醒和主动消息判断都用同一个场景时间。推进本身不发消息、不调用模型；
  提醒只有在剧本要求的调度检查里才会处理。时间运算按 UTC 进行，跨天、跨时区和夏令时都按真实经过的时长计算。
- 报告每轮记录 `scenario_time`；有推进的轮次记录 `time_advance`：起止时间、小时数、推进中自然过期的情境、
  因时间改变状态的事件。
- 旧的“整体移动时间戳”函数 `advance_time` 仍保留：实时轮次从不调用；只有回放 B.4 之前录制的报告时，才在原位置重放当时的推进（Phase 7 B.5）。
- 验收剧本 `acceptance_dialogue_30.json` 不改动。修正版写在单独的 `acceptance_dialogue_30.rev2.json`，绑定原剧本哈希，
  只能改未完成轮次的 `time` / `advance_hours`；`--plan-revision auto`（默认）使用匹配的最新修正版，`none` 用原剧本。
  保留的测试目录会检查原剧本哈希。每个已完成的轮次都记录了用哪个版本跑的（`plan_revision`），状态里记录了每个修正版的
  文件哈希；继续测试时逐轮核对（Phase 7 B.7.1）：当前修正版改动的已完成轮次，必须是用同一个修正版、同一个文件哈希跑的；
  用别的版本跑过的轮次，两个版本都不能改动它；没有版本记录的旧轮次，当前修正版不能改动它。已完成轮次还必须从第 1 轮起
  连续、不重复。`--seed-from` 复制的目录和直接 `--workdir` 续跑用同一套检查。
  修正版 2：第 15 轮在第 4 轮所说“下周三”的第二天上午 10 点（答辩日期之后），不再是第 14 轮之后 30 小时。
- 第 11 轮“不知道在哪”和第 13 轮“此刻所在：家里”按本轮 Chat 上下文里的情境判断：第 11 轮要求上下文里没有当前位置
  （只有活动情境也算通过）；第 13 轮要求上下文里有当前位置，值为“家里 / 在家 / 家中 / 在家里 / 回家了 / 老家”之一，
  并且能追溯到本轮之前某一轮的记忆整理；真实测试中必须是真实 MemoryAgent 写入的。聊天记录里出现“家”字不算。
  回复里提到的地点（宿舍、学校、家……）列在 `places_in_reply`，供人工评分参考。

### 复制测试目录与续跑回放（Phase 7 B.6.2）

- `--seed-from <已保存的测试目录>`：把它的 `violet-test.db` 和 `.violet-eval-state.json` **只读复制**到一个还不存在的
  `--workdir`，再按续跑规则继续。源目录只被读取：复制前后核对哈希；写入日志非空的数据库拒绝复制（可能缺数据）；
  检查源目录时以不可变方式只读打开，不会在源目录里生成 `-wal` / `-shm` 文件。
- `--continue-replay <报告> --continue-replay-sha256 <哈希>`（必须同时给 `--seed-from`）：用“接着这个目录跑的真实报告”
  离线重建紧接着的轮次，例如目录完成到第 13 轮、`--rounds 15`，就回放第 14 轮。拒绝运行的情况：
  - 报告哈希与给定的不一致（报告被改动过），或没有给哈希；
  - 报告的 `completed_rounds_before` 与目录的轮次不同，剧本或修正版哈希不同，或第一轮回放的场景时间与目录的
    `clock_end` 相差超过 1 毫秒（不是同一条时间线）；
  - 用户原话与剧本不同，轮次带主动消息检查或时间推进，或包含 `save_memory`（含 needs_decision → 新建）以外的写入；
  - 新建的记忆在之后的轮次里被改动过（无法确认它当时的样子）；
  - 目录里已经有这一轮（不会重复回放）。
- 回放时核对：相似候选记忆的内容和进展与报告起点一致，下一条事件 id 与报告一致，没有未完成的记忆任务；
  Violet 回复逐字一致，每次记忆写入的结果（needs_decision 的候选、新建的 id）与报告一致；新建记忆的内容、类型、进展、日期一致；
  除了这一轮的消息和记忆以外，档案、情境、提醒、生活事件、了解问题的数量都不变；结束后没有未完成的后台任务。
  场景时钟停在报告记录的下一轮开始时刻（跳跃之前）。
- 任何一项不一致都停止（退出码 4），状态里的续跑回放标为 `failed`，这个目录不能再继续测试；没有任何计费调用。
- 报告里没有的元数据（save_memory 的类别、重要度、情绪权重、具体时刻，消息和任务的精确时间）列在 `not_restored`。
- `--replay-only`：只回放、保存状态，不运行 `--rounds` 里的轮次。准备一次基准目录，之后每次真实复测都用
  `--seed-from <基准目录>` 复制一份新的，互不影响。

### 离线 Mock 图片（Phase 7 B.8.1，`--image-mock`）

默认图片功能关闭（`VIOLET_IMAGE_ENABLED=0`，测试用微信替身拒绝发图），不变。`--image-mock` 打开图片，但图片链路完全离线：

- **只有 Mock 图片模型**：在测试目录写 `image_providers.mock.json`，只含一个离线 `mock/mock-image-1`（生成纯色 PNG，
  不需要 Key）。启动时检查原始文件和产品注册表的读取结果：任何非 mock 适配器、`base_url`、`api_key_env`、
  返回 URL 的输出都会被拒绝（退出码 2，不留下目录）。
- **程序强制，不靠提示词**：本次运行里，除 mock 以外的图片适配器都换成直接拒绝的替身；图片适配器的 HTTP 传输层拒绝一切请求；
  另有 socket 级拦截，只允许连接文字模型接口（`--real` 时为 `--api-url` 的地址），以及 asyncio 内部的本机自连接；
  其他连接（包括本机上的其他服务）一律拒绝并记录。报告里的 `network_guard` 列出放行和拦截的连接。
- **测试用发送器**：发图时把文件复制到 `<测试目录>/sent_images/`，并在 `sent_images.jsonl` 记录轮次、文件、字节数和
  sha256；不会连接微信。发送失败时没有记录，outbox 中为 failed。
- **每轮的 `images`** 记录图片任务、调用、生成的图片和发送情况（只含 id、状态码、哈希，不含提示词）。
  **`image_checks`** 把三件事分开判断：
  1. 模型是否调用了 `create_image`；
  2. Mock 是否成功生成了 AI 图片（任务成功、图片可用）；
  3. 测试发送器是否记录了同一张图（outbox 为 sent，且 sha256 一致）。
  
  剧本里的 `tools_has` / `tools_lacks` / `db: violet_picture_sent` 按这三件事判断；只调用了工具不算发出了图。
- 紧跟图片轮次的那一轮（第 22 轮）会检查前提：之前确实发出过 AI 图片（按数据库判断，续跑和复制目录同样适用）。
  没有发出过，这一轮的图片真实性验收标为无效（`valid: false`），并列在汇总的 `image_rounds_invalid` 里。
- 回复的证据标记：把 AI 图片说成照片、拍的或摄像头拍的（`claims_photo_or_camera`）；没有发出图片却说“给你画了 / 看这张”
  （`claims_picture_without_one_sent`）。产品自己的回复守卫会先删掉或改写“说成照片”的句子，漏网的才会在这里标出。
- **验证不了的**：Mock 图是纯色块，图片内容本身（画面是否像读书、画风、与人设是否一致）无法验证；真实生图服务的协议、
  费用和效果都**需要生产环境验证**。

### 剧本检查的判定（Phase 7 B.9.1）

剧本里的 `tools_has` / `tools_lacks` / `db` 检查，每一项都记录在当轮的 `checks` 里，结果是下面四种之一：

| 结果 | 含义 |
|---|---|
| `PASS` / `FAIL` | 正常判定 |
| `NOT_APPLICABLE` | 这项功能在本次运行中关闭，**未测试**：没加 `--image-mock` 时的图片检查；网页工具（测试工具没有接入 web MCP）。附带原因 |
| `UNSUPPORTED` | 测试工具判断不了：未知的工具或未实现的 `db` 检查。会明确列出，不会静默跳过 |

- 工具按类别区分：图片（`create_image` / `edit_image` / `look_at_image`）、网页（`violet-web__*`）、其他。只有图片类的结果进入
  `image_checks` 和汇总的 `image_check_failures`；其他类别的失败进入 `check_failures`。
- `db: commitment_created`（第 26 轮）：这一轮新建的提醒（不算以前已有的），状态为 pending；内容与剧本的提醒事项一致；
  证据按产品自己的规则（`check_explicit_request`）必须是用户原话里的明确请求；到期时间按剧本（`明天` + `08:00`）以当轮场景
  日期、Violet 的时区计算，必须是同一时刻。任何一项不满足都判 FAIL，并写明原因。
- 汇总：`check_failures`、`untested_checks`（NOT_APPLICABLE / UNSUPPORTED）、`image_rounds_not_applicable`。
  有未测试的检查时，结论最多是 `pass_with_untested_checks`，不会是 `pass`。

### 执行是否完整、预算预留、到期提醒（Phase 7 B.10.1）

**“轮次执行过”和“后台任务都完成了”是两件事。** 每轮另外记录：

- `memory_jobs`：本轮记忆任务的最终状态、尝试次数、错误类型（不含原文以外的内容，Key 会被替换）；
- `background_complete`：本轮的记忆任务是否都是 `done`；
- `execution_errors`：API 错误以外、没有执行完的事情——没完成的记忆任务、被预算拒绝的调用（从未发出）、处理失败的用户消息。

有 `execution_errors` 时，即使 API 错误为 0、分数满分，汇总结论也是 `fail_execution_incomplete`；
汇总里另有 `execution_errors` 和 `rounds_with_unfinished_background_work`。状态文件里每轮也记下 `background_complete`。

**被预算拒绝的记忆任务不再立即重试**：测试工具的重试间隔是 0，产品的 MemoryWorker 会马上重试两次，但预算拒绝在本次
测试里不可能恢复。测试工具只把这种任务直接标成 failed（错误信息末尾注明 `[test tool: a budget refusal is not retried]`），
产品自己的重试策略不变。

**续跑**：测试目录里有没完成的记忆任务（数据库里 `memory_jobs` 不是 `done`，或状态文件标了 `background_complete: false`）时，
`--seed-from` / 续跑会拒绝。确认接受这个状态，加 `--accept-incomplete-memory`，报告的 `continuation` 里会列出被接受的任务。

**预算预留**：每次真实调用之前检查“已用 token（服务商报告的 usage）+ 这次调用的预留”是否超过 `--max-tokens`。
预留是估算：同一个 Agent 以前最大的一次调用 × 1.1；这个 Agent 还没调用过就取所有调用里最大的一次 × 1.1；
本次还没有任何调用时取 16,000。超过就拒绝这次调用并停止本次测试。报告的 `budget` 里分开记录：

| 字段 | 含义 |
|---|---|
| `used_tokens` / `remaining_tokens` | 服务商报告的真实用量和剩余（**不含**任何估算，usage 原样记录） |
| `stop_detail` | 停止原因、当时的已用量、剩余量、预留量和预留依据 |
| `refused_calls` | 被拒绝的调用（Agent、轮次、原因）：都没有发出，不计费 |

**限制**：上游服务没有“每个请求最多用多少 token”的限制，所以调用前检查只能做到“不在明知会超时发起调用”。
如果某一次调用比预留的大，累计用量仍可能略超上限。

**第 28 轮的 `allowed_or_skipped_without_bypass`**：开始前那次调度检查的结果分成两部分：

- `reminders`：检查前已经到期的提醒，每条记录 `commitment_id`、提醒事件、发件记录和 `delivery`：
  - `sent`：提醒状态 sent、对应的 `commitment_reminder` 事件 sent、该事件的发件记录 sent 且提到了提醒的事；
  - `postponed_quiet_hours`：在静默时段，按产品规则推迟；
  - `not_sent` / `sent_without_matching_message`：没有成功提醒。**普通主动消息不算提醒成功**，哪怕它提到了同一件事。
  - `late_minutes` / `late_cause`：比到期时间晚了多久。场景时钟一次推进跨过了到期时间时标注“测试时间跳跃导致的延迟”
    （测试工具不运行后台调度线程，提醒只在这次检查里处理；生产环境里调度线程每分钟检查一次）。
- `chat_proactive`：普通主动聊天的事件和发出的消息，与提醒分开。

判定：普通主动消息没有经过调度闸门就发出、提醒在静默时段发出、到期提醒没有成功发出（推迟到静默时段结束的除外），都算未通过。
测试只发到本地模拟微信，不会发到真实微信。

## 5. 模型兼容性检查（`tools.model_compat`）

逐项检查：基本文本回复、`temperature`、`max_tokens` 与 `max_completion_tokens`、`reasoning_effort`、tool_calls、
工具结果回传（多轮工具调用）、`response_format=json_object`、只靠提示词的 JSON 输出、未知模型的 4xx。
错误按 Violet 的重试规则分类：`rate_limited` / `server_error` / `timeout` / `transport` 会重试，`bad_request` 不重试。
429 / 500 在真实接口上无法安全地主动触发，这部分用模拟服务商离线验证；`--include-timeout` 会在真实模式下多发一个超时请求。

结果只根据接口的实际响应判断，不看模型名。结果会给出 `config/model_params.json` 的建议条目；
只有加 `--write-policy`、并且是真实检查、并且再输入一次 `yes` 时才会写入（带 `_verified` 记录日期和报告名）。
不会修改服务商或中转服务的任何配置。

## 5b. 减少冗余模型调用的开关（Phase 7 B.2）

四个开关默认都是 `1`，可以在 `--config` 文件里设为 `0` 做对比（例如同一组轮次分别跑一次）：

| 开关 | 作用 | 报告里怎么看 |
|---|---|---|
| `VIOLET_INTENT_HIDE_EMPTY_MEMORY_TOOL` | 记忆库里没有任何 `query_memory` 能查到的事件（已删除、被用户禁用的不算；过期的仍算）时，不给 IntentAgent 提供这个工具 | Intent 调用的 `tools` 为 0、每轮 Intent 只调用 1 次 |
| `VIOLET_MEMORY_FINISH_SIGNAL` | 后台记忆整理任务里提供 `memory_done`：模型在同一步里写入并调用它、且所有写入都返回明确的成功结果时，直接结束，不再请求收尾回复 | `runs` 里 MemoryAgent 的 `finished_after_tools: true`、`steps` 少 1 |
| `VIOLET_MEMORY_HIDE_EMPTY_QUERY_TOOL` | （Phase 9.4，cys-p94-sched-r1）记忆库里一条事件记忆都没有（被用户禁用的也算有，因为 MemoryAgent 查询时会看到它们、避免重存）时，不给 MemoryAgent 提供 `query_memory`，并告诉它不会有重复、直接决定；有任何事件记忆时照常提供、照常提示先查 | MemoryAgent 第一次调用里没有 `query_memory`、少一轮调用 |
| `VIOLET_CONTEXT_HIDE_LEGACY_NICKNAME` | 已有确认的称呼档案时，Chat 上下文不再显示旧核心记忆里的称呼（只影响显示，不改数据） | Chat 上下文的 `legacy_core` 段变短 |

`memory_done` 是否被真实模型使用，只能用真实模型验证；模型不调用它时行为和以前完全一样（不节省，也不丢写入）。

报告的 `memory_jobs` 里每个任务还有 `outcome`（Phase 9.4 r1）：`complete`（模型做完了，status=done）、`partial`（写进去了一部分，之后中断；`writes` 列出每次尝试真正成功的写入）、`interrupted`（尝试过、什么都没写、没做完）、`unprocessed`（还没尝试）；`budget_interrupted: true` 表示最后一次是测试工具的预算拒绝（测试工具不重试它，`budget_not_retried`）。`status=failed` 不再等于“什么都没记下”，要看 `outcome` 和 `writes`。

## 6. 本地无法模拟的微信行为

以下内容只能在真实微信环境里验证（**需要生产环境验证**）：

- iLink 长轮询的真实节奏、重复投递、`get_updates_buf` 的续传和登录过期；
- 用户连续发多条时真实的防抖合并效果（测试工具的防抖缩短到 0.05–0.1 秒）；
- “正在输入”提示、模拟打字延迟的观感（测试中关闭）；
- 微信消息长度限制、分条发送的显示效果、表情和特殊字符的渲染；
- 图片、语音、文件的收发与 CDN 加解密（测试工具不处理媒体，图片功能关闭）；
- `send_image` 的真实可用性；
- 主动消息在真实时间里的节奏（测试中靠 `/advance` 和 `/proactive` 模拟，不是后台线程）；
- 手机 MCP、Web MCP、真实 MCP 服务器的延迟和故障；
- 用户的真实回复习惯（测试用户是虚构的，按剧本说话）。

## 7. B.11.2 / B.11.3 第 27–28 轮合成种子专项

这是 **synthetic 专项测试**，不是原第 1–26 轮数据库恢复，不是原连续验收，也不计入原 32 轮总分。

在 `Violet` 目录运行，指定一个尚不存在、位于工作区 `local/` 内且不在 Windows TEMP 下的持久化目录：

```powershell
..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --workdir D:/unity/violet/local/b112/my-new-run
```

默认离线；使用现有 VioletTestSession、ScenarioClock、本地微信替身、真实 MemoryAgent 工具处理器和提醒调度代码。
默认所有外部网络及本机服务连接均被 NetworkGuard 拒绝。已存在的目录会在写入前被拒绝，运行失败也保留目录。

只读来源为 `20261014-001147`、`20261014-002003`、`20261013-073654` 三份历史 `report.json`：

- `seed.json`：5 项档案、3 项事件、第 24–26 轮用户和 Violet 原话、pending 交作业提醒及来源哈希/JSON 指针。
  每个状态字段标记为 `report`、`prior_observation`、`inferred` 或 `synthetic`；原始报告没有的数据列入 unknown，值为 null。
- 报告的结束时间是 `2026-10-22T16:13:52.562000+08:00`；种子按本阶段要求截到 `16:13:52`，标为 inferred，原精确值另行保留。
  “少追问”的值由第 19 轮原话保守推断为 `less`；报告只保存了写入动作，没有完整偏好行，不能声称精确恢复。
- `seed_materialized.json`：新数据库五类种子表的逐字段值及来源。新 ID、默认权重、证据/审计和写入时间等为 synthetic；
  Violet 回复的原发送时刻未知，采用对应轮次开始时间的近似记录明确标为 inferred。不恢复 Violet 生活、不把过期位置当作当前位置。
- `.violet-synthetic.json`：独立范围标记。原 replay、`--seed-from`、连续验收、summarize、recheck 和原评分导入均拒绝该范围的数据。
  不写 `.violet-eval-state.json`，不伪造“原 1–26 轮已恢复”。
- `specialized_report.json`：第 27 轮模拟简短收尾及对长期偏好误写的对抗探针；第 28 轮先推进 20 小时，再处理提醒、重复检查、用户“早”的回复及静默控制探针。
  `proactive_chat=no` 是独立的 synthetic 控制条件，不是历史偏好，也不是第 27 轮的写入结果。
  所有探针都只作用于新数据库和测试目录配置。
- 第 28 轮检查 commitment、proactive event、outbox 三方 ID/状态及提醒内容；场景跳到次日约 `12:13:52` 才调用调度，
  因而晚于 `08:00` 约 253 分钟。报告标注“测试时间跳跃导致的延迟”，**没有验证现实环境按时送达**。
- `specialized_scores.csv`：保留第 27、28 轮人工评分空栏。请复制为新的专项评分文件再填写，保留原始证据；
  模拟台词通过字面检查不代表真实模型会自然收尾，必须人工评估，也不能把专项评分导入原总分。

离线回归：`run_tests.ps1 -q tests/test_synthetic_rounds_b112.py tests/test_reminder_b101.py`。
变异测试只能在独立 worktree 内串行运行；方法与本阶段证据位置见 `docs/knowledge/testing.md` 的 B.11.2 节。
### B.11.3 受控真实文字模型路径

必须同时具备 `--real`、当前 shell 的 `VIOLET_TEST_ALLOW_REAL_API=1`、文字接口地址、测试 Key 和交互输入 `yes`。
复用现有 RealApiGate、VioletTestSession 的 OpenAI 客户端、BudgetedClient 和 NetworkGuard，没有第二套客户端。
Key 通过隐藏输入或现有测试环境变量提供，不写配置文件；本入口不显示 Key 的前后缀，也没有 `--yes` 确认绕过选项。
接口 URL 禁止用户名、密码、query 和 fragment。仅允许配置接口解析出的 IP/端口连接；使用代理时代理连接也会被拒绝。
微信始终为本地替身，图片、MCP、Web 和后台生活线程关闭；真实文字模型不代表真实微信送达。

真实路径由模型实际运行 Intent / Chat / Memory，不注入模拟 MemoryScript；第27轮保留逐Agent调用数和人工评分入口。
第28轮提醒先进入真实调度流程；模型生成无效而退回模板，或任何模型调用失败，即使本地发送成功，专项也判失败。
`stages` 分别记录 chat、reminder_due、repeat_check、user_return 和静默探针；调用标注实际轮次和阶段，
`calls`、逐轮 `calls`、`accounting` 和 `call_summary` 总数必须一致。提醒生成的 Chat 调用计入第28轮，
调度和用户返回共享该轮预算，不会因切换阶段重置。`budget_reservations` 单列每次调用的预留和批准/拒绝；
`budget.refused_calls` 保存未发出的请求。预算停止和模型失败分别记录为 `stopped_budget` / `failed_model_call`，不会标通过。

默认上限：每轮10次、全程16次、150000 Token；首次预留16000，随后复用既有最大调用量×1.1规则。
预留是估算，上游单次响应可能超过预留；真实使用量依供应商usage，缺失不伪造。可用 `--price-per-1k` 与 `--max-cost` 增加费用门槛。
预计6–12次调用、约4万–10万Token只是规划范围；参考历史两份报告分别16次/134621Token、5次/44444Token，
但合成上下文不同，实际依工具轮数、重试、模型和提示词而变，不能当报价或硬上限。

以下命令仅供用户审核后手动运行；B.11.3开发阶段**未执行真实调用**。每次使用全新目录，启动后隐藏输入Key并确认预算：

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host '文字模型 API 基础地址'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$specializedDir = 'D:\unity\violet\local\b113\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --workdir $specializedDir --max-calls-per-round 10 --max-calls 16 --max-tokens 150000
```

该config必须仅含工具允许的模型设置，不能含Key；没有合适文件时可省略 `--config` 使用env_example模型设置，先核对门禁显示的模型。
专项报告仍带 synthetic / specialized_test / 非连续标记，不能导入原32轮验收；人工评分另存副本，不改原证据。
真实模型自然度、实际Token/费用、真实微信与现实准时送达均需各自验证，不能从离线fake SDK结果推断。

## 8. B.11.5 第29–32轮独立合成专项

使用同一受控runner，新增 `--suite round29_32_specialized_test`；省略suite仍运行原27–28专项。
默认完全离线；Intent查询、Memory更正、ContextEngine、后台任务、SQLite和本地发送均走产品代码，只有模型响应使用模拟。
真实路径仍通过B.11.3同一RealApiGate、确认、预算、网络白名单和脱敏客户端，没有新API客户端。

```powershell
Set-Location 'D:\unity\violet\Violet'
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --workdir 'D:\unity\violet\local\b115\my-new-mock' --max-calls 32 --max-tokens 300000
```

所有目录必须全新、持久化且位于工作区local内。失败保留证据，不覆盖旧报告或种子。
种子来源：B.11.2使用的三份历史报告，以及 `local/b113/real-20261019-014631/specialized_report.json` 中的提醒已发送证据。
源报告和原剧本/rev2不修改、不复制数据库；种子逐字段列路径、哈希和JSON指针。
已发送提醒的本地ID、事件/发件关联、默认值和新审计字段为synthetic；旧元数据、完整偏好/对话/生活和原29–32时间为unknown。
少追问值less继续标inferred；不导入B.11.3的proactive_chat=no、全天静默、静默探针或近期答辩答案。
锚点采用已有提醒发送时刻 `2026-10-23T12:15:12.640000+08:00`，标inferred并说明原29轮时间未知；不额外设置日期或跳时。
“昨天交了作业”原话照读，不擅自为它跳一天。无Eve生活预载。

- R29：不预置作业已交；验证不重建提醒、不重复发送/事件/发件箱。调度去重明确标为附加探针，不是改写原剧本的调度步骤。
- R30：预置报告中done的答辩；无近期对话种子。检查Intent实际query_memory、返回目标ID/内容/done，以及Chat实际对应事件行；通用“已结束”提示语不能替代。
- R31→32：只预置旧专业天文；等待真实MemoryAgent处理完成，检查物理新事实、旧事实superseded和双向替代ID。同一数据库直接进入32轮，核对实际上下文fact_ids和答案。31失败时32不执行，不允许提前种物理。
- 报告保留原剧本user/rubric、逐轮状态、来源、调用/阶段/预算及人工评分空栏。回复字面检查仍有局限，人工自然度和原32轮累计指标不由自动结果证明。

新purpose即使独立出现也被原计划/报告读取、replay、状态续跑、seed复制、summary/recheck和评分导入拒绝；不能计入原32轮，也不恢复原连续数据库。
独立worktree不会包含被忽略的报告，复跑离线/变异时须把上述四份报告逐字节复制到worktree全新路径并核对哈希；不能生成假历史替代。

以下真实命令仅供用户审核后手动执行；本阶段**未执行**。已知模型设置文件不得含Key，门禁显示模型后由用户核对；Key隐藏输入并输入yes确认：

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host '文字模型 API 基础地址'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$tailDir = 'D:\unity\violet\local\b115\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --workdir $tailDir --max-calls-per-round 10 --max-calls 32 --max-tokens 300000
```

预计真实四轮14–20次、约12万–18万Token，仅估算；依据此前27–28真实专项8次/66905Token，R30增加查询、R31可能增加工具回合。
命令上限32次、每轮10次、300000Token；预留规则沿用原机制，单次响应仍可能超过估算预留。价格未知，不提供虚构金额；可按实际单价另加费用上限。
真实文字模型不验证微信或现实准时送达。测试记录与方法见知识库testing.md §8。

### B.11.6 失败后的复验边界

B.11.6修复自然回忆问句在query_memory入口的词面匹配，并将R30模拟查询改为完整原用户问句；未降低专项断言。
用户已有的R29真实通过报告应保留，后续可设计仅R30、31→32的独立复验；31→32必须沿用同一数据库并等待更正任务完成。
**B.11.6时CLI尚无轮次选择或失败续跑参数**，不能把上述四轮命令当成“只测30–32”。B.11.7新增受限选轮，见§9；仍不支持读取旧数据库续跑。
新目录中从可追溯种子开始，或从失败库的专用副本开始，必须明确两者区别并披露选择；不能修改/续写原失败库，也不能把专项纳入连续验收。模型实际query/category未保存在原报告，精确触发参数仍unknown；离线修复不代表真实模型复验已通过。

## 9. B.11.7 受控选轮复验

新增 `--rounds 30-32`，仅适用于round29_32_specialized_test。默认或29-32仍执行四轮；不接受31-32、32等其他选择，避免绕过前置验证。
新目录从既有合成种子启动；不读取旧数据库，不导入29轮对话/记忆写入，不预置物理。29本次未执行，报告单列历史真实通过来源及哈希/指针；缺失该证据时拒绝启动。需要保留第五份只读源报告 `local/b115/real-20261019-041807/specialized_report.json`，另四份来源同§8。
30失败停止后续；31失败不能执行32，31→32使用同一数据库。报告只给选择轮次评分栏，调用账本只统计实际调用，不把历史29的调用/分数算入本次。
memory_query_audit记录实际工具参数、实际数据库查询主题与过滤范围、返回事件ID/数量/progress；chat_context_audit在Chat请求边界记录目标是否进入实际事件行。参数经凭据脱敏，不从断言回填；空查询/异常不虚构搜索结果。审计可能含专项对话内容，仅保存在本地证据中；普通工具报告仍只保留摘要。

离线运行：

```powershell
Set-Location 'D:\unity\violet\Violet'
$offlineDir = 'D:\unity\violet\local\b117\mock-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --rounds 30-32 --workdir $offlineDir --max-calls-per-round 10 --max-calls 24 --max-tokens 250000
```

以下真实命令仅供用户另行授权后手动执行；B.11.7未执行。仍须隐藏输入测试Key并输入yes确认：

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host '文字模型 API 基础地址'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$selectedDir = 'D:\unity\violet\local\b117\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --rounds 30-32 --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --workdir $selectedDir --max-calls-per-round 10 --max-calls 24 --max-tokens 250000
```

预计30–32真实复验10–16次、约8万–14万Token：历史30实际5次/43172Token，31–32尚未真实执行，估算存在不确定性。命令上限24次/每轮10次/250000Token，沿用首次16000和后续预留规则；实际账单取决于usage与单价，不保证模型自然度或真实微信送达。必须全新目录，不覆盖历史证据。

## 10. B.11.8 扩展查询修复后的最低成本复验

本阶段不执行真实API。先以B117真实审计中的两条完整查询作离线回归，不能以短词查询代替：

```powershell
Set-Location 'D:\unity\violet\Violet'
.\run_tests.ps1 -q tests/test_expanded_recall_b118.py
```

本地证据 `local/b118/20261019-codex/exact-queries-mock/specialized_report.json` 为模拟30–32，不是真实模型通过；其中11次模拟调用、两个真实参数均返回ID1/done并进入实际Chat事件行。原失败证据 `local/b117/real-20261019-053449` 保留。
审计的normalized_query仍是传入数据库的实际外壳归一化查询；返回事件的retrieval记录topics/focus及original_similarity/topic_similarity/similarity，分别表示字面主题、原字符分数、主题分数和合并分数，不把期望答案当输入。
模型扩展词不再压低已经匹配的主要主题；复合词不任意拆成前缀，零/负阈值也不能返回无字面证据的全部记录。仅查询路径启用，写入去重、阈值、候选范围和权限规则不变。Intent固定0.35，Memory使用实际传入阈值，schema已澄清。
这是轻量词法检索，不保证无字面共享同义词、单个复合词内的无依据限定、错误首主题或同主题多事件的语义消歧。共同后缀匹配是候选扩展，不能证明修饰词或事件身份；expired仍带旧状态标签，disabled/deleted不增加上下文权限。
审核并另行授权后，最低成本的现有真实入口仍是§9的30–32命令，使用新的 `local/b118/real-<时间戳>` 目录；不再付费执行29。30失败即停止31–32，30通过后才继续同库更正。当前不支持只选30，不能用预算截断伪装完整通过。
依据本次30失败5调用/42896Token，30成功仍需真实模型核验；完整30–32预估约10–16调用/8万–14万Token，仅估算，retrieval元数据会增加少量上下文。保持24次/每轮10次/250000Token上限及原门禁、预留规则，实际按供应商usage记账。没有新增付费模型或额外检索API。

## 11. B.11.9 第30轮回复断言与事后离线复评

第30轮文字断言为 `round30.answer_recalls_outcome/v2`（`tools/synthetic_tail.py` 的 `recalls_completed_outcome`），结构检查与运行时共用 `round30_checks`；报告第30轮记录 `answer_assertion`。根因与边界见 `docs/knowledge/bugs-and-risks.md` B.11.9。

对已有真实运行做事后离线复评（不调用模型、不改原目录，输出目录必须不存在）：

```powershell
Set-Location 'D:\unity\violet\Violet'
& ..\.venv\Scripts\python.exe -X utf8 -m tools.reassess_tail --source 'D:\unity\violet\local\b118\real-20261019-060349' --out ('D:\unity\violet\local\b119\reassess-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
```

输出 `round30_reassessment.json`：kind=post_hoc_offline_reassessment、model_calls=0、源7文件SHA-256、v1/v2判定差异、violet_head及tools是否未提交。证据缺失、调用失败、非真实运行或v1无法复现原记录时为“无法验证”。31–32保持“未执行”，原报告分数和人工评分不变。

## 12. B.11.10 第31–32轮独立验收入口

`--rounds 31-32` 仅用于 `round29_32_specialized_test`。第29、30轮不调用模型，报告 `skipped_rounds` 标“未执行”并引用历史证据；不接受任何人工PASS参数。建目录前由 `tools/synthetic_tail.py` 的 `round30_evidence` 核验，任一不符即拒绝并列出原因：

- `local/b118/real-20261019-060349/specialized_report.json` 与 `local/b119/reassess-20261019-063152/round30_reassessment.json` 的SHA-256等于代码内登记值；
- 原报告为本专项真实运行，第30轮用户输入与原剧本一致、调用全部成功、结构检查全部通过、唯一失败项为v1文字断言；
- 复评为post-hoc离线复评（0模型调用）、使用 `round30.answer_recalls_outcome/v2`、代码已提交、来源与登记报告一致、差异仅answer_recalls_outcome false→true、未复评31–32；
- 当前v2代码现场复评同一报告，结果与登记复评一致。

第29轮沿用B.11.7的历史证据核验。31–32从全新合成库开始（专业仅预置旧值天文，无对话历史，不复用旧库）；31失败即停止，32使用31执行后的同一数据库。调用/Token只计31–32；仍不计原32轮。

离线运行：

```powershell
Set-Location 'D:\unity\violet\Violet'
$offlineDir = 'D:\unity\violet\local\b1110\mock-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --rounds 31-32 --workdir $offlineDir --max-calls-per-round 10 --max-calls 16 --max-tokens 160000
```

真实命令仅供用户另行授权后手动执行（B.11.10未执行）；隐藏输入测试Key并输入yes确认：

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host '文字模型 API 基础地址'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$selectedDir = 'D:\unity\violet\local\b1110\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& ..\.venv\Scripts\python.exe -X utf8 -m tools.synthetic_rounds --suite round29_32_specialized_test --rounds 31-32 --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --workdir $selectedDir --max-calls-per-round 10 --max-calls 16 --max-tokens 160000
```

预估（仅估算）：31约4–6次/3.5万–5.5万Token（Intent 1–2、Chat 1、Memory 2–3），32约3–6次/2.5万–4.5万Token；合计约7–12次、6万–10万Token。依据B.11.8/B.11.9真实单次规模：Intent约7.7k、Chat约6k、Memory约10.7k。31–32从未真实执行，实际以供应商usage为准；上限16次/每轮10/160000Token，沿用首次16000与后续预留规则。
## 13. Phase 8.1 聊天体验评估（`tools/chat_eval.py`，完全离线）

评估集 `tests/samples/chat_eval/scenarios.p81.v1.json` 由 `build` 生成后冻结。它包含两部分：
- 33 个真实场景（`REAL_SELECTION`），只摘录报告原文，每个来源都记录 SHA-256；
- 18 个合成探针（`probes.p81.v1.json`，人工编写，`kind=synthetic`，没有参考回复）。

历史来源逐条标注：`same_report`、`replay_source:<报告>`、`specialized_seed`、`same_report:proactive_reminder`、`plan_user_line`（只有剧本里的用户原话）、`unknown_lost_test_directory`（Violet 回复未知，保持为空）。未知信息不补写，统一列入 `missing`。要改评估集，须新建版本文件，不覆盖 v1。

```powershell
Set-Location 'D:\unity\violet\Violet'
$py = '..\.venv\Scripts\python.exe'
& $py -X utf8 -m tools.chat_eval verify                              # 重新核对所有来源哈希
$run = 'D:\unity\violet\local\p81\blind-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_eval blind --variant historical_real --variant <新变体.json> --seed 20261019 --out $run
#   评分人只拿到 $run\sheet.md（阅读）与 $run\sheet.csv（填写）；key.json 由组织者保管
& $py -X utf8 -m tools.chat_eval score --sheet <填好的.csv> --key $run\key.json --out $run\result.json
& $py -X utf8 -m tools.chat_eval diagnose --variant historical_real --variant <新变体.json> --out $run\diagnostics.json
```

- **变体文件**：`{"variant","set_sha256","environment":"real_model|fake_sdk_offline|mock|manual","model","prompt_sha256","generated_at","replies":{"R08":[["第一条","第二条"]]}}`。`historical_real` 内置，取自各真实场景的原回复；合成探针没有基线，只在新变体之间比较。
- **盲评**：场景顺序和候选顺序按 seed 随机；表中不出现变体名和环境；每个候选记录文本哈希，评分后改动过文本的行会被拒绝。
- **评分**：每个维度打 0/1/2；`self`、`intimacy`、`respect` 不适用时填 `na`。每行必须写理由；`grounded<2` 时必须在 `grounded_evidence` 里引用原句。`rank` 在同一题内为 1..k。结果按变体和场景标签汇总，保留全部理由和证据，并记录 sheet 与 key 的哈希。
- **诊断**：`diagnose` 输出长度、与用户消息的长度比、称呼率、问句结尾率、“又”字数、重复 4 字片段，并标记 `diagnostic_only`。这些数字只用来发现回退，不作优化目标。

| 维度 | 2 | 1 | 0 |
|---|---|---|---|
| natural 日常自然 | 像熟人随手回的微信 | 能看懂，但有客服、总结或模板感 | 明显 AI 腔，或答非所问 |
| context 上下文 | 接住当前话题、时间与关系 | 理解了，但忽略了关键线索 | 误解话题，或把旧事当成现在 |
| grounded 有依据 | 没有无依据的内容 | 轻微臆测，比如没依据的“又……” | 编造经历、理由，或用户没说过的话 |
| rhythm 节奏 | 长短、条数与对方匹配 | 略长或略生硬 | 简单的话回成长篇或清单 |
| self 自己 | 有自己的看法、情绪或生活（且有依据） | 有一点 | 只围着用户转（不适用填 na） |
| intimacy 亲近 | 撒娇、亲近合乎关系和时机 | 稍过或稍冷 | 过早亲昵、施压或冷淡（不适用填 na） |
| respect 偏好边界 | 遵守已知偏好和边界 | 小幅偏离 | 违背明确偏好（不适用填 na） |
## 14. Phase 8.3 ChatAgent A/B 回放（`tools/chat_ab.py`）

同一个冻结场景分别用 v0（chat.v0.txt + persona_core.v1.0.0.json，后者由 git e86c23b 逐字节提取；人设节标题按当时的 ai/persona.py 还原，只在本工具里替换）和 v1（chat.v1.txt + persona_core.json 1.1.0）生成回复。四个版本文件按 sha256 固定，内容一变工具就拒绝运行。

- **`plan`（离线）**：每个场景在独立子进程和全新测试库中重建状态，渲染 ChatAgent 上下文，再与历史上 ChatAgent 实际看到的内容逐项比较（各节长度，人设节除外；历史消息条数）。
  - 同一次运行中更早的轮次按产品顺序离线回放：构建它们的上下文，以复现 onboarding 记录；只应用报告里有证据的写入（correction_evidence、preference_probe）。
  - Violet 的多行回复按产品规则逐行存成多条消息。
  - 分类：`complete`（完全一致）、`partial`（不一致、缺场景时间或缺轮前档案）、`unfair`（前序 Violet 回复未知）、`synthetic`（人工编写的探针）。只有 `complete` 和 `synthetic` 可以用于 A/B。
- **`run`**：每个“场景 × 版本 × 样本”都用独立子进程和全新测试库；两组用同一份状态规格、同一模型、同样的采样参数和预算。
  - 经产品 ChatAgent 生成，再经一致性守卫，最后按产品规则拆行。
  - 输出 `variant-v0.json`、`variant-v1.json`，可直接交给 chat_eval，并附逐条生成的元数据：提示词与人设哈希、上下文指纹、调用数、Token、守卫结果、错误与重试。
  - `run.json` 的 `fairness` 检查同一场景所有样本和两组的上下文指纹（屏蔽“当前时间”，并另报最大时间偏差）。指纹不同时状态为 `context_mismatch`，不能当作公平对比。
- **安全**：默认 mock。`--real` 需要 RealApiGate（VIOLET_TEST_ALLOW_REAL_API=1、`--api-url`、隐藏输入 Key、输入 yes），门禁在建目录之前执行。子进程只有在拿到父进程的一次性确认 token 时才会发起真实调用。网络只允许访问指定接口；预算跨子进程累计，被拒即停止；输出写完后扫描是否含有 Key；所有输出目录都必须是新的。

```powershell
Set-Location 'D:\unity\violet\Violet'
$py = '..\.venv\Scripts\python.exe'
$plan = 'D:\unity\violet\local\p83\plan-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_ab plan --out $plan
$ab = 'D:\unity\violet\local\p83\ab-mock-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_ab run --manifest "$plan\manifest.json" --scenarios R28,R31,P05,P11 --samples 2 --out $ab
& $py -X utf8 -m tools.chat_eval blind --variant "$ab\variant-v0.json" --variant "$ab\variant-v1.json" --seed 20261019 --out "$ab\blind"
```

真实运行在用户授权后手动执行：在上面 `run` 命令后加 `--real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --max-calls <N> --max-tokens <T>`，环境准备与 §9 相同（VIOLET_TEST_ALLOW_REAL_API、NO_PROXY、隐藏输入 Key）。
## 15. Phase 8.4 Chat 上下文写法（`VIOLET_CONTEXT_STYLE`）

- **`natural`（默认）**：
  - 档案节标题为“你了解的对方”，不带来源和日期；称呼一行附“熟了以后偶尔叫就好”。
  - 偏好写成具体做法，例如“不希望被追问：平常的分享直接接话……对方问你问题时照常回应”。
  - 历史消息之间、以及最后一条到现在，按数据库真实时间在间隔达到 `VIOLET_CONTEXT_SESSION_GAP_HOURS`（默认 3 小时）时插入时间标记。
  - 近 48 小时内已发出的提醒标为“已经提醒过了（做没做完不知道，对方没提起就别再催）”。
  - 新增“你们的相处”一节，按聊天记录给出认识时长。
  - 原始 32 轮剧本检查的字面文字（“希望被称呼为：小满”“不希望被追问（用户明确说过）”）保持不变。
- **`legacy`**：与之前各阶段逐字相同。
- **`tools.chat_ab`**：`plan` 固定用 legacy 与历史记录比对；`run --context-style legacy|natural`，默认 legacy，用于复现 8.3。natural 模式只接受合成探针，因为真实场景没有记录消息的发送时间；声明为“熟悉”的探针，在 `fairness` 中标注“相处时长只来自种子历史”。

```powershell
& $py -X utf8 -m tools.chat_ab run --manifest "$plan\manifest.json" --scenarios P01,P03,P05,P08,P11,P14,P17 --samples 2 --context-style natural --out $ab
```

## 16. Phase 8.5 上下文写法 A/B（`chat_ab run --arms`）

比较 legacy 与 natural 两种 ContextEngine 写法。两组用同一份 Chat v1 提示词和人设 1.1.0，场景、用户输入、历史、偏好、模型、采样参数和预算也都相同，只有 `VIOLET_CONTEXT_STYLE` 不同。

- **`--arms v1:legacy,v1:natural`**（代替 `--context-style`，两者不能同时给）：
  - 变体名为 `chat_v1_legacy` / `chat_v1_natural`，文件为 `variant-v1-legacy.json` / `variant-v1-natural.json`。
  - 不传 `--arms` 时，名称和行为与 8.3 相同（`chat_v0`、`chat_v1`）。
  - 一次只允许一个因素不同：提示词版本和写法同时不同会被拒绝。
- **公平性**（`run.json` 的 `fairness`）：两组的上下文按设计不同，所以检查分两部分：
  - 每组内各样本的上下文指纹必须一致；
  - 跨组的系统提示词、人设、状态规格文件、用户本轮消息、模型和温度的哈希必须一致（`shared_inputs_identical`）。
  - 任一项不一致，状态即为 `context_mismatch`；有生成失败时状态为 `has_failed_jobs`。两者都不算完整对比。
- **时间不可伪造**：只要有一组用 natural，真实场景就一律拒绝（它们没有记录发送时间）。
- **声明为“熟悉”的合成探针**：natural 的“你们的相处”一节只按种子历史计算，P14、P17 会被写成“第一次聊天”，P08 会被写成“还不太熟”，这与探针的前提不符。
  - `--familiar-relationship omit`：natural 组对这类探针去掉这一节，不补任何内容。每条生成记录 `relationship_line`，`fairness.limitations` 写明这一处理。
  - 默认值 `product`：保留产品输出，并在 limitations 中标注。
- **记录**：
  - `run.json` 包含 `planned_generations`、`not_run`（预算停止后没有执行的生成）、`failed_jobs`、每次生成的调用数和 Token。
  - 每条生成的 json 保存完整请求、规格哈希、用户消息哈希、上下文报告、错误和网络守卫结果。
  - `rater_focus.md` 是评分关注点，随盲评表一起交给评分人，其中不含版本信息。
- **盲评**：`chat_eval blind --all-samples` 为每个“场景 × 样本序号”出一题，同一题内比较两组的同序号样本，key 记录样本序号。不加这个参数时输出与之前逐字相同（只用样本 0）。

```powershell
Set-Location 'D:\unity\violet\Violet'
$py = '..\.venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$plan = "D:\unity\violet\local\p85\plan-$stamp"
& $py -X utf8 -m tools.chat_ab plan --out $plan --scenarios P03,P08,P14,P17
$ab = "D:\unity\violet\local\p85\ab-mock-$stamp"
& $py -X utf8 -m tools.chat_ab run --manifest "$plan\manifest.json" --scenarios P03,P08,P14,P17 --samples 2 --arms v1:legacy,v1:natural --familiar-relationship omit --out $ab
& $py -X utf8 -m tools.chat_eval blind --variant "$ab\variant-v1-legacy.json" --variant "$ab\variant-v1-natural.json" --all-samples --seed 20261085 --out "$ab\blind"
```

用户授权后的真实运行（16 次生成，预计 16–20 次调用、约 9.7 万–12 万 Token。依据：8.3 真实运行每次约 6000 Token，natural 的上下文多 60 Token 左右。一致性守卫要求重写时，每次会多一次调用）。上限设为 32 次调用、20 万 Token；预算停止时，run.json 的 `not_run` 会列出没有执行的生成。

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host '文字模型 API 基础地址'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$py = '..\.venv\Scripts\python.exe'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$plan = "D:\unity\violet\local\p85\plan-real-$stamp"
& $py -X utf8 -m tools.chat_ab plan --out $plan --scenarios P03,P08,P14,P17
$ab = "D:\unity\violet\local\p85\ab-real-$stamp"
& $py -X utf8 -m tools.chat_ab run --manifest "$plan\manifest.json" --scenarios P03,P08,P14,P17 --samples 2 --arms v1:legacy,v1:natural --familiar-relationship omit --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --max-calls 32 --max-tokens 200000 --out $ab
& $py -X utf8 -m tools.chat_eval blind --variant "$ab\variant-v1-legacy.json" --variant "$ab\variant-v1-natural.json" --all-samples --seed 20261085 --out "$ab\blind"
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API
```

评分人只拿 `$ab\blind\sheet.md`、`sheet.csv` 和 `$ab\rater_focus.md`，`key.json` 留到评分完成后再用 `chat_eval score` 合并。

## 17. Phase 8.7 多轮聊天体验评估（`tools/chat_dialogue.py`）

用固定的合成剧本跑 Violet 的**完整产品流程**，生成可以从头读到尾的聊天记录。每段剧本使用独立子进程和全新测试库。

- **剧本**：`tests/samples/chat_dialogue/episodes.p87.v1.json`（版本化，全部 synthetic）。E1–E8 每段 8–10 句用户台词，覆盖：闲聊与沉默、好消息与坏心情、话题延续（含档案纠错和提醒）、熟悉关系下的调侃与撒娇、Violet 主动表达、用户忙碌、多天后重新联系、少追问偏好与邀请提问。
  - 每段的背景、带日期的历史聊天和 Violet 生活记录都写在剧本里，并标为合成。
  - 用户台词固定，不随 Violet 的回复变化。
  - `validate` 子命令检查剧本格式：时间顺序、历史早于开始时间、每步只能是 user 或 tick、台词数量。
- **执行**（`run`）：
  - 每句台词走 `VioletRuntime`：IntentAgent → ChatAgent → 一致性守卫 → 发件箱；“提醒我…”在回复前保存；MemoryAgent 后台任务完成后才进入下一步。
  - `tick: scheduler` 步骤运行一次调度检查，用于提醒到期和主动消息。
  - 场景时钟按剧本推进，包括时间跳跃。
  - 不连接微信，不读写生产库。
- **两种记忆模式**：
  - `--memory real`：真实模式默认，由 MemoryAgent 自己决定写什么。
  - `--memory scripted`：mock 模式默认（mock 没有记忆模型），用每步的 `memory_script` 通过 MemoryAgent 自己的工具处理器写入。剧本只给 E3（纠正专业）和 E6（忙碌状态）写了脚本。
  - `mock_intent: silent` 只在 mock 下生效，真实模式忽略。
- **变体**：
  - `--label` 是本次运行的名字；可选 `--context-style legacy|natural`、`--chat-prompt-file`（记录文件哈希和 Violet 实际收到的文本哈希）、`--config`。
  - 比较不同代码版本时，在各自的 worktree 里用同一份剧本分别运行，再用 `blind` 合并。
  - `run.json` 记录 git HEAD、未提交的产品文件、产品树哈希、剧本文件和每段剧本的哈希，以及每段实际使用的模型、温度、各 Agent 提示词哈希、人设、生活配置和上下文写法。
- **输出**：
  - `transcript.md`：完整聊天记录，含日期、间隔、谁说的、实际回复、沉默、提醒、主动消息、失败和未执行的步骤；
  - `debug.json`：每步的调用、Chat 的完整请求、意图判断、守卫、记忆任务和状态变化；
  - `diagnostics.json`：问句率、称呼率、重复四字片段、谈自己的次数、沉默次数。只作诊断，不是合格标准；
  - `run.json`：每段和整次运行的状态、调用和 Token、未执行的步骤。
- **状态**：每段剧本的状态为 `complete`、`stopped_budget`、`failed_model_call`、`failed_step`、`failed_execution`、`child_failed`、`episode_mismatch` 或 `not_run`。只有全部为 `complete` 时，整次运行才是 `complete`。
- **重试后恢复**（Phase 8.9.1，工具版本 `p87.v2`）：用户消息这一步里，IntentAgent 或 ChatAgent 的某次调用返回 HTTP 502/503（Phase 9.4 R2 起也包括没有状态码的 `APITimeoutError` / `APIConnectionError`，Violet 自己会重试这两种；500、429 和没有类型的错误仍算失败），但满足以下全部条件时，这一步记为 `recovered`，剧本继续：
  - 同一次处理里后面有一次调用成功；
  - 成功的那次用的是同一个模型（不是备用模型）；
  - 这次处理正常结束；
  - 这一步没有执行错误、没有被预算拒绝，并且有回复，或者由 Intent 决定不回复。

  失败的调用照样全部保留在 `calls` 和 `call_errors` 里（第几次尝试、状态码、延迟、无用量），同样计入调用预算；这一步的 `recovered_calls` 写明是哪次调用、由哪次成功的调用恢复。

  以下情况仍然停止：其他状态码（500、429、超时）、其他 Agent、重试用完、只靠备用模型成功、恢复后既没有回复也没有决定不回复、预算拒绝（`stopped_budget`），以及所有调度检查步骤。

  run.json 有 `recovered_steps` 和 `failed_calls`；transcript 标出每个恢复的步骤（ℹ）；盲评表不显示这些标记。
- **盲评**：
  - `blind --run <目录> [--run <目录>…] --seed N --out <新目录>`：每段剧本一题，候选是各次运行的完整聊天，不显示名字、环境、记忆模式和代码版本。只有每次运行都完成、且剧本哈希相同的段落才会出题。
  - 评分维度 1–5 分：自然度、性格、亲近是否合时宜、无追问说教、上下文延续、诚实、边界。必须写理由，可以按步骤写标记。
  - `score` 校验候选文字没有被改过，再输出各次运行的平均分、各段得分、两两偏好和标记。

```powershell
Set-Location 'D:\unity\violet\Violet'
$py = '..\.venv\Scripts\python.exe'
& $py -X utf8 -m tools.chat_dialogue validate
$run = 'D:\unity\violet\local\p87\mock-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_dialogue run --out $run --label mock
& $py -X utf8 -m tools.chat_dialogue blind --run $run --seed 2026110 --out "$run\blind"
```

**真实运行**（每次都要单独授权）。估算依据是 gpt-6-luna 的历史调用：每次请求有约 4700 Token 的固定部分，Intent 约 7000、Chat 约 6000–7500、Memory 每次约 11000 Token，聊天历史越长，后面的轮次越大。

| 范围 | 调用次数 | Token |
|---|---|---|
| E1、E5、E8，`--memory scripted`（30 句） | 约 60–70 次 | 约 42 万–50 万 |
| 全部 8 段，`--memory scripted`（75 句） | 约 155–170 次 | 约 110 万–125 万 |
| 全部 8 段，`--memory real` | 约 230–380 次 | 约 190 万–350 万 |

```powershell
Set-Location 'D:\unity\violet\Violet'
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host '文字模型 API 基础地址'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$py = '..\.venv\Scripts\python.exe'
$run = 'D:\unity\violet\local\p87\real-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
& $py -X utf8 -m tools.chat_dialogue run --out $run --label baseline --episodes E1,E5,E8 --memory scripted --real --api-url $textEndpoint --config 'D:\unity\violet\local\violet-test-config\gpt6luna.env' --max-calls 90 --max-tokens 700000
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API
```

读 `$run\transcript.md` 就能看完整聊天。之后的版本用同一份剧本、换一个 `--label` 再跑一次，再用 `blind --run <旧目录> --run <新目录>` 生成对比评分表。

## 18. Phase 8.8 互动信号（`ai/interaction.py`）与第一批真实多轮测试

产品行为的改动，除忙碌有效期外只在 natural 写法下生效；legacy 与之前逐字相同。设计见 decisions.md T13。

- **本轮信号**：根据用户这句话的原文判断它属于哪一类，不调用模型：
  - invite：请 Violet 提问；
  - question：用户在提问，包括“是不是”“你觉得”这类句末没有疑问词的问句；
  - open：问候或闲聊，如“在吗”“干嘛呢”“好无聊”“随便聊点”；
  - share：其他。
- **少追问偏好**：用户明确说过 no 或 less，或者推断出的值是 no 时生效。
  - 不再给出 onboarding 的可选了解提示，也不记录为已提问；
  - 遇到 share 或 open，在用户消息前紧挨着加一句本轮提示；遇到 invite，则提示可以问一个问题；
  - 回复仍以提问结尾时，重写一次（`VIOLET_FOLLOW_UP_CHECK`，默认开）。重写后仍然提问、或没通过一致性检查，就保留原回复，不会删除问号。用户自己提问或请 Violet 提问的那一句不受影响。
- **用户问自己的事**：“你还记得我…吗”“我…来着”会带出所有有效的档案事实；纠错后的旧值是 superseded，不会出现。另外，档案项新增 `asked_as`，例如“学什么”“做什么的”，让这类问法也能带出对应事实。
- **Violet 的生活节**：
  - 只有心里难受、任务、或长消息（30 字以上）才算需要专注回应的时刻，单纯的问句不算；
  - 问 Violet 自己的事、问候、闲聊，以及提到书或音乐时，带上她的生活记录。
- **忙碌有效期**：由用户原话决定，只会延长，不会缩短明确给出的 until。
  - “今天一整天 / 今天都”到当天 23:59；
  - “这两天”到明天 23:59；
  - “这几天 / 这周”到后天 23:59；
  - 其他说法沿用 3 小时默认值。
  - 明确要求的提醒仍按时发送，调度先处理提醒，再看是否忙碌。
- **IntentAgent**：能看到最近 6 条带时间的消息（`VIOLET_INTENT_RECENT_MESSAGES`），用于判断互道晚安后是否需要回复。

**第一批真实多轮测试（建议，需要单独授权）**：同一份 8.7 剧本，分别跑 8.8 之前（3e74408）和之后两个版本，记忆都用剧本脚本，这样差异只来自聊天和意图判断，然后由你读完整聊天记录并盲评。

| 批次 | 内容 | 调用 | Token |
|---|---|---|---|
| 1 | E1、E5、E8；修改前和修改后各一次 | 约 120–150 次 | 约 85 万–105 万 |
| 2（可选） | E3、E6，只跑修改后，`--memory real`，看真实 MemoryAgent 会不会纠错、会不会设置有效期 | 约 60–95 次 | 约 50 万–90 万 |

```powershell
# 修改前的代码放进单独的 worktree（只读用途），两次都用同一个 Python 环境
Set-Location 'D:\unity\violet'
git worktree add --detach 'D:\unity\violet\local\p88\baseline-3e74408' 3e74408
$env:PYTHONIOENCODING = 'utf-8'
$env:VIOLET_TEST_ALLOW_REAL_API = '1'
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$textEndpoint = Read-Host '文字模型 API 基础地址'
$textHost = ([Uri]$textEndpoint).DnsSafeHost
$env:NO_PROXY = "$textHost,127.0.0.1,localhost"
$py = 'D:\unity\violet\.venv\Scripts\python.exe'
$cfg = 'D:\unity\violet\local\violet-test-config\gpt6luna.env'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
Set-Location 'D:\unity\violet\local\p88\baseline-3e74408\Violet'
& $py -X utf8 -m tools.chat_dialogue run --out "D:\unity\violet\local\p88\real-before-$stamp" --label before-8.8 --episodes E1,E5,E8 --memory scripted --real --api-url $textEndpoint --config $cfg --max-calls 80 --max-tokens 600000
Set-Location 'D:\unity\violet\Violet'
& $py -X utf8 -m tools.chat_dialogue run --out "D:\unity\violet\local\p88\real-after-$stamp" --label after-8.8 --episodes E1,E5,E8 --memory scripted --real --api-url $textEndpoint --config $cfg --max-calls 80 --max-tokens 600000
& $py -X utf8 -m tools.chat_dialogue blind --run "D:\unity\violet\local\p88\real-before-$stamp" --run "D:\unity\violet\local\p88\real-after-$stamp" --seed 2026118 --out "D:\unity\violet\local\p88\real-blind-$stamp"
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API
```

## 19. Phase 8.9 Violet 的日常、关系状态和回复习惯

产品行为的改动只在 natural 写法下进入 Chat 上下文；legacy 与之前逐字相同。设计见 decisions.md T14。

- **普通的一天**（`ai/daily_life.py`，`VIOLET_DAILY_LIFE`，默认开）：
  - 素材在 `persona/life_config.json` 的 `daily`：上午、下午、晚上三个时段，每段几件小事（买早饭、去书店翻书、在咖啡馆坐坐、煮面、散步……），都在人设允许的住处、书店、公园、咖啡馆范围内；
  - 按日期固定选出 0–3 件，不调用模型；约 15% 的日子什么也没有；
  - 开始时间到了才记成已发生的生活事件（source `life_daily`），要先通过和其他生活事件一样的一致性检查（时间冲突、用户自己的经历、人设限制），没通过就不算；还没到的只是打算，不存；
  - 在 Chat 构建上下文时补齐今天和前两天，最早的先补，所以以后再问“昨天干嘛了”答案不变；
  - 不计入生活引擎每周的事件上限，也不作为主动分享的素材；生活引擎的事件会避开同一时段。
- **生活节的变化**（natural，chat）：
  - 今天和昨天发生过的事、此刻在做什么（没有就写“没在做什么特别的事”，什么都没有的日子写“普普通通的一天”）；
  - 正在进行的生活线带上她现在的感受，随着这条线的事件增多而变化（`interests.*.impressions`）；
  - 听过的曲子带上具体曲目（`interests.music.details`），说出曲名不再被当成没有记录的经历；
  - 这次聊天里她已经说过的，标“已经说过了，别再重复”；之前跟对方提过的，标“之前跟对方提过”；
  - 用户提到她的书名、曲子，或者接着她刚说的生活聊（“钢琴曲？好文艺啊”），也会带上生活节。
- **关系状态**（`ai/relationship.py`，`VIOLET_RELATIONSHIP_STATE`，默认开）：每次根据聊天记录重新判断，不存分数。
  - 依据：聊天天数和时间跨度、对方主动开启聊天的次数、对方说过的亲近的话和玩笑、对方明确要保持距离的话、很久没聊；
  - 四种状态：还在互相熟悉 / 慢慢熟起来 / 已经很熟 / 对方要保持距离。写成一句描述，说明这种关系里什么是自然的，不规定台词，没有随机；
  - “别撒娇”“别这么黏”这类话一直有效，直到对方之后说“可以撒娇”；很熟但超过 21 天没聊，会退回“慢慢熟起来”。
- **本轮提示**：新增 closing（去忙、去睡、晚安）和 ack（单独一个“嗯”“好吧”）两类；邀请提问的提示改了措辞（8.8 的“真正好奇”被真实回复原样说了出来）；用户回答 Violet 应邀提出的问题时，可以再顺着问一句，不触发少追问重写。
- **回复习惯提示**（`VIOLET_REPLY_HABIT_NOTE`，默认开）：统计这次聊天里 Violet 最近 4 条回复，已经叮嘱、关心过对方，或最近 3 条有 2 条以问句结尾时，在用户消息前说明一下。对方难受、提出任务或说了长消息时不加。
- **输出**：发送前去掉 Markdown 加粗标记（真实运行中出现过 `**注意休息，别太累啦**`）。

**评估工具的变化**：

- `chat_dialogue` 的 debug.json 每一步新增 `follow_up_check`：是否重写、原因，以及被替换或被保留的初稿。剧本里的生活线按内容判断兴趣（“听…”是音乐，含“记”是笔记，其余是读书），剧本文件本身没有改。
- 32 轮验收（`dialogue_eval`）、`replay` 和 `synthetic_rounds` 回放的都是 8.9 之前录下的报告，所以这些工具关闭普通的一天，避免回放出报告里没有的生活事件。
- tests/conftest.py 默认关闭普通的一天（它依赖日期，和关闭生活引擎的理由相同）；8.9 的测试用固定的剧本时钟单独打开。

**Phase 8.9.5 主动表达、输出可靠性与测试分类**（设计见 decisions.md T20）：

- **步骤状态更细**（工具版本 p87.v3）：
  - ok；
  - recovered：所有失败的调用都是 502/503，并且在同一次处理里重试成功。现在 ProactiveAgent 和调度步骤也按这条规则判断。8.9.4 真实 A1 的主动消息已经发出，却因为 ProactiveAgent 一次 503 被判失败、写着“未知原因”；
  - degraded：只有 IntentAgent 重试用完，产品按设计改走普通聊天，ChatAgent 成功，用户收到了真正的回复，不是兜底话术（8.9.4 真实 A3 第 4 步）。剧本继续执行，但这段剧本的状态是 degraded，不进盲评；
  - failed：其他所有情况，包括用户收到兜底话术“我这边刚刚卡了一下…”（即使没有任何调用失败）。

  调度步骤失败时会写出失败的调用，不再显示“未知原因”。run.json 新增 degraded_steps。每一步的 `responses` 记录模型的原始输出（Intent / Chat / Proactive），可以和守卫的记录、最终发出的文字对照。
- **英文草稿混进回复**：一段 5 个词以上的英文里，有 3 个以上“写草稿时才用的词”（maybe、perhaps、question、tone、line(s)、use、mechanical……），就记为 leaked_draft：先改写一次，还不行就去掉那一行；主动消息同样检查。“I miss you so much”“好的ok”“赶deadline”“I'll be there, don't worry”不受影响。
- **主动消息不再套路化**：
  - 主动消息的生成要求里加了一句：不一定要提问，可以只说一件事、一句想念或一句撒娇；
  - 调度器统计 Violet 近 3 天主动发出的消息（recent_proactive）；其中有以问近况收尾的（“今天过得怎么样”“吃得怎么样”“准备怎么过”），就生成 proactive_habit_note，同时交给决策和生成；
  - 新增理由 continue：最近两天聊到过的、Violet 还没完成的打算，可以接着说；没聊过的仍是 ask_opinion；
  - 想念的说明允许只说一句想念、撒个娇讨点关注，不一定要问近况。
- **依赖和可爱**：已经很亲近时，关系描述加上“偶尔害羞、俏皮或闹点小脾气也自然”。“在吗 好久没聊了”“好久不见”也算对方回来了（慢慢熟和亲近阶段）。

**Phase 8.9.4 主动依恋与长期陪伴**（设计见 decisions.md T19）：

- **新的合成剧本** `tests/samples/chat_dialogue/episodes.p894.v1.json`（p894.v1，独立于冻结的 p87.v1），运行时加 `--episodes-file`：
  - A1：已经很亲近，第二天主动联系，之后对方回复并接着聊；对方刚说完话时调度应等待；
  - A2：对方说“先别主动找我”时拦下；放开以后允许；没有回应时不再拿想念当理由；连续两条没有回应时等对方先回来；
  - A3：一个月没聊，关系变淡，主动消息不以想念为由，对方回来时自然接上；
  - A4：亲密日常——回来、被夸、调侃、帮她拿主意、暂时离开、再回来。
- **主动联系的四个环节分开记录**：每个调度步骤的 `proactive_chain`：
  - scheduler_allowed / scheduler_reason：调度是否允许；
  - model_called / model_decided_to_send / decision_reason / topic：模型是否被调用、是否决定发、为什么；
  - message_generated / sent / outcome / outcome_reason：消息是否生成并发出，没发出时的原因。

  对方之后能不能接着聊，看下一个用户步骤。transcript 里每个调度步骤写一行“主动联系：…”，盲评表不显示。
- **守卫**：判断一句话是否有生活记录支持时，不再算“了会、会儿、一下”这类虚词（之前“我刚刚也随手听了会儿歌”被“在公园散了会儿步”误判为有记录）。以前聊到过天气、这次对方又没提时，回复里说“今天也冷”“雨天确实…”“这种下雨天”会被当成把旧天气说成现在（stale_weather），先改写一次，还不行就去掉那一行。只检查给用户的回复，主动消息里天气任务的结果不检查。
- **叮嘱按天统计**：回复习惯里的叮嘱、关心改为统计 Violet 当天所有的回复（以前只统计这次聊天，忙碌的一天里每隔几小时就从零开始）；“别一忙又忘了喝水”“吃点东西”等也算叮嘱。
- **对方回来**：慢慢熟起来或已经很亲近时，对方说“我回来啦”“忙完了”“到家了”，提示开心地接上，不用机械地问一句“怎么样”。
- **奇怪的片段**：diagnostics.json 的 `suspicious_fragments` 列出“_久久爱”“ablytyped”“,伊人”这类片段，供人工阅读，不会自动删除（“赶deadline”看起来也一样）。

**Phase 8.9.3 亲密关系与主动依恋**（设计见 decisions.md T18）：

- **人设 1.2.0**（`persona/persona_core.json`，人工修改）：软萌、乖巧；慢热，熟起来后主动找对方、撒娇、想念会说出来；很亲近以后明显黏人，日常小事爱请对方拿主意，会舍不得结束聊天；有自己的小喜好，但不执着于坚持观点；“黏人但不绑人”。一致性检查用的规则没有变。1.1.0 原样保存在 `persona/chat_prompt_versions/persona_core.v1.1.0.json`，chat_ab 的 v1 组改用这份（哈希不变）。Chat v1 提示词没有改。
- **关系成长**（`ai/relationship.py`）：仍然根据聊天记录判断，不按轮数。新增的依据是对方跟 Violet 说过多少自己的事（有效的 event / fact 记忆达到 3 条），可以代替亲近的话或玩笑。三个阶段的描述：还在互相熟悉（温柔、有点害羞，多聊自己喜欢的东西）→ 慢慢熟起来（主动找话题、开玩笑、偶尔撒娇、说一句有点想对方）→ 已经很亲近（明显黏人：讨关注、分享小心事、日常小事请对方拿主意、说想念、舍不得结束；不让对方愧疚，不要求对方只陪她）。对方要求保持距离时，仍然优先按保持距离处理。
- **对方要离开**：慢慢熟起来或已经很亲近时，告别提示允许带一点舍不得或期待下次，但要顺着对方的安排，不挽留、不提问、不叮嘱。
- **亲近不重复**：8.9.2 的“这一句不用再加码”改为“想继续亲近也很好，换个说法，别重复同一句，别堆颜文字和动作”；对方说“就这？”时，可以更热情、更可爱，但要换个新说法。
- **主动找对方的理由**（`proactive_motives`，ProactiveScheduler 的 event_context 里的 `violet_motives`、`relationship_stage` 和 `motive_note`）：
  - share：有一件还没分享的生活事件；
  - miss：已经很亲近时，对方 6 小时以上没说话；慢慢熟起来时是 24 小时以上。要求最近没有未被回应的主动消息，关系也不是久不联系后变淡的状态；
  - ask_opinion：慢慢熟起来或已经很亲近时，有一件还在进行的生活打算，可以问问对方的看法。

  ProactiveAgent 的上下文也加上了关系那一节。什么时候可以主动联系，仍由原来的规则决定：静默时段、每日上限、对方忙、冷场次数、随机间隔。没有新增频率配额。

**Phase 8.9.2 自然表达**（依据 8.9.1 真实运行 local/p891/real-r2-20261020-094248，只在 natural 写法下生效；设计见 decisions.md T17）：

- **刚问过的问题**：Violet 上一条以真正的问句结尾（以“？”或“吗”结尾；“是不是？”“满意了吗？”这类调侃的反问不算），对方这句又只是平常的分享或“嗯”、没有转向 Violet 时，提示里会写出她刚问的那句，并说明对方这句就是回应，没提到的别换个说法再问。单字回应的提示还说明不用替对方把感受说出来。
- **亲近不加码**：回复习惯里统计这次聊天里的撒娇、想念、亲昵动作和颜文字。累计 2 次以上时，提示这一句不用再加码；对方刚被表达过亲近、又说“就这？”“还有呢”时，提示可以斗斗嘴或大方认一下，不用越说越重。最近 4 条里叫了对方名字 2 次以上时，也会提一句。没有次数或概率配额，第一次撒娇不受影响。
- **话题交给她时说自己的事**：“好无聊”“随便聊点什么吧”这类把话题交给 Violet 的闲聊，在她还有这次没说过的生活内容时，提示她挑一件自己的事说，不用出题考对方；这时也不再给“上学还是工作”的可选了解。问候和“在干嘛呢”不算。
- **以前说的天气**：进入这次聊天的那个时间标记加上一句“在这之前说的天气、心情和在做的事，是那时候的”；没有中断时，加在“以上是之前的聊天”那句上。
- **多余的括号**：发送前去掉没有括住任何内容的“【”“】”（真实回复里出现过“…听听看】【。”）；真正成对的【…】保留。

**Phase 8.9.1 一致性修复**（8.9 真实测试预检发现，都只在 natural 写法下生效；设计见 decisions.md T15）：

- **聊天中途不会“去午睡”**：日常小事开始的那一刻，前后 20 分钟内都有消息（`daily.chat_gap_minutes`，默认 20），说明 Violet 正在聊天，这件事就不算发生，也不存。正在处理的这条用户消息也算聊天：Intent 的上下文在消息写入记录前就要构建。已经存下的事件不删不改。开始后 Violet 回过消息的，不再写成“正在做”，而是算已经做完。
- **熟了以后不再问“上学还是工作”**：关系是“已经很熟”或“保持距离”，或者已经在 5 天以上聊过、前后跨了 14 天以上，就不给可选了解的提示，也不记录为已提问；新用户不受影响。受 `VIOLET_RELATIONSHIP_STATE` 控制。
- **亲近的证据更严格**：单独的“找你”“跟你说”“告诉你个”不再算亲近，要说成“想找你”“找你聊”“想跟你说说话”才算；亲近的说法前面紧挨着否定词（没 / 不 / 别 / 顾不上 / 懒得）也不算，比如“都没顾上找你”“不想你”。
- **之前聊过的不当新鲜事**：生活线以前被聊过就标“之前跟对方提过”。认定依据是在这条线进行期间，有消息提到书名或曲目，或者用户问起“你那本书”且 Violet 回答了，而当时只有这一本书在读。生活事件发生后 Violet 在以前的聊天里讲过，也算。这些都从聊天记录推断，不写数据库；这次聊天里说过的仍标“已经说过了”。

## 20. Phase 8.9.6 自由聊天体验测试（`tools/free_chat.py`）

你自己决定说什么、什么时候让时间过去、什么时候结束。消息走 **Violet 的真实处理流程**：VioletRuntime → IntentAgent → ChatAgent → 一致性守卫 → 发件箱，MemoryAgent 在后台运行，ProactiveScheduler 负责主动联系。和 `violet_test_chat` 的区别：会话**可以接着聊**（同一个数据库、同一个模拟时钟），每一轮的细节都保存下来，聊完可以给具体的轮次做标记。`violet_test_chat` 没有改动，仍然是一次性的临时会话。

**隔离**：和 `violet_test_chat` 用同一套机制（harness）：独立的测试数据库，不连微信，不读 `.env` 和生产数据；MCP、联网、图片（生图本阶段禁用）、生活引擎全部关闭；网络只允许连到 `--api-url`。真实模型要同时满足 4 个条件：`VIOLET_TEST_ALLOW_REAL_API=1`、`--real`、Key（在隐藏提示里输入，或用 `VIOLET_TEST_API_KEY` 环境变量；不能写在命令行或配置文件里）、看完说明后输入 `yes`。所有记录写入前都会把 Key 替换成 `<redacted>`。

**模型**：`--model cys` 让 Violet 的所有文字角色（意图、聊天、记忆、主动、执行）都用同一个模型名，备用模型清空。会话建立后，模式（模拟 / 真实）和模型名都不能再换，换了会拒绝启动（另建一个会话）。每次调用都记下上游实际回应的模型名（`upstream_model`）和结束原因（`finish_reason`），用来看同一个映射背后是不是换了上游。

**会话目录**：`--session 名字` 放在 `D:\unity\violet\local\free_chat\名字`（不进 Git），也可以给完整路径（不能在 Violet 里面）。同一时间只能有一个窗口打开同一个会话。

| 文件 | 内容 |
|---|---|
| `session.json` | 模式、模型、模拟时间、每次启动的用量（调用、token、失败、平均延迟）、代码和提示词版本 |
| `events.jsonl` | 每一轮一行（只追加）：说了什么、发了什么、延迟、每次调用、token、错误和分类（ok / recovered / degraded / failed，规则和 chat_dialogue 一样）、意图判断、守卫改写、**模型原始输出**、记忆任务、档案和记忆的变化、关系阶段 |
| `requests.jsonl` | Intent / Chat / Proactive 每次请求的完整消息 |
| `feedback.jsonl` / `feedback.md` | 你的标记，每条都带着被标记的那一轮和前一轮 |
| `transcript.md` | 可读的聊天记录（按天分段，标出时间间隔、主动消息、调度检查、出错和标记） |
| `violet/` | Violet 的测试数据库和数据 |

**聊天里的命令**：

| 命令 | 作用 |
|---|---|
| `/advance 3`（或 `40m`、`1d`、`2h30m`） | 让时间过去。调度器按生产里的方式检查：到它自己定的下次时间（或有约定的提醒到期）就检查一次；有只靠时间才能打开的条件（离对方上一句太近、对方在忙等）时，每 10 分钟再看一次。**不强制发消息**，发不发由模型决定 |
| `/check` | 让调度器现在检查一次，假定它的随机等待已经到了；静默时段、每日上限、对方忙、冷场次数这些条件照常，模型照常决定 |
| `/mark 标签 [#n] [备注]` | 标记某一轮（默认最近一轮）。标签：`好`（自然、可爱）、`机械`（机械、重复或缺少亲近感）、`矛盾`（生活经历或时间矛盾）、`该主动`（该主动却没有主动，可以标在一次时间推进上）、`备注` |
| `/show [n]` | 这一轮的细节：原始输出和发出的文字、守卫、意图、每次调用、主动联系的环节 |
| `/state`、`/time`、`/usage`、`/help`、`/quit` | 档案和记忆、模拟时间、本次和累计用量、帮助、结束（Ctrl+C 也可以） |

**主动联系分开记录**：每次检查都写下 (1) 调度器是否允许（不允许时写原因）；(2) 模型是否决定发、理由和话题；(3) 是否生成并发出。(4) 对方之后回的那一轮标上“回复第 n 条主动消息”，并记下隔了多久。没有调用模型的检查只按原因计数。

**接着聊**：用同一个 `--session` 再启动就行（不要再写 `--seed-episode` 或 `--start`）。模拟时间从上次停下的地方继续，关掉窗口的这段时间不算。想让时间过去就用 `/advance`。

**开局**：默认是全新的关系（刚认识）。想直接测亲密关系，就在新建时加 `--seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json`，用这个合成剧本的背景（已经很亲近、称呼“小满”、12 条之前的聊天、Violet 的生活记录）开局，模拟时间也从剧本的开始时间算起。剧本文件只读，不会被修改。

**预算**：`--max-calls 150`、`--max-tokens 500000` 是**本次启动**的上限，`--max-calls-per-round 12` 是单轮上限。达到上限就停止调用并保存。一轮普通聊天大约 3–6 次调用；模型决定主动联系的一次检查大约 2–3 次。

**真实模型会话的累计上限（Phase 9.1.1）**：新建真实会话时设定一次（`--session-max-calls` / `--session-max-tokens`，不写就等于这一次的 `--max-calls` / `--max-tokens`），存在 session.json 的 `session_budget`；之后每次启动只能用剩下的部分（本次可用 = min(本次上限, 会话剩余)），启动时会先打印“本次启动最多 … / 会话累计 … 剩余 …”，剩余不够之前最小的一次调用时直接不启动（在 Key 提示之前）。累计上限之后只能调低，不能调高；需要更多额度就新建会话。用量按 `usage.jsonl` 计：每次模型调用记录时写一行，窗口被关掉也不丢。9.1.1 之前建立的真实会话取第 1 次启动的上限。`/usage` 分三行：上一轮、本次启动（和本次上限）、会话累计（和累计上限、剩余）。模拟会话不受累计上限限制。

**没有正常结束的启动**：关掉窗口或进程被结束时，finish() 来不及运行。Windows 关窗口时会先把这次启动记为 `end=console_closed`；其他情况在下一次启动时提示“第 n 次启动没有正常结束”，在 session.json 里标 `end=abnormal` 和计入的用量（`counted`），并先检查测试数据库（`PRAGMA quick_check`，不通过就不启动）、把 WAL 合并回数据库（`db_open` 里记录前后大小）。会话锁是系统锁，进程结束就自动释放。

**MemoryAgent 的工具调用**：events.jsonl 每一轮的 `memory_tools` 记录 MemoryAgent 实际传的参数和收到的结果（密钥替换，手机号、证件号、邮箱、银行卡、精确地址、健康 / 财务等字样整段只留长度）。

```powershell
cd "D:\unity\violet\Violet"
$env:PYTHONIOENCODING = "utf-8"
$py = "D:\unity\violet\.venv\Scripts\python.exe"

# 先用模拟模型试一下命令（不联网、不花钱；回复是“（模拟回复）……”）
& $py -X utf8 -m tools.free_chat chat --session try-mock --seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json

# 真实模型（cys）：Key 在提示时输入，不显示、不保存
$env:VIOLET_TEST_ALLOW_REAL_API = "1"
Remove-Item Env:VIOLET_TEST_API_KEY -ErrorAction SilentlyContinue
$api = Read-Host "文字模型接口地址（以 /v1 结尾）"
$env:NO_PROXY = ([Uri]$api).DnsSafeHost + ",127.0.0.1,localhost"
& $py -X utf8 -m tools.free_chat chat --session cys-close-1 --real --api-url $api --model cys --seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json

# 下次接着聊（同一个会话）
& $py -X utf8 -m tools.free_chat chat --session cys-close-1 --real --api-url $api --model cys

# 聊完以后标记（离线，不调用模型）
& $py -X utf8 -m tools.free_chat review --session cys-close-1
Remove-Item Env:VIOLET_TEST_ALLOW_REAL_API
```

`review` 会先打印整段聊天记录，然后输入 `5 机械 又在问今天怎么样` 这样的行（轮次、标签、备注），`show 5` 看细节，`q` 结束。

**Phase 8.9.8 新增**（设计见 decisions.md T22）：

- `--chat-prompt v1|v2`、`--proactive-prompt v1|v2`：新建会话时选择提示词版本。v1 就是 env_example 里的稳定提示词（不改动）；v2 是候选，来自 `persona/chat_prompt_versions/chat.v2.txt` 和 `persona/proactive_prompt_versions/proactive.v2.txt`（`proactive.v1.txt` 是稳定提示词的原样副本，只作对照）。会话建立后不能换版本，transcript 开头会写明。
- `transcript.md` 和 `feedback.md` 每轮、每次标记后都会重写，直接关窗口也不会丢。
- 跨天的时间推进会显示日期（例如“11-06 23:51 → 11-07 23:51”）。
- `--life on|off`（Phase 8.9.12，默认 off，新建会话时选定）：时间推进时，Violet 的生活引擎按生产里的规则运行。每个模拟日最多一次，在 21:00 以后；只用模板措辞，不调用模型；每周有上限，按日期确定随机种子，所以同样的日子得到同样的结果。每次运行和新增的生活记录都写进那次时间推进（events.jsonl 的 `life_runs`）和 transcript；数据库里另有 `life_engine_runs`。没有 LifeScheduler 线程，也不运行网页探索。life_config 的生成方式不是 template 时，拒绝启动。
- v1 / v2 对比：同一个模型、同样的开局，各建一个会话（例如 `ab-v1`、`ab-v2`），按同一份话题清单聊，最后在 `review` 里标记。

```powershell
& $py -X utf8 -m tools.free_chat chat --session ab-v1 --real --api-url $api --model cys --chat-prompt v1 --proactive-prompt v1 --seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json
& $py -X utf8 -m tools.free_chat chat --session ab-v2 --real --api-url $api --model cys --chat-prompt v2 --proactive-prompt v2 --seed-episode A4 --episodes-file tests\samples\chat_dialogue\episodes.p894.v1.json
```

## 21. Phase 9.0 Violet Control Center（管理后台，`/control`）

设计见 decisions.md T28，测试见 testing.md §34。后台是**单管理员**：Violet 只服务一段微信对话（数据库没有用户列），所以没有注册、没有多个账号，也不假装有多租户。以后要支持多个使用者时的做法写在 architecture.md §7。

**本地启动和登录**

```powershell
cd "D:\unity\violet\Violet"
$py = "D:\unity\violet\.venv\Scripts\python.exe"
# 1. 设置管理员密码（只需一次；输入不回显，至少 12 个字符；只保存 scrypt 哈希）
& $py -X utf8 -m tools.violet_admin set-password
# 2. 启动 Violet（后台默认只监听 127.0.0.1:7100）
& $py -X utf8 main.py
# 3. 浏览器打开 http://127.0.0.1:7100/control ，用密码登录
```

- 没设密码时，可以用管理令牌登录：环境变量 `VIOLET_ADMIN_TOKEN`，或第一次启动时自动生成的 `data\admin_token` 文件里的内容。令牌也用于脚本（`Authorization: Bearer …`）。
- `python -m tools.violet_admin status` 查看是否设了密码和令牌状态；`logout-all` 让所有登录失效；忘记密码时直接再 `set-password`（需要能登录这台电脑，这就是恢复方式）。
- 管理令牌（Phase 9.0.1）：`token-disable` 禁用（登录和 Bearer 都不再接受，需要先设密码）；`token-rotate` 换一个新令牌（只显示一次，旧令牌立即失效，只保存哈希，也能替换掉 `VIOLET_ADMIN_TOKEN`）；`token-enable` 回到 `VIOLET_ADMIN_TOKEN` / `admin_token` 文件。都不需要重启，并且会让所有登录失效。`admin.json` 损坏时令牌和密码都不能登录，用 `set-password` 和 `token-enable` 修复。
- 登录：同一个地址 15 分钟内输错 5 次，锁定 15 分钟（锁定期间正确的密码也不放行）；所有地址合计 50 次时全部暂停。无操作 120 分钟（`VIOLET_DASHBOARD_IDLE_MINUTES`）或登录满 12 小时（`VIOLET_DASHBOARD_SESSION_HOURS`）要重新登录。改密码、点“退出所有登录”会让所有会话失效。

**远程访问**（Phase 9.0.1 起）：后台**只监听本机地址**，`VIOLET_DASHBOARD_HOST` 填别的地址也会退回 127.0.0.1（日志里有错误说明）；9.0 的 `VIOLET_DASHBOARD_PUBLIC` 已经去掉，因为任何开关都不能证明前面真的有 HTTPS。两种方式：

1. SSH 隧道（推荐，个人使用）：在自己电脑上 `ssh -L 7100:127.0.0.1:7100 <服务器>`，然后打开 `http://127.0.0.1:7100/control`。
2. 同一台服务器上的 HTTPS 反向代理（nginx / Caddy）：代理监听 443，转发到 `http://127.0.0.1:7100`，并带上 `X-Forwarded-For` 和 `X-Forwarded-Proto`；Violet 设 `VIOLET_DASHBOARD_TRUST_PROXY=1` 和 `VIOLET_DASHBOARD_COOKIE_SECURE=1`。这时不是 HTTPS 的请求一律 403（在读取密码之前），HTTPS 响应带 HSTS；转发头只在连接来自本机时才被采用，远程客户端伪造不了地址和协议。

本阶段没有部署，也没有配置任何代理。部署前请逐项核对下面的清单。

**以后部署到服务器前的安全检查清单**（本阶段只写清单，不部署）

| # | 检查 | 怎么确认 |
|---|---|---|
| 1 | 后台只监听本机 | `ss -ltnp \| grep 7100` 只有 `127.0.0.1:7100`；启动日志没有 “is not a loopback address” |
| 2 | 设了管理员密码，并禁用或更换了令牌 | `python -m tools.violet_admin status`：密码“已设置”；令牌“已禁用”或“已更换”（脚本需要 Bearer 时用更换后的令牌） |
| 3 | 远程只走 SSH 隧道或 HTTPS 代理 | 防火墙不放行 7100；代理的证书有效；`curl -I http://域名/control` 被跳到 https 或拒绝 |
| 4 | 代理配置正确 | 代理和 Violet 在同一台机器；转发 `X-Forwarded-For`、`X-Forwarded-Proto`；Violet 设 `VIOLET_DASHBOARD_TRUST_PROXY=1`、`VIOLET_DASHBOARD_COOKIE_SECURE=1`；从外网用 http 访问得到 403，用 https 登录后 Cookie 带 `Secure`，响应有 `Strict-Transport-Security` |
| 5 | 代理自身的日志 | 代理的访问日志会记录完整 URL（含聊天搜索词）：关闭 `/api/control/` 的访问日志或去掉查询串 |
| 6 | 代理层限速 | 对 `/auth/login` 再加一层限速（Violet 的锁定在内存里，重启会清零） |
| 7 | 文件权限 | `data/control/`、`data/admin_token`、`.env` 只有运行 Violet 的用户可读（`chmod 600`）；它们和数据库都不进 Git，也不放进备份以外的地方 |
| 8 | 不用 root 运行 | Violet 用单独的系统用户运行 |
| 9 | 会话时长 | 按需要调 `VIOLET_DASHBOARD_IDLE_MINUTES` / `VIOLET_DASHBOARD_SESSION_HOURS`；离开前点“退出所有登录” |
| 10 | 部署后验收 | 按 testing.md §35 的项目在服务器上（用测试数据目录）再跑一遍：无认证访问、登录锁定、HTTPS 拒绝、设置保存与重启 |
| 11 | 回退 | 删除 `data/control/settings.json`、`secrets.json` 就回到 `.env`；`admin.json` 出问题时 `set-password` + `token-enable` |

**页面**

| 页签 | 内容 |
|---|---|
| 概览 | 微信登录、收消息 / 回复 / 记忆线程、排队、主动联系状态和今日次数、生活引擎、各 Agent 的模型、辅助模型 24 小时调用、最近的失败（只有位置、时间和错误类型，不显示内容）。Intent / Chat / Memory / Proactive 的单次调用不写数据库，所以没有按次的统计 |
| 聊天记录 | 按日期、发送方、关键词查询，往前翻页；标出对方、Violet 回复、Violet 主动、提醒、系统（依据发件箱记录）。**只能看，不能改或删**。可以导出一段日期的 JSON（文件里有聊天全文，请妥善保存） |
| 记忆 | 事件记忆和核心记忆：搜索、按状态筛选、分页；显示来源、更新时间、修改次数；纠正（原文写进修改记录）；删除要确认（事件记忆是标记为已删除，行保留；核心记忆先把原文写进修改记录再删除）；查看修改记录 |
| 设置 | 只有代码真正读取的设置（见下表），每项标出立即生效还是重启后生效、当前值来自后台、.env 还是默认值。**显示的是 Violet 正在使用的值**（9.0.1）：模型和温度取自运行中的 Agent，主动联系取自调度器合并后的配置，提示词显示正在用哪个版本。保存前整体检查，有一项不合格就一项也不改；写文件失败时什么都不变。“不再覆盖”把这一项交回 .env |
| 修改记录 | 每次保存一条：谁、原因、改了什么（API Key 只有指纹）。可以回滚到某次之后的状态（不动 API Key）；API Key 可以恢复上一个值 |
| 账号安全 | 修改密码（需要当前密码或令牌）、退出所有登录、退出 |

**可以在后台改的设置**（其余环境变量不显示、不能改）

| 分组 | 设置 | 生效 |
|---|---|---|
| 接口 | `LLM_API_URL`；`LLM_API_KEY`（只能替换）；请求超时、连接超时、最多尝试次数、重试间隔、总时限 | 立即（保存后重载 Agent） |
| 模型与 Agent | Intent / Chat / Memory / Proactive / Performer 各自的模型、备用模型、温度（代码里只有温度一个采样参数） | 立即 |
| 提示词版本 | Chat、主动联系决策：留空用 .env 里的提示词，或选 `persona/*_prompt_versions/` 里的版本 | 立即 |
| 消息发送 | 等对方说完（`DEBOUNCE_NORMAL`）、像没说完时多等（`DEBOUNCE_EXTRA`）、打字间隔上下限；模拟打字开关 | 立即（9.0.1 起模拟打字开关也立即生效） |
| 主动联系 | 开关、静默时段、每天最多几次、对方说话后多久、两次之间多久、连续几次没回就等 | 立即（调度每次都重新读取） |
| 生活引擎 | `VIOLET_LIFE_ENGINE` | 立即（9.0.1 起：关闭在下一次检查时生效，打开马上启动）；临时暂停 / 恢复在同一页 |
| 聊天行为 | 关系阶段描述、聊天历史保留几轮、回复习惯提示 | 立即 |

回复的条数没有上限，后台也不提供这样的设置。

**保存在哪里**：`data\control\`（和 `data\` 一起不进 Git）：`settings.json`（普通设置）、`secrets.json`（API Key 和最多 3 个旧值）、`history.jsonl`（只追加）、`admin.json`（密码哈希和会话版本）、`session_secret`。启动时和每次重载时，先读 `.env`，再把这些设置放在上面；删除 `data\control\settings.json` 和 `secrets.json` 就完全回到 `.env`。

## 22. Phase 9.1 长期记忆可靠性

设计见 decisions.md T30，问题与证据见 bugs-and-risks.md M1–M9，测试见 testing.md §36。

**新的设置**（都有默认值，`env_example` 里有说明）：`VIOLET_MEMORY_KEEP_CLEAR_WEIGHT`（3）、`VIOLET_MEMORY_SEARCH_WINDOW`（5000）、`VIOLET_MEMORY_WORD_RECALL`（1）、`VIOLET_MEMORY_WORD_RECALL_LIMIT`（3）、`VIOLET_MEMORY_NOT_FOUND_NOTE`（1）。把后三个设为 0 就回到 9.0 的召回行为。

**测试工具**：这 5 项加进了 harness 的 `ALLOWED_KEYS`，测试配置文件（`--config`）可以把它们关掉，和 9.0 的行为对比。`chat_ab` 的子进程固定 `VIOLET_MEMORY_NOT_FOUND_NOTE=0`：它的真实场景是 9.1 之前录下的上下文，重建必须和录制一致（结果里记为 `memory_not_found_note`）；chat_ab 直接调用 ContextEngine，不经过补充召回。

**旧衰减删掉的记忆**：9.1 之前，每晚衰减会把记忆改成 deleted。部署 9.1 后不会再发生，但已经被删的不会自动回来。用户 / 后台删除一定有 retract 修改记录，所以“deleted 而没有 retract 记录”的行不是用户删的（也可能是 2.0 之前的旧版本删的，区分不了）。先在**备份副本**上只列出：

```powershell
& $py -X utf8 -m tools.memory_restore_decayed --db <备份副本\violet.db>
```

确认要恢复后，停掉 Violet，在正式库上加 `--apply`（可以用 `--ids 3,8` 只恢复其中几条）。恢复后的状态是 forgotten（可检索，标“印象很模糊”），每条写一条 restore 修改记录，内容不变。

**free_chat 的记忆衰减**：新会话默认 `--memory-decay on`，`/advance` 每经过一个凌晨 3 点就运行一次 `decay_event_memories()`（和 main.py 一样），结果记在 events.jsonl 的 `memory_decay_runs`；在这次推进结束时运行。旧会话没有这一项，按 off 处理，不会被改变。

**一轮小规模真实记忆验证（设计；需要用户授权后由用户运行，本阶段没有运行）**

目的：不是让模型复述资料，而是看 Violet 在自然聊天里、经过几天到几周以后，能不能自然地想起来、不记错、不乱提。用 free_chat 的真实模式，新建会话，全程虚构内容（不要用朋友的真实聊天）：

```powershell
$env:VIOLET_TEST_ALLOW_REAL_API = "1"
& $py -X utf8 -m tools.free_chat chat --session mem91-real-1 --real --api-url <测试接口> --model <模型> `
    --start 2026-11-02T20:00:00+08:00 --memory-decay on --max-calls 120 --max-tokens 400000
```

| 步 | 输入（自然地说，可以自己改写） | 之后 | 观察（用 `/state` 和 `/show` 核对，回复用 `/mark` 标记） |
|---|---|---|---|
| 1 | 闲聊几句后：“我家柯基叫豆豆，今天又把拖鞋咬了” | — | `/state`：有一条宠物记忆，来源是这条消息 |
| 2 | “下周三下午三点我有个实习面试，有点慌” | — | 一条 planned 事件，时间是那个周三 15:00 |
| 3 | “等我面试完，我们一起云看《海上钢琴师》好不好” | `/advance 3d` | 约定是另一条 planned（没有并进面试） |
| 4 | “今天食堂的麻辣烫好难吃” | — | Violet 不硬提面试、电影或豆豆（`/show` 里没有相关记忆一节） |
| 5 | 推进到周三晚上：`/advance` 到 20:30，然后“面试结束啦，感觉还行” | — | 回复接得上面试（不问“什么面试”）；后台把它更新为 done |
| 6 | “之前说好的那个电影，今晚看吧？” | — | 记得是约好还没看的电影，不说“上次一起看的” |
| 7 | “看完了，结尾哭死” | `/advance 10d` | 约定变为 done |
| 8 | “豆豆最近学会握手了” | — | 自然接上，知道豆豆是柯基；不重复问是什么狗 |
| 9 | “不对不对，它叫豆包，不叫豆豆，你记错啦”，过一轮再说“豆包今天好乖” | — | `/state`：记忆改成豆包，修改记录来源 user_confirmed（evidence 是原话）；之后用“豆包”，随口一句不会把名字改回去（后台纠正的路径已由离线测试覆盖，free_chat 没有后台） |
| 10 | “还记得我上次说的那家拉面店吗”（从来没说过） | `/advance 20d` | 不编店名，承认记不清或请对方说说 |
| 11 | 三周后：“还记得我们一起看的那部电影吗” | — | 记得片名、记得已经看过（衰减后仍在，标签不是“印象很模糊”） |
| 12 | “宝贝晚安” | — | 不突然翻出之前的亲密话题或面试 |

判定：每步记为通过 / 失败 / 无法验证，并写出依据（回复原文 + `/show` 里 Chat 实际收到的记忆行）。重点是第 5、6、8、10、11 步：想起来的内容对不对、状态（计划中 / 已结束）对不对、有没有把没发生的说成发生过、有没有反复问已经说过的事。预算：约 12 轮用户消息，每轮 3–5 次调用（Intent、Chat、可能的改写、Memory），预计 40–60 次调用。

**实际运行（2026-10-23，mem91-real-1）的结果与 Phase 9.1.1 的修改**：结论见 docs/handoff/log/2026-10-23-p911-claude.md，缺陷见 bugs-and-risks M10–M14。实际成本约 27000 token / 轮（MemoryAgent 每轮都运行，2–3 步，每步约 8000），比上表的估计高得多。整个会话的离线回放：tests/test_mem91_replay_p911.py。

**删除与遗忘（Phase 9.1.1）**：后台“删除”只把记忆标为已删除，聊天记录不动——最近的聊天里如果还有原话，Violet 仍能看到。“遗忘”另外把这条记忆来源那一轮里用户说这件事的分句、和 Violet 同一轮里相关的回复，在聊天记录和那一轮的整理任务里替换掉，记忆本身连同修改记录一起删除，不能恢复（`POST /api/control/memories/event/<id>/forget`，需要 `confirm: true`）。聊天里用户明确说“忘掉 / 别记 / 删掉”时，MemoryAgent 也可以遗忘（引用原话）。真实测试里离线删除用的 local/p91/real/delete_memory.py 走的是“删除”。

**Phase 9.1.2**：聊天里明确的遗忘请求改为在回复**之前**处理：用户的话只对上一条记忆时遗忘它，否则什么都不删，Violet 的回复按真实结果说（“已经忘掉了”或“没确定是哪件，没有删”）；这一轮不再交给 Performer，同一轮后台整理不会再补删。纠正一个事实后，同一事实在别的记忆里的旧说法会一并改正（确定是同一事实时），不确定的标为“待确认”（后台记忆列表里能看到，召回时 Violet 也会看到“其中“年糕”已被用户纠正为“汤圆””）。检查已有数据库里的旧冲突（只读，不修改；请用副本）：

```powershell
& $py -X utf8 -m tools.memory_conflicts --db <数据库副本>         # 加 --json 输出 JSON；有冲突时退出码 1
```

列出的三类：同一 key 两条有效档案（9.1.1 之前的纠正留下的）、已纠正的旧值还在某条记忆 / 核心记忆 / 档案里、待确认冲突。处理在后台用“纠正 / 停用 / 删除”。真实会话 p911-real-1 的离线回放：tests/test_p911_replay_p912.py。

## 23. Phase 9.2 统一工具管理与 MCP

设计见 decisions.md T34，问题见 bugs-and-risks.md P92-1–P92-6，测试见 testing.md §39，接口约定见 eve-mcp-contract.md。

**一轮里工具怎么用**：回复前先列出现在真的可用的外部工具（连上了、启用了、在白名单里）→ IntentAgent 看这份目录，需要时在 JSON 里加 `tool_query` → 只读查询留在 ChatAgent 这一轮：Chat 只拿到计划里的工具，第一次调用必须先用工具（`VIOLET_TOOL_CHOICE_REQUIRED=1`），按结果用平常的语气回答 → 执行类（写入）工具才交给 PerformerAgent，而且只有它真有可用工具时才建任务 → 工具没有 / 停用 / 连不上 / 没权限时不建任务，Chat 知道是哪一种，照实说查不到，不编造。闲聊时 Chat 不带外部工具（服务器设为 `"offer": "always"` 的除外，内置网页工具就是 always）。

**配置**：`MCP_SERVERS` 里的每一项可以是 `streamable_http` / `sse`（地址；非本机必须 https）或 `stdio`（本地子进程，`"command": ["python", "-m", "模块"]`，不占端口）。每个服务器的工具必须在 `mcp_tool_policy.json` 或它自己的 `"tools"` 里标 read / write / high_risk，没标的隐藏。管理后台的“工具”页能看连接状态和工具、停用服务器 / 单个工具、改可用的 Agent 和超时、替换凭据（只写，不显示原值）；“检查连接”会立刻重新连接。地址、命令、权限级别只能改 `.env` / 白名单文件。

**虚构工具试用（不联网、不调用真实模型）**：

```powershell
# MCP_SERVERS 里加（测试配置或本机 .env，不要用在生产）：
# {"name":"campus","transport":"stdio","command":["D:\\unity\\violet\\.venv\\Scripts\\python.exe","-m","tools.fake_mcp_server"],
#  "agents":["chat"],"tools":{"get_course_schedule":"read","get_weather":"read","search_web":"read"}}
# 故障演练：加 "env": {"FAKE_MCP_MODE": "error"}（工具报错）/ "crash"（调用中断开）/ "slow"（超时）
```

**小规模真实验证**：free_chat 新建会话时加 `--fake-tools on`，只接上面的虚构 campus 服务器（stdio 子进程，不联网；其他 MCP 仍然一律拒绝；之后不能换）。不用 `/advance` / `/check` 就不会有主动消息。会话里：

- 每轮回复后自动列出这一轮的工具步骤（`· 工具判断：route / tool_query → 计划`、`· 模型调用 ChatAgent：提供的外部工具、tool_choice → tool_calls(参数)`、`· MCP 工具(参数) → 成功 / 失败 status`）；`/show` 看全部（含工具目录、每次模型调用）。都存在 events.jsonl 的 `tool_trace` 里（已去掉密钥）。
- `/tool off get_weather` / `/tool on get_weather`：停用 / 启用一个虚构工具（和后台“工具”页同一份设置文件，下一轮生效）。
- `/fake-mode error`（或 crash / slow）：虚构服务器之后的调用都失败；`/fake-mode normal` 恢复。切换记录在 session.json 的 `tool_switches`。

```powershell
$env:VIOLET_TEST_ALLOW_REAL_API = "1"
& $py -X utf8 -m tools.free_chat chat --session p92-tools-1 --real --api-url <测试接口> --model <模型> --fake-tools on `
    --start 2026-11-02T19:30:00+08:00 --max-calls 60 --max-tokens 250000 --session-max-calls 60 --session-max-tokens 250000
```

模拟时间 2026-11-02 是周一（虚构课表：周二 10:00 数据结构；周三 08:00 线性代数、15:50 体育；虚构天气永远是“多云转小雨，14–21℃，降雨 70%”；搜索结果是没有具体内容的占位条目）。

| 轮 | 之前的命令 | 输入 | 应该看到 |
|---|---|---|---|
| 1 | — | 刚吃完晚饭，有点撑，躺着不想动 | 计划 none；Chat 请求里没有外部工具；没有 MCP 调用 |
| 2 | — | 对了，我明天上午有课吗？忘了 | tool_query 选 get_course_schedule；Chat 第一次调用 tool_choice=required；参数 day=周二；回复“10 点数据结构” |
| 3 | — | 明天杭州天气怎么样，要带伞吗 | get_weather(city=杭州)；回复按“多云转小雨、70%”建议带伞 |
| 4 | — | 最近有部电影叫《星河旅人》，网上评价怎么样？ | search_web；结果没有实际评价 → 不编造评价，照实说没查到什么具体的 |
| 5 | — | 帮我订一张周五去上海的高铁票吧 | 计划 blocked（not_configured / no_executor）或 none；没有任务；没有工具调用；照实说做不了 |
| 6 | `/tool off get_weather` | 那周五杭州天气呢？ | 目录里 get_weather 停用；没有对它的调用；回复不编造天气 |
| 7 | `/fake-mode error` | 我周三都有什么课？ | MCP 失败 status=tool_error；不重复调用、不建任务；回复照实说暂时查不到、没有技术信息 |
| 8 | `/fake-mode normal`、`/tool on get_weather` | 周三下午要是没课，我想去西湖边走走，你帮我看看那天下午有没有课、天气合不合适 | 两个工具都提供、都调用（课表 day=周三，天气 city=杭州）；回复：下午 15:50 有体育课、可能下雨 |
| 9 | — | 好啦不聊课了，今天有点累，想早点睡 | 计划 none、没有外部工具；语气和平常一样 |

预算紧时先保证 1–4 和 6–7（下面 §24 是 Phase 9.3 的联网验证）。每轮分开记录：Intent 是否要求工具（漏判）/ 提供了哪些工具 / 模型是否发起 tool_calls（没选、选错）/ 参数 / 中转接口是否拒绝 tool_choice=required（模型调用出错 BadRequestError 后紧接一次 auto）/ MCP 是否成功 / 结果是否进入 Chat 并被正确使用 / 回复是否自然。

## 24. Phase 9.3 联网搜索与网页阅读：配置与真实验证安排

设计见 decisions.md T35 / T36，测试见 testing.md §42–§45。

**配置**（.env；服务商、接口地址、网络限制只能在 .env 改）：

```
VIOLET_WEB_ENABLED=1
VIOLET_WEB_OFFER=on_demand                         # 默认；always = 每轮都给 Chat
VIOLET_WEB_SEARCH_PROVIDER=searxng                 # 默认（用户决定，decisions T36）：自建开源 SearXNG
VIOLET_WEB_SEARXNG_URL=http://127.0.0.1:18089      # deploy/searxng/ 的 compose 只发布到本机回环
```

采用的方式（用户决定）：Violet 照旧用 start.sh 跑在服务器上；SearXNG 跑在 Docker 里，只发布到宿主机 127.0.0.1:18089。部署（只在执行 `deploy/searxng/searxng.sh up` 时拉镜像、启动）见 deploy/searxng/README.md。

**连接与限制**：
- SearXNG 本身免费，替 Violet 去查公开搜索引擎；它从服务器发出的请求可能被部分引擎限流或拒绝（显示为“没有响应的引擎”）；全部失败时 Violet 报搜索失败，不说成没搜到。
- SafeFetcher 不使用任何代理（防止绕过地址检查），网页直连；SearXNG 地址是唯一受信任的内部地址，fetch_url 不能访问它。
- 每日上限 VIOLET_WEB_MAX_SEARCHES_PER_DAY（默认 30）/ VIOLET_WEB_MAX_FETCHES_PER_DAY（默认 40），后台可调；10 分钟内同样的搜索用缓存。

**博查 / Brave**：代码保留为可选实现（`VIOLET_WEB_SEARCH_PROVIDER=bocha|brave` + `VIOLET_WEB_SEARCH_API_KEY` 或后台替换；需要自己开通，付费），不是默认来源。它们的真实 API 验证已**取消**（用户决定，2026-10-24）：原来的阶段 A 冒烟脚本 local/p93/web_smoke.py（只在本机 local/ 里，不在 git 里）不再安排运行；free_chat 的 `--web on`（tests/test_free_chat_web_p93.py）保留，但只接 bocha / brave。

**SearXNG 部署后的真实验证（等服务器部署完成，需要用户另行授权）**：
1. 服务器上 `deploy/searxng/searxng.sh test`：SearXNG 能返回 JSON 结果，看“没有响应的引擎”。
2. Violet 的 .env 按上面配置、重启后，后台“工具”页显示“搜索 可用”。
3. 用真实模型做几轮联网对话（搜索最新信息、读用户给的网页、搜索后读原文、无结果、不联网的闲聊、SearXNG 停掉后的降级）。用 free_chat 做这一步需要先给 `--web on` 加 SearXNG 支持（它的连接复查现在只放行公网地址，要放行受信任的本机 SearXNG）——到时候作为单独的小改动提出。

**free_chat `--web on`（保留，只接 bocha / brave）**：只能在新建会话时选，只用于 `--real`；复用 violet-web + Tool Manager（Intent 决定何时联网，单工具 required、多工具 auto），不另起搜索实现。启动时先过模型的门（Key + yes），再过联网的门：搜索 Key（VIOLET_TEST_SEARCH_API_KEY 或隐藏输入）+ 第二个 yes。联网的 HTTP 请求按会话累计计数（web_usage.jsonl，每次连接前写入，跳转也算），`--session-max-searches` / `--session-max-pages` 新建时设定、之后只能调低；超过就在连接前拒绝，联网工具报错、Violet 照实说。网络守卫只放行模型接口和经过 SafeFetcher 地址检查、再次确认是公网 80/443 的联网连接（每次一个连接）。每轮记录：Intent 的 tool_query 和计划、给了哪些工具和 tool_choice、搜索词、每条结果的标题 / 链接 / 来源 / 发布时间、读到的网页标题 / 字数 / 发布时间、每个 HTTP 连接、回复里保留和去掉的链接、最终回复（events.jsonl；/show n 查看）。原来为博查准备的 6 轮方案见提交 dbf73e2 的本节（已取消）。

## 25. Phase 9.4.4 free_chat 的虚构课表（`--schedule on`）

为以后用真实模型验收“课表日常感知与主动联系”准备；本阶段只做了离线验证（tests/test_free_chat_schedule_p944.py，模拟模型）。

- 新建会话时加 `--schedule on`（之后不能改）：在会话自己的测试库（`local/free_chat/<会话>/violet/violet-test.db`）里放一份虚构课表，打开 `VIOLET_SCHEDULE_ENABLED` 和 `VIOLET_SCHEDULE_PROACTIVE`。课表按会话开始时间生成：开始那周是第 2 周；高数周一三五第 1–2 节，英语周二四第 3–4 节，物理实验周四下午双周，体育周五第 5–6 节；明天 10:00 一场高数期中；附近一节课改到 16:00、一节课停课。老师、教室、座位号、备注都是虚构的（用来检查它们没有进主动决策）。不会接触真实课表或 Violet 的真实数据库。
- `--schedule-context on`（可选，也要新建时选）：聊天时加“按课表对方可能在上课（到几点）”那一行（`VIOLET_SCHEDULE_CONTEXT`）。
- `/schedule`：闸门现在拦不拦（到几点）、今天和明天的安排、这次检查能提供的课表背景（明天考试 / 今天的课上完了 / 课多）、已经用过的背景、最近几次主动联系的状态和原因。`/schedule cancel [日期]`：列出那天可以停的课（不改任何东西）；`/schedule cancel 2026-10-30 08:00`：先显示“即将停课：日期 时间 课程”，输入 yes 才停（只改这个会话的虚构课表），下一次检查就按新课表算。2026-10-25 起不再自动挑“下一节”（真实验收里它停错了课）。
- `/advance` 时被课表拦下的检查记在 idle_checks 里（原因如 `schedule: class by the timetable until 09:45`）；进行了的检查在 checks 里（决策、调用、发出的内容）。
- 默认是模拟模型；真实模型仍然要 `--real` + `VIOLET_TEST_ALLOW_REAL_API=1` + 隐藏输入的 Key + yes，会话累计调用 / token 上限跨启动有效（§20）。真实验收需要用户另行授权。
- 建议的真实验收步骤（待授权）：从早上 07:30 开始，聊一句后 `/advance 1h`（上课中，看 idle 原因）、`/advance 3h`（下课后，看是否自然、是否说成一定下课）、傍晚 `/check`（明天考试：最多祝一次、不追问、不问考场座位），再 `/advance 1d` 确认不重复祝；中途 `/schedule cancel` 看改课表后立即生效。- 课表会话的证据（真实验收前补上，testing §51）：工具追踪也在 `--schedule on` 时打开（Intent 的 tool_query 和计划、给了哪些工具和 tool_choice、violet-schedule 调用和结果，结果最多 3000 字）；每一轮保存时带 `schedule_state`（闸门、今天算出来的安排、此刻可提供 / 已用过的课表背景）；每次进行了的主动检查带 `schedule`（检查时的闸门、提供给决策的背景、决策用了哪些）；`/show n` 都能看到。真实验收方案和逐行输入：local/p94/real-plan/PLAN.md（证据汇总脚本 evidence.py 在同一目录）。