"""Answer-quality evaluation with an LLM judge (see judge.py and ADR 0006).

Two datasets, both JSONL:

- answers:     questions run end to end through the orchestrator (single or team mode,
               optionally with company documents ingested for RAG), then graded.
- calibration: fixed answers hand-labelled "pass"/"fail". Measures how often the judge
               agrees with a human before its scores are trusted as a quality gate.

Every answer row runs under its own throwaway tenant, so documents never leak between
rows or into real tenants; its documents and conversation are deleted afterwards."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.judge import CRITERIA, Judge, Verdict
from orchestrator.knowledge import KnowledgeRejected
from orchestrator.service import ChatResult, Mode, Orchestrator

_MODES = ("single", "team")
_LABELS = ("pass", "fail")


def _require(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise ValueError(f"{where}: {msg}")


def _check_common(row: dict[str, Any], where: str) -> None:
    _require(isinstance(row.get("id"), str) and bool(row["id"]), where, "'id' must be a string")
    _require(
        isinstance(row.get("question"), str) and bool(row["question"].strip()),
        where,
        "'question' must be a non-empty string",
    )
    docs = row.get("documents", [])
    _require(isinstance(docs, list), where, "'documents' must be a list")
    for doc in docs:
        _require(
            isinstance(doc, dict)
            and isinstance(doc.get("title"), str)
            and isinstance(doc.get("text"), str),
            where,
            "each document needs string 'title' and 'text'",
        )
    points = row.get("expected_points", [])
    _require(
        isinstance(points, list) and all(isinstance(p, str) for p in points),
        where,
        "'expected_points' must be a list of strings",
    )


def load_rows(path: Path, *, calibration: bool = False) -> list[dict[str, Any]]:
    """Load and validate a dataset, failing on the first bad line with its number."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            where = f"{path}:{n}"
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{where}: invalid JSON ({exc.msg})") from exc
            _require(isinstance(row, dict), where, "each line must be a JSON object")
            _check_common(row, where)
            _require(row["id"] not in seen, where, f"duplicate id {row['id']!r}")
            seen.add(row["id"])
            if calibration:
                _require(isinstance(row.get("answer"), str), where, "'answer' is required")
                _require(row.get("label") in _LABELS, where, "'label' must be pass or fail")
            else:
                _require(row.get("mode", "single") in _MODES, where, "'mode' must be single/team")
            rows.append(row)
    _require(bool(rows), str(path), "dataset is empty")
    return rows


def _documents_text(row: dict[str, Any]) -> str | None:
    docs = row.get("documents") or []
    return "\n\n".join(f"# {d['title']}\n{d['text']}" for d in docs) or None


def _usage_add(total: dict[str, Any], usage: dict[str, Any]) -> None:
    for key in ("input_tokens", "output_tokens", "llm_calls"):
        total[key] = total.get(key, 0) + usage.get(key, 0)
    total["cost_usd"] = round(total.get("cost_usd", 0.0) + usage.get("cost_usd", 0.0), 6)


