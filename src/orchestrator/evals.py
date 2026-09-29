"""Routing evaluation: top-1 accuracy and recall@k against a labelled dataset.

Run offline in CI (hashing embedder, retrieval only) as a regression gate, and
against real models (LLM router + hosted embeddings) in the nightly eval job."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestrator.service import Orchestrator


@dataclass
class EvalReport:
    total: int = 0
    top1_hits: int = 0
    recall_hits: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    by_lang: dict[str, list[int]] = field(default_factory=dict)
    failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def top1(self) -> float:
        return self.top1_hits / self.total if self.total else 0.0

    @property
    def recall_at_k(self) -> float:
        return self.recall_hits / self.total if self.total else 0.0

    def summary(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)
        return {
            "total": self.total,
            "top1_accuracy": round(self.top1, 3),
            "recall_at_k": round(self.recall_at_k, 3),
            "top1_by_lang": {
                lang: round(hits / n, 3) for lang, (hits, n) in sorted(self.by_lang.items())
            },
            "latency_ms_p50": round(lat[len(lat) // 2], 1) if lat else 0.0,
            "latency_ms_p95": round(lat[int(len(lat) * 0.95) - 1], 1) if lat else 0.0,
            "failures": self.failures,
        }


def load_dataset(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def run_routing_eval(orch: Orchestrator, dataset: list[dict[str, Any]]) -> EvalReport:
    report = EvalReport()
    for row in dataset:
        expected = set(row["expected"])
        started = time.perf_counter()
        decision = await orch.route(row["question"])
        report.latencies_ms.append((time.perf_counter() - started) * 1000)
        hit = decision.agent_id in expected
        recalled = hit or any(c.agent_id in expected for c in decision.candidates)
        report.total += 1
        report.top1_hits += hit
        report.recall_hits += recalled
        lang = row.get("lang", "?")
        stats = report.by_lang.setdefault(lang, [0, 0])
        stats[0] += hit
        stats[1] += 1
        if not hit:
            report.failures.append(
                {
                    "question": row["question"],
                    "expected": sorted(expected),
                    "got": decision.agent_id,
                    "recalled": recalled,
                }
            )
    return report
