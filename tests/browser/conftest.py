"""Test fixtures for the wayfinder browser layer.

* A local http.server that serves a small set of curated HTML fixtures.
* A session-scoped Playwright browser for tests that need a real Chromium.
* A per-test LocalExecutor wrapper.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import socketserver
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from wayfinder.browser import IdentityStore, LocalExecutor, Session

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# -------- fixture HTTP server --------

@dataclass
class ServerRecord:
    port: int
    base: str
    hits: list[str] = field(default_factory=list)


class _Handler(http.server.SimpleHTTPRequestHandler):
    # Overridden below to record hits into a shared list.
    def log_message(self, *args, **kwargs):  # silence
        return


def _make_handler(record: ServerRecord):
    class H(_Handler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(FIXTURES_DIR), **kw)

        def do_GET(self):  # noqa: N802
            record.hits.append(self.path)
            # Virtual endpoints used by tests.
            if self.path.startswith("/slow"):
                time.sleep(0.3)
                return self._text("slow-ok")
            if self.path.startswith("/redirect-to-login"):
                self.send_response(302)
                self.send_header("Location", "/login.html")
                self.end_headers()
                return
            if self.path.startswith("/oauth/callback"):
                # Fake provider redirects back here with tokens in the fragment.
                self._html(
                    "<!doctype html><html><head><title>Back</title></head>"
                    "<body><h1 id=done>Signed in</h1></body></html>"
                )
                return
            super().do_GET()

        def do_POST(self):  # noqa: N802
            record.hits.append("POST " + self.path)
            if self.path.startswith("/echo"):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                self._html(
                    "<!doctype html><html><head><title>Echo</title></head>"
                    f"<body><main><h1 id=heading>Submitted</h1>"
                    f"<pre id=payload>{urllib.parse.quote(json.dumps(parsed))}</pre>"
                    "</main></body></html>"
                )
                return
            self.send_response(404)
            self.end_headers()

        def _html(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _text(self, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return H


@pytest.fixture(scope="session")
def test_server():
    record = ServerRecord(port=0, base="")
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _make_handler(record))
    port = httpd.server_address[1]
    record.port = port
    record.base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True, name="wf-fixture-http")
    thread.start()
    try:
        yield record
    finally:
        httpd.shutdown()


# -------- Playwright executor (session-scoped) --------

@pytest.fixture(scope="session")
def executor():
    ex = LocalExecutor()
    ex.launch(headless=True)
    try:
        yield ex
    finally:
        ex.shutdown()


# -------- Session factory (per test) --------

@pytest.fixture
def session_factory(executor, tmp_path, test_server):
    sessions: list[Session] = []

    def _mk(*, identity: str = "test",
            allowed_domains: list[str] | None = None,
            store: IdentityStore | None = None,
            accept_downloads: bool = False) -> Session:
        if allowed_domains is None:
            allowed_domains = ["127.0.0.1"]
        s = Session(executor=executor, store=store)
        res = s.open(identity=identity, allowed_domains=allowed_domains,
                     headless=True, load_storage=store is not None,
                     accept_downloads=accept_downloads)
        assert res.ok, f"open failed: {res.error} {res.error_detail}"
        sessions.append(s)
        return s

    yield _mk

    for s in sessions:
        with contextlib.suppress(Exception):
            s.close()


@pytest.fixture
def identity_store(tmp_path):
    key = b"0" * 32
    return IdentityStore(root=tmp_path / "identities", key=key)
