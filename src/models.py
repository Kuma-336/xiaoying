from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Action:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    decision_basis: str = ""


@dataclass
class AssetState:
    request_id: str
    asset_id: str
    requested_time: str
    confirmation: dict[str, Any]
    simulate: dict[str, Any]
    max_steps: int
    observations: dict[str, Any] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None

    def context_for_model(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "asset_id": self.asset_id,
            "requested_time": self.requested_time,
            "confirmation": self.confirmation,
            "observations": self.observations,
            "steps_remaining": self.max_steps - len(self.trace),
        }

