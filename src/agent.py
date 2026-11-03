from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import Action, AssetState
from .planners import Planner
from .tools import BusinessTools, summarize_observation


class SchedulingAgent:
    def __init__(self, tools: BusinessTools, planner: Planner, max_steps: int = 8):
        if max_steps < 1:
            raise ValueError("max_steps 必须大于 0")
        self.tools = tools
        self.planner = planner
        self.max_steps = max_steps

    def run(self, request: dict[str, Any]) -> dict[str, Any]:
        request_id = request["request_id"]
        items: list[dict[str, Any]] = []
        all_traces: list[dict[str, Any]] = []
        for asset_request in request.get("assets", []):
            state = AssetState(
                request_id=request_id,
                asset_id=asset_request["asset_id"],
                requested_time=asset_request["requested_time"],
                confirmation=asset_request.get("confirmation", {}),
                simulate=asset_request.get("simulate", {}),
                max_steps=self.max_steps,
            )
            item = self._run_asset(state)
            items.append(item)
            all_traces.extend(state.trace)
        status = self._overall_status(items)
        return {
            "request_id": request_id,
            "status": status,
            "planner_mode": self.planner.mode,
            "published": False,
            "items": items,
            "trace": all_traces,
        }

    def _run_asset(self, state: AssetState) -> dict[str, Any]:
        for step in range(1, self.max_steps + 1):
            try:
                action = self.planner.next_action(state, self.tools.tool_specs)
            except Exception as exc:  # 模型/网络异常需要形成明确业务结果
                state.trace.append(
                    self._trace_entry(state, step, "planner_error", {}, {"ok": False, "error": str(exc)}, "Planner 调用失败。")
                )
                return self._build_result(state, "failed", "planner_failed")
            if action.name == "finish":
                state.trace.append(
                    self._trace_entry(state, step, "finish", {}, {"ok": True, "finished": True}, action.decision_basis)
                )
                self._link_next_actions(state.trace)
                return self._build_result(state)
            policy_error = self._validate_action(state, action)
            observation = policy_error or self.tools.execute(action.name, action.arguments, state.simulate)
            state.observations[action.name] = observation
            state.trace.append(
                self._trace_entry(state, step, action.name, action.arguments, observation, action.decision_basis)
            )
        state.trace.append(
            self._trace_entry(
                state,
                self.max_steps + 1,
                "step_limit",
                {},
                {"ok": False, "error": "step_limit_reached"},
                "达到最大执行步数，停止处理以避免失控循环。",
            )
        )
        self._link_next_actions(state.trace)
        return self._build_result(state, "incomplete", "step_limit_reached")

    def _validate_action(self, state: AssetState, action: Action) -> dict[str, Any] | None:
        """在工具层前执行确定性安全约束，不能只依赖模型提示词。"""
        valid_names = {spec["name"] for spec in self.tools.tool_specs}
        if action.name not in valid_names:
            return {"ok": False, "error": "action_not_allowed", "message": f"不允许的动作：{action.name}"}
        if "asset_id" in action.arguments and action.arguments["asset_id"] != state.asset_id:
            return {"ok": False, "error": "asset_mismatch", "message": "动作素材 ID 与当前素材不一致"}
        if "requested_time" in action.arguments and action.arguments["requested_time"] != state.requested_time:
            return {
                "ok": False,
                "error": "hard_requirement_violation",
                "message": "动作试图修改用户硬性发布时间",
            }
        if action.name != "save_draft":
            return None
        read = state.observations.get("read_asset", {})
        rules = state.observations.get("check_rules", {})
        slots = state.observations.get("query_slots", {})
        if not read.get("ok") or read.get("missing_fields"):
            return {"ok": False, "error": "unsafe_sequence", "message": "素材未完整读取，禁止保存草稿"}
        if not rules.get("ok") or not rules.get("passed"):
            return {"ok": False, "error": "unsafe_sequence", "message": "规则未通过，禁止保存草稿"}
        if not slots.get("ok"):
            return {"ok": False, "error": "unsafe_sequence", "message": "时段尚未查询，禁止保存草稿"}
        proposed = action.arguments.get("proposed_time")
        if proposed != state.requested_time:
            if proposed not in slots.get("alternatives", []):
                return {"ok": False, "error": "invalid_alternative", "message": "建议时段不在工具返回的候选中"}
            accepted = state.confirmation.get("accepted_time") == proposed
            pending = action.arguments.get("pending_confirmations", [])
            pending_match = any(item.get("proposed_time") == proposed for item in pending)
            if not accepted and (action.arguments.get("review_status") != "pending_confirmation" or not pending_match):
                return {
                    "ok": False,
                    "error": "hard_requirement_violation",
                    "message": "替代时段未经确认，必须作为 pending_confirmation 草稿保存",
                }
        elif not slots.get("available"):
            return {"ok": False, "error": "slot_conflict", "message": "冲突时段不能保存为已通过草稿"}
        return None

    def _trace_entry(
        self,
        state: AssetState,
        step: int,
        action: str,
        arguments: dict[str, Any],
        observation: dict[str, Any],
        basis: str,
    ) -> dict[str, Any]:
        return {
            "asset_id": state.asset_id,
            "step": step,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "tool_parameters": arguments,
            "return_summary": summarize_observation(observation),
            "decision_basis": basis,
            "next_action": None,
        }

    @staticmethod
    def _link_next_actions(trace: list[dict[str, Any]]) -> None:
        for current, following in zip(trace, trace[1:]):
            current["next_action"] = following["action"]

    def _build_result(
        self, state: AssetState, forced_status: str | None = None, reason: str | None = None
    ) -> dict[str, Any]:
        read = state.observations.get("read_asset", {})
        rules = state.observations.get("check_rules", {})
        slots = state.observations.get("query_slots", {})
        confirmation = state.observations.get("request_confirmation")
        save = state.observations.get("save_draft")
        missing = read.get("missing_fields", [])
        risks = rules.get("risks", [])
        pending = []
        if confirmation:
            pending.append(
                {
                    "kind": confirmation.get("kind"),
                    "message": confirmation.get("message"),
                    "proposed_time": confirmation.get("proposed_time"),
                }
            )
        suggested = state.requested_time
        if slots and not slots.get("available", True) and slots.get("alternatives"):
            suggested = slots["alternatives"][0]

        if forced_status:
            completion = forced_status
        elif save and not save.get("ok"):
            completion = "failed"
            reason = save.get("error", "save_failed")
        elif missing or not read.get("ok", False):
            completion = "incomplete"
            reason = "missing_information"
        elif risks:
            completion = "blocked"
            reason = "rule_risk"
        elif save and save.get("ok"):
            completion = "pending_confirmation" if pending else "draft_saved"
            reason = "alternative_time_confirmation_required" if pending else "ready_for_human_confirmation"
        else:
            completion = "incomplete"
            reason = reason or "not_saved"

        draft_status: dict[str, Any]
        if save is None:
            draft_status = {"status": "not_attempted", "draft_id": None}
        elif save.get("ok"):
            draft_status = {
                "status": "existing" if save.get("existing") else "saved",
                "draft_id": save.get("draft_id"),
            }
        else:
            draft_status = {
                "status": "failed",
                "draft_id": save.get("draft_id"),
                "error": save.get("error"),
                "message": save.get("message"),
            }
        return {
            "asset_id": state.asset_id,
            "completion_status": completion,
            "reason": reason,
            "review_status": "blocked" if risks else ("incomplete" if missing else "approved"),
            "risk_reasons": risks,
            "missing_fields": missing,
            "requested_time": state.requested_time,
            "suggested_time": suggested,
            "hard_requirement_changed": False,
            "pending_confirmations": pending,
            "draft": draft_status,
            "published": False,
        }

    @staticmethod
    def _overall_status(items: list[dict[str, Any]]) -> str:
        statuses = {item["completion_status"] for item in items}
        if "failed" in statuses:
            return "failed"
        if "incomplete" in statuses or "blocked" in statuses:
            return "partial"
        if "pending_confirmation" in statuses:
            return "pending_confirmation"
        return "completed"

