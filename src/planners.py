from __future__ import annotations

import json
import os
import uuid
from typing import Any, Protocol

from openai import OpenAI

from .models import Action, RunState


SYSTEM_PROMPT = """你是素材审核与发布排期 Agent。你必须通过函数工具逐步处理请求，每次响应只能调用一个工具。
先获取必要事实，再根据工具观察选择补查、阻断、查询时段、请求确认、保存草稿或结束。
禁止虚构工具结果；禁止修改用户的硬性 requested_start；替代时间未经明确接受不得保存草稿。
系统不存在发布工具，任何情况下都不得声称已发布。所有工具调用都要提供一句简短 decision_basis，
它只能描述可审计的业务依据，不得输出内部思维过程。观察足够时调用 finish。"""


class PlannerProtocolError(RuntimeError):
    pass


class Planner(Protocol):
    mode: str
    model_name: str

    def next_action(self, state: RunState, tool_specs: list[dict[str, Any]]) -> Action:
        ...


class DeepSeekPlanner:
    mode = "deepseek"

    def __init__(self) -> None:
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY 未配置；请撤销暴露的旧密钥并通过环境变量提供新密钥")
        self.model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
        self.client = OpenAI(
            api_key=api_key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )

    def next_action(self, state: RunState, tool_specs: list[dict[str, Any]]) -> Action:
        if not state.conversation:
            state.conversation.extend(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(state.model_context(), ensure_ascii=False),
                    },
                ]
            )
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=state.conversation,
            tools=tool_specs,
            tool_choice="auto",
            stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
        message = response.choices[0].message
        calls = message.tool_calls or []
        if len(calls) != 1:
            raise PlannerProtocolError(f"模型必须返回一个工具调用，实际数量：{len(calls)}")
        call = calls[0]
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError as exc:
            raise PlannerProtocolError("模型工具参数不是合法 JSON") from exc
        basis = arguments.get("decision_basis")
        if not isinstance(basis, str) or not basis.strip():
            raise PlannerProtocolError("工具参数缺少 decision_basis")
        assistant_message = message.model_dump(exclude_none=True)
        return Action(
            name=call.function.name,
            arguments=arguments,
            decision_basis=basis.strip(),
            tool_call_id=call.id,
            model=self.model_name,
            assistant_message=assistant_message,
        )


class MockPlanner:
    """确定性测试替身，只验证流程和安全边界，不证明真实模型决策效果。"""

    mode = "mock"
    model_name = "deterministic-mock"

    def _action(self, name: str, arguments: dict[str, Any], basis: str) -> Action:
        return Action(
            name=name,
            arguments={**arguments, "decision_basis": basis},
            decision_basis=basis,
            tool_call_id="mock-" + uuid.uuid4().hex[:10],
            model=self.model_name,
        )

    def next_action(self, state: RunState, tool_specs: list[dict[str, Any]]) -> Action:
        request = state.request
        asset_id = request["asset_id"]
        version = request["asset_version"]
        requirement = request["publish_requirement"]
        requested = requirement.get("requested_start") or requirement.get("earliest_start")
        latest = requirement.get("latest_start")
        read = state.last_observation("read_asset")
        if read is None:
            return self._action("read_asset", {"asset_id": asset_id, "asset_version": version}, "先读取素材并确认信息完整性。")
        if not read.get("ok") or read.get("missing_fields"):
            if state.last_observation("request_information") is None:
                missing = read.get("missing_fields", [])
                return self._action(
                    "request_information",
                    {"missing_fields": missing, "message": "请补充素材字段：" + ", ".join(missing)},
                    "素材信息不足，无法安全审核和排期。",
                )
            return self._action("finish", {"completion_status": "incomplete"}, "已登记资料补充请求，等待用户提供信息。")
        rules = state.last_observation("check_rules")
        if rules is None:
            return self._action(
                "check_rules",
                {"asset_id": asset_id, "asset_version": version, "proposed_start": requested},
                "素材信息完整，执行内容、平台和授权规则检查。",
            )
        if not rules.get("passed"):
            return self._action("finish", {"completion_status": "blocked"}, "发现阻断风险，禁止进入排期和保存。")
        confirmations = request.get("confirmations", [])
        accepted = next(
            (
                item
                for item in confirmations
                if item.get("confirmation_type") == "alternative_time" and item.get("status") == "accepted"
            ),
            None,
        )
        candidate = accepted["suggested_start"] if accepted else requested
        slots = state.last_observation("query_slots")
        if slots is None:
            return self._action(
                "query_slots",
                {
                    "request_id": request["request_id"],
                    "asset_id": asset_id,
                    "candidate_start": candidate,
                    "latest_start": latest,
                },
                "规则通过，查询用户要求或已确认的候选时段。",
            )
        if not slots.get("available"):
            if accepted:
                return self._action("finish", {"completion_status": "pending_confirmation"}, "已确认时段现已冲突，需要重新建议并再次确认。")
            if state.last_observation("request_confirmation") is None:
                alternatives = slots.get("alternatives", [])
                if not alternatives:
                    return self._action("finish", {"completion_status": "incomplete"}, "时段冲突且没有可用替代时间。")
                suggested = alternatives[0]
                return self._action(
                    "request_confirmation",
                    {
                        "request_id": request["request_id"],
                        "confirmation_type": "alternative_time",
                        "requested_start": requested,
                        "suggested_start": suggested,
                        "message": f"{requested} 已占用，是否接受 {suggested}？",
                    },
                    "要求时段冲突，只建议替代时间，未经确认不保存草稿。",
                )
            return self._action("finish", {"completion_status": "pending_confirmation"}, "替代时段已提出，等待用户确认。")
        saved = state.last_observation("save_draft")
        if saved is None:
            return self._action(
                "save_draft",
                {
                    "request_id": request["request_id"],
                    "asset_id": asset_id,
                    "asset_version": version,
                    "requested_start": requested,
                    "scheduled_start": candidate,
                    "confirmation_status": "accepted" if accepted else "not_required",
                    "review_status": "approved",
                    "risk_codes": rules.get("risk_codes", []),
                },
                "审核和时段前置条件已满足，幂等保存草稿。",
            )
        if not saved.get("ok"):
            return self._action("finish", {"completion_status": "failed"}, "草稿保存失败，明确返回失败状态。")
        return self._action("finish", {"completion_status": "completed"}, "草稿已保存或命中已有记录，结束处理。")


def make_planner(name: str) -> Planner:
    if name == "mock":
        return MockPlanner()
    if name == "deepseek":
        return DeepSeekPlanner()
    raise ValueError(f"未知 planner：{name}")
