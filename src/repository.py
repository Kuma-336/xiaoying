from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class DataRepository:
    def __init__(self, data_dir: Path, schedule_overrides: list[dict[str, Any]] | None = None):
        assets_doc = load_json(data_dir / "assets.json")
        windows_doc = load_json(data_dir / "publish_windows.json")
        schedule_doc = load_json(data_dir / "existing_schedule.json")
        self.assets = {item["asset_id"]: item for item in assets_doc["assets"]}
        self.windows = {
            (item["platform"], item["account_id"]): item for item in windows_doc["accounts"]
        }
        self.schedule = schedule_doc["schedule_items"]
        if schedule_overrides:
            schedule_by_id = {item["schedule_id"]: item for item in self.schedule}
            for override in schedule_overrides:
                schedule_id = override.get("schedule_id")
                if schedule_id not in schedule_by_id:
                    raise ValueError(f"未知排期覆盖项：{schedule_id}")
                allowed = {key: value for key, value in override.items() if key in {"start", "end", "status"}}
                schedule_by_id[schedule_id].update(allowed)
        self.rules = load_json(data_dir / "review_rules.json")
        self.context = load_json(data_dir / "evaluation_context.json")

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        return self.assets.get(asset_id)


class DraftStore:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "drafts.json"

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        value = load_json(self.path)
        if not isinstance(value, list):
            raise ValueError("草稿状态文件必须是 JSON 数组")
        return value

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        drafts = self.load()
        key = ":".join(
            [payload["request_id"], payload["asset_id"], str(payload["asset_version"])]
        )
        fingerprint_source = {
            field: payload.get(field)
            for field in [
                "requested_start",
                "scheduled_start",
                "review_status",
                "risk_codes",
                "confirmation_status",
            ]
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_source, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        for draft in drafts:
            if draft["idempotency_key"] != key:
                continue
            if draft["payload_fingerprint"] != fingerprint:
                return {
                    "ok": False,
                    "error_code": "idempotency_conflict",
                    "message": "相同请求标识对应不同草稿内容",
                    "retryable": False,
                    "draft_id": draft["draft_id"],
                }
            return {
                "ok": True,
                "save_status": "existing",
                "workflow_status": draft["workflow_status"],
                "draft_id": draft["draft_id"],
                "created_new_draft": False,
                "payload_fingerprint": fingerprint,
            }
        draft_id = "DR-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper()
        record = {
            **payload,
            "draft_id": draft_id,
            "idempotency_key": key,
            "payload_fingerprint": fingerprint,
            "workflow_status": "draft_saved",
        }
        drafts.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(drafts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(self.path)
        return {
            "ok": True,
            "save_status": "saved",
            "workflow_status": "draft_saved",
            "draft_id": draft_id,
            "created_new_draft": True,
            "payload_fingerprint": fingerprint,
        }
