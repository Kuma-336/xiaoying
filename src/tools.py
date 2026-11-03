from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


class BusinessTools:
    """本地业务工具；只保存草稿，不提供发布动作。"""

    def __init__(self, data_dir: Path, state_dir: Path):
        self.assets = {item["asset_id"]: item for item in _load_json(data_dir / "assets.json")}
        self.rules = _load_json(data_dir / "rules.json")
        self.schedule = _load_json(data_dir / "schedule.json")
        self.drafts_path = state_dir / "drafts.json"

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            {"name": "read_asset", "arguments": {"asset_id": "string"}},
            {
                "name": "check_rules",
                "arguments": {"asset_id": "string", "requested_time": "ISO-8601 string"},
            },
            {
                "name": "query_slots",
                "arguments": {
                    "requested_time": "ISO-8601 string",
                    "request_id": "string",
                    "asset_id": "string",
                },
            },
            {
                "name": "request_confirmation",
                "arguments": {"kind": "string", "message": "string", "proposed_time": "string|null"},
            },
            {
                "name": "save_draft",
                "arguments": {
                    "request_id": "string",
                    "asset_id": "string",
                    "requested_time": "string",
                    "proposed_time": "string",
                    "review_status": "string",
                    "risks": "array",
                    "pending_confirmations": "array",
                },
            },
        ]

    def execute(self, name: str, arguments: dict[str, Any], simulate: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "read_asset": self.read_asset,
            "check_rules": self.check_rules,
            "query_slots": self.query_slots,
            "request_confirmation": self.request_confirmation,
            "save_draft": self.save_draft,
        }
        if name not in handlers:
            return {"ok": False, "error": "unknown_tool", "message": f"未知工具：{name}"}
        if name == "save_draft" and simulate.get("save_failure"):
            return {"ok": False, "error": "storage_unavailable", "message": "草稿存储服务不可用（模拟）"}
        try:
            return handlers[name](**arguments)
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": "invalid_arguments", "message": str(exc)}

    def read_asset(self, asset_id: str) -> dict[str, Any]:
        asset = self.assets.get(asset_id)
        if asset is None:
            return {"ok": False, "error": "asset_not_found", "asset_id": asset_id}
        required = self.rules["required_asset_fields"]
        missing = [field for field in required if asset.get(field) in (None, "", [])]
        return {"ok": True, "asset": deepcopy(asset), "missing_fields": missing}

    def check_rules(self, asset_id: str, requested_time: str) -> dict[str, Any]:
        asset = self.assets.get(asset_id)
        if asset is None:
            return {"ok": False, "error": "asset_not_found"}
        risks: list[dict[str, str]] = []
        searchable = " ".join(
            [str(asset.get("title", "")), str(asset.get("description", ""))]
        )
        for term in self.rules["forbidden_terms"]:
            if term in searchable:
                risks.append(
                    {"code": "forbidden_claim", "severity": "block", "reason": f"包含禁用表述：{term}"}
                )
        duration = asset.get("duration_seconds")
        maximum = self.rules["platform_duration_limits"].get(asset.get("platform"))
        if duration is not None and maximum is not None and duration > maximum:
            risks.append(
                {
                    "code": "duration_exceeded",
                    "severity": "block",
                    "reason": f"时长 {duration}s 超过平台上限 {maximum}s",
                }
            )
        if "finance" in asset.get("content_tags", []) and not asset.get("disclaimer"):
            risks.append(
                {"code": "missing_disclaimer", "severity": "block", "reason": "金融内容缺少风险声明"}
            )
        return {"ok": True, "passed": not risks, "risks": risks, "checked_time": requested_time}

    def _drafts(self) -> list[dict[str, Any]]:
        if not self.drafts_path.exists():
            return []
        return _load_json(self.drafts_path)

    def query_slots(self, requested_time: str, request_id: str, asset_id: str) -> dict[str, Any]:
        requested = datetime.fromisoformat(requested_time)
        occupied = {
            item["time"]: {"source": "existing_schedule", "reference": item["schedule_id"]}
            for item in self.schedule
        }
        for draft in self._drafts():
            if draft["request_id"] == request_id and draft["asset_id"] == asset_id:
                continue
            occupied[draft["proposed_time"]] = {
                "source": "draft",
                "reference": draft["draft_id"],
            }
        conflict = occupied.get(requested_time)
        alternatives: list[str] = []
        for offset in range(1, 5):
            candidate = (requested + timedelta(hours=offset)).isoformat()
            if candidate not in occupied:
                alternatives.append(candidate)
            if len(alternatives) == 2:
                break
        return {
            "ok": True,
            "requested_time": requested_time,
            "available": conflict is None,
            "conflict": conflict,
            "alternatives": alternatives,
        }

    def request_confirmation(
        self, kind: str, message: str, proposed_time: str | None = None
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "confirmation_required": True,
            "kind": kind,
            "message": message,
            "proposed_time": proposed_time,
        }

    def save_draft(
        self,
        request_id: str,
        asset_id: str,
        requested_time: str,
        proposed_time: str,
        review_status: str,
        risks: list[dict[str, Any]],
        pending_confirmations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        drafts = self._drafts()
        idempotency_key = f"{request_id}:{asset_id}"
        payload = {
            "request_id": request_id,
            "asset_id": asset_id,
            "requested_time": requested_time,
            "proposed_time": proposed_time,
            "review_status": review_status,
            "risks": risks,
            "pending_confirmations": pending_confirmations,
        }
        payload_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for draft in drafts:
            if draft["idempotency_key"] == idempotency_key:
                if draft["payload_hash"] != payload_hash:
                    return {
                        "ok": False,
                        "error": "idempotency_conflict",
                        "message": "同一请求标识对应不同草稿内容",
                        "draft_id": draft["draft_id"],
                    }
                return {"ok": True, "saved": True, "existing": True, "draft_id": draft["draft_id"]}
        draft_id = "DR-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:10].upper()
        record = {**payload, "draft_id": draft_id, "idempotency_key": idempotency_key, "payload_hash": payload_hash}
        drafts.append(record)
        _atomic_write(self.drafts_path, drafts)
        return {"ok": True, "saved": True, "existing": False, "draft_id": draft_id}


def summarize_observation(observation: dict[str, Any]) -> str:
    if not observation.get("ok"):
        return f"失败：{observation.get('error')} - {observation.get('message', '')}".strip()
    if "asset" in observation:
        missing = observation.get("missing_fields", [])
        return f"素材已读取；缺失字段：{', '.join(missing) if missing else '无'}"
    if "risks" in observation:
        return f"规则检查完成；风险数：{len(observation['risks'])}"
    if "available" in observation:
        return "请求时段可用" if observation["available"] else f"请求时段冲突；替代时段：{observation['alternatives']}"
    if observation.get("confirmation_required"):
        return f"已登记待确认项：{observation['kind']}"
    if observation.get("saved"):
        marker = "命中已有草稿" if observation.get("existing") else "新草稿已保存"
        return f"{marker}：{observation['draft_id']}"
    return "工具执行成功"

