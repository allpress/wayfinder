# wayfinder.browser — AI agent quick reference

> Read this first. Everything an LLM-driven caller needs to drive the browser
> without re-reading DESIGN.md or spelunking the source.

## One-sentence mental model

The page is a flat list of **interactables keyed by handles**; the AI calls
`observe()` to get handles, then drives verbs (`click`, `fill`, `goto`, etc.)
by handle — not by CSS selector.

## The Session lifecycle

```python
from wayfinder.browser import Session, LocalExecutor

s = Session(LocalExecutor())
s.open(identity="anthropic-careers", allowed_domains=["anthropic.com"])
s.goto("https://www.anthropic.com/careers")
obs = s.observe()
# obs.handles -> list[Interactable], obs.text_blocks -> list[TextBlock]
# obs.landmarks -> main/nav/banner regions, obs.fingerprint -> snapshot hash

target = next(h for h in obs.handles if h.role == "link" and "engineer" in h.name.lower())
s.click(target.handle)          # triggers navigation → old handles now stale
obs = s.observe()               # REQUIRED after navigation before next verb
s.close()
```

Always `close()`; the LocalExecutor owns a background Chromium thread.

## Rules that will bite you

1. **`allowed_domains` is required and scoped.** Every `goto(url)` and every
   in-page request is checked against it. Cross-origin nav returns
   `ErrCode.scope_violation`. Out-of-scope requests have `Authorization` and
   `Cookie` stripped at the route interceptor. Always include the full host
   you intend to visit (no wildcards — subdomain match is implicit:
   `anthropic.com` matches `job-boards.anthropic.com`).

2. **Handles are snapshot-scoped.** After any action that navigates or mutates
   the DOM, the old `Observation` is stale. Verbs return `ActResult` — if
   `result.navigated` is `True` or `result.error == ErrCode.handle_stale`,
   re-`observe()` before doing anything else.

3. **`fill()` refuses literals for credential-shaped fields.** If the handle's
   `name`/`label`/`placeholder` looks like password/pin/otp/token/api-key,
   `value_literal=` is rejected with `ErrCode.secret_denied`. Use `value_ref=`
   + a `secret_resolver` callable.

4. **Results never raise for domain errors.** `ok: bool` + closed-set
   `ErrCode` enum. Only programmer errors (bad types, etc.) raise.

5. **One Session = one tab.** Not re-entrant. Don't call verbs from multiple
   threads against the same Session.

## Verbs (all sync, all return structured results)

| Verb | Returns | Notes |
|---|---|---|
| `open(identity, allowed_domains, headless=True, load_storage=True)` | `OpenResult` | Loads storage_state from the IdentityStore if one is attached. |
| `goto(url, wait="domcontentloaded", timeout_s=30)` | `ActResult` | Scope-checked. |
| `back()` / `reload()` | `ActResult` | |
| `observe(viewport_only=True, include_screenshot=False)` | `Observation` | `viewport_only=False` unlocks the 1500-handle cap but costs more. |
| `click(handle, modifiers=(), timeout_s=10)` | `ActResult` | |
| `fill(handle, *, value_ref=..., value_literal=..., secret_resolver=..., timeout_s=10)` | `ActResult` | Pass exactly one of `value_ref` / `value_literal`. |
| `select(handle, option, timeout_s=10)` | `ActResult` | Option text or value. |
| `check(handle, state=True, timeout_s=10)` | `ActResult` | |
| `press(handle_or_None, key, timeout_s=10)` | `ActResult` | Handle=None → page-level press. |
| `submit(form_handle, timeout_s=10)` | `ActResult` | Falls back to pressing Enter if not a form. |
| `wait_for(url_contains=..., text_contains=..., handle_role=..., handle_name=..., timeout_s=10)` | `ActResult` | Needs at least one condition. |
| `extract_text(handle, max_chars=2000)` | `ExtractResult` | |
| `extract_attribute(handle, name)` | `ExtractResult` | |
| `screenshot(full_page=False)` | `ScreenshotResult` | |
| `recent_requests(host_contains=..., limit=20)` | `list[NetEvent]` | Observed traffic tap. |
| `save_storage()` | `SaveResult` | Persists cookies/localStorage to the attached IdentityStore. |
| `close()` | `None` | Always call. |

## Error taxonomy (`ErrCode`)

Dispatch on these — never on `error_detail` text.

`handle_stale` `handle_not_found` `not_visible` `disabled` `timeout`
`scope_violation` `navigation_blocked` `secret_denied` `secret_unknown`
`identity_locked` `playwright_error` `network_dead` `oauth_required`
`session_unknown` `bad_argument`

Classifier lives in `errors.classify_exception`; maps Playwright exceptions
onto these.

## Executors: LocalExecutor vs WardenWebClient

| You are… | Use |
|---|---|
| A local script / weaver CLI running in-process | `Session(LocalExecutor())` |
| A sandboxed agent talking to warden over RPC | `WardenWebClient(client, identity=..., allowed_domains=[...])` |

`WardenWebClient` is an RPC shim with the same verb names as `Session`. The
real `Session` lives inside warden's `BrowserV2Worker` (see
`warden/warden/workers/browser_v2.py`). Policy allow-list for `web.*`
is in `warden/config/default_policy.yaml`.

`WardenExecutor` exists but is a stub — do not plug it into `Session()`
and expect it to work remotely. Use `WardenWebClient` instead.

## Identities & storage_state

