"""HttpWalkerWayfinder — wraps the existing `walk()` primitive as a Wayfinder.

Inputs:
  targets:   [{url, headers?, tag?}, ...]                 required
  policy:    optional dict overrides (max_retries, halt_on_status, ...)
  user_agent: optional UA override

Output:
  successes:   {url: {status, headers, body_b64}}
  failures:    [{url, status, error, host}]
  halted:      bool
  halt_reason: str | null
  broken_hosts: [str]
"""
from __future__ import annotations

import base64
from typing import Any

from wayfinder.base import (
    EmitFn,
    SecretResolver,
    Wayfinder,
    WayfinderEvent,
    WayfinderReport,
    WayfinderSpec,
)
from wayfinder.http_client import HttpxAdapter
from wayfinder.policy import FetchPolicy
from wayfinder.walker import WalkTarget, walk


_SPEC = WayfinderSpec(
    type_name="http_walker",
    description=(
        "Resilient HTTP walker with per-host circuit breakers, Retry-After "
        "handling, and a supervised event stream."
    ),
    required_inputs=frozenset({"targets"}),
    allowed_inputs=frozenset({"targets", "policy", "user_agent"}),
    secret_refs_allowed=False,
)


class HttpWalkerWayfinder:
    spec = _SPEC

    def run(
        self,
        inputs: dict[str, Any],
        *,
        secret_resolver: SecretResolver | None = None,
        emit: EmitFn,
    ) -> WayfinderReport:
        raw_targets = inputs["targets"]
        if not isinstance(raw_targets, list) or not raw_targets:
            return WayfinderReport(
                spawn_id=inputs.get("__spawn_id", ""),
                type_name=_SPEC.type_name,
                status="failed",
                error="`targets` must be a non-empty list",
            )
        targets = [_to_target(t) for t in raw_targets]
        policy = _policy_from_dict(inputs.get("policy") or {})
        user_agent = str(inputs.get("user_agent") or "weaver-aggregator/0.1")

        http = HttpxAdapter(user_agent=user_agent)

        def _on_event(e: Any) -> None:
            emit(WayfinderEvent.now(
                _kind_for(e.kind.value),
                url=e.url, status=e.status, host=e.host,
                retry_after_s=e.retry_after_s, error=e.error,
            ))

        try:
            report = walk(targets, policy, http=http, on_event=_on_event)
        finally:
            try:
                http.close()
            except Exception:  # noqa: BLE001
                pass

        successes: dict[str, Any] = {}
        for url, evt in report.successes.items():
            successes[url] = {
                "status": evt.status,
                "headers": evt.headers,
                "body_b64": base64.b64encode(evt.body or b"").decode("ascii"),
            }
        failures = [
            {"url": e.url, "status": e.status, "error": e.error, "host": e.host}
            for e in report.failures
        ]
        return WayfinderReport(
            spawn_id=inputs.get("__spawn_id", ""),
            type_name=_SPEC.type_name,
            status="completed" if not report.halted else "terminated",
            output={
                "successes": successes,
                "failures": failures,
                "halted": report.halted,
                "halt_reason": report.halt_reason,
                "broken_hosts": sorted(report.broken_hosts),
                "events_count": len(report.events),
            },
            error=report.halt_reason if report.halted else None,
            events_count=len(report.events),
        )


# ---------- helpers ----------

def _to_target(raw: dict[str, Any]) -> WalkTarget:
    if not isinstance(raw, dict) or "url" not in raw:
        raise ValueError(f"bad target (need {{url, headers?, tag?}}): {raw!r}")
    return WalkTarget(
        url=str(raw["url"]),
        headers=dict(raw.get("headers") or {}),
        tag=raw.get("tag"),
    )


def _policy_from_dict(d: dict[str, Any]) -> FetchPolicy:
    """Caller passes a plain dict; we translate to a FetchPolicy. Unknown keys
    are ignored so callers can be forward-compatible with policy evolution."""
    base = FetchPolicy()
    kwargs: dict[str, Any] = {}
    for f in ("max_retries", "backoff_base_s", "backoff_max_s", "timeout_s",
              "halt_after_host_consecutive_failures", "halt_after_global_failures",
              "respect_retry_after"):
        if f in d:
            kwargs[f] = d[f]
    if "halt_on_status" in d:
        kwargs["halt_on_status"] = frozenset(int(x) for x in d["halt_on_status"])
    if "failure_statuses" in d:
        kwargs["failure_statuses"] = frozenset(int(x) for x in d["failure_statuses"])
    if not kwargs:
        return base
    # `FetchPolicy` is frozen; build a new one with the merged values.
    return FetchPolicy(
        max_retries=kwargs.get("max_retries", base.max_retries),
        backoff_base_s=kwargs.get("backoff_base_s", base.backoff_base_s),
        backoff_max_s=kwargs.get("backoff_max_s", base.backoff_max_s),
        timeout_s=kwargs.get("timeout_s", base.timeout_s),
        halt_on_status=kwargs.get("halt_on_status", base.halt_on_status),
        halt_after_host_consecutive_failures=kwargs.get(
            "halt_after_host_consecutive_failures",
            base.halt_after_host_consecutive_failures,
        ),
        halt_after_global_failures=kwargs.get(
            "halt_after_global_failures", base.halt_after_global_failures,
        ),
        respect_retry_after=kwargs.get("respect_retry_after", base.respect_retry_after),
        failure_statuses=kwargs.get("failure_statuses", base.failure_statuses),
    )


def _kind_for(walker_kind: str) -> str:
    """Map walker.events.EventKind → the coarser WayfinderEvent.kind taxonomy."""
    return {
        "fetched": "finding",
        "error": "error",
        "retry": "progress",
        "host_break": "scope_violation",
        "skipped": "progress",
        "halted": "error",
    }.get(walker_kind, walker_kind)
