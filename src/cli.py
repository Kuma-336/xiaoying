from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .agent import AgentRuntime
from .planners import make_planner
from .repository import DataRepository, DraftStore, load_json
from .tools import BusinessTools


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _state_path(root: Path, namespace: str) -> Path:
    if not namespace or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in namespace):
        raise ValueError("state_namespace 只能包含字母、数字、下划线和连字符")
    resolved_root = root.resolve()
    candidate = (resolved_root / namespace).resolve()
    if candidate.parent != resolved_root:
        raise ValueError("状态目录越界")
    return candidate


def _reset_state(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def execute_scenario(
    document: dict[str, Any],
    planner_name: str,
    state_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    scenario_id = document["scenario_id"]
    controls = document.get("test_controls", {})
    state_dir = _state_path(state_root, controls.get("state_namespace", scenario_id))
    if controls.get("reset_state_before_run") or controls.get("reset_state_before_first_run"):
        _reset_state(state_dir)
    repeat_count = int(controls.get("repeat_count", 1))
    context = load_json(ROOT / "data" / "evaluation_context.json")
    limits = {**context["execution_limits"], **controls.get("execution_limits", {})}
    results: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for invocation in range(1, repeat_count + 1):
        repository = DataRepository(ROOT / "data")
        tools = BusinessTools(repository, DraftStore(state_dir), controls.get("tool_failures"))
        runtime = AgentRuntime(
            tools,
            make_planner(planner_name),
            int(limits["max_agent_steps_per_asset"]),
            int(limits["max_tool_calls_per_asset"]),
        )
        result, trace = runtime.run(scenario_id, document["request"])
        result["invocation"] = invocation
        results.append(result)
        traces.append({"invocation": invocation, "steps": trace})
    if repeat_count == 1:
        output: dict[str, Any] = results[0]
        trace_output: dict[str, Any] = {
            "scenario_id": scenario_id,
            "planner_mode": planner_name,
            "invocations": traces,
        }
    else:
        output = {
            "scenario_id": scenario_id,
            "planner_mode": planner_name,
            "overall_status": "failed" if any(item["overall_status"] == "failed" for item in results) else "completed",
            "invocations": results,
            "persistent_draft_count": len(DraftStore(state_dir).load()),
            "published": False,
        }
        trace_output = {
            "scenario_id": scenario_id,
            "planner_mode": planner_name,
            "invocations": traces,
        }
    return output, trace_output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="素材审核与发布排期 Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="运行一个场景")
    run.add_argument("--input", required=True, type=Path)
    run.add_argument("--planner", choices=["deepseek", "mock"], default="deepseek")
    run.add_argument("--state-dir", type=Path, default=ROOT / ".runtime" / "single")
    run.add_argument("--output", type=Path)
    run.add_argument("--trace-output", type=Path)
    suite = subparsers.add_parser("run-suite", help="运行 examples/inputs 下全部场景")
    suite.add_argument("--planner", choices=["deepseek", "mock"], default="deepseek")
    suite.add_argument("--input-dir", type=Path, default=ROOT / "examples" / "inputs")
    suite.add_argument("--output-dir", type=Path, default=ROOT / "examples" / "outputs")
    suite.add_argument("--trace-dir", type=Path, default=ROOT / "examples" / "traces")
    suite.add_argument("--state-root", type=Path, default=ROOT / ".runtime" / "suite")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            document = load_json(args.input)
            state_root = args.state_dir.parent
            document = {
                **document,
                "test_controls": {
                    **document.get("test_controls", {}),
                    "state_namespace": args.state_dir.name,
                },
            }
            output, trace = execute_scenario(document, args.planner, state_root)
            if args.output:
                _write_json(args.output, output)
            else:
                print(json.dumps(output, ensure_ascii=False, indent=2))
            if args.trace_output:
                _write_json(args.trace_output, trace)
            return 1 if output.get("overall_status") == "failed" else 0
        exit_code = 0
        for input_path in sorted(args.input_dir.glob("*.json")):
            document = load_json(input_path)
            output, trace = execute_scenario(document, args.planner, args.state_root)
            stem = input_path.stem
            _write_json(args.output_dir / f"{stem}.output.json", output)
            _write_json(args.trace_dir / f"{stem}.trace.json", trace)
            if output.get("overall_status") == "failed" and document["scenario_id"] != "save_failure":
                exit_code = 1
        return exit_code
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

