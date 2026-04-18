# Wayfinder — Design Spec (Browser Layer)

> An AI-driven wrapper over headless Playwright. Replaces selectors/sleeps with
> an accessibility-tree + stable-handle model, structured diffs, and a
> compact error taxonomy. Integrates with warden for secrets, storage_state,
> and OAuth token capture.

## 1. Scope

**In scope**
- A Python library (`wayfinder.browser`) that drives Playwright Chromium in a
  way that a non-vision LLM can observe, act, and recover from failure in
  small, structured steps.
- A warden worker (`warden/workers/browser_v2.py`) that exposes those library
  primitives as RPC methods under a new `web.*` namespace (v1 `browser.*`
  methods stay around for one release, then get deleted).
- Persistent identities: named Chromium `storage_state` blobs held by warden
  and reused across sessions (cookies, IndexedDB, refreshed OAuth tokens).
- OAuth helpers: detect login walls, run the interactive dance headfully when
  a storage_state is missing, capture tokens from redirects, re-store.

**Non-goals (for this spec)**
- Rewriting the existing HTTP walker (`wayfinder/walker.py`). That's a
  separate read-only feed fetcher and stays as-is.
- Building an autonomous planner / graph search. Wayfinder is the
  *instrument*; the planner lives in weaver or the LLM driving it. We will
  later add a thin `goals.py` module but that is out of scope here.
- Replacing the `sync_playwright` loop in warden; we reuse `BrowserLoop`.

## 2. Placement in the trio

```
  [AI / weaver CLI]
        │  high-level calls (python import or RPC)
        ▼
  wayfinder.browser     ── pure logic: AX tree, handles, diffs, verbs, errors
        │
        ▼
  warden browser_v2 worker   ── Playwright owner, secret deref, policy, scope
        │                                (runs on warden's BrowserLoop thread)
        ▼
  Chromium (Playwright, headless by default)
```

- **weaver** imports `wayfinder.browser.Session` and either (a) runs in-process
  for local CLI use, or (b) calls warden's `web.*` RPC methods when running in
  sandboxed mode. Both modes share one code path — the Session class has an
  abstract executor.
- **warden** owns the process that owns Chromium and the Keychain. It holds
  `storage_state` blobs, dereferences `secret://…` refs, and enforces the
  existing per-method allow-list.
