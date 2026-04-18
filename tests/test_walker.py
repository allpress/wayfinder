from __future__ import annotations

from wayfinder.events import EventKind
from wayfinder.policy import FetchPolicy
from wayfinder.walker import WalkTarget, walk


def _ok(body: bytes = b"ok") -> "FakeResponse":
    from tests.conftest import FakeResponse
    return FakeResponse(status_code=200, content=body)


def _status(code: int, *, headers=None, body=b"") -> "FakeResponse":
    from tests.conftest import FakeResponse
    return FakeResponse(status_code=code, content=body, headers=headers or {})


# ---------- successful walks ----------

def test_walk_returns_fetched_events_for_success(fake_http, no_sleep) -> None:
    http = fake_http({
        "https://a/1": _ok(b"one"),
        "https://a/2": _ok(b"two"),
    })
    targets = [WalkTarget(url="https://a/1"), WalkTarget(url="https://a/2")]
    report = walk(targets, FetchPolicy(), http=http, sleep=no_sleep)

    assert not report.halted
    assert len(report.successes) == 2
    assert report.successes["https://a/1"].body == b"one"
    kinds = [e.kind for e in report.events]
    assert kinds.count(EventKind.fetched) == 2


def test_on_event_callback_sees_every_event(fake_http, no_sleep) -> None:
    http = fake_http({"https://a/1": _ok()})
    seen = []
    walk([WalkTarget(url="https://a/1")], FetchPolicy(),
         http=http, sleep=no_sleep, on_event=seen.append)
    assert any(e.kind == EventKind.fetched for e in seen)


def test_custom_headers_are_passed_through(fake_http, no_sleep) -> None:
    http = fake_http({"https://a/1": _ok()})
    walk([WalkTarget(url="https://a/1",
                      headers={"If-None-Match": 'W/"v1"'})],
         FetchPolicy(), http=http, sleep=no_sleep)
    assert http.calls[0][1] == {"If-None-Match": 'W/"v1"'}


# ---------- halts ----------

def test_429_halts_immediately_with_retry_after(fake_http, no_sleep) -> None:
    http = fake_http({
        "https://huggingface.co/blog/feed.xml":
            _status(429, headers={"Retry-After": "60"}),
        "https://other/x": _ok(),   # MUST NOT be fetched (halt is immediate)
    })
    report = walk(
        [
            WalkTarget(url="https://huggingface.co/blog/feed.xml"),
            WalkTarget(url="https://other/x"),
        ],
        FetchPolicy(),
        http=http, sleep=no_sleep,
    )
    assert report.halted is True
    assert "huggingface.co" in report.halt_reason
    assert "Retry-After=60" in report.halt_reason
    # Second URL was NOT attempted.
    assert [c[0] for c in http.calls] == ["https://huggingface.co/blog/feed.xml"]
    # The halt event carried retry_after_s.
    halt = next(e for e in report.events if e.kind == EventKind.halted)
    assert halt.retry_after_s == 60.0


def test_503_in_halt_set_halts(fake_http, no_sleep) -> None:
    http = fake_http({"https://a/1": _status(503)})
    report = walk([WalkTarget(url="https://a/1")], FetchPolicy(),
                   http=http, sleep=no_sleep)
    assert report.halted is True


def test_host_circuit_break_without_global_halt(fake_http, no_sleep) -> None:
    http = fake_http({
        "https://bad/1": _status(500),
        "https://bad/2": _status(500),
        "https://bad/3": _status(500),
        "https://bad/4": _status(500),   # skipped after break
        "https://good/1": _ok(),
    })
    report = walk(
        [
            WalkTarget(url="https://bad/1"),
            WalkTarget(url="https://bad/2"),
            WalkTarget(url="https://bad/3"),
            WalkTarget(url="https://bad/4"),
            WalkTarget(url="https://good/1"),
        ],
        FetchPolicy(max_retries=0,
                    halt_after_host_consecutive_failures=3,
                    halt_after_global_failures=100),
        http=http, sleep=no_sleep,
    )
    assert not report.halted
    assert "bad" in report.broken_hosts
    # bad/4 was skipped (host broken)
    skipped = [e for e in report.events if e.kind == EventKind.skipped]
    assert any("bad/4" in e.url for e in skipped)
    # good/1 still succeeded
    assert "https://good/1" in report.successes


