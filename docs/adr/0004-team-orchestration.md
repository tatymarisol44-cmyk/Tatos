# ADR 0004: Team orchestration with a planner, dependency waves and a synthesizer

**Status:** accepted

## Context

Many business requests span several specialties ("launch plan: pricing, landing page, SEO, security review"). Routing to one specialist answers only part of them. The SaaS needs to put several of the 260 agents to work on one request, while keeping cost, latency and failure behaviour predictable.

## Options

1. **Free-form supervisor loop.** A supervisor LLM calls agents as tools until it decides it is done. Flexible, but the number of calls, cost and latency are unbounded, and runs are hard to reproduce or test.
2. **Fan out to the top-k retrieved agents and merge.** Cheap and predictable, but every agent answers the whole request: no division of labour, a lot of overlap.
3. **Plan once, execute a DAG, synthesize once.** *Chosen.*

## Decision

Option 3, in the same LangGraph graph as single-agent routing:

- The planner sees only retrieved candidates (k=16) and returns at most `TEAM_MAX_AGENTS` steps with `depends_on`. Validation keeps only steps whose dependencies point to *earlier* steps, so the plan is a DAG by construction.
- Execution uses `Send` to fan out every ready step; a `join` node collects each wave and dispatches the next. Dependents receive their dependencies' outputs.
- One synthesis call merges the contributions; a one-step plan skips it.
- Cost is bounded: 1 planner + at most `TEAM_MAX_AGENTS` specialists + 1 synthesizer call.

## Consequences

- Every failure degrades instead of failing: invalid plan → retrieval plan from distinct divisions; failed specialist → recorded, dependents informed; synthesizer outage → contributions returned as sections.
- Latency is roughly planner + (DAG depth × specialist latency) + synthesizer, so long chains are slower. The planner prompt favours parallel steps.
- The plan and per-step results are part of the API response and the SSE stream, which makes the orchestration auditable by tenants and easy to show in a UI.
- Team answer quality needs its own evaluation (plan coverage, faithfulness of the synthesis); the routing eval does not cover it.
