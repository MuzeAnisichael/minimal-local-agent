"""Capability policy compiled into the actual model-visible tool surface."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal

from minimal_local_agent.config import Settings

Decision = Literal["allow", "ask", "deny", "preview"]
Effect = Literal["read", "write", "external-read"]

BUILTIN_EFFECTS: dict[str, Effect] = {
    "list_files": "read",
    "read_file": "read",
    "search_text": "read",
    "write_file": "write",
    "edit_files": "write",
}


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    effect: Effect
    decision: Decision
    source: str


class PolicyEngine:
    """Resolve defaults and explicit overrides without trusting the model."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.overrides = dict(settings.tool_policies)

    def decision(self, name: str, effect: Effect | None = None) -> Decision:
        effect = effect or BUILTIN_EFFECTS.get(name, "external-read")
        if effect == "write" and self.settings.write_policy in {"deny", "preview"}:
            # Global run modes are hard ceilings; per-tool overrides cannot turn a
            # read-only or dry-run invocation back into a mutating one.
            return self.settings.write_policy
        explicit = self.overrides.get(name)
        if explicit is not None:
            return explicit  # Settings validates values.
        if effect in {"read", "external-read"}:
            return "allow"
        return "ask"

    def exposes(self, name: str, effect: Effect | None = None) -> bool:
        return self.decision(name, effect) != "deny"

    def capabilities(
        self,
        external_tools: tuple[str, ...] = (),
        *,
        python_tools: tuple[str, ...] = (),
    ) -> tuple[Capability, ...]:
        values = [
            Capability(
                name=name,
                effect=effect,
                decision=self.decision(name, effect),
                source="builtin",
            )
            for name, effect in BUILTIN_EFFECTS.items()
        ]
        values.extend(
            Capability(
                name=name,
                effect="external-read",
                decision=self.decision(name, "external-read"),
                source="mcp",
            )
            for name in external_tools
        )
        values.extend(
            Capability(
                name=name,
                effect="external-read",
                decision=self.decision(name, "external-read"),
                source="python",
            )
            for name in python_tools
        )
        return tuple(values)

    def manifest(
        self,
        external_tools: tuple[str, ...] = (),
        *,
        python_tools: tuple[str, ...] = (),
    ) -> list[dict[str, str]]:
        return [
            asdict(capability)
            for capability in self.capabilities(
                external_tools, python_tools=python_tools
            )
        ]

    def fingerprint(
        self,
        external_tools: tuple[str, ...] = (),
        *,
        python_tools: tuple[str, ...] = (),
    ) -> str:
        encoded = json.dumps(
            self.manifest(external_tools, python_tools=python_tools),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BUILTIN_EFFECTS",
    "Capability",
    "Decision",
    "Effect",
    "PolicyEngine",
]
