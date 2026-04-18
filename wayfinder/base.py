"""The Wayfinder protocol — the shape every agent-worker implements.

A Wayfinder is a scoped, supervised worker that Warden spawns on the
sandbox's behalf. It receives inputs and a secret-resolver, emits events
while it runs, and returns a final structured report.

Today we ship one concrete type (`http_walker`). Future siblings: a
browser-driving walker, a per-site scraper, a signup+verification agent,
a paginated-API crawler.
"""
from __future__ import annotations

import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


# ---------- identity / schema ----------

@dataclass(slots=True, frozen=True)
class WayfinderSpec:
    """Metadata Warden uses to register and describe a wayfinder type."""

    type_name: str
    """Short identifier (e.g. `http_walker`, `browser_session`, `signup_agent`).
    Referenced by policy and by the `wayfinder.spawn` RPC."""

    description: str

    required_inputs: frozenset[str] = frozenset()
    """Keys the caller MUST supply in the spawn `inputs` map."""

    allowed_inputs: frozenset[str] = frozenset()
    """All keys the caller MAY supply. Extra keys are rejected by runtime."""

    secret_refs_allowed: bool = False
    """If False, the runtime refuses `secret://...` refs in this wayfinder's
    inputs. Today only the HTTP walker declines secrets; the browser walker
    and signup agent will set this True."""


# ---------- wire types on the event stream ----------

@dataclass(slots=True, frozen=True)
class WayfinderEvent:
    """A single supervised event emitted by a running wayfinder."""

    ts: float
    """Unix seconds when the event was produced."""

    kind: str
    """Coarse category: `progress` | `finding` | `error` | `status` | `scope_violation`."""

    data: dict[str, Any] = field(default_factory=dict)
    """Payload. Shape varies by wayfinder type; always JSON-serializable."""

    @classmethod
    def now(cls, kind: str, **data: Any) -> "WayfinderEvent":
        return cls(ts=time.time(), kind=kind, data=dict(data))


@dataclass(slots=True)
class WayfinderReport:
    """Terminal state of a spawned wayfinder."""

    spawn_id: str
    type_name: str
    status: str                          # "completed" | "failed" | "terminated"
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    events_count: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.error is None


EmitFn = Callable[[WayfinderEvent], None]


# ---------- secret resolution inside a wayfinder ----------

class SecretResolver(Protocol):
    """Wayfinders never touch the keychain directly. They receive one of these
    from the runtime and call `.get(value_ref, scope=...)` to dereference."""

    def get(self, value_ref: str, *, scope: "SecretScope") -> str: ...


@dataclass(slots=True, frozen=True)
class SecretScope:
    """Scope constraints that must be true at the moment the secret is used.
    The resolver refuses if the current state doesn't satisfy the scope."""

    context: str
    """The secret's context must match; cross-context refs are refused."""

    host: str | None = None
    """For HTTP use, the hostname the secret will be attached to — must be in
    the wayfinder's declared `allowed_domains`."""


# ---------- the interface ----------

class Wayfinder(Protocol):
    """What every wayfinder type implements."""

    spec: WayfinderSpec

    @abstractmethod
    def run(
        self,
        inputs: dict[str, Any],
        *,
        secret_resolver: SecretResolver | None,
        emit: EmitFn,
    ) -> WayfinderReport: ...


def validate_inputs(spec: WayfinderSpec, inputs: dict[str, Any]) -> None:
    """Fail fast on missing required keys or unknown keys.

    Called by the runtime before invoking the wayfinder; keeps spawn failures
    informative instead of surfacing as a KeyError inside the worker."""
    keys = set(inputs.keys())
    missing = spec.required_inputs - keys
    if missing:
        raise ValueError(
            f"wayfinder {spec.type_name!r} missing required inputs: {sorted(missing)}"
        )
    if spec.allowed_inputs:
        unknown = keys - spec.allowed_inputs
        if unknown:
            raise ValueError(
                f"wayfinder {spec.type_name!r} got unknown inputs: {sorted(unknown)}"
            )