```python
from pathlib import Path
from wayfinder.browser import IdentityStore, Session, LocalExecutor

store = IdentityStore(root=Path.home() / ".wayfinder" / "identities",
                      key=os.urandom(32))   # 32 bytes, AES-256-GCM

s = Session(LocalExecutor(), store=store)
s.open(identity="anthropic-careers", allowed_domains=["anthropic.com"])
# ... log in, browse ...
s.save_storage()        # encrypts + persists cookies/localStorage
s.close()

# Later — same identity, cookies restored automatically
s = Session(LocalExecutor(), store=store)
s.open(identity="anthropic-careers", allowed_domains=["anthropic.com"])
```

Inside warden, the key is derived from the capability master via HKDF;
callers never see it. Identity files land at `~/.warden/identities/`.

## OAuth flow (for protected identities)

1. Caller detects login via `Observation.login_hint` (set when
   `observe()` fingerprints a provider's authorize page).
2. Caller invokes `web.oauth_login` RPC (headful) — user completes dance.
3. Warden captures tokens from final redirect, writes
   `secret://<context>/<provider>/access_token` into weaver's secret store,
   persists storage_state.
4. Subsequent `open_session(identity=…)` restores state silently.
5. REST callers that don't want a browser use `web.fetch_token(identity,
   scope=…)` — refresh-token flow handled internally.

Provider detection + refresh live in `oauth.py` (pure, testable, no browser).

## Observation model

```python
obs.url               # str — current URL
obs.title             # str — <title>
obs.handles           # list[Interactable] — flat, viewport-first
obs.landmarks         # list[Landmark] — main/nav/banner/contentinfo
obs.text_blocks       # list[TextBlock] — readable text, non-interactive
obs.console_tail      # last ~50 console lines
obs.network_tail      # last ~50 request/response/scope_strip events
obs.fingerprint       # str — stable hash of handles+landmarks
obs.truncated         # bool — True if handle cap was hit
obs.login_hint        # LoginHint | None — provider + reason if login wall
obs.screenshot_b64    # str | None — only when include_screenshot=True
```

Each `Interactable` carries: `handle`, `role`, `name`, `value`, `label`,
`placeholder`, `required`, `disabled`, `checked`, `editable`, `in_form`
(handle of owning form), `landmark`, `ordinal`, `bbox`.

Use `obs.by_handle("h7a2")` to look one up.

## Diffs

Every `ActResult` after the first action carries `.diff: ObservationDiff`:

```python
diff.url_changed          # bool
diff.added_handles        # list[str]  -- new handles since last observation
diff.removed_handles      # list[str]
diff.changed_handles      # list[str]  -- role/name/value change
diff.added_text           # list[str]  -- new text_block handles
diff.new_network          # list[NetEvent]
diff.new_console          # list[str]
```

Callers should consume diffs instead of re-parsing the full observation.

## Testing

Fixtures under `tests/browser/fixtures/` (static HTML: `form.html`,
`login.html`, `list.html`, `modal.html`, `dynamic.html`). Serve with
`http.server` via `tests/browser/conftest.py`. 102 tests green.

When adding functionality: write the test against a fixture page first,
assert on diff + ErrCode, never on free text.

## Quick recipes

### Scrape a list of jobs

```python
s = Session(LocalExecutor())
s.open(identity="anthropic-research", allowed_domains=["anthropic.com"])
s.goto("https://www.anthropic.com/careers")
obs = s.observe(viewport_only=False)
jobs = [t.text for t in obs.text_blocks if t.tag in ("h2", "h3")]
s.close()
```

### Fill a form that stops at OAuth

```python
s.goto(login_url)
obs = s.observe()
if obs.login_hint:
    # hand off to oauth flow; don't try to fill username/password
    raise RuntimeError(f"need oauth: {obs.login_hint.provider}")
```

### Use a secret without letting the AI see it

```python
def resolver(ref: str) -> str:
    # your side: look up in your secret store; return literal
    return my_store[ref]

s.fill(handle, value_ref="secret://work/google/password", secret_resolver=resolver)
```

## When to reach for this vs the HTTP walker

| Need | Use |
|---|---|
| Plain feed/HTTP fetch, no JS, no auth | `wayfinder.walk()` (resilient HTTP walker) |
| JS-rendered page, form fill, auth, multi-step flow | `wayfinder.browser.Session` |

They share the package but are independent — `walk()` is sync HTTP with
per-host circuit breakers; `Session` is Playwright under an AX-handle skin.

## Known gotchas in the wild

- **Cloudflare JS challenges** (e.g. `anthropic.com/careers/jobs`) serve a
  `Just a moment…` page before the real content. `observe()` will show the
  challenge, not the target. Prefer the site's public data endpoint when one
  exists — Anthropic, for instance, has a Greenhouse API at
  `boards-api.greenhouse.io/v1/boards/anthropic/jobs` that's unauthenticated
  and returns the full job list + custom questions as JSON.
- **SPA-style nav without a URL change.** Some pages swap the DOM without
  navigating. `ActResult.navigated` will be `False` even though the page
  looks different. Rely on `diff.added_handles` / `diff.removed_handles`
  instead of `navigated` to decide whether to re-observe.
- **Viewport truncation.** `observe(viewport_only=True)` (the default) caps
  handles at ~400 and omits off-screen content. Pass `viewport_only=False`
  for scrapes; expect the 1500-handle cap.

## Pointers

- Full spec: [DESIGN.md](../../DESIGN.md)
- Session source: [session.py](session.py)
- Warden worker: `warden/warden/workers/browser_v2.py`
- Policy allow-list: `warden/config/default_policy.yaml` (the `web.*` block)
