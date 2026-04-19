"""Executors: pluggable back-ends that actually drive the browser.

* ``LocalExecutor`` — imports Playwright directly. Runs Chromium in a dedicated
  thread (Playwright's sync API is not thread-safe with respect to the
  creating thread, so we marshal calls through that thread). Safe to use
  from any thread of the caller's process.

* ``WardenExecutor`` — a stub alias that imports the RPC client when called.
  Left minimal here; the full RPC glue lives in warden's browser_v2 worker
  and is built on the same primitives.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)


# ---------- abstract interface ----------

class Executor(Protocol):
    """The narrow interface a Session needs from its back-end.

    Every method is synchronous from the caller's perspective. Implementations
    are responsible for any thread marshalling required by their back-end.
    """

    def launch(self, *, headless: bool) -> None: ...
    def shutdown(self) -> None: ...
    def new_context(self, *, storage_state: dict | None,
                    accept_downloads: bool = False) -> Any: ...
    def close_context(self, context: Any) -> None: ...
    def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Run `fn(*args, **kwargs)` on the browser-owning thread and return its result."""
        ...


# ---------- LocalExecutor ----------

_SHUTDOWN = object()


@dataclass
class _Task:
    fn: Callable[..., Any] | object
    args: tuple
    kwargs: dict
    rq: queue.Queue


class LocalExecutor:
    """Own Playwright + Chromium in a dedicated thread. Marshal all calls."""

    def __init__(self, *, browser_type: str = "chromium") -> None:
        self._browser_type = browser_type
        self._q: queue.Queue[_Task] = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="wayfinder-browser")
        self._started = False
        self._pw = None
        self._browser = None
        self._headless = True
        self._start_error: Exception | None = None

    # --- public ---

    def launch(self, *, headless: bool) -> None:
        self._headless = headless
        if not self._started:
            self._thread.start()
            self._started = True
            self._ready.wait(timeout=30)
            if self._start_error is not None:
                raise RuntimeError(f"playwright launch failed: {self._start_error}")

    def shutdown(self) -> None:
        if not self._started:
            return
        t = _Task(_SHUTDOWN, (), {}, queue.Queue())
        self._q.put(t)
        self._thread.join(timeout=10)
        self._started = False

    def new_context(self, *, storage_state: dict | None,
                    accept_downloads: bool = False,
                    downloads_path: Path | None = None) -> Any:
        return self.run(self._new_context, storage_state, accept_downloads, downloads_path)

    def close_context(self, context: Any) -> None:
        try:
            self.run(self._close_context, context)
        except Exception as e:   # noqa: BLE001
            log.warning("close_context error: %s", e)

    def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        if not self._started:
            raise RuntimeError("LocalExecutor.launch() not called")
        rq: queue.Queue = queue.Queue()
        self._q.put(_Task(fn, args, kwargs, rq))
        status, payload = rq.get()
        if status == "err":
            raise payload
        return payload

    # --- owner-thread callables (take self implicitly via closure) ---

    def _new_context(self, storage_state: dict | None,
                     accept_downloads: bool,
                     downloads_path: Path | None) -> Any:
        assert self._browser is not None
        kwargs: dict[str, Any] = {"accept_downloads": bool(accept_downloads)}
        if storage_state is not None:
            kwargs["storage_state"] = storage_state
        ctx = self._browser.new_context(**kwargs)
        if accept_downloads and downloads_path is not None:
            downloads_path.mkdir(parents=True, exist_ok=True)
        return ctx

    def _close_context(self, context: Any) -> None:
        try:
            context.close()
        except Exception as e:   # noqa: BLE001
            log.debug("context.close raised: %s", e)

    # --- internals ---

    def _loop(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            self._start_error = e
            self._ready.set()
            return
        try:
            self._pw = sync_playwright().start()
            launcher = getattr(self._pw, self._browser_type)
            self._browser = launcher.launch(headless=self._headless, channel="chrome")
        except Exception as e:  # noqa: BLE001
            self._start_error = e
            self._ready.set()
            return
        self._ready.set()

        while True:
            task = self._q.get()
            if task.fn is _SHUTDOWN:
                break
            try:
                assert callable(task.fn)
                result = task.fn(*task.args, **task.kwargs)
                task.rq.put(("ok", result))
            except Exception as e:  # noqa: BLE001
                task.rq.put(("err", e))

        try:
            if self._browser is not None:
                self._browser.close()
        except Exception as e:   # noqa: BLE001
            log.debug("browser.close raised: %s", e)
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception as e:   # noqa: BLE001
            log.debug("pw.stop raised: %s", e)


class WardenExecutor:
    """Executor that proxies to warden's ``web.*`` RPC methods.

    This isn't a drop-in replacement for LocalExecutor at the Session level —
    the warden worker *owns* its own Session instance inside the daemon, so
    calling Session verbs against WardenExecutor doesn't make sense. Instead,
    importers who want to drive a remote warden-hosted session should use the
    ``WardenWebClient`` convenience below, which exposes the same verb surface
    but issues RPC calls directly.
    """
    def __init__(self, client: Any) -> None:
        self._client = client

    def launch(self, *, headless: bool) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def new_context(self, *, storage_state: dict | None,
                    accept_downloads: bool = False) -> Any:
        raise NotImplementedError(
            "WardenExecutor cannot be used to run a local Session; "
            "use WardenWebClient instead."
        )

    def close_context(self, context: Any) -> None:
        return None

    def run(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "WardenExecutor does not run code locally; use WardenWebClient."
        )


class WardenWebClient:
    """Thin wrapper that translates verb calls into warden ``web.*`` RPC calls.

    Provides the same method names as Session (``goto``, ``click``, ``fill``,
    ``observe``, etc.) so AI callers can switch between local and warden-hosted
    usage with a one-line constructor change. Results are returned as dicts
    exactly as they arrive over the RPC boundary; callers who want typed
    dataclasses can re-hydrate via ``wayfinder.browser.models.from_dict``.
    """
    def __init__(self, client: Any, *, identity: str,
                 allowed_domains: list[str] | None = None) -> None:
        self._client = client
        self._identity = identity
        self._allowed = list(allowed_domains or [])
        self._sid: str | None = None

    def open(self, *, headless: bool = True, load_storage: bool = True) -> dict:
        kwargs: dict[str, Any] = {
            "context": self._identity.split(":", 1)[0] if ":" in self._identity else self._identity,
            "identity": self._identity,
            "headless": headless,
            "load_storage": load_storage,
        }
        if self._allowed:
            kwargs["allowed_domains"] = self._allowed
        resp = self._client.call("web.open_session", **kwargs)
        self._sid = resp["session_id"]
        return resp

    def close(self) -> dict:
        if self._sid is None:
            return {"closed": True}
        try:
            return self._client.call("web.close_session", session_id=self._sid)
        finally:
            self._sid = None

    def __getattr__(self, method: str) -> Callable[..., Any]:
        # Any unresolved attribute on this object is treated as a web.* verb.
        if method.startswith("_") or self._sid is None:
            raise AttributeError(method)
        sid = self._sid

        def _call(**kwargs: Any) -> Any:
            return self._client.call(f"web.{method}", session_id=sid, **kwargs)

        return _call


__all__ = ["Executor", "LocalExecutor", "WardenExecutor", "WardenWebClient"]
