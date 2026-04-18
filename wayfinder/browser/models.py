"""Data models for the browser layer.

Every field is JSON-serialisable so results can cross the warden RPC boundary
without custom codecs. `to_dict` / `from_dict` round-trips are tested.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any

from wayfinder.browser.errors import ErrCode


# ---------- primitive pieces ----------

@dataclass(frozen=True, slots=True)
class Interactable:
    handle: str
    role: str
    name: str
    value: str | None = None
    label: str | None = None
    placeholder: str | None = None
    required: bool = False
    disabled: bool = False
    checked: bool | None = None       # tri-state for checkbox/radio/switch
    editable: bool = False
    in_form: str | None = None        # handle of containing form, if any
    landmark: str | None = None       # landmark region the element lives in
    ordinal: int = 0                  # nth element matching (role, name)
    bbox: tuple[int, int, int, int] | None = None   # (x, y, w, h), optional


@dataclass(frozen=True, slots=True)
class Landmark:
    handle: str
    role: str                         # main, navigation, banner, contentinfo, ...
    name: str                         # aria-label or heading if available


@dataclass(frozen=True, slots=True)
class TextBlock:
    handle: str                       # id is scoped to the snapshot
    tag: str                          # h1/h2/h3/p/li/...
    text: str
    landmark: str | None = None


@dataclass(frozen=True, slots=True)
class NetEvent:
    ts: float
    event: str                        # "request" | "response" | "scope_strip" | "download"
    url: str
    method: str | None = None
    status: int | None = None
    host: str | None = None


# ---------- observation ----------

@dataclass(frozen=True, slots=True)
class LoginHint:
    provider: str                     # "microsoft" | "google" | "github" | "generic"
    reason: str                       # short human string (why we think this)


@dataclass(frozen=True, slots=True)
class Observation:
    url: str
    title: str
    handles: list[Interactable] = field(default_factory=list)
    landmarks: list[Landmark] = field(default_factory=list)
    text_blocks: list[TextBlock] = field(default_factory=list)
    console_tail: list[str] = field(default_factory=list)
    network_tail: list[NetEvent] = field(default_factory=list)
    fingerprint: str = ""
    truncated: bool = False
    login_hint: LoginHint | None = None
    screenshot_b64: str | None = None
    snapshot_id: str = ""             # opaque id for this snapshot

    def by_handle(self, handle: str) -> Interactable | None:
        for h in self.handles:
            if h.handle == handle:
                return h
        return None

    def with_screenshot(self, b64: str) -> Observation:
        return _replace_frozen(self, screenshot_b64=b64)


@dataclass(frozen=True, slots=True)
class ObservationDiff:
    url_changed: bool
    title_changed: bool
    url_before: str
    url_after: str
    added_handles: list[str] = field(default_factory=list)
    removed_handles: list[str] = field(default_factory=list)
    changed_handles: list[str] = field(default_factory=list)
    added_text: list[str] = field(default_factory=list)     # new text_block handles
    new_network: list[NetEvent] = field(default_factory=list)
    new_console: list[str] = field(default_factory=list)


# ---------- action / observation results ----------

@dataclass(frozen=True, slots=True)
class OpenResult:
    ok: bool
    session_id: str = ""
    identity: str = ""
    allowed_domains: list[str] = field(default_factory=list)
    headless: bool = True
    loaded_storage: bool = False
    error: ErrCode | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class SaveResult:
    ok: bool
    identity: str = ""
    bytes_written: int = 0
    error: ErrCode | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class ActResult:
    ok: bool
    error: ErrCode | None = None
    error_detail: str | None = None
    url_before: str = ""
    url_after: str = ""
    navigated: bool = False
    diff: ObservationDiff | None = None


@dataclass(frozen=True, slots=True)
class ExtractResult:
    ok: bool
    text: str = ""
    truncated: bool = False
    error: ErrCode | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class ScreenshotResult:
    ok: bool
    b64: str = ""
    width: int = 0
    height: int = 0
    error: ErrCode | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthResult:
    ok: bool
    identity: str = ""
    provider: str = ""
    stored_tokens: list[str] = field(default_factory=list)   # secret refs that were written
    expires_at: float | None = None                          # epoch seconds, best-effort
    error: ErrCode | None = None
    error_detail: str | None = None


@dataclass(frozen=True, slots=True)
class IdentityInfo:
    name: str
    provider: str | None = None
    allowed_domains: list[str] = field(default_factory=list)
    last_refresh: float | None = None                        # epoch seconds
    has_storage: bool = False


# ---------- JSON round-trip helpers ----------

_ENUMS: tuple[type, ...] = (ErrCode,)


def to_dict(obj: Any) -> Any:
    """Recursively convert dataclass / enum / tuple trees to JSON-safe dicts."""
    if obj is None:
        return None
    if isinstance(obj, _ENUMS):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in fields(obj):
            out[f.name] = to_dict(getattr(obj, f.name))
        return out
    if isinstance(obj, list):
        return [to_dict(x) for x in obj]
    if isinstance(obj, tuple):
        return [to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    return obj


_DATACLASSES: dict[str, type] = {
    cls.__name__: cls
    for cls in [
        Interactable, Landmark, TextBlock, NetEvent, LoginHint,
        Observation, ObservationDiff,
        OpenResult, SaveResult, ActResult, ExtractResult, ScreenshotResult,
        OAuthResult, IdentityInfo,
    ]
}


def from_dict(cls: type, data: Any) -> Any:
    """Inverse of to_dict, given the target dataclass type.

    Does not do union/optional inference beyond what's needed by the concrete
    set of types we use (None, list, primitive, ErrCode, nested dataclass,
    tuple-as-list for bbox).
    """
    if data is None:
        return None
    if not is_dataclass(cls):
        return data
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        raw = data[f.name]
        kwargs[f.name] = _coerce(f.type, raw)
    return cls(**kwargs)


def _coerce(annotation: Any, raw: Any) -> Any:
    if raw is None:
        return None
    anno = annotation if isinstance(annotation, str) else repr(annotation)
    # ErrCode
    if "ErrCode" in anno:
        return ErrCode(raw) if not isinstance(raw, ErrCode) else raw
    # tuple[int, int, int, int] for bbox
    if anno.startswith("tuple[") or "tuple[int" in anno:
        return tuple(raw) if not isinstance(raw, tuple) else raw
    # list[<Dataclass>]
    if anno.startswith("list[") and not anno.startswith("list[str]"):
        inner_name = anno[len("list[") : -1]
        inner = _DATACLASSES.get(inner_name)
        if inner is not None and isinstance(raw, list):
            return [from_dict(inner, item) for item in raw]
        return list(raw)
    # Optional / nested single dataclass
    for name, inner in _DATACLASSES.items():
        if name in anno and isinstance(raw, dict):
            return from_dict(inner, raw)
    return raw


def _replace_frozen(obj: Any, **changes: Any) -> Any:
    """dataclasses.replace that tolerates frozen slots."""
    data = asdict(obj)
    data.update(changes)
    return type(obj)(**data)


__all__ = [
    "Interactable", "Landmark", "TextBlock", "NetEvent", "LoginHint",
    "Observation", "ObservationDiff",
    "OpenResult", "SaveResult", "ActResult", "ExtractResult",
    "ScreenshotResult", "OAuthResult", "IdentityInfo",
    "to_dict", "from_dict",
]
