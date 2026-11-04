from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.agent import AgentRuntime
from src.cli import ROOT, execute_scenario
from src.models import Action
from src.repository import DataRepository, DraftStore, load_json
from src.tools import BusinessTools


class SequencePlanner:
    mode = "test"
    model_name = "sequence-test"

    def __init__(self, actions: list[Action]):
        self.actions = iter(actions)

    def next_action(self, state, tool_specs):
        return next(self.actions)


def action(name: str, **arguments):
    basis = arguments.pop("decision_basis", "测试动作")
    return Action(name, {**arguments, "decision_basis": basis}, basis, model="sequence-test")


class AcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state_root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_example(self, filename: str):
        document = load_json(ROOT / "examples" / "inputs" / filename)
        return execute_scenario(document, "mock", self.state_root)

    def test_normal_schedule(self) -> None:
        output, trace = self.run_example("01-normal-schedule.json")
        self.assertEqual("completed", output["overall_status"])
        self.assertEqual("approved", output["review_status"])
        self.assertEqual("saved", output["draft"]["save_status"])
        self.assertFalse(output["published"])
        self.assertIn("save_draft", [step["selected_action"] for step in trace["invocations"][0]["steps"]])

    def test_missing_information(self) -> None:
        output, trace = self.run_example("02-missing-information.json")
        self.assertEqual("incomplete", output["overall_status"])
        self.assertIn("duration_seconds", output["missing_fields"])
        self.assertEqual("not_attempted", output["draft"]["save_status"])
        actions = [step["selected_action"] for step in trace["invocations"][0]["steps"]]
        self.assertIn("request_information", actions)
        self.assertNotIn("query_slots", actions)

    def test_rule_risk_blocks(self) -> None:
        output, trace = self.run_example("03-rule-risk.json")
        self.assertEqual("blocked", output["overall_status"])
        self.assertIn("prohibited_claim", output["risk_codes"])
        self.assertIn("missing_required_disclosure", output["risk_codes"])
        actions = [step["selected_action"] for step in trace["invocations"][0]["steps"]]
        self.assertNotIn("query_slots", actions)
        self.assertNotIn("save_draft", actions)

    def test_slot_conflict_requests_confirmation_without_save(self) -> None:
        output, trace = self.run_example("04-slot-conflict.json")
        self.assertEqual("pending_confirmation", output["overall_status"])
        self.assertEqual("2026-11-05T18:00:00+08:00", output["requested_start"])
        self.assertEqual("2026-11-05T19:00:00+08:00", output["suggested_start"])
        self.assertFalse(output["hard_requirement_changed"])
        self.assertEqual("not_attempted", output["draft"]["save_status"])
        actions = [step["selected_action"] for step in trace["invocations"][0]["steps"]]
        self.assertIn("request_confirmation", actions)
        self.assertNotIn("save_draft", actions)

    def test_confirmed_alternative_rechecks_and_saves(self) -> None:
        output, trace = self.run_example("08-confirmed-alternative.json")
        self.assertEqual("completed", output["overall_status"])
        self.assertEqual("2026-11-05T19:00:00+08:00", output["suggested_start"])
        self.assertTrue(output["hard_requirement_change_authorized"])
        self.assertEqual("saved", output["draft"]["save_status"])
        actions = [step["selected_action"] for step in trace["invocations"][0]["steps"]]
        self.assertLess(actions.index("query_slots"), actions.index("save_draft"))

    def test_duplicate_submission_creates_one_draft(self) -> None:
        output, _ = self.run_example("05-duplicate-submission.json")
        self.assertEqual(2, len(output["invocations"]))
        first, second = output["invocations"]
        self.assertEqual("saved", first["draft"]["save_status"])
        self.assertEqual("existing", second["draft"]["save_status"])
        self.assertEqual(first["draft"]["draft_id"], second["draft"]["draft_id"])
        self.assertEqual(1, output["persistent_draft_count"])

    def test_save_failure_is_explicit(self) -> None:
        output, _ = self.run_example("06-save-failure.json")
        self.assertEqual("failed", output["overall_status"])
        self.assertEqual("failed", output["draft"]["save_status"])
        self.assertEqual("storage_unavailable", output["draft"]["error_code"])
        self.assertTrue(output["draft"]["retryable"])
        self.assertFalse(output["published"])

    def test_execution_limit(self) -> None:
        output, trace = self.run_example("07-execution-limit.json")
        self.assertEqual("incomplete", output["overall_status"])
        self.assertEqual("execution_limit_reached", output["completion_reason"])
        self.assertEqual("execution_limit", trace["invocations"][0]["steps"][-1]["selected_action"])
        self.assertLessEqual(output["usage"]["tool_calls"], 2)

    def test_query_reference_slot(self) -> None:
        repository = DataRepository(ROOT / "data")
        tools = BusinessTools(repository, DraftStore(self.state_root / "slots"))
        occupied = tools.query_slots("REQ-X", "M01", "2026-11-05T18:00:00+08:00", None)
        available = tools.query_slots("REQ-X", "M01", "2026-11-05T19:00:00+08:00", None)
        self.assertFalse(occupied["available"])
        self.assertEqual("2026-11-05T19:00:00+08:00", occupied["alternatives"][0])
        self.assertTrue(available["available"])

    def test_executor_blocks_save_before_checks(self) -> None:
        document = load_json(ROOT / "examples" / "inputs" / "01-normal-schedule.json")
        request = document["request"]
        planner = SequencePlanner(
            [
                action(
                    "save_draft",
                    request_id=request["request_id"],
                    asset_id="M05",
                    asset_version=4,
                    requested_start="2026-11-07T11:00:00+08:00",
                    scheduled_start="2026-11-07T11:00:00+08:00",
                    confirmation_status="not_required",
                    review_status="approved",
                    risk_codes=[],
                ),
                action("finish", completion_status="failed"),
            ]
        )
        tools = BusinessTools(DataRepository(ROOT / "data"), DraftStore(self.state_root / "unsafe"))
        output, _ = AgentRuntime(tools, planner, 10, 8).run("unsafe", request)
        self.assertEqual("failed", output["draft"]["save_status"])
        self.assertEqual("unsafe_sequence", output["draft"]["error_code"])

    def test_trace_has_required_audit_fields(self) -> None:
        _, trace = self.run_example("04-slot-conflict.json")
        required = {
            "step",
            "model",
            "selected_action",
            "tool_parameters",
            "tool_result_summary",
            "next_action",
            "decision_basis",
            "timestamp",
        }
        for step in trace["invocations"][0]["steps"]:
            self.assertTrue(required.issubset(step))


if __name__ == "__main__":
    unittest.main()

