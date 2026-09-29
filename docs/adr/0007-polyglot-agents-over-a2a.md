# ADR 0007: Polyglot specialists as remote A2A agents

**Status:** accepted (extends ADR 0003)

## Context

ADR 0003 kept the core in Python and deferred other languages to A2A. Some specialists are better built where their domain lives: a JVM performance agent in Java, next to the tools and libraries it talks about; an ERP integration in .NET; a team's existing Spring AI service. They should be routable from the same catalog, take part in team plans, and not require the orchestrator to know their implementation.

## Options

1. **Call them as MCP tools.** MCP models *tools* the LLM calls, not peer agents with their own reasoning and memory, and it would bypass routing and team planning.
2. **Custom REST contract per agent.** Simple, but every integration is bespoke and nothing is discoverable.
3. **A2A remote agents discovered from their Agent Cards.** *Chosen.* A2A is the open agent-to-agent protocol our API already serves, so the orchestrator becomes a client too.

## Decision

- `REMOTE_AGENTS` lists base URLs (operator config only, never request data). At startup the orchestrator fetches each `/.well-known/agent-card.json`, validates it and adds an `AgentSpec` with `division="remote"` and a `remote_url`. The card's description and skills form the indexed text, so routing and planning need no special case.
- The catalog version hashes remote agents too, so the versioned routing index (ADR 0002) is rebuilt when a card changes.
- `specialist` and `worker` nodes call `message/send` when the agent is remote. `contextId = uuid5(tenant thread, agent)` gives the agent stable multi-turn context without revealing tenant or thread ids.
- **Failure policy (degrade, don't fail):** discovery retries late starters and skips agents that never come up. At request time, any transport, protocol or format error makes the LLM answer in the agent's role from its card, flagged `remote.status = "fallback"`.
- **Trust boundary:** the JSON-RPC URL must share the card's origin (a card cannot redirect traffic to an arbitrary host). No redirects are followed. Bodies are capped at 1 MB while streaming, replies at `REMOTE_AGENT_MAX_CHARS`. Replies pass the output guard. The orchestrator refuses to register itself (no loops). Tenant RAG context is withheld unless `REMOTE_SHARE_KNOWLEDGE=true`.
- **Reference agent:** `agents/jvm-specialist`, Java 21 + Spring Boot 4, A2A implemented directly (Agent Card and JSON-RPC with correct error codes). It is rule-based by default (deterministic, no key), and its Spring AI mode is grounded in the same playbook and falls back to it.

## Consequences

- A specialist can now be written in any language and deployed and scaled on its own. CI proves the contract end to end: it builds the Java agent, runs its JUnit tests, starts it and runs a Python contract test over real HTTP.
- Discovery happens once, at startup: adding an agent needs a restart. Periodic refresh must rebuild the index atomically, and is future work.
- Remote calls add network latency and a new failure mode. Both show up in traces (`agent.remote` span) and in the response (`remote.status`).
- One shared `REMOTE_AGENTS_API_KEY` is used for every remote agent. Per-agent credentials (or mTLS inside the cluster) would be the next step for third-party agents.
