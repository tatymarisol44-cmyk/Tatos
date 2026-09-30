"""A2A client: specialist agents written in any language (a Spring Boot service, a
.NET Semantic Kernel agent, ...) join the catalog and are routed like local ones.

At startup each configured base URL is asked for its Agent Card; the card becomes an
`AgentSpec` whose description and skills feed the routing index. When the router or
planner picks it, the graph calls `message/send` instead of an LLM.

Trust boundary: remote agents are operator-configured, but their cards and replies are
still untrusted input. The JSON-RPC URL must stay on the card's own origin (a card
cannot redirect traffic elsewhere), responses are size-capped, and replies go through
the same output guard as local answers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from orchestrator.catalog import AgentSpec
from orchestrator.config import Settings

log = logging.getLogger(__name__)

CARD_PATH = "/.well-known/agent-card.json"
_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_BYTES = 1_000_000


class RemoteAgentError(RuntimeError):
    """A remote agent could not be reached or replied with something unusable."""


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
    return parts.scheme, (parts.hostname or "").lower(), port


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def card_to_spec(base_url: str, card: dict[str, Any]) -> AgentSpec:
    """Validate an Agent Card and turn it into a routable catalog entry."""
    name = _text(card.get("name"), 120)
    description = _text(card.get("description"), 2000)
    if not name or not description:
        raise ValueError("agent card needs a name and a description")
    transport = card.get("preferredTransport", "JSONRPC")
    if transport != "JSONRPC":
        raise ValueError(f"unsupported transport {transport!r} (only JSONRPC)")
    rpc_url = urljoin(base_url.rstrip("/") + "/", _text(card.get("url"), 2000) or "a2a")
    if _origin(rpc_url) != _origin(base_url):
        raise ValueError(f"card url {rpc_url} is not on the agent's origin {base_url}")
    slug = _SLUG.sub("-", name.lower()).strip("-")[:60]
    if not slug:
        raise ValueError("agent card name has no usable characters")

    skills = [s for s in card.get("skills") or [] if isinstance(s, dict)][:20]
    skill_lines = []
    for skill in skills:
        if not _text(skill.get("name"), 80):
            continue
        line = f"- {_text(skill.get('name'), 80)}: {_text(skill.get('description'), 300)}"
        tags = [t for t in skill.get("tags") or [] if isinstance(t, str)][:10]
        skill_lines.append(line + (f" (tags: {', '.join(tags)})" if tags else ""))
    # Markdown shaped like the local agents, so the same index text and (if the remote
    # agent is down) a local fallback prompt can be built from it.
    prompt = f"# {name}\n\n## Core Mission\n{description}\n"
    if skill_lines:
        prompt += "\n## Skills\n" + "\n".join(skill_lines) + "\n"
    return AgentSpec(
        id=f"remote-{slug}",
        name=name,
        description=description,
        division="remote",
        system_prompt=prompt,
        emoji="🌐",
        vibe=" ".join(_text(s.get("name"), 80) for s in skills),
        path=base_url,
        remote_url=rpc_url,
    )


def _reply_text(result: Any) -> str:
    """Text of a `message/send` result, which may be a Message or a Task."""
    if not isinstance(result, dict):
        return ""
    parts: list[Any] = []
    if result.get("kind") == "message":
        parts = list(result.get("parts") or [])
    elif result.get("kind") == "task":
        for artifact in result.get("artifacts") or []:
            if isinstance(artifact, dict):
                parts.extend(artifact.get("parts") or [])
        status = result.get("status")
        message = status.get("message") if isinstance(status, dict) else None
        if not parts and isinstance(message, dict):
            parts = list(message.get("parts") or [])
    texts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
    return "\n".join(texts).strip()


class A2AClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        headers = {"User-Agent": "agency-orchestrator"}
        if settings.remote_agents_api_key is not None:
            headers["X-API-Key"] = settings.remote_agents_api_key.get_secret_value()
        self._http = httpx.AsyncClient(
            timeout=settings.remote_agent_timeout_s,
            headers=headers,
            transport=transport,
            follow_redirects=False,  # a redirect could leave the configured origin
        )

    async def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        body = bytearray()
        deadline = self.settings.remote_agent_timeout_s
        try:
            # httpx timeouts apply per operation (connect, each read...): a server that
            # trickles a byte just under the read timeout would never time out. The
            # deadline bounds the whole exchange.
            async with asyncio.timeout(deadline):
                async with self._http.stream(method, url, **kwargs) as resp:
                    if resp.status_code != 200:
                        raise RemoteAgentError(f"HTTP {resp.status_code} from {url}")
                    # Stop reading as soon as the cap is passed instead of buffering it all.
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_BYTES:
                            raise RemoteAgentError(
                                f"response from {url} exceeds {_MAX_BYTES} bytes"
                            )
        except TimeoutError as exc:
            raise RemoteAgentError(f"no complete response from {url} in {deadline}s") from exc
        except httpx.HTTPError as exc:
            raise RemoteAgentError(f"{type(exc).__name__} calling {url}") from exc
        try:
            return json.loads(body)
        except ValueError as exc:
            raise RemoteAgentError(f"non-JSON response from {url}") from exc

    async def discover(self, base_url: str) -> AgentSpec:
        card = await self._json("GET", base_url.rstrip("/") + CARD_PATH)
        if not isinstance(card, dict):
            raise RemoteAgentError(f"agent card at {base_url} is not an object")
        try:
            return card_to_spec(base_url, card)
        except ValueError as exc:
            raise RemoteAgentError(f"invalid agent card at {base_url}: {exc}") from exc

    async def send(self, agent: AgentSpec, text: str, context_id: str) -> str:
        request_id = uuid.uuid4().hex
        body = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "message/send",
            "params": {
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": uuid.uuid4().hex,
                    "contextId": context_id,
                    "parts": [{"kind": "text", "text": text}],
                }
            },
        }
        data = await self._json("POST", agent.remote_url, json=body)
        if not isinstance(data, dict):
            raise RemoteAgentError(f"{agent.id}: JSON-RPC reply is not an object")
        if "error" in data:
            error = data["error"] if isinstance(data["error"], dict) else {}
            raise RemoteAgentError(
                f"{agent.id}: JSON-RPC error {error.get('code')}: "
                f"{_text(error.get('message'), 200)}"
            )
        if data.get("id") != request_id:
            raise RemoteAgentError(f"{agent.id}: JSON-RPC reply id does not match the request")
        reply = _reply_text(data.get("result"))
        if not reply:
            raise RemoteAgentError(f"{agent.id}: reply has no text parts")
        return reply[: self.settings.remote_agent_max_chars]

    async def close(self) -> None:
        await self._http.aclose()


def context_id(thread_key: str, agent_id: str) -> str:
    """Stable per (tenant thread, agent), so the remote agent keeps multi-turn context,
    but opaque: tenant names and thread ids never leave our boundary."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agency:{thread_key}:{agent_id}"))


