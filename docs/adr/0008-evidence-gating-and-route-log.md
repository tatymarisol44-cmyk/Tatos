# ADR 0008: Evidence gating, citation verification and the route log

**Status:** accepted

## Context

Until now the RAG path retrieved once and told the model, in the prompt, to "say so if the excerpts do not cover the request". Pearson et al., *Graph-Based Agentic AI with LangGraph* (arXiv:2607.19297), argue that weak evidence should be a **route** in the workflow, not a hidden prompt instruction, and that a workflow's decisions should be inspectable from typed state rather than read out of model prose. Two concrete failure modes followed from the old design: follow-up questions ("and how many days?") retrieved the wrong document because they carry no context of their own, and nothing stopped an answer from citing `[7]` when only four excerpts existed.

## Options

1. **Keep prompt-only instructions.** Cheap, but not testable and invisible in traces.
2. **An LLM grader node** (self-RAG style). Adds a model call to every request and is itself non-deterministic.
3. **Deterministic grading and checks as graph nodes.** *Chosen.* Retrieval scores already carry the signal needed to grade, and citation markers can be checked against the retrieved list without a model.

## Decision

- `grade_evidence` labels the retrieved excerpts `strong` (best score ≥ `KNOWLEDGE_STRONG_SCORE`), `weak` or `none`.
- `after_grade` sends **weak evidence on a follow-up** (there is a previous user turn and attempts remain under `KNOWLEDGE_MAX_ATTEMPTS`) to `rewrite_query`, which prefixes the previous user turn, and back to `knowledge`. The retry's results replace the first attempt's unless the retry finds nothing: the contextual query states the intent better even when the bare follow-up happened to score higher on another document (a test covers exactly this case).
- Weak evidence that reaches the agent is labelled in the prompt as loosely related.
- `verify_citations` checks every `[n]` outside code blocks against the retrieved excerpts. In single mode an invalid citation sends the answer back to the specialist once (`CITATION_MAX_RETRIES`) with a correction; otherwise, and always in team mode, invalid markers are stripped and the response is flagged `citation:invalid` (fail closed). Without retrieved excerpts, brackets are not treated as citations (`arr[3]` stays intact).
- Every deciding node appends `{node, decision, ...detail}` to `route_log`, which is reset each turn and returned with the response, together with `evidence`, `citations` and a `decision_record`.
- Route functions stay small and read only state; the helpers live in `evidence.py` and are unit-tested. `tests/test_pathways.py` asserts routes and state transitions, not answer quality, as the paper recommends.

## Consequences

- A follow-up with weak evidence costs one extra retrieval (an embedding call and a vector search), never an extra LLM call. A regenerated answer costs one more specialist call, only when the model invented a citation.
- `KNOWLEDGE_STRONG_SCORE` depends on the embedder, like `KNOWLEDGE_MIN_SCORE`: about 0.25 with the lexical hashing embedder, about 0.5 with semantic embeddings. It should be calibrated from the answer evals.
- The streaming API emits a new `evidence` event after retrieval.