- **wayfinder** is the pure-logic layer: no Playwright import at the top
  level (it's injected), no secret reads, no policy decisions.

This preserves the guardian-isolation invariant: the sandbox (weaver, Claude)
still never sees raw secrets. `value_ref` stays the only way to inject
credentials; wayfinder never accepts `value_literal` for anything that looks
like a credential (we add a heuristic flag — see §7).

## 3. Mental model: handles, not selectors

The surface the AI sees is a flat list of **interactables** keyed by **handles**.

- A handle is a short opaque string like `h7a2` — generated deterministically
  from the element's AX path + role + accessible name + ordinal, stable within
  a single observation. A second `observe()` on the same page returns the same
  handles for elements that haven't moved.
- Handles are NOT valid across page navigations. After any action that
  triggers navigation or a major DOM mutation, handles are invalidated and the
  caller must call `observe()` again.
- The AI never writes a CSS or XPath selector. Underneath, wayfinder resolves
  a handle to a Playwright `Locator` using the accessible role+name (Playwright
  exposes this via `get_by_role` / `get_by_label`) plus an ordinal fallback.

This is the single most important design choice: **handles are the namespace.**

## 4. API surface

One class, `Session`. All verbs are idempotent-where-possible, return a
structured result, and never raise for domain errors (only programmer errors).

```python
class Session:
    # -- lifecycle --
    def open(self, *, identity: str, allowed_domains: list[str],
             headless: bool = True, load_storage: bool = True) -> OpenResult
    def close(self) -> None
    def save_storage(self) -> SaveResult           # persists via warden
    # -- navigation --
    def goto(self, url: str, *, wait: str = "domcontentloaded") -> ActResult
    def back(self) -> ActResult
    def reload(self) -> ActResult
    # -- observation --
    def observe(self, *, viewport_only: bool = True,
                include_screenshot: bool = False) -> Observation
    # -- interaction (all take handle, not selector) --
    def click(self, handle: str, *, modifiers: list[str] = ()) -> ActResult
    def fill(self, handle: str, *, value_ref: str | None = None,
             value_literal: str | None = None) -> ActResult
    def select(self, handle: str, *, option: str) -> ActResult
    def check(self, handle: str, *, state: bool = True) -> ActResult
    def press(self, handle: str | None, *, key: str) -> ActResult
    def submit(self, form_handle: str) -> ActResult
    # -- semantic waits (no selectors) --
    def wait_for(self, *, url_contains: str | None = None,
                 text_contains: str | None = None,
                 handle_role: str | None = None,
                 timeout_s: int = 10) -> ActResult
    # -- extraction --
    def extract_text(self, handle: str, *, max_chars: int = 2000) -> ExtractResult
    def extract_attribute(self, handle: str, name: str) -> ExtractResult
    def screenshot(self, *, full_page: bool = False) -> ScreenshotResult
    # -- network taps (for OAuth capture, see §8) --
    def recent_requests(self, *, host_contains: str | None = None,
                        limit: int = 20) -> list[NetEvent]
```

### 4.1 Result shapes

Every result is a dataclass with `ok: bool`, `error: ErrCode | None`, and a
`diff: ObservationDiff | None` where applicable. The diff describes what
changed since the previous observation so the AI never has to re-parse a
full page to detect an effect.

```python
@dataclass(frozen=True, slots=True)
class ActResult:
    ok: bool
    error: ErrCode | None = None
    error_detail: str | None = None           # human-readable, ≤200 chars
    url_before: str | None = None
    url_after: str | None = None
    navigated: bool = False                    # True if url changed
    diff: ObservationDiff | None = None        # None if caller skipped observe

@dataclass(frozen=True, slots=True)
class Observation:
    url: str
    title: str
    handles: list[Interactable]                # flat list, see §5
    landmarks: list[Landmark]                  # main/nav/header/footer regions
    text_blocks: list[TextBlock]               # readable text chunks, non-interactive
    console_tail: list[str]
    network_tail: list[NetEvent]
    screenshot_b64: str | None = None
    # for diffs:
    fingerprint: str                           # hash of handles+landmarks
```

### 4.2 Error taxonomy

`ErrCode` is a string enum. Closed set — no free-text surprises:

- `handle_stale` — observation has been invalidated by a navigation or mutation.
- `handle_not_found` — handle not in current snapshot.
- `not_visible` — element is in AX tree but offscreen/hidden.
- `disabled` — interactable but not enabled right now.
- `timeout` — wait_for exceeded its budget.
- `scope_violation` — URL or cross-origin redirect outside allowed_domains.
- `navigation_blocked` — page prevented navigation (e.g. `beforeunload`).
- `secret_denied` — value_ref rejected by warden policy.
- `secret_unknown` — value_ref points at a nonexistent secret.
- `identity_locked` — another session has storage_state for this identity open.
- `playwright_error` — anything else from PW; detail carries the class name.
- `network_dead` — page.goto raised a net error; detail has the code.
- `oauth_required` — a login wall was detected (see §8).

The caller maps these to retry / replan / fail. In particular, `handle_stale`
means "call observe() and try again"; `oauth_required` means "kick off the
login flow".

## 5. The interactable node

```python
@dataclass(frozen=True, slots=True)
class Interactable:
    handle: str                      # e.g. "h7a2"
    role: str                        # aria role (button, textbox, link, ...)
    name: str                        # accessible name, truncated to 200 chars
    value: str | None = None         # current value for inputs
    label: str | None = None         # associated <label>, if any
    placeholder: str | None = None
    required: bool = False
    disabled: bool = False
    checked: bool | None = None      # tri-state
    editable: bool = False
    in_form: str | None = None       # handle of the owning form, if any
    landmark: str | None = None      # landmark region it lives in
    bbox: tuple[int, int, int, int] | None = None   # optional, if screenshot
```

The snapshot is produced by one injected JS function (§6) that walks
`document` and emits only elements whose computed role is in a whitelist
(button, link, textbox, combobox, checkbox, radio, tab, menuitem, option,
form, searchbox, switch, slider, spinbutton). Everything else falls into
`text_blocks`.

Hard cap: `MAX_HANDLES = 400`. If the page exceeds it, we keep the
viewport-visible ones plus any in the currently-focused landmark, and mark
`Observation.truncated = True`. `viewport_only=False` raises the cap to
1500 with a log warning.

## 6. Injected JS (the observer)

One shared, read-only script built at load time and injected via
`page.add_init_script`. It installs `window.__wayfinder__.snapshot()` which:

1. Walks the accessibility tree using `document.evaluate` + a tagname pre-filter,
   cross-referenced with `element.getAttribute("role")`, `aria-*`, and
   computed role rules for native elements (`button`, `a[href]`, `input`, ...).
2. Computes a stable handle:
   `sha1(role + "|" + name + "|" + ax_path + "|" + ordinal)[0:4]` prefixed
   with `h`. Collisions resolved by bumping the ordinal suffix.
3. Returns `{handles, landmarks, text_blocks, fingerprint}`.

No arbitrary eval. No sandbox-supplied JS. The script source lives at
`wayfinder/browser/observer.js` and is loaded as a string.

### 6.1 Handle resolution (going back)

Given `handle = "h7a2"`, the Session resolves it by:
1. Looking it up in `_current_snapshot.by_handle` to get the role+name+ordinal.
2. Building a Playwright `Locator` via `page.get_by_role(role, name=name).nth(ordinal)`.
3. If that locator matches zero elements → return `handle_stale`.
4. If it matches more than one → prefer the one whose bbox matches the
   snapshot's bbox; tiebreak → `handle_stale`.

## 7. Secrets & fill()

`fill()` takes exactly one of `value_ref` or `value_literal`. Rules:

- If `value_literal` is passed and the handle's role is `textbox` or
  `searchbox` AND the handle's `name`/`label`/`placeholder` matches any of
  `["password", "passwd", "pin", "mfa", "otp", "totp", "token", "secret",
  "api key", "card number", "cvv", "cvc"]` (case-insensitive, substring) →
  reject with `secret_denied`. The error message says "literal refused for
  credential-shaped field; use value_ref".
- `value_ref` is always allowed if policy permits.
- Inside the warden worker, `value_ref` is dereferenced exactly as it is today
  in `browser_worker._resolve_value` / `_fetch_secret`. That code moves with
  minimal change into the v2 worker.

## 8. Identity, storage_state, and OAuth

### 8.1 Identity

An **identity** is a named persistent browser profile, e.g. `ms-work`,
`google-personal`, `github-ci`. It maps to:
- A Chromium `storage_state` JSON blob (cookies, localStorage, IndexedDB).
- An `allowed_domains` default list.
- An optional `oauth_provider` annotation (`microsoft`, `google`, `github`,
  `okta`, `generic_oidc`).

Warden stores these under `~/.warden/identities/<name>.json` (the state blob)
and `~/.warden/identities/<name>.meta.yaml` (domains, provider, last_refresh).
The blob is AES-GCM-encrypted with a key derived from the Keychain-backed
capability master (same secret machinery as today).

### 8.2 New warden RPC methods

Added to the policy with conservative rate limits:

| Method                       | Args                                                | Notes                                   |
|------------------------------|-----------------------------------------------------|-----------------------------------------|
| `web.open_session`           | `identity, allowed_domains?, headless?`             | Loads storage_state if present.         |
| `web.close_session`          | `session_id`                                        |                                         |
| `web.save_storage`           | `session_id`                                        | Atomic replace of `<identity>.json`.    |
| `web.observe`                | `session_id, viewport_only?, include_screenshot?`   | Returns Observation.                    |
| `web.goto`                   | `session_id, url, wait?`                            |                                         |
| `web.click`                  | `session_id, handle, modifiers?`                    |                                         |
| `web.fill`                   | `session_id, handle, value_ref? \| value_literal?`  | Same secret rules.                      |
| `web.select` / `check` / `press` / `submit` | ...                                  |                                         |
| `web.wait_for`               | `session_id, url_contains?, text_contains?, handle_role?, timeout_s?` | Bounded ≤60s.       |
| `web.extract_text`           | `session_id, handle, max_chars?`                    |                                         |
| `web.screenshot`             | `session_id, full_page?`                            | Returned base64.                        |
| `web.oauth_login`            | `identity, provider, start_url, timeout_s?`         | Headful; see §8.3.                      |
| `web.identity_list`          | —                                                   | Names + provider + last_refresh.        |
| `web.identity_forget`        | `identity`                                          | Deletes blob + meta.                    |

The old `browser.*` methods remain in the policy for one release, flagged
`deprecated: true`. They proxy to the new worker.

### 8.3 OAuth login flow

Detection: if `observe()` fingerprints a login wall we know (`login.microsoftonline.com`,
`accounts.google.com`, `github.com/login`, …) OR a `handle_role='textbox'`
with label containing "email"/"username" appears alongside a
`handle_role='button'` with name "Next"/"Sign in", we set an
`Observation.login_hint = {provider, reason}` flag. We do NOT auto-login;
the caller decides.

`web.oauth_login` runs the provider's dance once, **headfully by default**
(so a human can complete MFA / consent), within a temporary Chromium
context scoped to the provider's domains plus the app's domains. On successful
return to a redirect that matches the app origin:
1. The worker persists `storage_state` to `<identity>.json`.
2. The worker scans the network tap for `access_token` / `id_token` query or
   fragment params on the final redirect and, if found, writes them to the
   existing weaver secret store under
   `secret://<context>/<provider>/access_token` (literal, short-TTL — 1h
   default, configurable per provider). These are standard weaver secrets and
   never leave warden.
3. The worker closes the temporary context and returns
   `{identity, provider, stored_tokens: [...], expires_at}`.

Subsequent `web.open_session(identity=…)` calls load the storage_state, so
cookie-authenticated requests work silently. If cookies have expired, the
next observe returns `login_hint` again and the caller calls `oauth_login`.

Headless mode is allowed but only when the identity already has a live
storage_state AND the provider's page can be detected as "reauth prompt,
no MFA required" — this is best-effort. Default to headful.

### 8.4 Token reuse outside the browser

For callers that want to hit a REST API directly (MS Graph, Google APIs)
without a browser, expose `web.fetch_token`:

- Args: `identity, scope?`
- Returns: an access_token value (the literal, registered for redaction)
- Internally: looks up `secret://<context>/<provider>/access_token`; if
  missing or `expires_at` passed, runs a silent refresh using the refresh
  token and the provider's token endpoint (OIDC standard), updates the
  store, returns.

This is the bridge the user asked for: **browser-captured tokens, usable
outside the browser.** All token values stay inside warden.

## 9. Process / transport

Two execution modes, one code path.

### 9.1 In-process (local CLI / dev)

```python
from wayfinder.browser import Session, LocalExecutor
s = Session(LocalExecutor())
s.open(identity="ms-work", allowed_domains=["outlook.office.com"])
obs = s.observe()
s.click(obs.handles[3].handle)
```

`LocalExecutor` imports Playwright directly and runs it in the calling
process. No warden in the loop. Suited for scripts and for weaver CLI
when not sandboxed. Secrets still come from the same weaver secret store
(Keychain-backed).

### 9.2 Warden-brokered (sandboxed)

```python
from wayfinder.browser import Session, WardenExecutor
s = Session(WardenExecutor(socket="~/.warden/sock", capability=os.environ["WARDEN_CAP"]))
# identical API from here
```

`WardenExecutor` translates each verb to the matching `web.*` RPC call.
The Observation / ActResult shapes round-trip losslessly (already
JSON-serializable). This is the mode weaver uses when driven by Claude.

### 9.3 Weaver command

New subcommand `weaver web` with subcommands:

- `weaver web open <identity> --url <url>` — start a session, print session_id.
- `weaver web observe <session_id>` — pretty-print the current Observation.
- `weaver web act <session_id> <verb> [args…]` — one-shot verb.
- `weaver web login <identity> <provider>` — runs oauth_login headfully.
- `weaver web identities` — list known identities.

All of these are thin shells around `wayfinder.browser.Session` using
`WardenExecutor`. They exist for humans; the AI uses the Python/RPC API.

## 10. Security posture

- The existing warden invariants hold unchanged: policy allow-list,
  rate-limits, audit log, redaction on outbound responses.
- `allowed_domains` per session is enforced at three layers:
  (a) `goto()` pre-flight, (b) `page.route('**/*', …)` strips
  Authorization/Cookie on out-of-scope requests (same as today),
  (c) `current_url` check before every action (scope can't drift
  mid-session).
- `storage_state` blobs are encrypted at rest with a Keychain-held key;
  never sent over the wire in full (only handle/identity names).
- `value_literal` for credential-shaped fields is rejected (§7).
- Downloads are disabled by default. To enable: explicit session option
  `downloads_to: Path`. Path must be under `~/.warden/downloads/<session>/`.
- Screenshots are opt-in per call and subject to a cap on size + rate.
- Observations are scrubbed through `warden.redaction.scrub()` before
  leaving warden, same as other RPC responses. Any secret literal that was
  registered during the session (e.g. a just-dereferenced password) is
  masked if it ever appears in a text_block or network_tail.

## 11. Module layout

```
wayfinder/
  wayfinder/
    browser/
      __init__.py          # re-exports Session, ErrCode, dataclasses
      session.py           # Session class, the main API
      executor.py          # abstract Executor; LocalExecutor, WardenExecutor
      observer.py          # AX-snapshot logic (Python side) + handle algebra
      observer.js          # injected JS (string at runtime)
      diff.py              # ObservationDiff computation
      errors.py            # ErrCode enum + mapping helpers
      models.py            # dataclasses: Observation, Interactable, ActResult, ...
      oauth.py             # provider detection + refresh-token flow
    # existing HTTP walker, unchanged:
    walker.py
    events.py
    http_client.py
    policy.py
    breaker.py
  tests/
    browser/
      test_session.py      # against an in-process Playwright + a fixture site
      test_observer.py     # JS observer output on known HTML fixtures
      test_diff.py         # diff correctness
      test_handle_stability.py
      test_oauth_detection.py
      test_identity_roundtrip.py
    # existing HTTP tests, unchanged
```

warden gains:
```
warden/
  warden/workers/
    browser_v2.py          # new worker, uses wayfinder.browser internally
    browser_loop.py        # unchanged
    browser_worker.py      # kept for one release, deprecated, proxies to v2
  config/
    default_policy.yaml    # add web.* rules; mark browser.* deprecated: true
```

## 12. Testing strategy

- **Unit:** observer.js runs against static HTML fixtures (can be tested via
  a tiny `pyppeteer`-free approach: load HTML in a headless Chromium using
  Playwright in the test itself, inject the script, assert snapshot shape).
  Handle stability test: render a page, snapshot, mutate unrelated subtree,
  snapshot again → handles of unchanged elements must match.
- **Integration:** a `tests/browser/fixtures/` mini site served via
  `http.server` with: a login form, a multi-step form, a list+detail page,
  a page with a JS-driven modal, a page that 302-redirects cross-origin.
- **Secrets:** reuse warden's existing test harness; assert `value_literal`
  refusal on credential-shaped fields.
- **OAuth:** mock the provider endpoints (authorization + token); assert
  `web.oauth_login` produces a storage_state blob and writes
  `secret://ctx/<provider>/access_token` with correct TTL.
- **Cross-package:** add `weaver/tests/test_web_session_bridge.py` paralleling
  `test_warden_store_bridge.py` and `test_mail_worker_bridge.py`.

## 13. Milestones

Each milestone is a shippable slice. No milestone breaks the existing
`browser.*` methods.

**M0 — scaffolding (½ day)**
- Create `wayfinder/browser/` with empty modules and dataclasses.
- Add `playwright>=1.45` as an optional extra: `wayfinder[browser]`.
- Wire the test harness (Playwright install in CI).

**M1 — observer (2 days)**
- Write `observer.js` and `observer.py`. Handle algebra + stability tests.
- Snapshot + resolve-handle-to-Locator round-trip.

**M2 — verbs + LocalExecutor (2 days)**
- Implement Session with the full verb surface on LocalExecutor.
- Error taxonomy mapped from Playwright exceptions.
- Diff engine.
- Pass the integration fixtures.

**M3 — warden v2 worker (2 days)**
- Port the existing `browser_worker.py` to a thin shell over
  `wayfinder.browser.Session` running on the `BrowserLoop` thread.
- Add `web.*` policy rules (marked `allow: true` with the same rate limits
  currently on `browser.*`).
- Mark `browser.*` deprecated; those methods proxy to v2 by translating
  selector → synthetic handle via a best-effort locator shim (used only to
  keep older callers working during the cutover).

**M4 — identities & storage_state (2 days)**
- Encrypted identity store at `~/.warden/identities/`.
- `web.open_session` load/save, `web.save_storage`, `web.identity_list/forget`.
- Round-trip test: open → click → save → close → reopen → observe shows
  logged-in state.

**M5 — OAuth (3 days)**
- Provider detection for Microsoft, Google, GitHub.
- `web.oauth_login` with headful default.
- Token capture + storage in weaver secret store.
- Silent refresh path + `web.fetch_token`.

**M6 — weaver CLI (1 day)**
- `weaver web` subcommand group, delegating to `WardenExecutor`.

**M7 — deprecation cleanup (next release)**
- Delete `browser_worker.py` and `browser.*` policy rules.

Total: ~12 working days end-to-end, with M3 producing something
weaver-usable.

## 14. Open questions

1. **Multi-page flows.** A session is one tab today. If a click opens a new
   tab (common for OAuth), we follow it and make the new tab the active page,
   emitting a `popup` event in the next Observation. Do we ever need to
   expose multiple pages as siblings? Deferred — add `page_handle` later if
   a real use case demands it.
2. **Download capture.** Do we want structured download metadata in
   Observations, or keep downloads as a side-channel surfaced via a new
   `web.download_get` method? Lean side-channel for simplicity.
3. **Vision fallback.** When `handles == []` but the page clearly has
   content (e.g., canvas-based UI), should Session auto-screenshot and return
   the image for a VLM to act on? Out of scope for v1; add a
   `Session.screenshot()` result to the Observation only when the caller asks.
4. **Record & replay.** Playwright has a native trace; do we surface
   trace-zip download via an RPC method for debugging? Defer.
5. **Scope-strip during OAuth redirects.** The interactive dance by
   definition crosses origins. `oauth_login` widens `allowed_domains` for
   the duration to the provider's domain + the app domain; we must not
   strip Authorization on those hops. Confirm the route interceptor can be
   swapped per-session-phase.
