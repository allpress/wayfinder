"""HttpWalkerWayfinder wraps walk() — spot check the translation layer."""
from __future__ import annotations

import base64

from wayfinder import HttpWalkerWayfinder
from wayfinder.http_client import HttpResponse


class _ReplayClient:
    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self._r = responses
        self.calls: list[str] = []

    def get(self, url: str, *, headers=None, timeout=None) -> HttpResponse:
        self.calls.append(url)
        return self._r.get(url, HttpResponse(status_code=404))

    def close(self) -> None:
        pass


def test_spec_shape() -> None:
    wf = HttpWalkerWayfinder()
    assert wf.spec.type_name == "http_walker"
    assert "targets" in wf.spec.required_inputs
    assert wf.spec.secret_refs_allowed is False


def test_run_maps_walk_output_to_report(monkeypatch, no_sleep) -> None:
    # Force a fake http adapter by patching the module-local import.
    import wayfinder.walkers.http as mod
    replay = _ReplayClient({
        "https://ex/1": HttpResponse(status_code=200, content=b"one",
                                      headers={"content-type": "text/plain"}),
        "https://ex/2": HttpResponse(status_code=200, content=b"two",
                                      headers={"content-type": "text/plain"}),
    })
    monkeypatch.setattr(mod, "HttpxAdapter", lambda **kw: replay)

    events: list = []
    wf = HttpWalkerWayfinder()
    report = wf.run(
        {"targets": [
            {"url": "https://ex/1"},
            {"url": "https://ex/2"},
         ]},
        secret_resolver=None,
        emit=events.append,
    )

    assert report.status == "completed"
    assert not report.output["halted"]
    assert len(report.output["successes"]) == 2
    one = report.output["successes"]["https://ex/1"]
    assert one["status"] == 200
    assert base64.b64decode(one["body_b64"]) == b"one"
    # Events flowed through (WayfinderEvent instances).
    assert events, "expected at least one event"


def test_run_surfaces_halt_on_429(monkeypatch, no_sleep) -> None:
    import wayfinder.walkers.http as mod
    replay = _ReplayClient({
        "https://slow/1": HttpResponse(
            status_code=429, headers={"Retry-After": "30"}, content=b"",
        ),
    })
    monkeypatch.setattr(mod, "HttpxAdapter", lambda **kw: replay)

    wf = HttpWalkerWayfinder()
    report = wf.run(
        {"targets": [{"url": "https://slow/1"}]},
        secret_resolver=None,
        emit=lambda _: None,
    )
    assert report.status == "terminated"
    assert report.output["halted"] is True
    assert "slow" in (report.error or "")


def test_policy_override_disables_halt_on_status(monkeypatch, no_sleep) -> None:
    import wayfinder.walkers.http as mod
    replay = _ReplayClient({
        "https://x/1": HttpResponse(status_code=429,
                                     headers={"Retry-After": "5"}, content=b""),
    })
    monkeypatch.setattr(mod, "HttpxAdapter", lambda **kw: replay)

    wf = HttpWalkerWayfinder()
    report = wf.run(
        {
            "targets": [{"url": "https://x/1"}],
            "policy": {
                "halt_on_status": [],                    # disable halt
                "max_retries": 0,
                "halt_after_host_consecutive_failures": 99,
                "halt_after_global_failures": 99,
            },
        },
        secret_resolver=None,
        emit=lambda _: None,
    )
    assert report.status == "completed"
    assert report.output["halted"] is False


def test_run_rejects_empty_targets() -> None:
    wf = HttpWalkerWayfinder()
    report = wf.run({"targets": []}, secret_resolver=None, emit=lambda _: None)
    assert report.status == "failed"
    assert "non-empty" in (report.error or "")