async def discover_all(client: A2AClient, urls: list[str]) -> list[AgentSpec]:
    """Discover every configured agent. Unreachable ones are retried with linear backoff
    (they may start slower than we do); after the last attempt they are logged and skipped,
    so one bad remote never stops the orchestrator from starting."""
    settings = client.settings
    own = _origin(settings.public_base_url)
    pending = []
    for url in urls:
        if _origin(url) == own:
            log.warning("skipping remote agent %s: it is this orchestrator (loop)", url)
        else:
            pending.append(url)
    found: dict[str, AgentSpec] = {}
    for attempt in range(1, settings.remote_discovery_attempts + 1):
        failed = []
        for url in pending:
            try:
                found[url] = await client.discover(url)
            except RemoteAgentError as exc:
                failed.append(url)
                log.warning("remote agent attempt %d: %s", attempt, exc)
        pending = failed
        if not pending or attempt == settings.remote_discovery_attempts:
            break
        await asyncio.sleep(settings.remote_discovery_backoff_s * attempt)
    for url in pending:
        log.error("remote agent %s unavailable after %d attempts; not routed", url, attempt)

    specs: dict[str, AgentSpec] = {}
    for url in urls:  # config order decides which of two same-named agents wins
        spec = found.get(url)
        if spec is None:
            continue
        if spec.id in specs:
            log.error("remote agent %s NOT registered: duplicate id %s", url, spec.id)
            continue
        specs[spec.id] = spec
        log.info("registered remote agent %s at %s", spec.id, spec.remote_url)
    return list(specs.values())
