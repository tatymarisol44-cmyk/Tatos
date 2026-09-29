# ADR 0006: Answer-quality evals with a calibrated LLM judge

**Status:** accepted

## Context

The routing eval tells us whether the right specialist was picked, not whether the answer was any good. Team mode (plan, workers, synthesis) and tenant RAG add failure modes that routing cannot see: a synthesis that drops a requested part, or an answer that contradicts the company's own policy. Human review does not scale to every change, and exact-match metrics do not fit free-form answers.

## Options

1. **DeepEval / RAGAS.** Rich metric catalogs, but heavy dependencies with their own LLM-provider wiring, parallel to our LiteLLM client. Their RAG metrics assume access to retrieved contexts in their own format, and their scores are harder to explain in a review.
2. **Human-labelled golden answers + string similarity (BLEU/ROUGE/embeddings).** Cheap and deterministic, but it penalises correct answers phrased differently and rewards fluent wrong ones.
3. **Own LLM judge with an explicit rubric, calibrated against human labels.** *Chosen.*

## Decision

- `judge.py` grades each answer 1-5 on **relevance**, **faithfulness** (consistent with the tenant documents and no invented specifics) and **completeness** (covers the row's `expected_points`). An answer passes only if every criterion reaches `JUDGE_MIN_SCORE` (default 4).
- The judge goes through the same `LLMClient` as everything else, so it works with any LiteLLM provider and offline with `FakeLLM`. It runs at temperature 0, on `JUDGE_MODEL`: a stronger model than the one answering, ideally from another family to reduce self-preference bias.
- **The answer under review is untrusted input.** It is delimited, any closing tags in it are escaped, and the rubric tells the judge to ignore instructions inside it. The calibration set includes an answer that tries to talk the judge into top scores.
- **Fail closed.** Non-JSON output, missing or out-of-range scores, provider errors, blocked requests or a broken row all count as *not passed*. They are reported in `judge_errors` and never skipped.
- **Calibrate the judge before trusting it.** `evals/judge_calibration.jsonl` holds fixed answers labelled pass or fail by a human (correct, contradicting, hallucinated, incomplete, off-topic, manipulative). `agency eval-judge` reports agreement and `false_pass`: a bad answer the judge approved, which is the costly error. The nightly job gates on agreement ≥ 0.85 and false passes ≤ 1 *before* the answer eval runs.
- `agency eval-answers` runs `evals/answers.jsonl` end to end: single and team mode, pinned agents, EN and ES, with and without documents. Each row gets a throwaway tenant, its documents are deleted afterwards, and pinned agent ids are checked against the catalog before any paid call.

## Consequences

- CI runs the answer eval offline as a smoke test (dataset schema, catalog drift, no blocked rows). The quality gates (pass rate ≥ 0.80, criterion mean ≥ 4.0) run nightly with real models and upload JSON reports with per-row scores, reasons and cost.
- Reports include judge and answer token cost, plus the judge models that actually responded. LiteLLM fallbacks can swap the judge model silently, and the report makes that visible.
- 14 answer rows and 16 calibration rows catch regressions but give wide confidence intervals. The set should grow from sampled, PII-redacted production traffic.
- An LLM judge has known biases (length, position, self-preference). The rubric says not to reward length, and the calibration set is the check that it holds for our data.
