# Wayfinder

> The web agent. Two layers under one roof: a **resilient HTTP walker** for
> feeds and bulk fetching, and an **AI-facing browser Session** that turns
> Playwright into an accessibility-tree + handle model an LLM can drive.

Used by Weaver's aggregator (HTTP walker) and by any caller that needs to
automate a real web page — form fills, authenticated flows, AI-directed
traversal — without writing CSS selectors or sleeps (browser Session).

## Two APIs

| Layer | Use it for | Entry point |
|---|---|---|
| **HTTP walker** | Feeds, JSON endpoints, batch fetch with policy/backoff | `from wayfinder import walk, WalkTarget, FetchPolicy` |
| **Browser Session** | JS-rendered pages, forms, auth flows, AI-driven navigation | `from wayfinder.browser import Session, LocalExecutor` |

For the AI-facing browser manual (handles, scope rules, ErrCode taxonomy,
OAuth flow), see [`wayfinder/browser/AGENTS.md`](wayfinder/browser/AGENTS.md).
The full design spec is [DESIGN.md](DESIGN.md). For the *thesis* behind
the browser layer — why an agent-native surface is different from a human
one — see [`ideas/01-agent-native-browser.md`](ideas/01-agent-native-browser.md).

## Browser Session — 30-second tour

```python
from wayfinder.browser import Session, LocalExecutor

s = Session(LocalExecutor())
s.open(identity="anthropic-careers", allowed_domains=["anthropic.com"])
s.goto("https://www.anthropic.com/careers")

obs = s.observe()                # flat list of interactable handles
link = next(h for h in obs.handles
            if h.role == "link" and "engineer" in h.name.lower())
s.click(link.handle)             # diff returned on ActResult

obs = s.observe()                # re-observe after navigation
s.close()
```

Key properties:

- **Handles, not selectors.** The AI picks from `observe().handles`; every
  handle resolves to a Playwright locator via accessible role + name + ordinal.
- **Scope-enforced.** `allowed_domains` is checked on every `goto` and every
  in-page request; out-of-scope requests have Authorization/Cookie stripped.
- **Closed-set errors.** Every verb returns `ok: bool` + `ErrCode | None`.
  Callers dispatch on the code — never raises for domain errors.
- **Diff-on-action.** Each `ActResult` carries an `ObservationDiff` so you
  don't re-parse a 400-handle page to learn what changed.
- **Encrypted persistent identities.** Optional `IdentityStore` holds
  AES-GCM-encrypted `storage_state` blobs keyed by identity name.
- **Secrets never leave warden.** `fill(handle, value_ref="secret://…")`
  dereferences inside a `secret_resolver` callable; credential-shaped fields
  refuse `value_literal`.

## HTTP walker — the original primitive

```python
from wayfinder import FetchPolicy, WalkTarget, walk

report = walk(
    [
        WalkTarget(url="https://martinfowler.com/feed.atom"),
        WalkTarget(url="https://huggingface.co/blog/feed.xml"),
        WalkTarget(url="https://lilianweng.github.io/index.xml"),
    ],
    FetchPolicy(),            # defaults halt on 429/503
    on_event=lambda e: print(e.pretty()),
)

if report.halted:
    print(f"stopped: {report.halt_reason}")
else:
    for url, evt in report.successes.items():
        process(evt.body, url=url, status=evt.status)
```

What you get: per-host circuit breaker (N consecutive failures → skip host),
`Retry-After` capture on 429/503 halts, exponential backoff, network-free
tests via an injected `HttpClient` Protocol.

## Install

```bash
pip install -e path/to/wayfinder              # HTTP walker only
pip install -e "path/to/wayfinder[browser]"   # + Playwright
playwright install chromium                                        # one-time
pytest                                                             # 102 tests
```

## Integration points

- **[Weaver](../weaver/)** — orchestrator + aggregator. Uses the HTTP walker
  through `weaver/aggregator/`. Browser Session is reachable via the new
  `weaver web` CLI and the `web` skill.
- **[Warden](../warden/)** — guardian daemon. Hosts a long-lived
  `BrowserV2Worker` that wraps `Session` behind the `web.*` RPC namespace.
  Holds encrypted identities under `~/.warden/identities/`. Policy at
  `warden/config/default_policy.yaml`.
- **`WardenWebClient`** — drop-in shim with the same verb names as `Session`
  but routes through warden's RPC. Swap `LocalExecutor` for this when running
  in a sandbox that isn't allowed to own a Playwright process.
