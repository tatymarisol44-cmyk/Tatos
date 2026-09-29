"""Loads specialist agents from The Agency markdown catalog. Remote A2A agents (see
remote.py) are added at startup and routed like local ones."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
# Top-level folders that are not agent divisions (see divisions.json in the catalog).
_NON_DIVISION_DIRS = {"integrations", "examples", "scripts", "strategy", ".git", ".github"}


@dataclass(frozen=True)
class AgentSpec:
    id: str
    name: str
    description: str
    division: str
    system_prompt: str
    emoji: str = ""
    vibe: str = ""
    path: str = ""
    # JSON-RPC endpoint of a remote A2A agent; empty for local (prompt-based) agents.
    remote_url: str = ""

    def index_text(self) -> str:
        """Text used to embed the agent for routing. The name is repeated to weight it,
        and the mission is truncated: longer excerpts added noise and lowered recall
        on evals/routing.jsonl."""
        mission = _section(self.system_prompt, "Core Mission")[:300]
        return "\n".join(
            part for part in (self.name, self.name, self.description, self.vibe, mission) if part
        )


@dataclass
class Catalog:
    agents: dict[str, AgentSpec]
    version: str
    divisions: list[str] = field(default_factory=list)

    def get(self, agent_id: str) -> AgentSpec | None:
        return self.agents.get(agent_id)

    def __len__(self) -> int:
        return len(self.agents)

    def add_remote(self, specs: list[AgentSpec]) -> list[AgentSpec]:
        """Register remote agents, skipping ids already taken. The catalog version covers
        them too, so the (immutable, versioned) routing index is rebuilt when they change."""
        added = [s for s in specs if s.id not in self.agents]
        if not added:
            return []
        digest = hashlib.sha256(self.version.encode())
        for spec in sorted(added, key=lambda s: s.id):
            self.agents[spec.id] = spec
            for part in (spec.id, spec.remote_url, spec.name, spec.description, spec.system_prompt):
                digest.update(part.encode())
                digest.update(b"\x00")  # field separator
        self.version = digest.hexdigest()[:12]
        if "remote" not in self.divisions:
            self.divisions.append("remote")
        return added


def _section(body: str, heading: str) -> str:
    match = re.search(
        rf"^##[^\n]*{re.escape(heading)}[^\n]*\n(.*?)(?=^## |\Z)", body, re.DOTALL | re.MULTILINE
    )
    return match.group(1).strip() if match else ""


def _parse_frontmatter(raw: str) -> dict[str, object]:
    """YAML first; some catalog files have unquoted colons in values, so fall back to
    a flat `key: value` reader that splits on the first colon only."""
    try:
        meta = yaml.safe_load(raw)
        if isinstance(meta, dict):
            return meta
    except yaml.YAMLError:
        pass
    flat: dict[str, object] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not line[:1].isspace():
            flat[key.strip()] = value.strip().strip("\"'")
    return flat


def parse_agent(path: Path, division: str) -> AgentSpec | None:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = _FRONTMATTER.match(text)
    if not match:
        return None
    meta = _parse_frontmatter(match.group(1))
    if not isinstance(meta, dict) or not meta.get("name") or not meta.get("description"):
        return None
    return AgentSpec(
        id=path.stem,
        name=str(meta["name"]).strip(),
        description=str(meta["description"]).strip(),
        division=division,
        system_prompt=match.group(2).strip(),
        emoji=str(meta.get("emoji", "")),
        vibe=str(meta.get("vibe", "")),
        path=str(path),
    )


def _division_dirs(root: Path) -> list[str]:
    divisions_file = root / "divisions.json"
    if divisions_file.exists():
        data = json.loads(divisions_file.read_text(encoding="utf-8"))
        return sorted(data.get("divisions", {}))
    return sorted(p.name for p in root.iterdir() if p.is_dir() and p.name not in _NON_DIVISION_DIRS)


def load_catalog(root: Path) -> Catalog:
    if not root.is_dir():
        raise FileNotFoundError(
            f"Agent catalog not found at {root}. Run `git submodule update --init`."
        )
    agents: dict[str, AgentSpec] = {}
    digest = hashlib.sha256()
    divisions = _division_dirs(root)
    for division in divisions:
        for path in sorted((root / division).rglob("*.md")):
            agent = parse_agent(path, division)
            if agent is None:
                continue
            agents[agent.id] = agent
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    if not agents:
        raise ValueError(f"No agents with frontmatter found under {root}")
    return Catalog(agents=agents, version=digest.hexdigest()[:12], divisions=divisions)
