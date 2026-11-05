from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .repository import DataRepository, DraftStore


WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _nested(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


class BusinessTools:
    def __init__(
        self,
        repository: DataRepository,
        draft_store: DraftStore,
        tool_failures: dict[str, Any] | None = None,
    ):
        self.repository = repository
        self.draft_store = draft_store
        self.tool_failures = tool_failures or {}

    @property
    def specs(self) -> list[dict[str, Any]]:
        def spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
            properties = {
                **properties,
                "decision_basis": {
                    "type": "string",
                    "description": "一句简短、可审计的业务依据，不要输出内部思维过程。",
                },
            }
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required + ["decision_basis"],
                        "additionalProperties": False,
                    },
                },
            }

        return [
            spec(
                "read_asset",
                "读取指定版本的素材元数据，并返回缺失字段。",
                {"asset_id": {"type": "string"}, "asset_version": {"type": "integer"}},
                ["asset_id", "asset_version"],
            ),
            spec(
                "check_rules",
                "检查素材内容、平台限制和建议发布时间下的授权风险。",
                {
                    "asset_id": {"type": "string"},
                    "asset_version": {"type": "integer"},
                    "proposed_start": {"type": "string"},
                },
                ["asset_id", "asset_version", "proposed_start"],
            ),
            spec(
                "query_slots",
                "查询候选时段是否可用，并在冲突时返回向后的可用替代时段。",
                {
                    "request_id": {"type": "string"},
                    "asset_id": {"type": "string"},
                    "candidate_start": {"type": "string"},
                    "latest_start": {"type": ["string", "null"]},
                },
                ["request_id", "asset_id", "candidate_start", "latest_start"],
            ),
            spec(
                "request_information",
                "登记素材信息补充请求。",
                {
                    "missing_fields": {"type": "array", "items": {"type": "string"}},
                    "message": {"type": "string"},
                },
                ["missing_fields", "message"],
            ),
            spec(
                "request_confirmation",
                "登记替代时间或合规事项的用户确认请求，不保存排期草稿。",
                {
                    "request_id": {"type": "string"},
                    "confirmation_type": {"type": "string"},
                    "requested_start": {"type": "string"},
                    "suggested_start": {"type": "string"},
                    "message": {"type": "string"},
                },
                ["request_id", "confirmation_type", "requested_start", "suggested_start", "message"],
            ),
            spec(
                "save_draft",
                "幂等保存已满足安全前置条件的排期草稿，不执行发布。",
                {
                    "request_id": {"type": "string"},
                    "asset_id": {"type": "string"},
                    "asset_version": {"type": "integer"},
                    "requested_start": {"type": "string"},
                    "scheduled_start": {"type": "string"},
                    "confirmation_status": {"type": "string"},
                    "review_status": {"type": "string"},
                    "risk_codes": {"type": "array", "items": {"type": "string"}},
                },
                [
                    "request_id",
                    "asset_id",
                    "asset_version",
                    "requested_start",
                    "scheduled_start",
                    "confirmation_status",
                    "review_status",
                    "risk_codes",
                ],
            ),
            spec(
                "finish",
                "在已有观察足以形成结果时结束当前素材处理。",
                {
                    "completion_status": {
                        "type": "string",
                        "enum": ["completed", "incomplete", "blocked", "pending_confirmation", "failed"],
                    }
                },
                ["completion_status"],
            ),
        ]

    def execute(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if action in self.tool_failures:
            failure = self.tool_failures[action]
            return {"ok": False, **failure}
        handlers = {
            "read_asset": self.read_asset,
            "check_rules": self.check_rules,
            "query_slots": self.query_slots,
            "request_information": self.request_information,
            "request_confirmation": self.request_confirmation,
            "save_draft": self.save_draft,
        }
        if action not in handlers:
            return {"ok": False, "error_code": "unknown_tool", "message": action, "retryable": False}
        clean = {key: value for key, value in arguments.items() if key != "decision_basis"}
        try:
            return handlers[action](**clean)
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error_code": "invalid_arguments", "message": str(exc), "retryable": False}

    def read_asset(self, asset_id: str, asset_version: int) -> dict[str, Any]:
        asset = self.repository.get_asset(asset_id)
        if asset is None:
            return {"ok": False, "error_code": "asset_not_found", "asset_id": asset_id, "retryable": False}
        if asset["asset_version"] != asset_version:
            return {
                "ok": False,
                "error_code": "asset_version_mismatch",
                "expected_version": asset["asset_version"],
                "requested_version": asset_version,
                "retryable": False,
            }
        common = self.repository.rules["metadata_rules"][0]
        missing = [field for field in common["required_fields"] if _nested(asset, field) in common["empty_values"]]
        if asset.get("asset_type") == "short_video" and not asset.get("duration_seconds"):
            missing.append("duration_seconds")
        return {"ok": True, "asset": asset, "missing_fields": sorted(set(missing))}

    def check_rules(self, asset_id: str, asset_version: int, proposed_start: str) -> dict[str, Any]:
        read = self.read_asset(asset_id, asset_version)
        if not read.get("ok"):
            return read
        if read["missing_fields"]:
            return {
                "ok": True,
                "passed": False,
                "missing_fields": read["missing_fields"],
                "risks": [],
            }
        asset = read["asset"]
        risks: list[dict[str, Any]] = []
        searchable = " ".join(
            [
                asset.get("title", ""),
                asset.get("metadata", {}).get("summary", ""),
                asset.get("metadata", {}).get("transcript_excerpt", "") or "",
                " ".join(claim.get("text", "") for claim in asset.get("metadata", {}).get("claims", [])),
            ]
        )
        keywords = self.repository.rules["content_rules"][0]["keywords"]
        matched = [term for term in keywords if term in searchable]
        if matched:
            risks.append(
                {
                    "risk_code": "prohibited_claim",
                    "severity": "block",
                    "reason": f"命中禁止宣传表述：{', '.join(matched)}",
                    "matched_terms": matched,
                }
            )
        category = asset["compliance"]["content_category"]
        disclosures = asset["compliance"].get("provided_disclosures", [])
        if category == "financial_promotion" and "investment_risk_notice" not in disclosures:
            risks.append(
                {
                    "risk_code": "missing_required_disclosure",
                    "severity": "block",
                    "reason": "金融推广缺少投资风险提示",
                }
            )
        if category == "financial_promotion":
            risks.append(
                {
                    "risk_code": "compliance_review_required",
                    "severity": "needs_confirmation",
                    "reason": "金融推广需要合规人员确认",
                }
            )
        rights = asset["rights"]
        if rights["music_license_status"] != "not_required":
            expiry = datetime.fromisoformat(rights["music_license_expires_at"])
            candidate = datetime.fromisoformat(proposed_start)
            if rights["music_license_status"] != "valid" or expiry < candidate:
                risks.append(
                    {
                        "risk_code": "music_license_invalid",
                        "severity": "block",
                        "reason": "音乐授权无效或在建议发布时间前已过期",
                    }
                )
        if rights["portrait_release_status"] not in {"valid", "not_required"}:
            risks.append({"risk_code": "portrait_release_invalid", "severity": "block", "reason": "肖像授权无效"})
        if rights["third_party_material_status"] not in {"cleared", "not_required"}:
            risks.append({"risk_code": "third_party_rights_invalid", "severity": "block", "reason": "第三方素材权利未清理"})
        for rule in self.repository.rules["platform_rules"]:
            if rule["platform"] == asset["platform"] and rule["asset_type"] == asset["asset_type"]:
                duration = asset["duration_seconds"]
                limits = rule["duration_seconds"]
                if duration < limits["minimum"] or duration > limits["maximum"]:
                    risks.append(
                        {"risk_code": "platform_duration_invalid", "severity": rule["severity"], "reason": rule["reason_template"]}
                    )
        blocking = [risk for risk in risks if risk["severity"] in {"block", "needs_information"}]
        return {
            "ok": True,
            "passed": not blocking,
            "risks": risks,
            "risk_codes": [risk["risk_code"] for risk in risks],
            "requires_confirmation": any(risk["severity"] == "needs_confirmation" for risk in risks),
            "checked_start": proposed_start,
        }

    def _inside_window(self, asset: dict[str, Any], start: datetime) -> bool:
        config = self.repository.windows.get((asset["platform"], asset["account_id"]))
        if config is None:
            return False
        day = WEEKDAYS[start.weekday()]
        local_time = start.strftime("%H:%M")
        if not any(window["start"] <= local_time < window["end"] for window in config["weekly_windows"].get(day, [])):
            return False
        for blackout in config.get("blackout_windows", []):
            if datetime.fromisoformat(blackout["start"]) <= start < datetime.fromisoformat(blackout["end"]):
                return False
        return True

    def _conflicts(self, asset: dict[str, Any], request_id: str, start: datetime) -> list[dict[str, Any]]:
        end = start + timedelta(minutes=self.repository.rules["scheduling_policy"]["slot_granularity_minutes"])
        conflicts: list[dict[str, Any]] = []
        behavior = self.repository.rules["scheduling_policy"]["draft_conflict_behavior"]
        for item in self.repository.schedule:
            if item["platform"] != asset["platform"] or item["account_id"] != asset["account_id"]:
                continue
            conflict_type = behavior.get(item["status"], "ignore")
            if conflict_type == "ignore":
                continue
            item_start = datetime.fromisoformat(item["start"])
            item_end = datetime.fromisoformat(item["end"])
            if start < item_end and item_start < end:
                conflicts.append({"source": "existing_schedule", "reference": item["schedule_id"], "type": conflict_type})
        for draft in self.draft_store.load():
            if draft["request_id"] == request_id:
                continue
            if draft["platform"] != asset["platform"] or draft["account_id"] != asset["account_id"]:
                continue
            item_start = datetime.fromisoformat(draft["scheduled_start"])
            item_end = item_start + timedelta(minutes=60)
            if start < item_end and item_start < end:
                conflicts.append({"source": "draft", "reference": draft["draft_id"], "type": "soft_conflict_requires_confirmation"})
        return conflicts

    def query_slots(
        self,
        request_id: str,
        asset_id: str,
        candidate_start: str,
        latest_start: str | None,
    ) -> dict[str, Any]:
        asset = self.repository.get_asset(asset_id)
        if asset is None:
            return {"ok": False, "error_code": "asset_not_found", "retryable": False}
        candidate = datetime.fromisoformat(candidate_start)
        if latest_start in {"", "null", "None"}:
            latest_start = None
        conflicts = self._conflicts(asset, request_id, candidate)
        available = self._inside_window(asset, candidate) and not conflicts
        alternatives: list[str] = []
        horizon = candidate + timedelta(hours=72)
        if latest_start:
            horizon = min(horizon, datetime.fromisoformat(latest_start))
        cursor = candidate + timedelta(hours=1)
        while cursor <= horizon and len(alternatives) < 3:
            if self._inside_window(asset, cursor) and not self._conflicts(asset, request_id, cursor):
                alternatives.append(cursor.isoformat())
            cursor += timedelta(hours=1)
        return {
            "ok": True,
            "candidate_start": candidate_start,
            "available": available,
            "conflicts": conflicts,
            "alternatives": alternatives,
        }

    @staticmethod
    def request_information(missing_fields: list[str], message: str) -> dict[str, Any]:
        return {
            "ok": True,
            "confirmation_type": "missing_information",
            "status": "pending",
            "missing_fields": missing_fields,
            "message": message,
        }

    @staticmethod
    def request_confirmation(
        request_id: str,
        confirmation_type: str,
        requested_start: str,
        suggested_start: str,
        message: str,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "request_id": request_id,
            "confirmation_type": confirmation_type,
            "status": "pending",
            "requested_start": requested_start,
            "suggested_start": suggested_start,
            "message": message,
        }

    def save_draft(self, **payload: Any) -> dict[str, Any]:
        asset = self.repository.get_asset(payload["asset_id"])
        if asset is None:
            return {"ok": False, "error_code": "asset_not_found", "retryable": False}
        return self.draft_store.save({**payload, "platform": asset["platform"], "account_id": asset["account_id"]})


def summarize_result(action: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"失败：{result.get('error_code')} - {result.get('message', '')}".strip()
    if action == "read_asset":
        missing = result.get("missing_fields", [])
        return f"素材已读取；缺失字段：{', '.join(missing) if missing else '无'}"
    if action == "check_rules":
        return f"规则检查完成；通过={result.get('passed')}；风险={result.get('risk_codes', [])}"
    if action == "query_slots":
        return "候选时段可用" if result.get("available") else f"候选时段冲突；替代={result.get('alternatives', [])}"
    if action in {"request_information", "request_confirmation"}:
        return f"已登记待确认项：{result.get('confirmation_type')}"
    if action == "save_draft":
        return f"草稿保存状态：{result.get('save_status')}；draft_id={result.get('draft_id')}"
    return "工具执行成功"
