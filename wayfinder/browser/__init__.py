"""Wayfinder browser layer.

Public API: ``Session`` plus the dataclasses in :mod:`wayfinder.browser.models`
and the enum ``ErrCode``.
"""
from wayfinder.browser.credentials import is_credential_shaped
from wayfinder.browser.diff import diff as observation_diff
from wayfinder.browser.errors import ErrCode, classify_exception
from wayfinder.browser.executor import Executor, LocalExecutor, WardenExecutor, WardenWebClient
from wayfinder.browser.identity import IdentityError, IdentityStore
from wayfinder.browser.models import (
    ActResult,
    ExtractResult,
    IdentityInfo,
    Interactable,
    Landmark,
    LoginHint,
    NetEvent,
    OAuthResult,
    Observation,
    ObservationDiff,
    OpenResult,
    SaveResult,
    ScreenshotResult,
    TextBlock,
    from_dict,
    to_dict,
)
from wayfinder.browser.oauth import (
    PROVIDERS,
    OAuthError,
    Provider,
    TokenBundle,
    capture_tokens_from_url,
    detect_provider,
    refresh_token,
)
from wayfinder.browser.observer import (
    load_observer_script,
    parse_snapshot,
    resolve_handle,
)
from wayfinder.browser.session import Session

__all__ = [
    "Session",
    "Executor",
    "LocalExecutor",
    "WardenExecutor",
    "WardenWebClient",
    "IdentityStore",
    "IdentityError",
    # models
    "ActResult", "ExtractResult", "IdentityInfo", "Interactable", "Landmark",
    "LoginHint", "NetEvent", "OAuthResult", "Observation", "ObservationDiff",
    "OpenResult", "SaveResult", "ScreenshotResult", "TextBlock",
    "to_dict", "from_dict",
    # errors
    "ErrCode", "classify_exception",
    # observer
    "load_observer_script", "parse_snapshot", "resolve_handle",
    # oauth
    "Provider", "PROVIDERS", "TokenBundle", "OAuthError",
    "detect_provider", "capture_tokens_from_url", "refresh_token",
    # misc
    "is_credential_shaped", "observation_diff",
]