@dataclass
class AnswerEvalReport:
    rows: list[dict[str, Any]] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    answer_usage: dict[str, Any] = field(default_factory=dict)
    judge_usage: dict[str, Any] = field(default_factory=dict)
    judge_models: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def pass_rate(self) -> float:
        return sum(r["passed"] for r in self.rows) / self.total if self.total else 0.0

    def criterion_means(self) -> dict[str, float]:
        graded = [r["scores"] for r in self.rows if r["scores"]]
        if not graded:
            return {c: 0.0 for c in CRITERIA}
        return {c: round(sum(s[c] for s in graded) / len(graded), 3) for c in CRITERIA}

    def summary(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)
        by_mode: dict[str, list[int]] = {}
        for r in self.rows:
            stats = by_mode.setdefault(r["mode"], [0, 0])
            stats[0] += r["passed"]
            stats[1] += 1
        return {
            "total": self.total,
            "pass_rate": round(self.pass_rate, 3),
            "criterion_means": self.criterion_means(),
            "pass_rate_by_mode": {m: round(p / n, 3) for m, (p, n) in sorted(by_mode.items())},
            "blocked": sum(r["blocked"] for r in self.rows),
            "judge_errors": sum(r["error"] is not None for r in self.rows),
            "judge_models": sorted(self.judge_models),
            "latency_ms_p50": round(lat[len(lat) // 2], 1) if lat else 0.0,
            "usage": {"answers": self.answer_usage, "judge": self.judge_usage},
            "failures": [r for r in self.rows if not r["passed"]],
        }


def _row(
    row: dict[str, Any], mode: str, verdict: Verdict, answer: str = "", blocked: bool = False
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "mode": mode,
        "question": row["question"],
        "blocked": blocked,
        "answer_excerpt": answer[:400],
        **verdict.to_dict(),
    }


async def _answer(
    orch: Orchestrator, row: dict[str, Any], mode: Mode, tenant: str
) -> ChatResult | Verdict:
    try:
        return await orch.chat(
            row["question"],
            mode=mode,
            agent_id=row.get("agent_id"),
            agent_ids=row.get("agent_ids"),
            tenant=tenant,
        )
    except Exception as exc:  # a broken row must not abort the whole (paid) run
        return Verdict(error=f"orchestrator error: {type(exc).__name__}: {exc}"[:300])


def check_agents(orch: Orchestrator, rows: list[dict[str, Any]]) -> None:
    """Fail before any paid call if a row pins an agent the catalog does not have."""
    unknown = sorted(
        {
            f"{row['id']}: {agent}"
            for row in rows
            for agent in [row.get("agent_id"), *(row.get("agent_ids") or [])]
            if agent and agent not in orch.catalog.agents
        }
    )
    if unknown:
        raise ValueError(f"unknown agent ids in dataset: {', '.join(unknown)}")


async def run_answer_eval(
    orch: Orchestrator, judge: Judge, rows: list[dict[str, Any]]
) -> AnswerEvalReport:
    check_agents(orch, rows)
    report = AnswerEvalReport()
    run = uuid.uuid4().hex[:8]
    for row in rows:
        mode: Mode = "team" if row.get("mode") == "team" else "single"
        tenant = f"eval-{run}-{row['id']}"
        doc_ids: list[str] = []
        try:
            try:
                for doc in row.get("documents", []):
                    info = await orch.knowledge.add(tenant, doc["title"], doc["text"])
                    doc_ids.append(info.doc_id)
            except KnowledgeRejected as exc:
                report.rows.append(_row(row, mode, Verdict(error=f"document rejected: {exc}")))
                continue
            started = time.perf_counter()
            result = await _answer(orch, row, mode, tenant)
            report.latencies_ms.append((time.perf_counter() - started) * 1000)
            if isinstance(result, Verdict):
                report.rows.append(_row(row, mode, result))
                continue
            _usage_add(report.answer_usage, result.usage.get("total", {}))
            if result.blocked or not result.answer:
                reasons = ", ".join(result.guardrails.get("reasons", [])) or "empty answer"
                error = Verdict(error=f"blocked by guardrails: {reasons}")
                report.rows.append(_row(row, mode, error, blocked=True))
                continue
            verdict = await judge.grade(
                row["question"],
                result.answer,
                documents=_documents_text(row),
                expected_points=row.get("expected_points"),
            )
            _record_judge(report, verdict)
            report.rows.append(_row(row, mode, verdict, result.answer))
        finally:
            for doc_id in doc_ids:
                await orch.knowledge.delete(tenant, doc_id)
            # Nothing of the throwaway tenant survives: not even its conversation.
            await orch.delete_tenant_threads(tenant)
    return report


def _record_judge(report: AnswerEvalReport | CalibrationReport, verdict: Verdict) -> None:
    if verdict.usage:
        _usage_add(report.judge_usage, {**verdict.usage, "llm_calls": 1})
        report.judge_models.add(verdict.usage["model"])


@dataclass
class CalibrationReport:
    rows: list[dict[str, Any]] = field(default_factory=list)
    judge_usage: dict[str, Any] = field(default_factory=dict)
    judge_models: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def agreement(self) -> float:
        return sum(r["agree"] for r in self.rows) / self.total if self.total else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "agreement": round(self.agreement, 3),
            # The costly mistake: a bad answer the judge lets through.
            "false_pass": sum(r["label"] == "fail" and r["passed"] for r in self.rows),
            "false_fail": sum(r["label"] == "pass" and not r["passed"] for r in self.rows),
            "judge_errors": sum(r["error"] is not None for r in self.rows),
            "judge_models": sorted(self.judge_models),
            "usage": {"judge": self.judge_usage},
            "disagreements": [r for r in self.rows if not r["agree"]],
        }


async def run_calibration(judge: Judge, rows: list[dict[str, Any]]) -> CalibrationReport:
    report = CalibrationReport()
    for row in rows:
        verdict = await judge.grade(
            row["question"],
            row["answer"],
            documents=_documents_text(row),
            expected_points=row.get("expected_points"),
        )
        _record_judge(report, verdict)
        report.rows.append(
            {
                "id": row["id"],
                "label": row["label"],
                "agree": verdict.error is None and verdict.passed == (row["label"] == "pass"),
                **verdict.to_dict(),
            }
        )
    return report
