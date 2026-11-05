from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .models import Action, RunState
from .planners import Planner, PlannerProtocolError
from .tools import BusinessTools, summarize_result


class AgentRuntime:
    def __init__(self, tools: BusinessTools, planner: Planner, max_steps: int, max_tool_calls: int):
        self.tools = tools
        self.planner = planner
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls

    def run(self, scenario_id: str, request: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        state = RunState(scenario_id, request, self.max_steps, self.max_tool_calls)
        protocol_errors = 0
        forced_status: str | None = None
        forced_reason: str | None = None
        while True:
            if state.model_steps >= state.max_steps or state.tool_calls >= state.max_tool_calls:
                forced_status, forced_reason = "incomplete", "execution_limit_reached"
                self._append_limit_trace(state)
                break
            try:
                action = self.planner.next_action(state, self.tools.specs)
                state.model_steps += 1
            except PlannerProtocolError as exc:
                state.model_steps += 1
                protocol_errors += 1
                self._append_error_trace(state, "planner_protocol_error", str(exc))
                if protocol_errors > 1:
                    forced_status, forced_reason = "failed", "planner_protocol_error"
                    break
                state.conversation.append(
                    {
                        "role": "system",
                        "content": "上次响应不符合协议。请只调用一个已声明工具，并提供合法参数和 decision_basis。",
                    }
                )
                continue
            except Exception as exc:
                state.model_steps += 1
                self._append_error_trace(state, "planner_failed", str(exc))
                forced_status, forced_reason = "failed", "planner_failed"
                break
            validation_error = self._validate_action(state, action)
            if action.name == "finish":
                observation = validation_error or {"ok": True, "finished": True, **action.arguments}
                self._append_trace(state, action, observation)
                if validation_error:
                    forced_status, forced_reason = "failed", validation_error["error_code"]
                else:
                    forced_status = action.arguments["completion_status"]
                break
            state.tool_calls += 1
            observation = validation_error or self.tools.execute(action.name, action.arguments)
            state.add_observation(action.name, observation)
            self._append_trace(state, action, observation)
            if action.assistant_message:
                state.conversation.append(action.assistant_message)
                state.conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": action.tool_call_id,
                        "content": json.dumps(observation, ensure_ascii=False),
                    }
                )
        self._link_next_actions(state.trace)
        return self._build_result(state, forced_status, forced_reason), state.trace

    def _validate_action(self, state: RunState, action: Action) -> dict[str, Any] | None:
        specs = {item["function"]["name"]: item["function"]["parameters"] for item in self.tools.specs}
        schema = specs.get(action.name)
        if schema is None:
            return self._error("action_not_allowed", f"不允许的动作：{action.name}")
        missing = [field for field in schema["required"] if field not in action.arguments]
        extra = [field for field in action.arguments if field not in schema["properties"]]
        if missing or extra:
            return self._error("invalid_action_arguments", f"缺少={missing}，多余={extra}")
        invalid_enums = [
            field
            for field, definition in schema["properties"].items()
            if "enum" in definition
            and field in action.arguments
            and action.arguments[field] not in definition["enum"]
        ]
        if invalid_enums:
            return self._error("invalid_action_arguments", f"枚举值不合法：{invalid_enums}")
        if action.name == "finish" and action.arguments["completion_status"] not in {
            "completed",
            "incomplete",
            "blocked",
            "pending_confirmation",
            "failed",
        }:
            return self._error("invalid_completion_status", "finish 使用了不允许的完成状态")
        request = state.request
        if action.arguments.get("asset_id", request["asset_id"]) != request["asset_id"]:
            return self._error("asset_mismatch", "动作素材与当前请求不一致")
        if action.arguments.get("asset_version", request["asset_version"]) != request["asset_version"]:
            return self._error("asset_version_mismatch", "动作素材版本与当前请求不一致")
        if action.arguments.get("request_id", request["request_id"]) != request["request_id"]:
            return self._error("request_mismatch", "动作请求 ID 与当前请求不一致")
        if action.name == "request_information":
            read = state.last_observation("read_asset") or {}
            actual_missing = sorted(read.get("missing_fields", []))
            requested_missing = sorted(action.arguments["missing_fields"])
            if not actual_missing or requested_missing != actual_missing:
                return self._error("unsafe_sequence", "只有素材确实缺少字段时才能请求补充信息")
        if action.name == "request_confirmation":
            slots = state.last_observation("query_slots") or {}
            alternatives = slots.get("alternatives", [])
            requirement = request["publish_requirement"]
            requested = requirement.get("requested_start") or requirement.get("earliest_start")
            if (
                slots.get("available") is not False
                or action.arguments["requested_start"] != requested
                or action.arguments["suggested_start"] not in alternatives
            ):
                return self._error("unsafe_sequence", "替代时间确认必须基于最近一次冲突查询的候选结果")
        if action.name != "save_draft":
            return None
        read = state.last_observation("read_asset") or {}
        rules = state.last_observation("check_rules") or {}
        slots = state.last_observation("query_slots") or {}
        if not read.get("ok") or read.get("missing_fields"):
            return self._error("unsafe_sequence", "素材信息不完整，禁止保存")
        if not rules.get("passed"):
            return self._error("unsafe_sequence", "规则未通过，禁止保存")
        if not slots.get("available") or slots.get("candidate_start") != action.arguments["scheduled_start"]:
            return self._error("unsafe_sequence", "保存前必须确认同一候选时段仍可用")
        requirement = request["publish_requirement"]
        requested = requirement.get("requested_start") or requirement.get("earliest_start")
        if action.arguments["requested_start"] != requested:
            return self._error("hard_requirement_violation", "动作修改了原发布时间要求")
        scheduled = action.arguments["scheduled_start"]
        if scheduled != requested:
            accepted = any(
                item.get("confirmation_type") == "alternative_time"
                and item.get("status") == "accepted"
                and item.get("requested_start") == requested
                and item.get("suggested_start") == scheduled
                for item in request.get("confirmations", [])
            )
            if not accepted or action.arguments["confirmation_status"] != "accepted":
                return self._error("hard_requirement_violation", "替代时间未经用户明确接受")
        return None

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error_code": code, "message": message, "retryable": False}

    def _append_trace(self, state: RunState, action: Action, observation: dict[str, Any]) -> None:
        parameters = {key: value for key, value in action.arguments.items() if key != "decision_basis"}
        state.trace.append(
            {
                "step": len(state.trace) + 1,
                "model": action.model,
                "selected_action": action.name,
                "tool_parameters": parameters,
                "tool_result_summary": summarize_result(action.name, observation),
                "next_action": None,
                "decision_basis": action.decision_basis,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _append_error_trace(self, state: RunState, code: str, message: str) -> None:
        state.trace.append(
            {
                "step": len(state.trace) + 1,
                "model": self.planner.model_name,
                "selected_action": code,
                "tool_parameters": {},
                "tool_result_summary": message[:300],
                "next_action": None,
                "decision_basis": "模型调用或协议失败。",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _append_limit_trace(self, state: RunState) -> None:
        state.trace.append(
            {
                "step": len(state.trace) + 1,
                "model": self.planner.model_name,
                "selected_action": "execution_limit",
                "tool_parameters": {},
                "tool_result_summary": "达到模型步骤或工具调用上限",
                "next_action": None,
                "decision_basis": "停止循环以避免失控执行。",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    @staticmethod
    def _link_next_actions(trace: list[dict[str, Any]]) -> None:
        for current, following in zip(trace, trace[1:]):
            current["next_action"] = following["selected_action"]

    def _build_result(
        self,
        state: RunState,
        forced_status: str | None,
        forced_reason: str | None,
    ) -> dict[str, Any]:
        request = state.request
        requirement = request["publish_requirement"]
        requested = requirement.get("requested_start") or requirement.get("earliest_start")
        read = state.last_observation("read_asset") or {}
        rules = state.last_observation("check_rules") or {}
        slots = state.last_observation("query_slots") or {}
        info_observation = state.last_observation("request_information")
        confirmation_observation = state.last_observation("request_confirmation")
        info = info_observation if info_observation and info_observation.get("ok") else None
        confirmation = (
            confirmation_observation
            if confirmation_observation and confirmation_observation.get("ok")
            else None
        )
        saved = state.last_observation("save_draft")
        risks = rules.get("risks", [])
        blocking = any(risk.get("severity") in {"block", "needs_information"} for risk in risks)
        suggested = requested
        if confirmation:
            suggested = confirmation["suggested_start"]
        elif slots.get("available"):
            suggested = slots.get("candidate_start", requested)
        if forced_status:
            overall = forced_status
        elif saved and not saved.get("ok"):
            overall = "failed"
        elif read.get("missing_fields"):
            overall = "incomplete"
        elif blocking:
            overall = "blocked"
        elif confirmation:
            overall = "pending_confirmation"
        elif saved and saved.get("ok"):
            overall = "completed"
        else:
            overall = "incomplete"
        if saved is None:
            draft = {"save_status": "not_attempted", "workflow_status": "not_created", "draft_id": None}
        elif saved.get("ok"):
            draft = {
                "save_status": saved["save_status"],
                "workflow_status": saved["workflow_status"],
                "draft_id": saved["draft_id"],
                "created_new_draft": saved["created_new_draft"],
            }
        else:
            draft = {
                "save_status": "failed",
                "workflow_status": "not_created",
                "draft_id": saved.get("draft_id"),
                "error_code": saved.get("error_code"),
                "message": saved.get("message"),
                "retryable": saved.get("retryable", False),
            }
        pending = []
        if info:
            pending.append(info)
        if confirmation:
            pending.append(confirmation)
        review_status = "incomplete" if read.get("missing_fields") else ("blocked" if blocking else "approved")
        schedule_status = "not_attempted" if review_status != "approved" else ("alternative_suggested" if confirmation else "suggested")
        accepted_alternative = any(
            item.get("confirmation_type") == "alternative_time"
            and item.get("status") == "accepted"
            and item.get("suggested_start") == suggested
            for item in request.get("confirmations", [])
        )
        return {
            "run_id": str(uuid.uuid4()),
            "scenario_id": state.scenario_id,
            "request_id": request["request_id"],
            "planner_mode": self.planner.mode,
            "model": self.planner.model_name,
            "overall_status": overall,
            "completion_reason": forced_reason,
            "asset_id": request["asset_id"],
            "review_status": review_status,
            "risk_reasons": risks,
            "risk_codes": [risk.get("risk_code") for risk in risks],
            "missing_fields": read.get("missing_fields", []),
            "requested_start": requested,
            "suggested_start": suggested,
            "hard_requirement_changed": False,
            "hard_requirement_change_authorized": accepted_alternative,
            "schedule_status": schedule_status,
            "pending_confirmations": pending,
            "pending_confirmation_count": len(pending),
            "draft": draft,
            "published": False,
            "usage": {"model_steps": state.model_steps, "tool_calls": state.tool_calls},
        }
