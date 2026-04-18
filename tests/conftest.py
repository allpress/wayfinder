from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from wayfinder.http_client import HttpResponse


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        low = name.lower()
        for k, v in self.headers.items():
            if k.lower() == low:
                return v
        return None


class FakeHttp:
    """Replays a predetermined per-URL response queue."""

    def __init__(self, responses: dict[str, list[FakeResponse] | FakeResponse]) -> None:
        self._queues: dict[str, list[FakeResponse]] = {}
        for url, v in responses.items():
            self._queues[url] = v if isinstance(v, list) else [v]
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    def get(self, url: str, *, headers: dict[str, str] | None = None,
            timeout: float | None = None) -> HttpResponse:
        self.calls.append((url, dict(headers or {}) or None))
        q = self._queues.get(url)
        if not q:
            return HttpResponse(status_code=404, content=b"not set up")
        r = q.pop(0) if len(q) > 1 else q[0]
        return HttpResponse(
            status_code=r.status_code,
            content=r.content,
            headers=dict(r.headers),
        )


@pytest.fixture
def fake_http() -> type[FakeHttp]:
    return FakeHttp


@pytest.fixture
def fake_resp() -> type[FakeResponse]:
    return FakeResponse


@pytest.fixture
def no_sleep() -> Any:
    """Replace time.sleep with a recorder so tests run fast and assert durations."""
    slept: list[float] = []

    def _sleep(seconds: float) -> None:
        slept.append(seconds)

    _sleep.calls = slept  # type: ignore[attr-defined]
    return _sleep
