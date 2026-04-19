"""The Session — the AI-facing browser surface.

All verbs return structured results (never raise for domain errors). Action
results carry a ``diff`` describing what changed since the last observation,
so a caller never needs to re-parse a full page to learn the effect of an
action.
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from wayfinder.browser.credentials import is_credential_shaped
from wayfinder.browser.diff import diff as compute_diff
from wayfinder.browser.errors import ErrCode, classify_exception
from wayfinder.browser.executor import Executor, LocalExecutor
from wayfinder.browser.identity import IdentityError, IdentityStore
from wayfinder.browser.models import (
    ActResult,
    ExtractResult,
    Interactable,
    NetEvent,
    Observation,
    OpenResult,
    SaveResult,
    ScreenshotResult,
)
from wayfinder.browser.observer import (
    load_observer_script,
    make_net_event,
    parse_snapshot,
    resolve_handle,
)

log = logging.getLogger(__name__)


# ---------- configuration ----------

_DEFAULT_ACTION_TIMEOUT_S = 10
_DEFAULT_NAV_TIMEOUT_S = 30
_DEFAULT_WAIT_FOR_TIMEOUT_S = 10
_CONSOLE_TAIL_N = 50
_NETWORK_TAIL_N = 50
_EXTRACT_DEFAULT_MAX_CHARS = 2000


# ---------- session state ----------

@dataclass
class _SessionState:
    session_id: str
    identity: str
    allowed_domains: list[str]
    headless: bool
    loaded_storage: bool
    context: Any = None                       # playwright browser context
    page: Any = None                          # playwright page
    console_tail: list[str] = field(default_factory=list)
    network_tail: list[NetEvent] = field(default_factory=list)
    last_observation: Observation | None = None


# ---------- the Session ----------

class Session:
    """A single browser tab scoped to one identity.

    Lifecycle: ``open`` → verbs → ``close``. Not re-entrant; use one Session
    per logical flow. All verbs are safe to call after a failed call — the
    last observation and state are preserved.
    """

    def __init__(self, executor: Executor | None = None, *,
                 store: IdentityStore | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._executor: Executor = executor or LocalExecutor()
        self._store = store
        self._clock = clock
        self._state: _SessionState | None = None
        self._observer_js = load_observer_script()

    # --- lifecycle ---

    def open(self, *, identity: str, allowed_domains: list[str],
             headless: bool = True, load_storage: bool = True,
             accept_downloads: bool = False) -> OpenResult:
        if self._state is not None:
            return OpenResult(ok=False, error=ErrCode.bad_argument,
                              error_detail="session already open")
        if not identity:
            return OpenResult(ok=False, error=ErrCode.bad_argument,
                              error_detail="identity must be non-empty")
        if not allowed_domains:
            return OpenResult(ok=False, error=ErrCode.bad_argument,
                              error_detail="allowed_domains must be non-empty")

        normalised = [d.lower().lstrip(".") for d in allowed_domains]

        # Launch executor lazily.
        try:
            self._executor.launch(headless=headless)
        except Exception as e:   # noqa: BLE001
            return OpenResult(ok=False, error=ErrCode.playwright_error,
                              error_detail=f"launch failed: {type(e).__name__}: {e}")

        storage_state: dict | None = None
        loaded = False
        if load_storage and self._store is not None and self._store.has(identity):
            try:
                storage_state = self._store.load(identity)
                loaded = True
            except IdentityError as e:
                return OpenResult(ok=False, error=e.code, error_detail=e.detail)

        try:
            context = self._executor.new_context(
                storage_state=storage_state,
                accept_downloads=accept_downloads,
            )
            page = self._executor.run(_new_page, context, self._observer_js)
        except Exception as e:   # noqa: BLE001
            return OpenResult(ok=False, error=ErrCode.playwright_error,
                              error_detail=f"new_context failed: {type(e).__name__}: {e}")

        session_id = _mint_session_id()
        state = _SessionState(
            session_id=session_id,
            identity=identity,
            allowed_domains=normalised,
            headless=headless,
            loaded_storage=loaded,
            context=context,
            page=page,
        )

        # Hook console + network into the page.
        self._executor.run(
            _install_page_hooks, page, state, normalised,
            _CONSOLE_TAIL_N, _NETWORK_TAIL_N,
        )

        self._state = state
        return OpenResult(
            ok=True, session_id=session_id, identity=identity,
            allowed_domains=list(normalised), headless=headless,
            loaded_storage=loaded,
        )

    def close(self) -> None:
        state = self._state
        if state is None:
            return
        try:
            if state.context is not None:
                self._executor.close_context(state.context)
        finally:
            self._state = None

    def debug_dump(self) -> dict:
        """Return a structured snapshot of the current session state.

        Safe to call regardless of session state. Useful for tests and for
        any caller that wants to introspect what wayfinder is actually
        seeing — pages in the context, the active page URL, every handle
        in the last observation (role / name / label / handle id), the
        tail of console and network events.

        Nothing in the return value is secret-shaped: handles are ids,
        labels are scrubbed by the observer before they reach the session,
        and URLs are already in-scope (we never snapshot off-scope pages).
        """
        state = self._state
        if state is None:
            return {"open": False}

        pages_info: list[dict] = []
        try:
            pages = list(state.context.pages) if state.context is not None else []
        except Exception as e:   # noqa: BLE001
            pages = []
            pages_info.append({"context_error": repr(e)})
        for i, p in enumerate(pages):
            entry: dict = {"index": i, "active": p is state.page}
            try:
                entry["url"] = self._executor.run(lambda pg: pg.url, p)
            except Exception as e:   # noqa: BLE001
                entry["url_error"] = repr(e)
            try:
                entry["title"] = self._executor.run(lambda pg: pg.title(), p)
            except Exception as e:   # noqa: BLE001
                entry["title_error"] = repr(e)
            pages_info.append(entry)

        obs = state.last_observation
        handles_info: list[dict] = []
        if obs is not None:
            for h in obs.handles:
                handles_info.append({
                    "handle": h.handle,
                    "role": h.role,
                    "name": h.name,
                    "label": h.label,
                })

        return {
            "open": True,
            "session_id": state.session_id,
            "identity": state.identity,
            "allowed_domains": list(state.allowed_domains),
            "headless": state.headless,
            "loaded_storage": state.loaded_storage,
            "active_url": (obs.url if obs is not None
                           else _current_url(self._executor, state)),
            "pages": pages_info,
            "handle_count": len(handles_info),
            "handles": handles_info,
            "console_tail": list(state.console_tail),
            "network_tail_count": len(state.network_tail),
        }

    def save_storage(self) -> SaveResult:
        state = self._require_state()
        if state is None:
            return SaveResult(ok=False, error=ErrCode.session_unknown,
                              error_detail="session not open")
        if self._store is None:
            return SaveResult(ok=False, error=ErrCode.bad_argument,
                              error_detail="no IdentityStore configured")
        try:
            storage = self._executor.run(_dump_storage, state.context)
        except Exception as e:   # noqa: BLE001
            return SaveResult(ok=False, error=ErrCode.playwright_error,
                              error_detail=f"storage_state failed: {type(e).__name__}: {e}")
        try:
            import json
            info = self._store.save(state.identity, storage,
                                    allowed_domains=list(state.allowed_domains))
            size = len(json.dumps(storage))
        except IdentityError as e:
            return SaveResult(ok=False, error=e.code, error_detail=e.detail)
        return SaveResult(ok=True, identity=info.name, bytes_written=size)

    # --- navigation ---

    def goto(self, url: str, *, wait: str = "domcontentloaded",
             timeout_s: int = _DEFAULT_NAV_TIMEOUT_S) -> ActResult:
        state = self._require_state()
        if state is None:
            return ActResult(ok=False, error=ErrCode.session_unknown)

        if not _host_in_scope(_host_of(url), state.allowed_domains):
            return ActResult(ok=False, error=ErrCode.scope_violation,
                             error_detail=f"url {url!r} out of scope")

        url_before = _current_url(self._executor, state)
        try:
            self._executor.run(_do_goto, state.page, url, wait, timeout_s)
        except Exception as e:   # noqa: BLE001
            return ActResult(
                ok=False,
                error=classify_exception(e),
                error_detail=f"{type(e).__name__}: {e}",
                url_before=url_before,
                url_after=_current_url(self._executor, state),
            )
        return self._finalise_act(state, url_before)

    def back(self) -> ActResult:
        return self._nav_verb("go_back")

    def reload(self) -> ActResult:
        return self._nav_verb("reload")

    def _nav_verb(self, verb: str) -> ActResult:
        state = self._require_state()
        if state is None:
            return ActResult(ok=False, error=ErrCode.session_unknown)
        url_before = _current_url(self._executor, state)
        try:
            self._executor.run(_do_nav_verb, state.page, verb)
        except Exception as e:   # noqa: BLE001
            return ActResult(ok=False, error=classify_exception(e),
                             error_detail=f"{type(e).__name__}: {e}",
                             url_before=url_before,
                             url_after=_current_url(self._executor, state))
        return self._finalise_act(state, url_before)

    # --- observation ---

    def observe(self, *, viewport_only: bool = True,
                include_screenshot: bool = False) -> Observation:
        state = self._require_state()
        if state is None:
            return Observation(url="", title="")
        try:
            raw = self._executor.run(_do_snapshot, state.page, viewport_only,
                                      self._observer_js)
        except Exception as e:   # noqa: BLE001
            log.warning("snapshot failed: %s", e)
            raw = {"url": "", "title": "", "handles": [],
                   "landmarks": [], "text_blocks": [],
                   "fingerprint": "", "truncated": False}

        b64: str | None = None
        if include_screenshot:
            try:
                b64 = self._screenshot_b64(state)
            except Exception as e:   # noqa: BLE001
                log.info("screenshot failed: %s", e)
                b64 = None

        obs = parse_snapshot(
            raw,
            console_tail=state.console_tail,
            network_tail=state.network_tail,
            screenshot_b64=b64,
        )
        state.last_observation = obs
        return obs

    # --- interaction ---

    def click(self, handle: str, *, modifiers: tuple[str, ...] = (),
              timeout_s: int = _DEFAULT_ACTION_TIMEOUT_S) -> ActResult:
        return self._handle_act(
            handle,
            lambda locator: _do_click(locator, modifiers, timeout_s),
        )

    def fill(self, handle: str, *, value_ref: str | None = None,
             value_literal: str | None = None,
             secret_resolver: Callable[[str], str] | None = None,
             timeout_s: int = _DEFAULT_ACTION_TIMEOUT_S) -> ActResult:
        if (value_ref is None) == (value_literal is None):
            return ActResult(ok=False, error=ErrCode.bad_argument,
                             error_detail="pass exactly one of value_ref / value_literal")

        state = self._require_state()
        if state is None:
            return ActResult(ok=False, error=ErrCode.session_unknown)

        # Need the snapshot to reason about credential shape BEFORE filling.
        snapshot = state.last_observation or self.observe()
        el = snapshot.by_handle(handle)
        if el is None:
            return ActResult(ok=False, error=ErrCode.handle_not_found,
                             error_detail=f"handle {handle!r} not in last observation")

        if value_literal is not None and is_credential_shaped(el):
            return ActResult(ok=False, error=ErrCode.secret_denied,
                             error_detail="literal refused for credential-shaped field")

        if value_ref is not None:
            if secret_resolver is None:
                return ActResult(ok=False, error=ErrCode.bad_argument,
                                 error_detail="value_ref provided but no secret_resolver")
            try:
                value = secret_resolver(value_ref)
            except LookupError as e:
                return ActResult(ok=False, error=ErrCode.secret_unknown,
                                 error_detail=str(e))
            except PermissionError as e:
                return ActResult(ok=False, error=ErrCode.secret_denied,
                                 error_detail=str(e))
        else:
            assert value_literal is not None
            value = value_literal

        return self._handle_act(handle, lambda locator: _do_fill(locator, value, timeout_s),
                                snapshot=snapshot)

    def select(self, handle: str, *, option: str,
               timeout_s: int = _DEFAULT_ACTION_TIMEOUT_S) -> ActResult:
        return self._handle_act(handle,
                                 lambda loc: _do_select(loc, option, timeout_s))

    def check(self, handle: str, *, state: bool = True,
              timeout_s: int = _DEFAULT_ACTION_TIMEOUT_S) -> ActResult:
        return self._handle_act(handle,
                                 lambda loc: _do_check(loc, state, timeout_s))

    def press(self, handle: str | None, *, key: str,
              timeout_s: int = _DEFAULT_ACTION_TIMEOUT_S) -> ActResult:
        sstate = self._require_state()
        if sstate is None:
            return ActResult(ok=False, error=ErrCode.session_unknown)
        url_before = _current_url(self._executor, sstate)
        try:
            if handle is None:
                self._executor.run(_do_page_press, sstate.page, key, timeout_s)
            else:
                snapshot = sstate.last_observation or self.observe()
                loc, err, detail = self._resolve(snapshot, handle)
                if err is not None:
                    return ActResult(ok=False, error=err, error_detail=detail,
                                     url_before=url_before, url_after=url_before)
                self._executor.run(_do_locator_press, loc, key, timeout_s)
        except Exception as e:   # noqa: BLE001
            return ActResult(ok=False, error=classify_exception(e),
                             error_detail=f"{type(e).__name__}: {e}",
                             url_before=url_before,
                             url_after=_current_url(self._executor, sstate))
        return self._finalise_act(sstate, url_before)

    def submit(self, form_handle: str,
               timeout_s: int = _DEFAULT_ACTION_TIMEOUT_S) -> ActResult:
        return self._handle_act(form_handle,
                                 lambda loc: _do_submit(loc, timeout_s))

    # --- waits ---

    def wait_for(self, *, url_contains: str | None = None,
                 text_contains: str | None = None,
                 handle_role: str | None = None,
                 handle_name: str | None = None,
                 timeout_s: int = _DEFAULT_WAIT_FOR_TIMEOUT_S,
                 poll_ms: int = 150) -> ActResult:
        state = self._require_state()
        if state is None:
            return ActResult(ok=False, error=ErrCode.session_unknown)

        if url_contains is None and text_contains is None and handle_role is None:
            return ActResult(ok=False, error=ErrCode.bad_argument,
                             error_detail="wait_for needs at least one condition")

        url_before = _current_url(self._executor, state)
        deadline = self._clock() + max(0.5, float(timeout_s))
        interval = max(0.05, poll_ms / 1000.0)

        while self._clock() < deadline:
            try:
                url = self._executor.run(lambda p: p.url, state.page)
            except Exception:
                url = url_before
            if url_contains and url_contains in url:
                return self._finalise_act(state, url_before)
            if text_contains is not None:
                try:
                    visible = self._executor.run(_body_text, state.page)
                except Exception:
                    visible = ""
                if text_contains in visible:
                    return self._finalise_act(state, url_before)
            if handle_role is not None:
                try:
                    raw = self._executor.run(_do_snapshot, state.page, True,
                                              self._observer_js)
                except Exception:
                    raw = {"handles": []}
                for h in raw.get("handles", []):
                    if h.get("role") != handle_role:
                        continue
                    if handle_name is not None and handle_name not in (h.get("name") or ""):
                        continue
                    return self._finalise_act(state, url_before)
            time.sleep(interval)

        return ActResult(ok=False, error=ErrCode.timeout,
                         error_detail="wait_for deadline elapsed",
                         url_before=url_before,
                         url_after=_current_url(self._executor, state))

    # --- extraction ---

    def extract_text(self, handle: str, *,
                     max_chars: int = _EXTRACT_DEFAULT_MAX_CHARS) -> ExtractResult:
        state = self._require_state()
        if state is None:
            return ExtractResult(ok=False, error=ErrCode.session_unknown)
        snapshot = state.last_observation or self.observe()
        loc, err, detail = self._resolve(snapshot, handle)
        if err is not None:
            return ExtractResult(ok=False, error=err, error_detail=detail)
        try:
            text = self._executor.run(_do_extract_text, loc)
        except Exception as e:   # noqa: BLE001
            return ExtractResult(ok=False, error=classify_exception(e),
                                 error_detail=f"{type(e).__name__}: {e}")
        truncated = len(text) > max_chars
        return ExtractResult(ok=True, text=text[:max_chars], truncated=truncated)

    def extract_attribute(self, handle: str, name: str) -> ExtractResult:
        state = self._require_state()
        if state is None:
            return ExtractResult(ok=False, error=ErrCode.session_unknown)
        snapshot = state.last_observation or self.observe()
        loc, err, detail = self._resolve(snapshot, handle)
        if err is not None:
            return ExtractResult(ok=False, error=err, error_detail=detail)
        try:
            val = self._executor.run(_do_extract_attr, loc, name)
        except Exception as e:   # noqa: BLE001
            return ExtractResult(ok=False, error=classify_exception(e),
                                 error_detail=f"{type(e).__name__}: {e}")
        return ExtractResult(ok=True, text=val or "", truncated=False)

    def screenshot(self, *, full_page: bool = False) -> ScreenshotResult:
        state = self._require_state()
        if state is None:
            return ScreenshotResult(ok=False, error=ErrCode.session_unknown)
        try:
            b64, (w, h) = self._executor.run(_do_screenshot, state.page, full_page)
        except Exception as e:   # noqa: BLE001
            return ScreenshotResult(ok=False, error=classify_exception(e),
                                    error_detail=f"{type(e).__name__}: {e}")
        return ScreenshotResult(ok=True, b64=b64, width=w, height=h)

    def recent_requests(self, *, host_contains: str | None = None,
                        limit: int = 20) -> list[NetEvent]:
        state = self._require_state()
        if state is None:
            return []
        evts = list(state.network_tail)
        if host_contains is not None:
            evts = [e for e in evts if host_contains in (e.host or "")]
        return evts[-limit:]

    # --- internals ---

    def _require_state(self) -> _SessionState | None:
        return self._state

    def _resolve(self, snapshot: Observation, handle: str) -> tuple[Any | None, ErrCode | None, str]:
        state = self._require_state()
        assert state is not None
        return self._executor.run(resolve_handle, state.page, snapshot, handle)

    def _handle_act(
        self,
        handle: str,
        op: Callable[[Any], None],
        *,
        snapshot: Observation | None = None,
    ) -> ActResult:
        state = self._require_state()
        if state is None:
            return ActResult(ok=False, error=ErrCode.session_unknown)

        snapshot = snapshot or state.last_observation or self.observe()
        url_before = _current_url(self._executor, state)

        loc, err, detail = self._resolve(snapshot, handle)
        if err is not None:
            return ActResult(ok=False, error=err, error_detail=detail,
                             url_before=url_before, url_after=url_before)
        try:
            self._executor.run(op, loc)
        except Exception as e:   # noqa: BLE001
            return ActResult(
                ok=False, error=classify_exception(e),
                error_detail=f"{type(e).__name__}: {e}",
                url_before=url_before,
                url_after=_current_url(self._executor, state),
            )
        return self._finalise_act(state, url_before)

    def _finalise_act(self, state: _SessionState, url_before: str) -> ActResult:
        before_obs = state.last_observation
        # An action may have opened a new tab (Greenhouse Apply pattern) or
        # triggered a same-page navigation. Re-bind state.page to the newest
        # page in the context and wait for it to be ready before snapshotting.
        try:
            self._executor.run(_rebind_newest_page, state, self._observer_js)
        except Exception as e:   # noqa: BLE001
            log.info("rebind_newest_page failed: %s", e)
        # Take a fresh snapshot and compute diff.
        try:
            raw = self._executor.run(_do_snapshot, state.page, True,
                                      self._observer_js)
        except Exception as e:   # noqa: BLE001
            log.info("post-action snapshot failed: %s", e)
            raw = None
        if raw is not None:
            new_obs = parse_snapshot(raw, console_tail=state.console_tail,
                                      network_tail=state.network_tail)
        else:
            new_obs = None
        state.last_observation = new_obs
        url_after = new_obs.url if new_obs is not None else _current_url(self._executor, state)
        return ActResult(
            ok=True,
            url_before=url_before,
            url_after=url_after,
            navigated=(url_before or "") != (url_after or ""),
            diff=compute_diff(before_obs, new_obs),
        )

    def _screenshot_b64(self, state: _SessionState) -> str:
        data, _ = self._executor.run(_do_screenshot, state.page, False)
        return data


# ---------- module-level callables used by LocalExecutor.run ----------
# These are plain functions so they can be pickled/logged, and they take
# Playwright objects as arguments — they MUST be invoked through an executor
# that runs them on the browser-owning thread.

def _new_page(context: Any, observer_js: str) -> Any:
    page = context.new_page()
    # Cap every Playwright operation so a frozen page becomes a fast
    # TimeoutError instead of a process hang. 10s for actions, 15s for
    # navigations — both generous for real interactions but short enough
    # that a bot-detection stall or a wiped observer doesn't lock the
    # submission loop forever.
    try:
        page.set_default_timeout(10000)
        page.set_default_navigation_timeout(15000)
    except Exception:
        pass
    page.add_init_script(observer_js)
    # Install directly in the current page too (add_init_script only applies
    # to navigations after attach, not the initial about:blank).
    page.evaluate(observer_js)
    return page


def _rebind_newest_page(state: _SessionState, observer_js: str) -> None:
    """Rebind state.page to the most recent page in the context.

    Handles two post-action cases:
    - A click opened the next step in a new tab (common for SPA apply
      flows, Auth0 login redirects, etc.). state.page is still the
      original tab, which now has nothing useful on it.
    - A click caused a same-page navigation that's still in flight.

    In both cases we wait for the newest page to reach domcontentloaded
    before returning; that lets the caller snapshot a settled DOM.

    Set ``WAYFINDER_DEBUG=1`` to see every rebind decision on stderr.
    """
    try:
        pages = list(state.context.pages) if state.context is not None else []
    except Exception as e:   # noqa: BLE001
        _dbg(f"[rebind] context.pages failed: {e!r}")
        return
    if not pages:
        _dbg("[rebind] no pages in context")
        return
    newest = pages[-1]
    is_new = newest is not state.page
    cur_url = ""
    try:
        cur_url = newest.url
    except Exception:
        pass
    _dbg(f"[rebind] pages={len(pages)} is_new={is_new} url={cur_url!r}")
    try:
        newest.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception as e:   # noqa: BLE001
        _dbg(f"[rebind] wait_for_load_state raised: {e!r}")
    if is_new:
        try:
            newest.add_init_script(observer_js)
        except Exception as e:   # noqa: BLE001
            _dbg(f"[rebind] add_init_script: {e!r}")
        try:
            newest.evaluate(observer_js)
        except Exception as e:   # noqa: BLE001
            _dbg(f"[rebind] evaluate: {e!r}")
        try:
            newest.bring_to_front()
        except Exception:
            pass
        state.page = newest
        try:
            final_url = state.page.url
        except Exception:
            final_url = "?"
        _dbg(f"[rebind] rebound state.page -> {final_url!r}")


def _dbg(msg: str) -> None:
    """Debug-print to stderr when WAYFINDER_DEBUG is set. No-op otherwise."""
    import os as _os
    if _os.environ.get("WAYFINDER_DEBUG"):
        import sys as _sys
        print(msg, file=_sys.stderr)


def _install_page_hooks(page: Any, state: _SessionState, allowed: list[str],
                        console_cap: int, network_cap: int) -> None:
    def _on_console(msg: Any) -> None:
        try:
            state.console_tail.append(f"[{msg.type}] {str(msg.text)[:200]}")
            _truncate(state.console_tail, console_cap)
        except Exception:
            pass

    def _on_response(resp: Any) -> None:
        try:
            state.network_tail.append(make_net_event(
                event="response", url=str(resp.url),
                status=int(getattr(resp, "status", 0) or 0),
                host=_host_of(str(resp.url)),
            ))
            _truncate(state.network_tail, network_cap)
        except Exception:
            pass

    def _on_route(route: Any, request: Any) -> None:
        try:
            host = _host_of(str(request.url))
            headers = dict(request.headers)
            if not _host_in_scope(host, allowed):
                headers.pop("authorization", None)
                headers.pop("cookie", None)
                state.network_tail.append(make_net_event(
                    event="scope_strip", url=str(request.url),
                    method=str(request.method), host=host,
                ))
                _truncate(state.network_tail, network_cap)
            state.network_tail.append(make_net_event(
                event="request", url=str(request.url),
                method=str(request.method), host=host,
            ))
            _truncate(state.network_tail, network_cap)
            route.continue_(headers=headers)
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    page.on("console", _on_console)
    page.on("response", _on_response)
    page.route("**/*", _on_route)


def _truncate(lst: list[Any], n: int) -> None:
    extra = len(lst) - n
    if extra > 0:
        del lst[:extra]


def _do_goto(page: Any, url: str, wait: str, timeout_s: int) -> None:
    page.goto(url, wait_until=wait, timeout=timeout_s * 1000)


def _do_nav_verb(page: Any, verb: str) -> None:
    if verb == "go_back":
        page.go_back()
    elif verb == "reload":
        page.reload()
    else:
        raise ValueError(f"unknown nav verb: {verb}")


def _do_snapshot(page: Any, viewport_only: bool,
                 observer_js: str = "") -> dict[str, Any]:
    # React re-renders, same-origin SPA route changes, and any page
    # script that touches ``delete window.__wayfinder__`` can wipe the
    # observer binding mid-session. Re-inject the observer script
    # unconditionally before every snapshot. Cheap (tens of ms, plain
    # JS eval, no network), and it keeps every later ``observe()``
    # honest even on React-heavy forms like Greenhouse.
    if observer_js:
        try:
            page.evaluate(observer_js)
        except Exception:
            pass
    try:
        return page.evaluate(
            "(opts) => window.__wayfinder__ && window.__wayfinder__.snapshot(opts)",
            {"viewport_only": bool(viewport_only)},
        ) or _empty_snapshot(page)
    except Exception:
        return _empty_snapshot(page)


def _empty_snapshot(page: Any) -> dict[str, Any]:
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    return {"url": url, "title": title, "handles": [], "landmarks": [],
            "text_blocks": [], "fingerprint": "", "truncated": False}


def _do_click(locator: Any, modifiers: tuple[str, ...], timeout_s: int) -> None:
    kwargs: dict[str, Any] = {"timeout": timeout_s * 1000}
    if modifiers:
        kwargs["modifiers"] = list(modifiers)
    locator.click(**kwargs)


def _do_fill(locator: Any, value: str, timeout_s: int) -> None:
    locator.fill(value, timeout=timeout_s * 1000)


def _do_select(locator: Any, option: str, timeout_s: int) -> None:
    locator.select_option(option, timeout=timeout_s * 1000)


def _do_check(locator: Any, state: bool, timeout_s: int) -> None:
    if state:
        locator.check(timeout=timeout_s * 1000)
    else:
        locator.uncheck(timeout=timeout_s * 1000)


def _do_page_press(page: Any, key: str, timeout_s: int) -> None:
    page.keyboard.press(key)


def _do_locator_press(locator: Any, key: str, timeout_s: int) -> None:
    locator.press(key, timeout=timeout_s * 1000)


def _do_submit(locator: Any, timeout_s: int) -> None:
    # Try submit(); if not a form, emulate by pressing Enter on the active field.
    try:
        locator.evaluate("el => { if (el.tagName === 'FORM') el.requestSubmit ? el.requestSubmit() : el.submit(); }")
    except Exception:
        locator.press("Enter", timeout=timeout_s * 1000)


def _body_text(page: Any) -> str:
    try:
        return page.evaluate("() => document.body && document.body.innerText || ''")
    except Exception:
        return ""


def _do_extract_text(locator: Any) -> str:
    try:
        return locator.inner_text(timeout=5000) or ""
    except Exception:
        return locator.text_content(timeout=5000) or ""


def _do_extract_attr(locator: Any, name: str) -> str | None:
    return locator.get_attribute(name, timeout=5000)


def _do_screenshot(page: Any, full_page: bool) -> tuple[str, tuple[int, int]]:
    img = page.screenshot(full_page=full_page)
    vp = page.viewport_size or {"width": 0, "height": 0}
    return base64.b64encode(img).decode("ascii"), (int(vp["width"]), int(vp["height"]))


def _dump_storage(context: Any) -> dict:
    return context.storage_state()


def _current_url(executor: Executor, state: _SessionState) -> str:
    try:
        return executor.run(lambda p: p.url, state.page) or ""
    except Exception:
        return state.last_observation.url if state.last_observation else ""


# ---------- scope helpers ----------

def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _host_in_scope(host: str, allowed: list[str]) -> bool:
    if not host:
        return False
    for pat in allowed:
        p = str(pat).lower().lstrip(".")
        if host == p or host.endswith("." + p):
            return True
    return False


# ---------- session-id minting ----------

def _mint_session_id() -> str:
    import secrets as _s
    return _s.token_hex(8)


__all__ = ["Session"]
