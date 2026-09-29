# ADR 0001: Two-stage routing (retrieval, then LLM choice)

**Status:** accepted

## Context

The catalog has ~260 agents and grows every week. We need to route each question to one of them cheaply, quickly and in any language.

## Options

1. **One LLM call with the whole catalog in the prompt.** Simple, but about 25k tokens per request, latency and cost grow with the catalog, and accuracy drops on long candidate lists.
2. **Embedding similarity only.** Cheap and fast, but it can't reason about intent ("my site is slow": frontend? SRE? database?).
3. **Retrieve top-k, then let a small LLM choose.** *Chosen.*

## Decision

Option 3. Retrieval bounds the prompt to k candidates (default 8), so cost stays flat as the catalog grows. A small model (Haiku-class) makes the final choice. The LLM may only return an id from the candidate list; otherwise we fall back to the top retrieval hit.

## Consequences

- Recall@k of the retrieval stage caps overall accuracy, so it is the metric the CI gate tracks.
- Routing degrades gracefully: provider outage → retrieval-only routing, not an error.
- Two model calls per request (router and specialist). The router call is roughly 1k tokens on a cheap model.
