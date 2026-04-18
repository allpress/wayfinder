from __future__ import annotations

from wayfinder.policy import FetchPolicy


def test_default_bias_toward_halting() -> None:
    p = FetchPolicy()
    assert 429 in p.halt_on_status
    assert 503 in p.halt_on_status
    assert p.max_retries == 1
    assert p.halt_after_global_failures == 10


def test_strict_tightens_everything() -> None:
    p = FetchPolicy.strict()
    assert p.max_retries == 0
    assert p.halt_after_host_consecutive_failures == 2
    assert 502 in p.halt_on_status
    assert 504 in p.halt_on_status


def test_lenient_relaxes_counts() -> None:
    p = FetchPolicy.lenient()
    assert p.max_retries == 3
    assert p.halt_after_global_failures == 20


def test_backoff_exponential_with_max() -> None:
    p = FetchPolicy(backoff_base_s=1.0, backoff_max_s=8.0)
    assert p.backoff_for(0) == 1.0
    assert p.backoff_for(1) == 2.0
    assert p.backoff_for(2) == 4.0
    assert p.backoff_for(3) == 8.0
    assert p.backoff_for(4) == 8.0   # clamped to max


def test_404_not_a_failure_by_default() -> None:
    """A 404 is common on stale feeds; shouldn't count toward the host-break counter."""
    p = FetchPolicy()
    assert 404 not in p.failure_statuses
    assert 500 in p.failure_statuses
    assert 429 in p.failure_statuses
