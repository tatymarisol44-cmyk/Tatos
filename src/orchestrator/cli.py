"""`agency` command line: index, route, ask (single or --team), eval, eval-answers,
eval-judge, retention (alias purge-threads), serve, mcp."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Coroutine
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


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    """asyncio.run with the selector loop on Windows: psycopg's async mode (Postgres)
    cannot use the default Proactor loop there. Linux is unaffected."""
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)


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

    report = _run(_eval(load_dataset(dataset)))
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

    summary = _run(_answers(load_rows(dataset)))
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

    summary = _run(_calibrate(load_rows(dataset, calibration=True)))
    _print({k: v for k, v in summary.items() if k != "disagreements"})
    _write(summary, output)
    failures = []
    if summary["agreement"] < min_agreement:
        failures.append(f"agreement={summary['agreement']:.3f} (min {min_agreement})")
    if summary["false_pass"] > max_false_pass:
        failures.append(f"false_pass={summary['false_pass']} (max {max_false_pass})")
    return _gate(failures)


async def _retention(days: int, dry_run: bool) -> None:
    """The scheduled job (CronJob agency-retention). Starts only the databases; prints a
    JSON summary; any failure raises, so the process exits non-zero and the Job fails."""
    from datetime import timedelta

    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    try:
        await orch.start_maintenance()
        _print(await orch.retention(timedelta(days=days), dry_run=dry_run))
    finally:
        await orch.close()


async def _audit_anchor() -> None:
    """Print one JSON line per tenant with its chain head. Ship it to write-once storage
    (e.g. S3 Object Lock in compliance mode): that copy is what makes a rewritten chain
    detectable."""
    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    try:
        await orch.start_maintenance()
        for anchor in await orch.audit.anchors():
            print(json.dumps(anchor, ensure_ascii=False))
    finally:
        await orch.close()


async def _audit_verify(anchors: list[dict[str, Any]], tenant: str | None) -> int:
    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    try:
        await orch.start_maintenance()
        tenants = (
            [tenant]
            if tenant
            else sorted({*await orch.audit.tenants(), *(a["tenant"] for a in anchors)})
        )
        results = {t: await orch.audit.verify(t, anchors) for t in tenants}
    finally:
        await orch.close()
    _print(results)
    return 0 if all(r["ok"] for r in results.values()) else 1


async def _knowledge_reindex(tenant: str | None) -> int:
    from orchestrator.service import Orchestrator

    orch = Orchestrator(get_settings())
    try:
        await orch.start_maintenance()
        await orch.knowledge.start()  # embeds a probe and creates the collection if lost
        result = await orch.knowledge.reindex(tenant)
    finally:
        await orch.close()
    _print(result)
    return 1 if result["missing_text"] else 0


async def _db(action: str, revision: str | None, message: str | None) -> int:
    from alembic import command

    from orchestrator import migrate
    from orchestrator.db import build_engine

    engine = build_engine(get_settings())
    try:
        if action == "upgrade":
            await migrate.upgrade(engine, revision or "head")
        elif action == "stamp":  # a database created before migrations existed
            async with engine.begin() as conn:
                await conn.run_sync(lambda c: command.stamp(migrate._config(c), revision or "head"))
        elif action == "revision":
            await migrate.autogenerate(engine, message or "schema change")
        found, expected = await migrate.current(engine), migrate.head()
        _print({"current": found, "head": expected})
        return 1 if action == "check" and found != expected else 0
    finally:
        await engine.dispose()


def _pack_cmd(
    action: str, pack_id: str | None, strict: bool, parser: argparse.ArgumentParser
) -> int:
    from orchestrator import packs

    try:
        loaded = packs.load_packs()
    except ValueError as exc:  # a pack that does not validate must stop the pipeline
        print(f"invalid pack: {exc}", file=sys.stderr)
        return 1
    if action == "list":
        _print([packs.summarize(p) for p in loaded.values()])
    elif action == "show":
        if pack_id not in loaded:
            parser.error(f"unknown pack {pack_id!r}; available: {sorted(loaded)}")
        _print(loaded[pack_id].model_dump(mode="json"))
    else:
        failures = packs.strict_failures(loaded) if strict else []
        _print(
            {
                "packs": len(loaded),
                "unverified_refs": {p.id: len(packs.unverified_refs(p)) for p in loaded.values()},
                "strict_failures": failures,
            }
        )
        return 1 if failures else 0
    return 0


def _creative_cmd(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    from pydantic import ValidationError

    from orchestrator import creatives, packs
    from orchestrator.social import PUBLISHING

    loaded = packs.load_packs()
    if args.pack not in loaded or loaded[args.pack].abstract:
        parser.error(f"unknown or abstract pack {args.pack!r}")
    try:
        brief = creatives.Brief(
            title=args.title, points=args.point, cta=args.cta, practice_name=args.practice
        )
        if args.kind == "infographic":
            made = creatives.render_infographic(brief, loaded[args.pack], args.out, args.format)
        else:
            made = creatives.render_video(
                brief, loaded[args.pack], args.out, get_settings(), args.format
            )
    except ValidationError as exc:
        print(f"invalid brief: {exc.errors(include_url=False)}", file=sys.stderr)
        return 1
    except creatives.CreativeRejected as exc:
        print(f"copy rejected by the {args.pack} pack: {exc.violations}", file=sys.stderr)
        return 1
    except (creatives.CreativeUnavailable, creatives.CreativeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print(
        {
            "path": str(made.path),
            "media": f"{made.media_type}/{made.media_format}",
            "size": [made.width, made.height],
            "sha256": made.sha256,
            "needs_owner_approval": made.needs_owner_approval,
            # Before upload: Instagram will also need a public URL (M3).
            "preflight": {n: made.preflight(n).__dict__ for n in PUBLISHING},
        }
    )
    return 0


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
    for name in ("retention", "purge-threads"):  # purge-threads: the old name
        p_purge = sub.add_parser(
            name,
            help="Retention: delete conversations inactive for N days, expired memory "
            "facts and abandoned document versions",
        )
        p_purge.add_argument(
            "--older-than-days", type=int, default=None, help="Default: THREAD_RETENTION_DAYS"
        )
        p_purge.add_argument("--dry-run", action="store_true", help="Only count")
    sub.add_parser("audit-anchor", help="Print each tenant's audit-chain head (JSON lines)")
    p_verify = sub.add_parser(
        "audit-verify", help="Verify audit chains, optionally against anchors"
    )
    p_verify.add_argument("--anchors", type=Path, help="JSON lines from audit-anchor")
    p_verify.add_argument("--tenant")
    p_db = sub.add_parser("db", help="Schema migrations (Alembic)")
    p_db.add_argument("action", choices=["upgrade", "current", "check", "stamp", "revision"])
    p_db.add_argument("revision", nargs="?", help="Target revision (default: head)")
    p_db.add_argument("-m", "--message", help="revision: what changed")
    p_knowledge = sub.add_parser(
        "knowledge", help="Knowledge base: rebuild the vector index from the database"
    )
    p_knowledge.add_argument("action", choices=["reindex"])
    p_knowledge.add_argument("--tenant", help="Only this tenant (default: all)")
    p_pack = sub.add_parser("pack", help="Profession packs: list, validate, show")
    p_pack.add_argument("action", choices=["list", "validate", "show"])
    p_pack.add_argument("pack_id", nargs="?", help="show: the pack to print")
    p_pack.add_argument(
        "--strict",
        action="store_true",
        help="validate: fail if a production pack rests on unverified legal references",
    )
    p_creative = sub.add_parser(
        "creative", help="Render a marketing infographic (JPEG) or video (MP4) from a brief"
    )
    p_creative.add_argument("kind", choices=["infographic", "video"])
    p_creative.add_argument(
        "--pack", required=True, help="Its copy rules apply, e.g. ec-psychologist"
    )
    p_creative.add_argument("--title", required=True)
    p_creative.add_argument("--point", action="append", required=True, help="Repeat, up to 5")
    p_creative.add_argument("--cta", default="")
    p_creative.add_argument("--practice", required=True, help="Name shown in the footer")
    p_creative.add_argument("--format", choices=["feed", "story", "square"], default=None)
    p_creative.add_argument("--out", type=Path, required=True)
    p_serve = sub.add_parser("serve", help="Run the HTTP API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--workers", type=int, default=1, help="Worker processes")
    sub.add_parser("mcp", help="Run the MCP server over stdio")

    args = parser.parse_args(argv)
    if args.cmd == "index":
        _run(_index())
    elif args.cmd == "route":
        _run(_route(args.question))
    elif args.cmd == "ask":
        _run(_ask(args.question, args.agent_id, args.team))
    elif args.cmd == "eval":
        return _eval_cmd(args.dataset, args.min_top1, args.min_recall, args.output)
    elif args.cmd == "eval-answers":
        return _eval_answers_cmd(args.dataset, args.min_pass_rate, args.min_mean, args.output)
    elif args.cmd == "eval-judge":
        max_fp = args.max_false_pass if args.max_false_pass is not None else sys.maxsize
        return _eval_judge_cmd(args.dataset, args.min_agreement, max_fp, args.output)
    elif args.cmd in ("retention", "purge-threads"):
        days = args.older_than_days or get_settings().thread_retention_days
        if days < 1:
            parser.error("--older-than-days must be >= 1")
        _run(_retention(days, args.dry_run))
    elif args.cmd == "db":
        return _run(_db(args.action, args.revision, args.message))
    elif args.cmd == "audit-anchor":
        _run(_audit_anchor())
    elif args.cmd == "audit-verify":
        anchors = (
            [json.loads(x) for x in args.anchors.read_text("utf-8").splitlines() if x.strip()]
            if args.anchors
            else []
        )
        return _run(_audit_verify(anchors, args.tenant))
    elif args.cmd == "knowledge":
        return _run(_knowledge_reindex(args.tenant))
    elif args.cmd == "pack":
        return _pack_cmd(args.action, args.pack_id, args.strict, parser)
    elif args.cmd == "creative":
        args.format = args.format or ("feed" if args.kind == "infographic" else "story")
        return _creative_cmd(args, parser)
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run(
            "orchestrator.api.app:app_factory",
            factory=True,
            host=args.host,
            port=args.port,
            workers=args.workers,
            # psycopg (Postgres) needs the selector loop; uvicorn picks Proactor on Windows.
            loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
        )
    elif args.cmd == "mcp":
        from orchestrator.mcp_server import main as mcp_main

        mcp_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
