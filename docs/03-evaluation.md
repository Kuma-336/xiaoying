# 评估报告

## 自动化测试

执行：

```powershell
uv run python -m unittest discover -s tests -v
```

实际结果：11 项测试全部通过，覆盖八个业务场景、时段基准、安全守卫和 Trace 字段。测试摘要保存于 `examples/test-results.txt`。

## 场景结果

| 场景 | 实际 DeepSeek 结果 | 输出与 Trace |
|---|---|---|
| 正常排期 | `completed / saved` | `01-normal-schedule.*.json` |
| 信息缺失 | `incomplete / not_attempted` | `02-missing-information.*.json` |
| 规则风险 | `blocked / not_attempted` | `03-rule-risk.*.json` |
| 时段冲突 | 保留 18:00、建议 19:00、未保存 | `04-slot-conflict.*.json` |
| 重复提交 | 第一次 `saved`，第二次 `existing`，草稿总数 1 | `05-duplicate-submission.*.json` |
| 保存失败 | `failed / storage_unavailable` | `06-save-failure.*.json` |
| 执行上限 | `incomplete / execution_limit_reached` | `07-execution-limit.*.json` |
| 确认闭环 | 重新查询 19:00 后 `completed / saved` | `08-confirmed-alternative.*.json` |

输出位于 `examples/outputs/`，逐步记录位于 `examples/traces/`。当前文件均于 2026-11-05 由 CLI 调用 `deepseek-flash` 实际生成，并明确标记 `planner_mode=deepseek`。

## 动态路径证据

正常路径在规则通过和时段可用后选择 `save_draft`；缺信息路径在读取结果中发现 `duration_seconds` 缺失后选择 `request_information`；风险路径在规则工具返回阻断风险后直接结束；冲突路径观察到 18:00 占用后选择 `request_confirmation`，并且不调用保存工具。保存失败路径中，模型观察失败后尝试再次保存，第二次仍失败后选择 `finish` 并返回失败。

这些在线 Trace 证明模型会根据不同工具观察调整后续动作。离线 Mock 测试仍只用于稳定验证流程和安全边界。

## 在线验证

使用轮换后的本地 `.env` 配置执行：

```powershell
uv run python -m src.cli run-suite --planner deepseek
```

八个场景全部完成，业务不变量错误为 0，Trace 字段错误为 0，所有输出均为 `published=false`。密钥只存在于被 Git 忽略的 `.env`，仓库文件扫描未发现密钥格式。

## 已知限制

- JSON 文件存储没有并发锁，不是生产数据库。
- 规则和发布窗口是合成材料。
- 非思考模式仍可能产生非法工具参数；当前允许一次协议纠正。
- 在线模型结果具有波动性，自动化单元测试不依赖网络。
