"""Minimal HTTP client surface. Tests pass in fakes; prod gets httpx."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(slots=True)
class HttpResponse:
    status_code: int
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """Case-insensitive header lookup."""
        lower = name.lower()
        for k, v in self.headers.items():
            if k.lower() == lower:
                return v
        return None


class HttpClient(Protocol):
    """What walker.py requires. `get` returns an HttpResponse-like object.

    The only attributes walker touches: status_code, content, headers.
    """

    def get(self, url: str, *, headers: dict[str, str] | None = None,
            timeout: float | None = None) -> HttpResponse: ...


class HttpxAdapter:
    """Real httpx client wrapped to match HttpClient. Redirects followed."""

    def __init__(self, *, follow_redirects: bool = True,
                 default_timeout: float = 30.0,
                 user_agent: str = "wayfinder/0.1") -> None:
        import httpx
        self._client = httpx.Client(
            follow_redirects=follow_redirects,
            timeout=default_timeout,
            headers={"User-Agent": user_agent},
        )

    def get(self, url: str, *, headers: dict[str, str] | None = None,
            timeout: float | None = None) -> HttpResponse:
        resp = self._client.get(url, headers=headers, timeout=timeout)
        return HttpResponse(
            status_code=resp.status_code,
            content=resp.content,
            headers=dict(resp.headers),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpxAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
