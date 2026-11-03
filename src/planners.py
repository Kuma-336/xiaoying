from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

from .models import Action, AssetState


class Planner(Protocol):
    mode: str

    def next_action(self, state: AssetState, tool_specs: list[dict[str, Any]]) -> Action:
        ...


class MockPlanner:
    """离线状态机，只验证执行流程，不代表真实模型判断效果。"""

    mode = "mock"

    def next_action(self, state: AssetState, tool_specs: list[dict[str, Any]]) -> Action:
        observations = state.observations
        common = {"asset_id": state.asset_id}
        if "read_asset" not in observations:
            return Action("read_asset", common, "先读取素材元数据，确认审核所需信息是否齐全。")

        asset_read = observations["read_asset"]
        if not asset_read.get("ok") or asset_read.get("missing_fields"):
            if "request_confirmation" not in observations:
                missing = asset_read.get("missing_fields", [])
                message = "需要补充素材信息：" + (", ".join(missing) if missing else "素材不存在")
                return Action(
                    "request_confirmation",
                    {"kind": "missing_information", "message": message, "proposed_time": None},
                    "素材信息不足，无法安全执行规则检查和排期。",
                )
            return Action("finish", {}, "已登记缺失信息，等待内容团队补充。")

        if "check_rules" not in observations:
            return Action(
                "check_rules",
                {**common, "requested_time": state.requested_time},
                "素材信息完整，下一步执行审核规则检查。",
            )

        rule_result = observations["check_rules"]
        if not rule_result.get("ok") or not rule_result.get("passed"):
            return Action("finish", {}, "发现阻断级规则风险，不进入排期和草稿保存。")

        if "query_slots" not in observations:
            return Action(
                "query_slots",
                {
                    "requested_time": state.requested_time,
                    "request_id": state.request_id,
                    "asset_id": state.asset_id,
                },
                "规则通过，查询用户要求时段及可用替代时段。",
            )

        slots = observations["query_slots"]
        if not slots.get("ok"):
            return Action("finish", {}, "时段查询失败，无法形成可靠排期。")

        proposed_time = state.requested_time
        pending: list[dict[str, Any]] = []
        if not slots["available"]:
            alternatives = slots.get("alternatives", [])
            if not alternatives:
                return Action("finish", {}, "请求时段冲突且没有可用替代时段。")
            proposed_time = alternatives[0]
            accepted = state.confirmation.get("accepted_time") == proposed_time
            if not accepted and "request_confirmation" not in observations:
                return Action(
                    "request_confirmation",
                    {
                        "kind": "alternative_time",
                        "message": f"要求时段 {state.requested_time} 已占用，是否接受 {proposed_time}？",
                        "proposed_time": proposed_time,
                    },
                    "要求时段冲突；只建议替代时段，未经确认不改变硬性要求。",
                )
            if not accepted:
                confirmation = observations.get("request_confirmation", {})
                pending = [
                    {
                        "kind": "alternative_time",
                        "message": confirmation.get("message", "替代时段待确认"),
                        "proposed_time": proposed_time,
                    }
                ]

        if "save_draft" not in observations:
            return Action(
                "save_draft",
                {
                    "request_id": state.request_id,
                    "asset_id": state.asset_id,
                    "requested_time": state.requested_time,
                    "proposed_time": proposed_time,
                    "review_status": "pending_confirmation" if pending else "approved",
                    "risks": rule_result.get("risks", []),
                    "pending_confirmations": pending,
                },
                "审核与时段信息已明确，保存草稿供人工确认；不执行发布。",
            )

        saved = observations["save_draft"]
        if not saved.get("ok"):
            return Action("finish", {}, "草稿保存失败，明确结束并返回失败状态。")
        return Action("finish", {}, "草稿已保存或命中幂等记录，结束本项处理。")


class CompatibleLLMPlanner:
    """调用 OpenAI-compatible chat/completions 接口，由模型逐步选择动作。"""

    mode = "llm"

    def __init__(self) -> None:
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("LLM_MODEL", "")
        if not self.api_key:
            raise ValueError("LLM_API_KEY 未配置；请改用 --planner mock 或配置真实模型。")
        if not self.model:
            raise ValueError("LLM_MODEL 未配置；真实模型模式必须显式指定模型 ID。")

    def next_action(self, state: AssetState, tool_specs: list[dict[str, Any]]) -> Action:
        system = (
            "你是素材审核与发布排期 Agent。每次只选择一个动作。"
            "必须先读取素材、检查规则、查询时段，必要时补查或请求确认。"
            "不得绕过阻断风险，不得把草稿描述为已发布，未经用户同意不得改变硬性发布时间。"
            "输出严格 JSON：{\"name\":动作名,\"arguments\":对象,\"decision_basis\":一句可审计依据}。"
            "decision_basis 只写简短业务依据，不输出内部思维过程。结束时选择 finish。"
        )
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"state": state.context_for_model(), "available_tools": tool_specs + [{"name": "finish", "arguments": {}}]},
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"模型接口返回 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"模型接口不可用：{exc.reason}") from exc
        try:
            parsed = json.loads(body["choices"][0]["message"]["content"])
            return Action(
                name=parsed["name"],
                arguments=parsed.get("arguments", {}),
                decision_basis=parsed.get("decision_basis", "模型选择该动作。"),
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("模型未返回合法动作 JSON") from exc


def make_planner(name: str) -> Planner:
    if name == "mock":
        return MockPlanner()
    if name == "llm":
        return CompatibleLLMPlanner()
    raise ValueError(f"不支持的 planner：{name}")

