"""FetchPolicy — the supervisor's rules. All defaults bias toward stopping early."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class FetchPolicy:
    """Knobs the caller turns. Defaults assume 'bias toward halting'."""

    # --- per-target ---
    max_retries: int = 1
    """How many times to retry a single target after a transient failure."""

    backoff_base_s: float = 1.0
    """Exponential backoff base: sleep = base * (2 ** attempt)."""

    backoff_max_s: float = 30.0

    timeout_s: float = 20.0

    # --- halting ---
    halt_on_status: frozenset[int] = frozenset({429, 503})
    """Any of these codes from any URL halts the whole walk immediately."""

    halt_after_host_consecutive_failures: int = 3
    """After N back-to-back non-2xx from the SAME host, break that host."""

    halt_after_global_failures: int = 10
    """Across all hosts, halt if total failures exceed this."""

    respect_retry_after: bool = True
    """On 429/503 with a Retry-After header, record the value on the event."""

    # --- what counts as a failure ---
    failure_statuses: frozenset[int] = field(
        default_factory=lambda: frozenset(range(400, 600)) - frozenset({404})
    )
    """Status codes that count as failures. 404 is often legitimate (gone);
    exclude it from the host-break counter by default."""

    @classmethod
    def strict(cls) -> "FetchPolicy":
        """Tighter version: any non-2xx counts, one attempt only."""
        return cls(
            max_retries=0,
            halt_on_status=frozenset({429, 503, 502, 504}),
            halt_after_host_consecutive_failures=2,
            halt_after_global_failures=5,
            failure_statuses=frozenset(range(400, 600)),
        )

    @classmethod
    def lenient(cls) -> "FetchPolicy":
        """Looser: more retries, tolerate more failures. Use when caller recovers well."""
        return cls(
            max_retries=3,
            halt_after_host_consecutive_failures=5,
            halt_after_global_failures=20,
        )

    def backoff_for(self, attempt: int) -> float:
        seconds = self.backoff_base_s * (2 ** attempt)
        return min(seconds, self.backoff_max_s)
