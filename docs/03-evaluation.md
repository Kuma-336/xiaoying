# 评估报告

## 验收结果

执行命令：

```powershell
python -m unittest discover -s tests -v
```

实际结果：10 项测试全部通过。原始摘要见 [`examples/test-results.txt`](../examples/test-results.txt)。除六项业务验收外，还覆盖步数上限、Trace 字段、越序保存和硬性时间篡改。

| 验收点 | 预期 | 实际结果与证据 |
|---|---|---|
| 正常排期 | 审核通过、时段可用、保存草稿 | `draft_saved / saved`；[`01-normal.json`](../examples/outputs/01-normal.json) |
| 信息缺失 | 指出字段、请求补充、不保存 | 缺少 `duration_seconds`，`not_attempted`；[`02-missing-information.json`](../examples/outputs/02-missing-information.json) |
| 规则风险 | 返回风险并阻断排期 | 命中禁用表述和缺少金融声明；[`03-rule-risk.json`](../examples/outputs/03-rule-risk.json) |
| 时段冲突 | 保留 18:00，建议 19:00，等待确认 | `pending_confirmation` 且 `hard_requirement_changed=false`；[`04-slot-conflict.json`](../examples/outputs/04-slot-conflict.json) |
| 重复提交 | 只产生一份草稿 | 第二次为 `existing` 且草稿 ID 相同；[`第一次`](../examples/outputs/05-duplicate-first.json)、[`第二次`](../examples/outputs/05-duplicate-second.json) |
| 保存失败 | 明确失败，不伪装已保存 | `failed / storage_unavailable`，CLI 退出码 1；[`06-save-failure.json`](../examples/outputs/06-save-failure.json) |
| 步数上限 | 明确返回未完成 | 自动化测试断言 `incomplete / step_limit_reached` |
| 不真实发布 | 所有路径均不发布 | 顶层和单项结果始终 `published=false` |

## 两条差异化决策路径

正常路径的 Trace 为 `read_asset → check_rules → query_slots → save_draft → finish`。冲突路径为 `read_asset → check_rules → query_slots → request_confirmation → save_draft → finish`。规则风险路径在规则检查后直接结束。这些记录证明执行器会把不同工具观察交回 Planner，并得到不同后续动作。

## 测试方法

测试使用 `unittest` 和临时状态目录，不依赖网络或真实密钥。每个用例创建独立工具实例；重复提交用例故意复用同一状态目录并检查持久化文件中只有一条记录。

保存失败通过输入中的 `simulate.save_failure=true` 注入。该字段仅为本地合成工具的测试能力，不属于生产接口。

## 失败与限制

- 当前运行环境没有真实模型密钥，因此实际输出由离线 Mock 生成；它证明工程流程，不证明真实模型决策质量。
- OpenAI-compatible 服务对 `response_format` 的支持可能不同；接入其他供应商时可能需要调整请求参数。
- JSON 文件存储适合演示，不支持并发锁、事务恢复或跨进程高并发。
- 时段粒度固定按一小时寻找两个候选，没有处理素材时长、跨时区工作日或渠道配额。
- 规则为合成示例，真实业务应由合规人员维护版本、优先级和解释。
- 真实模型可能返回非法动作；当前会返回 `planner_failed`，尚未增加一次结构化纠错提示。

## 未完成部分

真实模型端到端 Trace、线上部署、界面、多 Agent、生产数据库、鉴权与真实发布均未实现。真实发布属于明确的安全边界，而不是遗漏功能。

