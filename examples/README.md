# 示例与运行记录

`inputs/` 保存六类验收输入，`outputs/` 保存由离线 CLI 实际产生的完整输出。每个输出的 `trace` 即运行记录，包含动作、参数、工具返回摘要、下一动作和简短决策依据。

重复提交使用同一状态目录连续执行两次：第一次输出为 `05-duplicate-first.json`，第二次为 `05-duplicate-second.json`。两次 `draft_id` 相同，第二次草稿状态为 `existing`。

`06-save-failure.json` 使用合成的 `simulate.save_failure=true` 注入存储失败，只用于验证失败处理。

