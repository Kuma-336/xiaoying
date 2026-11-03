from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.agent import SchedulingAgent
from src.models import Action
from src.planners import MockPlanner
from src.tools import BusinessTools


ROOT = Path(__file__).resolve().parents[1]


class SequencePlanner:
    mode = "test"

    def __init__(self, actions: list[Action]):
        self.actions = iter(actions)

    def next_action(self, state, tool_specs):
        return next(self.actions)


class AgentAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_input(self, filename: str, max_steps: int = 8) -> dict:
        request = json.loads((ROOT / "examples" / "inputs" / filename).read_text(encoding="utf-8"))
        tools = BusinessTools(ROOT / "data", self.state_dir)
        return SchedulingAgent(tools, MockPlanner(), max_steps=max_steps).run(request)

    def test_normal_schedule_saves_draft_without_publishing(self) -> None:
        result = self.run_input("01-normal.json")
        item = result["items"][0]
        self.assertEqual("draft_saved", item["completion_status"])
        self.assertEqual("saved", item["draft"]["status"])
        self.assertEqual(item["requested_time"], item["suggested_time"])
        self.assertFalse(result["published"])

    def test_missing_information_requests_confirmation_and_does_not_save(self) -> None:
        item = self.run_input("02-missing-information.json")["items"][0]
        self.assertEqual("incomplete", item["completion_status"])
        self.assertIn("duration_seconds", item["missing_fields"])
        self.assertEqual("missing_information", item["pending_confirmations"][0]["kind"])
        self.assertEqual("not_attempted", item["draft"]["status"])

    def test_rule_risk_blocks_scheduling(self) -> None:
        result = self.run_input("03-rule-risk.json")
        item = result["items"][0]
        codes = {risk["code"] for risk in item["risk_reasons"]}
        self.assertEqual("blocked", item["completion_status"])
        self.assertIn("forbidden_claim", codes)
        self.assertIn("missing_disclaimer", codes)
        self.assertNotIn("query_slots", [entry["action"] for entry in result["trace"]])

    def test_slot_conflict_suggests_but_does_not_change_hard_requirement(self) -> None:
        item = self.run_input("04-slot-conflict.json")["items"][0]
        self.assertEqual("2026-11-05T18:00:00+08:00", item["requested_time"])
        self.assertEqual("2026-11-05T19:00:00+08:00", item["suggested_time"])
        self.assertEqual("pending_confirmation", item["completion_status"])
        self.assertFalse(item["hard_requirement_changed"])
        self.assertEqual("alternative_time", item["pending_confirmations"][0]["kind"])

    def test_duplicate_submission_creates_only_one_draft(self) -> None:
        first = self.run_input("05-duplicate.json")
        second = self.run_input("05-duplicate.json")
        first_draft = first["items"][0]["draft"]
        second_draft = second["items"][0]["draft"]
        self.assertEqual("saved", first_draft["status"])
        self.assertEqual("existing", second_draft["status"])
        self.assertEqual(first_draft["draft_id"], second_draft["draft_id"])
        drafts = json.loads((self.state_dir / "drafts.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(drafts))

    def test_save_failure_is_explicit(self) -> None:
        result = self.run_input("06-save-failure.json")
        item = result["items"][0]
        self.assertEqual("failed", result["status"])
        self.assertEqual("failed", item["draft"]["status"])
        self.assertEqual("storage_unavailable", item["draft"]["error"])
        self.assertFalse(item["published"])

    def test_step_limit_returns_incomplete(self) -> None:
        result = self.run_input("01-normal.json", max_steps=2)
        item = result["items"][0]
        self.assertEqual("incomplete", item["completion_status"])
        self.assertEqual("step_limit_reached", item["reason"])
        self.assertEqual("step_limit", result["trace"][-1]["action"])

    def test_trace_contains_auditable_fields(self) -> None:
        trace = self.run_input("04-slot-conflict.json")["trace"]
        required = {"step", "action", "tool_parameters", "return_summary", "next_action", "decision_basis"}
        self.assertTrue(trace)
        for entry in trace:
            self.assertTrue(required.issubset(entry))
        self.assertIn("save_draft", [entry["action"] for entry in trace])

    def test_executor_blocks_save_before_required_checks(self) -> None:
        request = json.loads((ROOT / "examples" / "inputs" / "01-normal.json").read_text(encoding="utf-8"))
        planner = SequencePlanner(
            [
                Action(
                    "save_draft",
                    {
                        "request_id": request["request_id"],
                        "asset_id": "M04",
                        "requested_time": "2026-11-05T17:00:00+08:00",
                        "proposed_time": "2026-11-05T17:00:00+08:00",
                        "review_status": "approved",
                        "risks": [],
                        "pending_confirmations": [],
                    },
                    "恶意越序测试",
                ),
                Action("finish", {}, "结束测试"),
            ]
        )
        tools = BusinessTools(ROOT / "data", self.state_dir)
        result = SchedulingAgent(tools, planner).run(request)
        self.assertEqual("failed", result["items"][0]["draft"]["status"])
        self.assertEqual("unsafe_sequence", result["items"][0]["draft"]["error"])

    def test_executor_blocks_hard_time_tampering(self) -> None:
        request = json.loads((ROOT / "examples" / "inputs" / "04-slot-conflict.json").read_text(encoding="utf-8"))
        planner = SequencePlanner(
            [
                Action("read_asset", {"asset_id": "M01"}, "读取"),
                Action(
                    "check_rules",
                    {"asset_id": "M01", "requested_time": "2026-11-05T18:00:00+08:00"},
                    "检查",
                ),
                Action(
                    "query_slots",
                    {
                        "asset_id": "M01",
                        "request_id": request["request_id"],
                        "requested_time": "2026-11-05T18:00:00+08:00",
                    },
                    "查询",
                ),
                Action(
                    "save_draft",
                    {
                        "request_id": request["request_id"],
                        "asset_id": "M01",
                        "requested_time": "2026-11-05T19:00:00+08:00",
                        "proposed_time": "2026-11-05T19:00:00+08:00",
                        "review_status": "approved",
                        "risks": [],
                        "pending_confirmations": [],
                    },
                    "尝试篡改硬性时间",
                ),
                Action("finish", {}, "结束测试"),
            ]
        )
        tools = BusinessTools(ROOT / "data", self.state_dir)
        result = SchedulingAgent(tools, planner).run(request)
        item = result["items"][0]
        self.assertEqual("failed", item["draft"]["status"])
        self.assertEqual("hard_requirement_violation", item["draft"]["error"])
        self.assertEqual("2026-11-05T18:00:00+08:00", item["requested_time"])


if __name__ == "__main__":
    unittest.main()

