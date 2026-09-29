# ADR 0003: Python core; other languages join through A2A

**Status:** accepted

## Context

The ecosystem spans Python (LangChain/LangGraph), Java (Spring AI, Quarkus LangChain4j) and .NET (Semantic Kernel). Building the orchestrator in all three would triple maintenance without improving it.

## Decision

The orchestrator is Python with LangGraph, which has the most mature agent-graph, checkpointing and evaluation tooling. Interoperability goes through open protocols instead of shared code:

- **A2A** (Agent Card and JSON-RPC `message/send`), so agents written with Spring AI or Semantic Kernel can call the orchestrator or be called by it.
- **MCP**, so any MCP client (Claude Desktop, Claude Code, Cursor) can use the orchestrator as tools.

## Consequences

- A Java or .NET specialist is added by deploying it as an A2A service and registering it as a remote agent, with no changes to the core.
- The in-process agents and remote A2A agents need a common "agent" abstraction in the router. That is future work (see README roadmap).
