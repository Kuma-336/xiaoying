# 解决方案

## 技术取舍

项目使用 Python 3.12、uv 和原生 Agent 循环。没有引入 Agent 框架，因为本题的核心是展示模型如何选择动作、消费工具观察并改变下一步；原生实现更容易审查工具参数、安全守卫、幂等逻辑和停止条件。

在线 Planner 使用 OpenAI Python SDK 连接 DeepSeek 的 OpenAI-compatible Chat Completions。默认模型为 `deepseek-flash`，使用非思考模式和原生函数工具调用。离线 `MockPlanner` 是确定性测试替身，只证明流程和工程边界。

## 架构

```text
CLI 输入
  |
  v
AgentRuntime <------ DeepSeekPlanner / MockPlanner
  |                         |
  | 选择一个动作             | 最新工具观察
  v                         |
Tool Registry --------------+
  |
  +-- read_asset
  +-- check_rules
  +-- query_slots
  +-- request_information
  +-- request_confirmation
  +-- save_draft
  +-- finish
  |
  v
结构化结果 + 独立 Trace + 本地草稿状态
```

`test_controls` 只由 CLI 测试驱动器读取，不会进入模型上下文。模型看见业务请求、已有观察和剩余执行额度。

## ReAct 循环

每轮只允许一个工具调用：

1. Planner 根据请求和已有观察选择动作。
2. Runtime 验证工具名、必填参数、素材/请求一致性和安全前置条件。
3. 本地工具执行并返回结构化观察。
4. 在线模式把 assistant tool call 与 tool result 追加到对话。
5. 模型基于新观察选择下一动作或调用 `finish`。

模型返回多个工具、非法 JSON 或缺少 `decision_basis` 时，Runtime 允许一次协议纠正；再次失败返回 `planner_protocol_error`。接口失败返回 `planner_failed`，不会自动切换到 Mock。

## 工具职责

| 工具 | 职责 | 关键结果 |
|---|---|---|
| `read_asset` | 读取指定素材版本并检查必填信息 | 素材、缺失字段、版本错误 |
| `check_rules` | 检查内容、金融声明、授权和平台时长 | 风险代码、严重级别、是否通过 |
| `query_slots` | 检查平台账号时段和发布窗口 | 冲突来源、可用状态、替代时段 |
| `request_information` | 登记资料补充项 | 缺失字段、待确认状态 |
| `request_confirmation` | 登记替代时间确认 | 原时间、建议时间、待确认状态 |
| `save_draft` | 原子且幂等地保存本地草稿 | saved、existing 或 failed |
| `finish` | 结束当前素材处理 | completed、blocked、pending 等 |

业务工具只使用本地合成数据。系统没有发布工具，因此模型无法执行真实发布。

## 动态路径

- 信息缺失：`read_asset` 返回缺失字段，模型转为 `request_information`，不得查询时段。
- 规则风险：`check_rules` 返回阻断风险，模型以 `blocked` 结束，不得保存。
- 正常排期：规则通过且 `query_slots.available=true`，模型保存草稿。
- 时段冲突：18:00 冲突，工具返回 19:00，模型请求确认并结束；首次运行不保存。
- 确认闭环：用户输入接受 19:00，模型必须再次查询 19:00，仍可用才保存。
- 保存失败：模型观察到 `storage_unavailable` 后以 `failed` 结束。

这些路径共享执行骨架，但后续动作由 Planner 根据工具结果选择，不是由 Runtime 写死完整流程。

## 硬性时间和保存守卫

Runtime 在模型之外执行确定性校验：

- `requested_start` 必须等于原请求。
- `save_draft.scheduled_start` 必须与最近一次可用时段观察一致。
- 替代时间必须存在匹配的 `accepted` 用户确认。
- 资料缺失、规则未通过或时段不可用时禁止保存。
- 所有输出固定 `published=false`。

草稿以 `request_id + asset_id + asset_version` 为幂等键。相同键和相同负载返回原草稿；相同键但不同负载返回 `idempotency_conflict`。写入使用同目录临时文件后原子替换。

## 结果与 Trace

结果把两个容易混淆的状态分开：

- `draft.save_status`：`saved`、`existing`、`failed`、`not_attempted`。
- `draft.workflow_status`：`draft_saved`、`not_created`。

Trace 每步包含步骤号、模型、动作、脱敏参数、工具返回摘要、下一动作、简短 `decision_basis` 和时间戳。`decision_basis` 是业务依据，不是隐藏思维过程；`reasoning_content` 不保存。

## 执行上限与失败

每项素材默认最多 10 个模型步骤和 8 次工具调用。达到任一上限后追加 `execution_limit` Trace，并返回 `incomplete / execution_limit_reached`。

保存失败、模型失败、协议失败、参数错误和幂等冲突都有独立错误码。程序不把失败描述成草稿成功或发布成功。

## uv 与运行方式

项目以 `pyproject.toml` 和 `uv.lock` 为唯一依赖来源，固定 Python 3.12 和 `openai==3.44.0`。

```powershell
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python -m src.cli run-suite --planner deepseek
```

DeepSeek 密钥仅从 `DEEPSEEK_API_KEY` 环境变量读取。`.env.example` 只提供空占位，`.env` 和 `.runtime/` 被 Git 忽略。

