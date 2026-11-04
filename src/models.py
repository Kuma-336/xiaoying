from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Action:
    name: str
    arguments: dict[str, Any]
    decision_basis: str
    tool_call_id: str = "mock-call"
    model: str = "mock"
    assistant_message: dict[str, Any] | None = None


@dataclass
class RunState:
    scenario_id: str
    request: dict[str, Any]
    max_steps: int
    max_tool_calls: int
    observations: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    conversation: list[dict[str, Any]] = field(default_factory=list)
    model_steps: int = 0
    tool_calls: int = 0

    def observations_for(self, action: str) -> list[dict[str, Any]]:
        return [entry["result"] for entry in self.observations if entry["action"] == action]

    def last_observation(self, action: str) -> dict[str, Any] | None:
        matches = self.observations_for(action)
        return matches[-1] if matches else None

    def add_observation(self, action: str, result: dict[str, Any]) -> None:
        self.observations.append({"action": action, "result": result})

    def model_context(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "request": self.request,
            "observations": self.observations,
            "remaining_model_steps": self.max_steps - self.model_steps,
            "remaining_tool_calls": self.max_tool_calls - self.tool_calls,
        }
