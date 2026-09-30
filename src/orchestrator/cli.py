"""`agency` command line: index, route, ask (single or --team), eval, eval-answers,
eval-judge, purge-threads, serve, mcp."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from orchestrator.config import get_settings

if TYPE_CHECKING:
    from orchestrator.evals import EvalReport
    from orchestrator.service import Orchestrator


def _print(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=lambda o: o.__dict__))


async def _index() -> None:
    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    created = await orch.router.build_index()
    _print({"index": orch.router.index_name, "agents": len(orch.catalog), "created": created})


@asynccontextmanager
async def _started() -> AsyncIterator[Orchestrator]:
    """A started orchestrator whose connections (e.g. the Postgres pool) close on exit."""
    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    try:
        await orch.start()
        yield orch
    finally:
        await orch.close()


async def _route(question: str) -> None:
    async with _started() as orch:
        _print((await orch.route(question)).to_dict())


async def _ask(question: str, agent_id: str | None, team: list[str] | None) -> None:
    async with _started() as orch:
        if team is not None:
            result = await orch.chat(question, mode="team", agent_ids=team or None, tenant="cli")
        else:
            result = await orch.chat(question, agent_id=agent_id, tenant="cli")
    _print(result.__dict__)


async def _eval(rows: list[dict[str, Any]]) -> EvalReport:
    from orchestrator.evals import run_routing_eval

    async with _started() as orch:
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


def _write(summary: dict[str, Any], output: Path | None) -> None:
    if output:
        output.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")


def _gate(failures: list[str]) -> int:
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    return 1 if failures else 0


async def _answers(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from orchestrator.answer_eval import run_answer_eval
    from orchestrator.judge import Judge

    async with _started() as orch:
        report = await run_answer_eval(orch, Judge(orch.llm, orch.settings), rows)
    return report.summary()


def _eval_answers_cmd(
    dataset: Path, min_pass_rate: float, min_mean: float, output: Path | None
) -> int:
    from orchestrator.answer_eval import load_rows

    summary = asyncio.run(_answers(load_rows(dataset)))
    _print({k: v for k, v in summary.items() if k != "failures"})
    _write(summary, output)
    failures = []
    if summary["pass_rate"] < min_pass_rate:
        failures.append(f"pass_rate={summary['pass_rate']:.3f} (min {min_pass_rate})")
    failures += [
        f"{name} mean={mean:.3f} (min {min_mean})"
        for name, mean in summary["criterion_means"].items()
        if mean < min_mean
    ]
    return _gate(failures)


async def _calibrate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    from orchestrator.answer_eval import run_calibration
    from orchestrator.judge import Judge
    from orchestrator.llm import build_llm

    settings = get_settings()  # the judge alone: no catalog, index or database needed
    return (await run_calibration(Judge(build_llm(settings), settings), rows)).summary()


def _eval_judge_cmd(
    dataset: Path, min_agreement: float, max_false_pass: int, output: Path | None
) -> int:
    from orchestrator.answer_eval import load_rows

    summary = asyncio.run(_calibrate(load_rows(dataset, calibration=True)))
    _print({k: v for k, v in summary.items() if k != "disagreements"})
    _write(summary, output)
    failures = []
    if summary["agreement"] < min_agreement:
        failures.append(f"agreement={summary['agreement']:.3f} (min {min_agreement})")
    if summary["false_pass"] > max_false_pass:
        failures.append(f"false_pass={summary['false_pass']} (max {max_false_pass})")
    return _gate(failures)


async def _purge(days: int) -> None:
    from datetime import timedelta

    async with _started() as orch:
        deleted = await orch.purge_threads(timedelta(days=days))
    _print({"deleted_threads": deleted, "older_than_days": days})


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
    p_answers = sub.add_parser("eval-answers", help="Grade end-to-end answers with an LLM judge")
    p_answers.add_argument("--dataset", type=Path, default=Path("evals/answers.jsonl"))
    p_answers.add_argument("--min-pass-rate", type=float, default=0.0)
    p_answers.add_argument(
        "--min-mean", type=float, default=0.0, help="Minimum mean score (1-5) per criterion"
    )
    p_answers.add_argument("--output", type=Path)
    p_judge = sub.add_parser("eval-judge", help="Check the judge against human labels")
    p_judge.add_argument("--dataset", type=Path, default=Path("evals/judge_calibration.jsonl"))
    p_judge.add_argument("--min-agreement", type=float, default=0.0)
    p_judge.add_argument(
        "--max-false-pass", type=int, default=None, help="Max bad answers the judge may pass"
    )
    p_judge.add_argument("--output", type=Path)
    p_purge = sub.add_parser(
        "purge-threads", help="Retention: delete conversations inactive for N days"
    )
    p_purge.add_argument(
        "--older-than-days", type=int, default=None, help="Default: THREAD_RETENTION_DAYS"
    )
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
    elif args.cmd == "eval-answers":
        return _eval_answers_cmd(args.dataset, args.min_pass_rate, args.min_mean, args.output)
    elif args.cmd == "eval-judge":
        max_fp = args.max_false_pass if args.max_false_pass is not None else sys.maxsize
        return _eval_judge_cmd(args.dataset, args.min_agreement, max_fp, args.output)
    elif args.cmd == "purge-threads":
        days = args.older_than_days or get_settings().thread_retention_days
        if days < 1:
            parser.error("--older-than-days must be >= 1")
        asyncio.run(_purge(days))
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
