"""Closed-set error taxonomy for the browser layer.

Every verb in `Session` returns a result containing an `ErrCode | None`. Callers
dispatch on the code, not on free-text. Free-text detail is for humans.
"""
from __future__ import annotations

from enum import Enum


class ErrCode(str, Enum):
    handle_stale = "handle_stale"
    """The snapshot the caller held is no longer valid (navigation / mutation)."""

    handle_not_found = "handle_not_found"
    """Handle not in the current snapshot."""

    not_visible = "not_visible"
    """Element is in the AX tree but off-screen / hidden / display:none."""

    disabled = "disabled"
    """Element is present and visible but not enabled right now."""

    timeout = "timeout"
    """A wait or action exceeded its time budget."""

    scope_violation = "scope_violation"
    """Target URL or cross-origin redirect outside the session's allowed_domains."""

    navigation_blocked = "navigation_blocked"
    """The page prevented navigation (beforeunload, download that wasn't permitted)."""

    secret_denied = "secret_denied"
    """A value_literal was refused because the field looks credential-shaped."""

    secret_unknown = "secret_unknown"
    """A value_ref pointed at a secret that does not exist."""

    identity_locked = "identity_locked"
    """A live session already owns storage_state for this identity."""

    playwright_error = "playwright_error"
    """Uncategorised Playwright exception; detail carries the class name."""

    network_dead = "network_dead"
    """page.goto raised a net error (DNS, connection refused, TLS, ...)."""

    oauth_required = "oauth_required"
    """A login wall was detected. Caller should invoke the oauth flow."""

    session_unknown = "session_unknown"
    """The Session object has no active browser context (closed or never opened)."""

    bad_argument = "bad_argument"
    """The caller passed arguments that can't be reconciled (e.g. both
    value_ref and value_literal; neither; empty allowed_domains)."""


# Keyword fragments in Playwright exception messages that imply a timeout.
# The sync API raises the same TimeoutError subclass for selector timeouts,
# wait_for timeouts, and navigation timeouts, so we inspect the type name first
# and only fall back to substring matches.
_TIMEOUT_TYPE_NAMES = frozenset({"TimeoutError"})
_NOT_VISIBLE_FRAGMENTS = ("not visible", "is hidden", "not displayed")
_DISABLED_FRAGMENTS = ("not enabled", "is disabled")
_NETWORK_FRAGMENTS = (
    "net::ERR_",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_CERT_",
    "getaddrinfo",
)


def classify_exception(exc: BaseException) -> ErrCode:
    """Map a Playwright (or general) exception to a closed ErrCode.

    Deliberately pessimistic: unknown exceptions classify as `playwright_error`
    rather than being coerced into a friendlier code.
    """
    name = type(exc).__name__
    text = str(exc)
    lower = text.lower()

    if name in _TIMEOUT_TYPE_NAMES:
        return ErrCode.timeout
    for frag in _NOT_VISIBLE_FRAGMENTS:
        if frag in lower:
            return ErrCode.not_visible
    for frag in _DISABLED_FRAGMENTS:
        if frag in lower:
            return ErrCode.disabled
    for frag in _NETWORK_FRAGMENTS:
        if frag in text:
            return ErrCode.network_dead
    return ErrCode.playwright_error


__all__ = ["ErrCode", "classify_exception"]
