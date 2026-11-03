# 素材审核与发布排期 Agent

一个面向内容团队的 CLI Agent：读取素材、执行审核规则、检查发布时间冲突，并且只保存等待人工确认的排期草稿。项目没有真实发布工具，任何结果都不会被描述为“已发布”。

题号：待提交者补充。

## 评审入口

- 核心循环：[`src/agent.py`](src/agent.py)
- 模型与离线 Planner：[`src/planners.py`](src/planners.py)
- 业务工具与幂等保存：[`src/tools.py`](src/tools.py)
- 验收测试：[`tests/test_agent.py`](tests/test_agent.py)
- 六类实际运行结果：[`examples/outputs/`](examples/outputs/)
- 验收结果矩阵：[`docs/03-evaluation.md`](docs/03-evaluation.md)

## 环境与安装

- Python 3.11 或更高版本
- 离线模式没有第三方依赖，不需要密钥

```powershell
git clone https://github.com/Kuma-336/xiaoying.git
cd xiaoying
python --version
python -m unittest discover -s tests -v
```

## 最短运行命令

离线评审（Mock 只验证流程边界，不证明真实模型决策质量）：

```powershell
python -m src.cli run --input examples/inputs/04-slot-conflict.json --planner mock --state-dir .runtime/review
```

预期关键结果：M01 的 `18:00` 保留为 `requested_time`，建议 `19:00`，状态为 `pending_confirmation`，`published=false`。

保存输出文件：

```powershell
python -m src.cli run --input examples/inputs/01-normal.json --planner mock --state-dir .runtime/demo --output result.json
```

真实模型模式（支持实现 `/chat/completions` 的 OpenAI-compatible 服务）：

```powershell
Copy-Item .env.example .env
$env:LLM_API_KEY="your-key"
$env:LLM_BASE_URL="https://api.openai.com/v1"
$env:LLM_MODEL="your-model-id"
python -m src.cli run --input examples/inputs/04-slot-conflict.json --planner llm --state-dir .runtime/llm-review
```

程序不会自动读取 `.env`，避免隐式依赖；请通过进程环境变量注入配置。模型 ID 不设默认值，以免依赖已经弃用或供应商不支持的模型。不要提交真实密钥。

## ReAct 决策循环

每项素材都执行有上限的循环：Planner 选择一个动作，Agent 调用对应工具，将结果摘要加入状态，再让 Planner 决定下一步或结束。可用动作包括：

1. `read_asset`：读取素材与检查必填字段；
2. `check_rules`：检查禁用表述、时长和声明；
3. `query_slots`：检查冲突并返回替代时段；
4. `request_confirmation`：登记补充信息或替代时段确认；
5. `save_draft`：幂等保存草稿；
6. `finish`：由 Planner 根据观察结果结束。

默认每项素材最多 8 步。达到上限返回 `incomplete / step_limit_reached`，不会继续调用工具。

## 输出字段

每项素材返回审核状态、风险原因、原要求时间、建议时间、待确认项和草稿保存状态。顶层 `trace` 包含步骤、工具参数、返回摘要、下一动作和简短业务依据。`decision_basis` 是可审计的简要依据，不是模型内部思维过程。

## 验收覆盖

| 场景 | 输入 | 实际输出 |
|---|---|---|
| 正常排期 | `01-normal.json` | `01-normal.json` |
| 素材信息缺失 | `02-missing-information.json` | `02-missing-information.json` |
| 规则风险 | `03-rule-risk.json` | `03-rule-risk.json` |
| 时段冲突 | `04-slot-conflict.json` | `04-slot-conflict.json` |
| 重复提交 | `05-duplicate.json` | `05-duplicate-first.json`、`05-duplicate-second.json` |
| 保存失败 | `06-save-failure.json` | `06-save-failure.json` |

输入位于 `examples/inputs/`，输出位于 `examples/outputs/`。完整断言和限制见 [`docs/03-evaluation.md`](docs/03-evaluation.md)。

## 范围与限制

- 仅有 CLI，无界面、线上部署、多 Agent 或生产级 Runtime。
- 数据、规则、排期和失败注入均为本地合成材料。
- `mock` 是可复现的离线状态机，只证明执行链路、边界和工具行为。
- 本次环境未配置模型密钥，因此未宣称真实模型决策效果；真实模式入口已实现，接入后应补充真实模型 Trace。
- 当前不包含真实发布工具，保存结果始终是草稿。


