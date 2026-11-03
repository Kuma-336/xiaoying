# 解决方案

## 技术取舍

项目使用 Python 3.11 标准库和原生循环，没有引入 Agent 框架。三小时级作业更需要让决策边界、幂等逻辑和失败状态容易审查；框架并不会替代这些业务实现。CLI 也比界面或服务部署更便于评审者复制运行。

Planner 有两个实现：

- `CompatibleLLMPlanner`：调用 OpenAI-compatible `/chat/completions`，每轮由真实模型选择单个动作；
- `MockPlanner`：确定性离线状态机，用于无密钥环境下验证循环、工具和安全边界。

Mock 只验证流程，不证明真实模型判断效果。

## 调用流程

```text
输入 JSON
   |
   v
创建单素材状态 ---- 最大 8 步
   |
   v
Planner 选择动作
   |
   +--> read_asset ------> 缺失？登记补充并结束
   |
   +--> check_rules -----> 阻断风险？返回风险并结束
   |
   +--> query_slots -----> 冲突？查找替代时段
   |                         |
   |                         +--> request_confirmation
   |
   +--> save_draft ------> 成功 / 已存在 / 明确失败
   |
   +--> finish
   v
结构化结果 + Trace（始终 published=false）
```

Agent 不把整条路径写死在执行器中。执行器只负责调用 Planner、执行被选择的工具、记录观察并限制步数。真实模型模式下，下一动作来自模型返回的结构化 JSON。

模型动作还要通过确定性安全校验：素材 ID 和硬性时间必须匹配当前请求；保存前必须完成素材读取、规则通过和时段查询；未确认的替代时段只能保存为 `pending_confirmation`。因此安全边界不只依赖提示词。

## 不同观察导致的不同路径

- `read_asset` 返回缺失字段：下一步是 `request_confirmation`，而不是规则检查。
- `check_rules` 返回阻断风险：下一步是 `finish`，不会查询时段或保存草稿。
- `query_slots` 返回可用：下一步直接保存正常草稿。
- `query_slots` 返回冲突：先登记替代时段确认，再保存 `pending_confirmation` 草稿。
- `save_draft` 返回失败：下一步结束并返回 `failed`。
- `save_draft` 返回已有记录：使用相同草稿 ID 正常结束。

## 工具职责

| 工具 | 职责 | 关键返回 |
|---|---|---|
| `read_asset` | 读取合成素材并检查必填字段 | 素材、缺失字段 |
| `check_rules` | 检查禁用表述、平台时长和金融声明 | 风险代码、严重级别 |
| `query_slots` | 对比已有排期和其他草稿 | 冲突来源、替代时段 |
| `request_confirmation` | 登记人工待确认事项 | 确认类型、提示、建议时间 |
| `save_draft` | 原子写入本地 JSON 草稿 | 新建、已存在或失败 |

系统没有发布工具，因此 Planner 无法越权执行真实发布。

## 幂等和冲突处理

草稿以 `request_id + asset_id` 作为幂等键。相同键和相同业务负载返回原草稿；相同键但不同负载返回 `idempotency_conflict`，防止静默覆盖。写入采用同目录临时文件替换，避免半写文件。

时段查询会考虑已有排期和其他请求的草稿，但忽略当前请求自己的幂等草稿，因此重复执行不会错误地产生新替代时间。

## 硬性时间保护

`requested_time` 从输入复制到结果，任何 Planner 都不能通过工具覆盖它。冲突时 `proposed_time` 只作为建议写入草稿；未收到 `confirmation.accepted_time` 时状态必须为 `pending_confirmation`，并返回 `hard_requirement_changed=false`。

## Trace 和安全上限

Trace 记录动作、参数、工具返回摘要、下一动作和一句简短业务依据。它不保存模型隐藏思维过程。每项素材默认最多 8 步；达到限制后追加 `step_limit` 记录并返回 `incomplete / step_limit_reached`。

