"""The walker. Takes a list of targets; runs them under the policy; halts fast."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from wayfinder.breaker import HostBreaker
from wayfinder.events import EventKind, WalkEvent, WalkReport
from wayfinder.http_client import HttpClient, HttpResponse, HttpxAdapter
from wayfinder.policy import FetchPolicy

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class WalkTarget:
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    tag: str | None = None
    """Opaque caller-side identifier (e.g. "feed:fowler", "article:123")."""


OnEvent = Callable[[WalkEvent], None]


def walk(
    targets: list[WalkTarget],
    policy: FetchPolicy | None = None,
    *,
    http: HttpClient | None = None,
    on_event: OnEvent | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> WalkReport:
    """Execute the walk. Returns a WalkReport; halted=True means we bailed.

    Nothing in this function raises for HTTP-level failures. All error states
    become structured WalkEvents in the report. The only raised exceptions are
    programmer errors (bad types) or non-HTTP failures from the client
    adapter (network dead, DNS failure), which are captured and reported too.
    """
    policy = policy or FetchPolicy()
    http = http or HttpxAdapter()
    breaker = HostBreaker(threshold=policy.halt_after_host_consecutive_failures)
    report = WalkReport()

    def _emit(event: WalkEvent) -> None:
        report.events.append(event)
        if on_event is not None:
            try:
                on_event(event)
            except Exception:  # noqa: BLE001
                log.exception("on_event callback raised; continuing")

    global_failures = 0

    for target in targets:
        host = breaker.host_of(target.url)

        if breaker.is_broken(target.url):
            _emit(WalkEvent(
                kind=EventKind.skipped, url=target.url, host=host,
                error=f"host {host!r} circuit-broken earlier in this walk",
            ))
            continue

        fetched, halt = _try_target(
            target, policy, http, breaker, sleep, _emit,
        )
        if halt is not None:
            report.halted = True
            report.halt_reason = halt
            report.broken_hosts = set(breaker.broken)
            return report

        if fetched is None:
            # non-halting failure already emitted
            global_failures += 1
            if global_failures >= policy.halt_after_global_failures:
                reason = (
                    f"global failure cap ({policy.halt_after_global_failures}) hit"
                )
                _emit(WalkEvent(
                    kind=EventKind.halted, url=target.url, host=host,
                    error=reason,
                ))
                report.halted = True
                report.halt_reason = reason
                report.broken_hosts = set(breaker.broken)
                return report
        else:
            report.successes[target.url] = fetched

    # Failures + broken hosts assembled at the end for report convenience.
    report.failures = [e for e in report.events if e.kind == EventKind.error]
    report.broken_hosts = set(breaker.broken)
    return report


def _try_target(
    target: WalkTarget,
    policy: FetchPolicy,
    http: HttpClient,
    breaker: HostBreaker,
    sleep: Callable[[float], None],
    emit: Callable[[WalkEvent], None],
) -> tuple[WalkEvent | None, str | None]:
    """Attempt one URL with retries. Returns (success_event_or_None, halt_reason_or_None)."""
    host = breaker.host_of(target.url)

    for attempt in range(policy.max_retries + 1):
        started = time.monotonic()
        try:
            resp = http.get(target.url, headers=target.headers or None,
                            timeout=policy.timeout_s)
        except Exception as e:  # noqa: BLE001
            elapsed_ms = int((time.monotonic() - started) * 1000)
            emit(WalkEvent(
                kind=EventKind.error, url=target.url, host=host,
                status=None, elapsed_ms=elapsed_ms,
                error=f"{type(e).__name__}: {e}",
            ))
            if attempt < policy.max_retries:
                wait = policy.backoff_for(attempt)
                emit(WalkEvent(kind=EventKind.retry, url=target.url,
                               host=host, retry_after_s=wait,
                               error="network exception"))
                sleep(wait)
                continue
            tripped = breaker.record_failure(target.url)
            if tripped:
                emit(WalkEvent(kind=EventKind.host_break, url=target.url,
                               host=host, error=f"host {host!r} broken"))
            return None, None

        elapsed_ms = int((time.monotonic() - started) * 1000)

        # Immediate halt codes: return right away, skip retries.
        if resp.status_code in policy.halt_on_status:
            retry_after = _parse_retry_after(resp) if policy.respect_retry_after else None
            emit(WalkEvent(
                kind=EventKind.halted, url=target.url, host=host,
                status=resp.status_code, elapsed_ms=elapsed_ms,
                retry_after_s=retry_after,
                error=f"halt-on-status {resp.status_code}",
                headers=dict(resp.headers),
            ))
            return None, (
                f"{host} returned {resp.status_code}"
                + (f" (Retry-After={retry_after}s)" if retry_after else "")
            )

        if resp.status_code in policy.failure_statuses:
            if attempt < policy.max_retries:
                wait = policy.backoff_for(attempt)
                emit(WalkEvent(
                    kind=EventKind.retry, url=target.url, host=host,
                    status=resp.status_code, retry_after_s=wait,
                    error=f"HTTP {resp.status_code}",
                ))
                sleep(wait)
                continue
            emit(WalkEvent(
                kind=EventKind.error, url=target.url, host=host,
                status=resp.status_code, elapsed_ms=elapsed_ms,
                error=f"HTTP {resp.status_code}",
                headers=dict(resp.headers),
            ))
            tripped = breaker.record_failure(target.url)
            if tripped:
                emit(WalkEvent(kind=EventKind.host_break, url=target.url,
                               host=host, error=f"host {host!r} broken"))
            return None, None

        # Success path (includes 304, 2xx, 404-class non-failures).
        breaker.record_success(target.url)
        evt = WalkEvent(
            kind=EventKind.fetched, url=target.url, host=host,
            status=resp.status_code, elapsed_ms=elapsed_ms,
            body=resp.content, headers=dict(resp.headers),
        )
        emit(evt)
        return evt, None

    # Unreachable in practice.
    return None, None


def _parse_retry_after(resp: HttpResponse) -> float | None:
    raw = resp.header("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return float(raw)
    except ValueError:
        # HTTP-date form isn't worth parsing here; callers can read headers.
        return None
