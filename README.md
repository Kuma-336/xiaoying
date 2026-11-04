# 题号 1：素材审核与发布排期 Agent

帮助内容团队读取素材、执行审核规则、检查发布时间冲突，并且只保存等待确认的排期草稿。系统没有真实发布工具，任何路径都固定返回 `published=false`。

## 评审入口

- 技术方案：[`docs/02-solution.md`](docs/02-solution.md)
- Agent 循环：[`src/agent.py`](src/agent.py)
- 业务工具：[`src/tools.py`](src/tools.py)
- 自动化测试：[`tests/test_acceptance.py`](tests/test_acceptance.py)
- 输入与预期：[`examples/`](examples/)

## 环境

- Windows、macOS 或 Linux
- uv 0.13.6
- Python 3.12（uv 会按 `.python-version` 创建环境）

Windows 安装 uv：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.13.6/install.ps1 | iex"
uv sync --locked
```

不要提交真实密钥。若密钥曾出现在聊天、日志或提交中，应立即撤销并轮换。

## 离线运行

离线 Mock 只验证流程、工具、安全边界和结果结构，不证明真实模型决策效果。

```powershell
uv run python -m src.cli run `
  --input examples/inputs/04-slot-conflict.json `
  --planner mock `
  --state-dir .runtime/review `
  --output result.json `
  --trace-output trace.json
```

运行测试：

```powershell
uv run python -m unittest discover -s tests -v
```

## DeepSeek 在线运行

先复制配置模板并通过进程环境变量提供轮换后的密钥：

```powershell
$env:DEEPSEEK_API_KEY="your-rotated-key"
$env:DEEPSEEK_BASE_URL="https://api.deepseek.com"
$env:DEEPSEEK_MODEL="deepseek-flash"

uv run python -m src.cli run-suite --planner deepseek
```

在线模式不会在失败时偷偷降级为 Mock。缺少密钥或接口失败会明确返回错误。

## 验收场景

| 场景 | 关键结果 |
|---|---|
| 正常排期 | 审核通过、时段可用、保存草稿 |
| 信息缺失 | 请求补充 `duration_seconds`，不排期、不保存 |
| 规则风险 | 返回禁用宣传词和缺少金融声明，阻断 |
| 时段冲突 | 保留 18:00、建议 19:00、未确认不保存 |
| 确认闭环 | 接受 19:00 后重新查询并保存草稿 |
| 重复提交 | 第二次返回原草稿，持久化记录仍为一条 |
| 保存失败 | 明确返回 `failed` 和错误码 |
| 执行上限 | 返回 `incomplete / execution_limit_reached` |

## 安全边界

- 没有发布工具，只能保存本地 JSON 草稿。
- 未确认的替代时间不能保存。
- 规则未通过、资料缺失或时段未重新查询时不能保存。
- 每项素材最多 10 个模型步骤和 8 次工具调用。
- Trace 不记录 API 密钥或模型隐藏思维过程。
