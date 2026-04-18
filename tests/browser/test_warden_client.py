"""WardenWebClient — verbs translate into web.* RPC calls."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wayfinder.browser import WardenWebClient


@dataclass
class _FakeClient:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    responses: dict[str, Any] = field(default_factory=dict)

    def call(self, method: str, **params: Any) -> Any:
        self.calls.append((method, dict(params)))
        if method in self.responses:
            return self.responses[method]
        return {"ok": True}


def test_open_issues_web_open_session():
    fc = _FakeClient(responses={"web.open_session": {"session_id": "S1"}})
    c = WardenWebClient(fc, identity="ctx:me", allowed_domains=["x.test"])
    r = c.open()
    assert r == {"session_id": "S1"}
    method, params = fc.calls[0]
    assert method == "web.open_session"
    assert params["identity"] == "ctx:me"
    assert params["context"] == "ctx"
    assert params["allowed_domains"] == ["x.test"]


def test_verbs_add_session_id_and_namespace():
    fc = _FakeClient(responses={"web.open_session": {"session_id": "SX"}})
    c = WardenWebClient(fc, identity="ctx:x")
    c.open()
    c.goto(url="https://x.test")
    c.click(handle="h1")
    methods = [m for m, _ in fc.calls[1:]]
    assert methods == ["web.goto", "web.click"]
    for m, p in fc.calls[1:]:
        assert p["session_id"] == "SX"


def test_close_clears_session():
    fc = _FakeClient(responses={"web.open_session": {"session_id": "SX"}})
    c = WardenWebClient(fc, identity="i")
    c.open()
    c.close()
    assert fc.calls[-1][0] == "web.close_session"
    # After close, verbs are unreachable.
    import pytest
    with pytest.raises(AttributeError):
        c.goto
