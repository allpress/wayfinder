"""Structured events + the end-of-walk report. All caller-visible state lives here."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    fetched = "fetched"          # 2xx success
    error = "error"              # 4xx/5xx, non-halting
    retry = "retry"              # about to retry after backoff
    host_break = "host_break"    # circuit-broken this host
    skipped = "skipped"          # host is broken; skipped
    halted = "halted"            # walker is stopping immediately


@dataclass(slots=True, frozen=True)
class WalkEvent:
    kind: EventKind
    url: str
    status: int | None = None
    elapsed_ms: int | None = None
    retry_after_s: float | None = None
    error: str | None = None
    host: str | None = None
    body: bytes | None = None            # present only on `fetched`
    headers: dict[str, str] = field(default_factory=dict)

    def pretty(self) -> str:
        prefix = f"[{self.kind.value:10}]"
        code = f" {self.status}" if self.status is not None else ""
        extra = ""
        if self.retry_after_s is not None:
            extra += f" retry_after={self.retry_after_s}s"
        if self.error:
            extra += f" — {self.error}"
        return f"{prefix}{code} {self.url}{extra}"


@dataclass(slots=True)
class WalkReport:
    events: list[WalkEvent] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None
    successes: dict[str, WalkEvent] = field(default_factory=dict)
    failures: list[WalkEvent] = field(default_factory=list)
    broken_hosts: set[str] = field(default_factory=set)

    @property
    def total_attempts(self) -> int:
        return sum(1 for e in self.events
                   if e.kind in {EventKind.fetched, EventKind.error})

    def summary(self) -> dict[str, Any]:
        return {
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "successes": len(self.successes),
            "failures": len(self.failures),
            "broken_hosts": sorted(self.broken_hosts),
            "events": len(self.events),
        }
