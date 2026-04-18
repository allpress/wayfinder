"""Python side of the observer. Loads the injected script, post-processes the
raw snapshot into a typed `Observation`, and resolves handles to Playwright
`Locator`s."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wayfinder.browser.errors import ErrCode
from wayfinder.browser.models import (
    Interactable,
    Landmark,
    LoginHint,
    NetEvent,
    Observation,
    TextBlock,
)

# --- login-wall detection hints (URL substring → provider) ---

_LOGIN_URL_HINTS: tuple[tuple[str, str, str], ...] = (
    ("login.microsoftonline.com", "microsoft", "Microsoft login page"),
    ("login.live.com", "microsoft", "Microsoft personal account"),
    ("accounts.google.com", "google", "Google accounts login"),
    ("github.com/login", "github", "GitHub login"),
    ("github.com/session", "github", "GitHub session"),
    ("auth0.com/u/login", "generic", "Auth0-hosted login"),
    ("okta.com/login", "generic", "Okta login"),
)


@dataclass(slots=True)
class _ObserverState:
    """Mutable per-session state the observer needs."""
    script_source: str
    console_tail: list[str] = field(default_factory=list)
    network_tail: list[NetEvent] = field(default_factory=list)
    last_snapshot: Observation | None = None


def load_observer_script() -> str:
    """Read observer.js from the package. Cached on first call."""
    global _CACHED_SCRIPT
    if _CACHED_SCRIPT is None:
        path = Path(__file__).with_name("observer.js")
        _CACHED_SCRIPT = path.read_text(encoding="utf-8")
    return _CACHED_SCRIPT


_CACHED_SCRIPT: str | None = None


def parse_snapshot(
    raw: dict[str, Any],
    *,
    console_tail: list[str],
    network_tail: list[NetEvent],
    screenshot_b64: str | None = None,
) -> Observation:
    """Convert the raw JS dict into a typed Observation with login detection."""
    handles = [_parse_interactable(h) for h in raw.get("handles", [])]
    landmarks = [_parse_landmark(lm) for lm in raw.get("landmarks", [])]
    text_blocks = [_parse_text_block(tb) for tb in raw.get("text_blocks", [])]

    url = raw.get("url", "") or ""
    login_hint = _detect_login_wall(url, handles)

    return Observation(
        url=url,
        title=raw.get("title", "") or "",
        handles=handles,
        landmarks=landmarks,
        text_blocks=text_blocks,
        console_tail=list(console_tail[-20:]),
        network_tail=list(network_tail[-20:]),
        fingerprint=raw.get("fingerprint", "") or "",
        truncated=bool(raw.get("truncated", False)),
        login_hint=login_hint,
        screenshot_b64=screenshot_b64,
        snapshot_id=secrets.token_hex(6),
    )


def _parse_interactable(d: dict[str, Any]) -> Interactable:
    bbox = d.get("bbox")
    if bbox is not None and not isinstance(bbox, tuple):
        bbox = tuple(bbox)  # type: ignore[assignment]
    return Interactable(
        handle=d["handle"],
        role=d.get("role", "") or "",
        name=d.get("name", "") or "",
        value=d.get("value"),
        label=d.get("label"),
        placeholder=d.get("placeholder") or None,
        required=bool(d.get("required", False)),
        disabled=bool(d.get("disabled", False)),
        checked=d.get("checked"),
        editable=bool(d.get("editable", False)),
        in_form=d.get("in_form"),
        landmark=d.get("landmark"),
        ordinal=int(d.get("ordinal", 0) or 0),
        bbox=bbox,  # type: ignore[arg-type]
    )


def _parse_landmark(d: dict[str, Any]) -> Landmark:
    return Landmark(
        handle=d["handle"],
        role=d.get("role", "") or "",
        name=d.get("name", "") or "",
    )


def _parse_text_block(d: dict[str, Any]) -> TextBlock:
    return TextBlock(
        handle=d["handle"],
        tag=d.get("tag", "") or "",
        text=d.get("text", "") or "",
        landmark=d.get("landmark"),
    )


def _detect_login_wall(url: str, handles: list[Interactable]) -> LoginHint | None:
    """Pattern-match for known login pages.

    Matches on URL substrings first; falls back to a structural heuristic:
    a password-shaped textbox + a button named "Sign in"/"Log in"/"Next".
    """
    lower_url = url.lower()
    for needle, provider, reason in _LOGIN_URL_HINTS:
        if needle in lower_url:
            return LoginHint(provider=provider, reason=reason)

    # Structural fallback.
    has_password = any(
        h.role == "textbox" and (
            "password" in (h.placeholder or "").lower()
            or "password" in (h.label or "").lower()
            or "__wf_password__" in (h.label or "")
        )
        for h in handles
    )
    has_username = any(
        h.role in {"textbox", "searchbox"} and any(
            term in " ".join([h.name, h.placeholder or "", h.label or ""]).lower()
            for term in ("email", "username", "user name", "login", "account")
        )
        for h in handles
    )
    has_submit = any(
        h.role == "button" and any(
            term in h.name.lower() for term in ("sign in", "log in", "continue", "next", "login")
        )
        for h in handles
    )
    if has_password and (has_submit or has_username):
        return LoginHint(provider="generic", reason="password field + submit/username inferred")
    return None


# --- handle → Locator resolution ---


def resolve_handle(page: Any, obs: Observation, handle: str) -> tuple[Any | None, ErrCode | None, str]:
    """Return (locator, err, detail). err=None means locator is a usable Playwright Locator."""
    el = obs.by_handle(handle)
    if el is None:
        return None, ErrCode.handle_not_found, f"handle {handle!r} not in snapshot"
    return _resolve_interactable(page, el)


def _resolve_interactable(page: Any, el: Interactable) -> tuple[Any | None, ErrCode | None, str]:
    """Turn an Interactable record into a Playwright Locator."""
    role = el.role or ""
    name = el.name or ""

    # Playwright's get_by_role is the happy path.
    try:
        if name:
            loc = page.get_by_role(role, name=name)
        else:
            loc = page.get_by_role(role)
    except Exception as e:  # noqa: BLE001
        return None, ErrCode.playwright_error, f"get_by_role failed: {type(e).__name__}: {e}"

    try:
        count = loc.count()
    except Exception as e:  # noqa: BLE001
        return None, ErrCode.playwright_error, f"locator.count failed: {type(e).__name__}: {e}"

    if count == 0:
        # Fallback: for form we can try a positional selector.
        if role == "form":
            loc2 = page.locator("form")
            try:
                c2 = loc2.count()
            except Exception:
                return None, ErrCode.handle_stale, "locator disappeared"
            if c2 > el.ordinal:
                return loc2.nth(el.ordinal), None, ""
        return None, ErrCode.handle_stale, f"no element matches role={role!r} name={name!r}"

    if count == 1:
        return loc.first, None, ""

    # Multiple matches — use the ordinal, but guard against index out of range.
    if el.ordinal >= count:
        return loc.last, None, ""
    return loc.nth(el.ordinal), None, ""


# --- network event construction ---


def make_net_event(*, event: str, url: str, method: str | None = None,
                   status: int | None = None, host: str | None = None) -> NetEvent:
    return NetEvent(ts=time.time(), event=event, url=url[:300],
                    method=method, status=status, host=host)


__all__ = [
    "load_observer_script",
    "parse_snapshot",
    "resolve_handle",
    "make_net_event",
]