def test_global_failure_cap_halts(fake_http, no_sleep) -> None:
    # Different hosts so the host breaker doesn't catch it first.
    http = fake_http({
        f"https://host{i}/x": _status(500) for i in range(5)
    })
    report = walk(
        [WalkTarget(url=f"https://host{i}/x") for i in range(5)],
        FetchPolicy(max_retries=0,
                    halt_after_host_consecutive_failures=99,
                    halt_after_global_failures=3),
        http=http, sleep=no_sleep,
    )
    assert report.halted is True
    assert "global failure cap" in report.halt_reason


# ---------- retries & backoff ----------

def test_retry_succeeds_on_second_attempt(fake_http, no_sleep) -> None:
    from tests.conftest import FakeResponse
    http = fake_http({
        "https://flaky/1": [
            FakeResponse(status_code=500),
            FakeResponse(status_code=200, content=b"ok"),
        ],
    })
    report = walk(
        [WalkTarget(url="https://flaky/1")],
        FetchPolicy(max_retries=1, backoff_base_s=0.1),
        http=http, sleep=no_sleep,
    )
    assert not report.halted
    assert "https://flaky/1" in report.successes
    # One retry event was emitted before the success.
    kinds = [e.kind for e in report.events]
    assert EventKind.retry in kinds
    assert EventKind.fetched in kinds
    # sleep was called exactly once, with backoff for attempt 0.
    assert no_sleep.calls == [0.1]  # type: ignore[attr-defined]


def test_network_exception_retried_then_recorded_as_error(no_sleep) -> None:
    class Broken:
        calls = 0
        def get(self, url, *, headers=None, timeout=None):
            Broken.calls += 1
            raise ConnectionError("nope")

    report = walk(
        [WalkTarget(url="https://dead/1")],
        FetchPolicy(max_retries=2, backoff_base_s=0.1,
                    halt_after_host_consecutive_failures=99,
                    halt_after_global_failures=99),
        http=Broken(), sleep=no_sleep,
    )
    assert not report.halted
    assert Broken.calls == 3   # initial + 2 retries
    errors = [e for e in report.events if e.kind == EventKind.error]
    assert errors and "ConnectionError" in (errors[-1].error or "")


# ---------- non-failure status passthrough ----------

def test_304_is_treated_as_success(fake_http, no_sleep) -> None:
    """If-None-Match → 304 is normal caching behavior; the walker reports it
    as fetched so the caller can branch on status_code."""
    http = fake_http({"https://a/1": _status(304)})
    report = walk([WalkTarget(url="https://a/1")],
                   FetchPolicy(), http=http, sleep=no_sleep)
    assert not report.halted
    assert "https://a/1" in report.successes
    assert report.successes["https://a/1"].status == 304


def test_404_counts_as_success_path(fake_http, no_sleep) -> None:
    """Default policy excludes 404 from failure_statuses."""
    http = fake_http({"https://a/1": _status(404)})
    report = walk([WalkTarget(url="https://a/1")],
                   FetchPolicy(), http=http, sleep=no_sleep)
    assert not report.halted
    # 404 isn't a failure by default; it's a 'fetched' with status 404.
    assert "https://a/1" in report.successes
    assert report.successes["https://a/1"].status == 404


# ---------- telemetry shape ----------

def test_report_summary_and_pretty(fake_http, no_sleep) -> None:
    http = fake_http({
        "https://a/1": _ok(),
        "https://a/2": _status(500),
    })
    report = walk(
        [WalkTarget(url="https://a/1"), WalkTarget(url="https://a/2")],
        FetchPolicy(max_retries=0,
                    halt_after_host_consecutive_failures=10,
                    halt_after_global_failures=10),
        http=http, sleep=no_sleep,
    )
    summary = report.summary()
    assert summary["successes"] == 1
    assert summary["failures"] == 1
    # Pretty rendering includes status code.
    assert "500" in "\n".join(e.pretty() for e in report.events)
