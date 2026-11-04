# 评估报告

## 自动化测试

执行：

```powershell
uv run python -m unittest discover -s tests -v
```

实际结果：11 项测试全部通过，覆盖八个业务场景、时段基准、安全守卫和 Trace 字段。测试摘要保存于 `examples/test-results.txt`。

## 场景结果

| 场景 | 实际 Mock 结果 | 输出与 Trace |
|---|---|---|
| 正常排期 | `completed / saved` | `01-normal-schedule.*.json` |
| 信息缺失 | `incomplete / not_attempted` | `02-missing-information.*.json` |
| 规则风险 | `blocked / not_attempted` | `03-rule-risk.*.json` |
| 时段冲突 | 保留 18:00、建议 19:00、未保存 | `04-slot-conflict.*.json` |
| 重复提交 | 第一次 `saved`，第二次 `existing`，草稿总数 1 | `05-duplicate-submission.*.json` |
| 保存失败 | `failed / storage_unavailable` | `06-save-failure.*.json` |
| 执行上限 | `incomplete / execution_limit_reached` | `07-execution-limit.*.json` |
| 确认闭环 | 重新查询 19:00 后 `completed / saved` | `08-confirmed-alternative.*.json` |

输出位于 `examples/outputs/`，逐步记录位于 `examples/traces/`。当前文件均由 CLI 实际运行生成，且明确标记 `planner_mode=mock`。

## 动态路径证据

正常路径在规则通过和时段可用后选择 `save_draft`；缺信息路径在读取结果中发现 `duration_seconds` 缺失后选择 `request_information`；风险路径在规则工具返回阻断风险后直接结束；冲突路径观察到 18:00 占用后选择 `request_confirmation`，并且不调用保存工具。

这些 Mock Trace 证明 Runtime 会把工具结果传回 Planner 并支持不同路径，但不证明真实模型判断质量。

## 在线验证状态

DeepSeek Planner 已实现，但未使用用户在聊天中暴露的密钥。该密钥必须撤销并轮换。设置新的 `DEEPSEEK_API_KEY` 后执行：

```powershell
uv run python -m src.cli run-suite --planner deepseek
```

该命令会以真实在线输出覆盖当前同名 Mock 输出和 Trace。最终提交前应确认所有文件的 `planner_mode=deepseek`。

## 已知限制

- JSON 文件存储没有并发锁，不是生产数据库。
- 规则和发布窗口是合成材料。
- 非思考模式仍可能产生非法工具参数；当前允许一次协议纠正。
- 在线模型结果具有波动性，自动化单元测试不依赖网络。
- 当前尚缺轮换密钥后的 DeepSeek 实际 Trace，因此真实模型效果仍未验证。
