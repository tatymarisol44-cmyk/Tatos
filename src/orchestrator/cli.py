"""`agency` command line: index, route, ask (single or --team), eval, serve, mcp."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestrator.config import get_settings

if TYPE_CHECKING:
    from orchestrator.evals import EvalReport


def _print(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=lambda o: o.__dict__))


async def _index() -> None:
    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    created = await orch.router.build_index()
    _print({"index": orch.router.index_name, "agents": len(orch.catalog), "created": created})


async def _route(question: str) -> None:
    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    await orch.start()
    _print((await orch.route(question)).to_dict())


async def _ask(question: str, agent_id: str | None, team: list[str] | None) -> None:
    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    await orch.start()
    if team is not None:
        result = await orch.chat(question, mode="team", agent_ids=team or None, tenant="cli")
    else:
        result = await orch.chat(question, agent_id=agent_id, tenant="cli")
    _print(result.__dict__)


async def _eval(rows: list[dict[str, Any]]) -> EvalReport:
    from orchestrator.evals import run_routing_eval
    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    await orch.start()
    return await run_routing_eval(orch, rows)


def _eval_cmd(dataset: Path, min_top1: float, min_recall: float, output: Path | None) -> int:
    from orchestrator.evals import load_dataset

    report = asyncio.run(_eval(load_dataset(dataset)))
    summary = report.summary()
    _print({k: v for k, v in summary.items() if k != "failures"})
    if output:
        output.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    ok = report.top1 >= min_top1 and report.recall_at_k >= min_recall
    if not ok:
        print(
            f"FAIL: top1={report.top1:.3f} (min {min_top1}) "
            f"recall@k={report.recall_at_k:.3f} (min {min_recall})",
            file=sys.stderr,
        )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agency")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("index", help="Build the vector index for the current catalog")
    p_route = sub.add_parser("route", help="Show which agent would handle a question")
    p_route.add_argument("question")
    p_ask = sub.add_parser("ask", help="Route and answer a question")
    p_ask.add_argument("question")
    p_ask.add_argument("--agent-id")
    p_ask.add_argument(
        "--team",
        nargs="*",
        metavar="AGENT_ID",
        help="Team mode: plan across several specialists (optionally only these agents)",
    )
    p_eval = sub.add_parser("eval", help="Run the routing evaluation")
    p_eval.add_argument("--dataset", type=Path, default=Path("evals/routing.jsonl"))
    p_eval.add_argument("--min-top1", type=float, default=0.0)
    p_eval.add_argument("--min-recall", type=float, default=0.0)
    p_eval.add_argument("--output", type=Path)
    p_serve = sub.add_parser("serve", help="Run the HTTP API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("mcp", help="Run the MCP server over stdio")

    args = parser.parse_args(argv)
    if args.cmd == "index":
        asyncio.run(_index())
    elif args.cmd == "route":
        asyncio.run(_route(args.question))
    elif args.cmd == "ask":
        asyncio.run(_ask(args.question, args.agent_id, args.team))
    elif args.cmd == "eval":
        return _eval_cmd(args.dataset, args.min_top1, args.min_recall, args.output)
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "orchestrator.api.app:app_factory", factory=True, host=args.host, port=args.port
        )
    elif args.cmd == "mcp":
        from orchestrator.mcp_server import main as mcp_main

        mcp_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
