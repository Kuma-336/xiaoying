from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import SchedulingAgent
from .planners import make_planner
from .tools import BusinessTools


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="素材审核与发布排期 Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="处理审核与排期请求")
    run.add_argument("--input", required=True, type=Path, help="输入 JSON 文件")
    run.add_argument("--output", type=Path, help="输出 JSON 文件；未指定时打印到 stdout")
    run.add_argument("--planner", choices=["mock", "llm"], default="mock")
    run.add_argument("--state-dir", type=Path, default=PROJECT_ROOT / ".runtime")
    run.add_argument("--max-steps", type=int, default=8)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        request = json.loads(args.input.read_text(encoding="utf-8"))
        tools = BusinessTools(PROJECT_ROOT / "data", args.state_dir)
        agent = SchedulingAgent(tools, make_planner(args.planner), args.max_steps)
        result = agent.run(request)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

