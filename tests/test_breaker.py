from __future__ import annotations

from wayfinder.breaker import HostBreaker


def test_records_failures_and_trips_at_threshold() -> None:
    b = HostBreaker(threshold=3)
    assert b.record_failure("https://a.example/x") is False  # 1
    assert b.record_failure("https://a.example/y") is False  # 2
    assert b.record_failure("https://a.example/z") is True   # 3 → tripped
    assert b.is_broken("https://a.example/anything") is True


def test_success_resets_streak() -> None:
    b = HostBreaker(threshold=3)
    b.record_failure("https://a.example/1")
    b.record_failure("https://a.example/2")
    b.record_success("https://a.example/3")
    # Two more failures don't trip (streak was reset).
    assert b.record_failure("https://a.example/4") is False
    assert b.record_failure("https://a.example/5") is False


def test_hosts_are_independent() -> None:
    b = HostBreaker(threshold=2)
    b.record_failure("https://a.example/1")
    b.record_failure("https://b.example/1")
    assert b.is_broken("https://a.example/2") is False
    assert b.is_broken("https://b.example/2") is False
    b.record_failure("https://a.example/2")   # trips a
    assert b.is_broken("https://a.example/3") is True
    assert b.is_broken("https://b.example/3") is False


def test_subdomain_treated_as_distinct_host() -> None:
    """Conservative: different subdomains don't share a break state."""
    b = HostBreaker(threshold=1)
    b.record_failure("https://api.example.com/x")
    assert b.is_broken("https://api.example.com/y") is True
    assert b.is_broken("https://www.example.com/y") is False


def test_urls_without_host_handled_safely() -> None:
    b = HostBreaker(threshold=1)
    # urls with empty host still get a (empty) host; just shouldn't crash.
    tripped = b.record_failure("not-a-url")
    assert tripped is True
    assert b.is_broken("not-a-url")
