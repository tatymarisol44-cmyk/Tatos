# JVM Performance Specialist (A2A agent)

A remote specialist for the Agency Orchestrator, written in **Java 21 / Spring Boot 4** and served over the [A2A protocol](https://a2a-protocol.org). The orchestrator discovers it from its Agent Card and routes JVM questions (memory, GC, container sizing, startup, threads, profiling) to it. See [ADR 0007](../../docs/adr/0007-polyglot-agents-over-a2a.md).

| Endpoint | |
|---|---|
| `GET /.well-known/agent-card.json` | Agent Card: name, description, skills, JSON-RPC URL. Public, so callers can discover it. |
| `POST /a2a` | JSON-RPC 2.0 `message/send`. Errors: -32700 parse, -32600 invalid request, -32601 unknown method, -32602 invalid params, -32603 internal. |
| `GET /actuator/health/{liveness,readiness}` | Kubernetes probes. |

**Modes**

- `rules` (default, no API key): a deterministic playbook matched by English and Spanish keywords, with concrete flags and `jcmd`/JFR commands. Useful offline, fully testable.
- `llm` (`AGENT_LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY`): Spring AI `ChatClient`, grounded in the matched playbook and with bounded per-`contextId` memory. It falls back to the rules if the model fails (`metadata.mode = "rules-fallback"`).

**Configuration** (`AGENT_*` environment variables): `AGENT_PUBLIC_URL` (advertised base URL; derived from the request when empty), `AGENT_API_KEY` (require `X-API-Key` on `/a2a`), `AGENT_LLM_MODEL`, `PORT`.

```bash
./mvnw verify                 # build + 34 JUnit tests (needs JDK 21; the wrapper fetches Maven)
./mvnw spring-boot:run        # http://localhost:8080
curl -s localhost:8080/a2a -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"message/send",
  "params":{"message":{"kind":"message","role":"user","messageId":"m1","parts":[{"kind":"text","text":"Pods get OOMKilled"}]}}}'
```

The container image runs as non-root with `-XX:MaxRAMPercentage=75 -XX:+ExitOnOutOfMemoryError`, the same advice the agent gives.
