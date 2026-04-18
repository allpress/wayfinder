# The Agent-Native Browser

> Selectors are for humans. AI needs a different surface.

## The observation

Playwright is brilliant for humans writing deterministic tests. Give it to
an LLM and it degrades fast: the model guesses selectors, swallows stale
page state, loses track of what changed after an action, writes credentials
into whatever input field happens to be visible, and — worst — wanders off
the intended domain without anyone noticing.

Every real fix I've seen layers more prompt scaffolding on top of the same
surface. But the *surface* is the problem. A human writes `page.click("button.primary-cta")` because they've *looked* at the page. An agent hasn't. The
agent needs a surface that makes the thing it can't see — the page's
structure, the effect of its last action, the boundaries of scope and
secrecy — first-class.

## The five primitives

1. **Handles, not selectors.** `observe()` returns a flat list of
   interactables keyed by short opaque `handle` strings (`h7a2`), derived
   deterministically from the element's AX role + accessible name + ordinal.
   The agent never writes CSS or XPath. A handle resolves underneath to a
   Playwright locator built from the accessible role + name. If the handle
   doesn't resolve, the result is an `ErrCode.handle_stale` — a signal to
   re-observe, not to retry.

2. **Closed-set error taxonomy.** Every verb returns
   `{ok: bool, error: ErrCode | None, error_detail: str | None, ...}`.
   `ErrCode` is a string enum of 15 values (`handle_stale`, `scope_violation`,
   `secret_denied`, `oauth_required`, `timeout`, …). Agents dispatch on the
   code, never on the detail. Playwright exceptions are classified into this
   set at the boundary. An LLM writing the retry logic sees a state machine,
   not a stack trace.

3. **Diff-on-action.** Every action result carries an `ObservationDiff`
   describing what changed — handles added / removed / changed, text added,
   network events, console entries, url_changed. Re-observing the whole page
   after every click is expensive in tokens; the diff lets the agent update
   its mental model incrementally.

4. **Scope-enforced sessions.** A session opens with a list of
   `allowed_domains`. Every `goto` is pre-checked. Every in-page request runs
   through a route interceptor that strips `Authorization` and `Cookie` on
   out-of-scope hops, and emits a `scope_strip` network event that the agent
   sees on the next observation. Scope is not a prompt instruction, it's a
   runtime invariant.

5. **Credential-shape refusal.** `fill(handle, value_literal=…)` inspects
   the target handle: if its `name`/`label`/`placeholder` matches a password /
   pin / otp / token / api-key shape, the call is rejected with
   `ErrCode.secret_denied`. To put a real secret into such a field, the
   caller must pass `value_ref="secret://…"` plus a `secret_resolver`
   callable, which lets a guardian process (warden) dereference the secret
   inside its own boundary. An agent with no access to that resolver
   cannot, by construction, fill a credential field with a hallucinated
   string.

## Why it matters

- **Token efficiency.** Handles are ~4 chars. A diff after a click is
  typically a few dozen tokens vs. hundreds for a full re-observation.
- **Recoverability.** `ErrCode` codes are dispatchable. An agent loop that
  handles `handle_stale → observe → retry`, `scope_violation → abort`,
  `oauth_required → hand off` does not need prompt engineering to stay
  coherent.
- **Safety as a structural property, not a behaviour.** Scope and
  credential-shape refusal don't ask the model to be careful. They make the
  unsafe action unrepresentable at the API level.
- **Parity with human intent.** Humans navigate by "the sign-in button on
  the top right"; `get_by_role("button", name="Sign in")` is exactly that.
  Handles are the stable name for the same referent.

## Where it's implemented

- Session + verbs: [`wayfinder/browser/session.py`](../wayfinder/browser/session.py)
- Handle algebra: [`wayfinder/browser/observer.py`](../wayfinder/browser/observer.py) + [`observer.js`](../wayfinder/browser/observer.js)
- Error taxonomy + classifier: [`wayfinder/browser/errors.py`](../wayfinder/browser/errors.py)
- Diff computation: [`wayfinder/browser/diff.py`](../wayfinder/browser/diff.py)
- Credential-shape rule: [`wayfinder/browser/credentials.py`](../wayfinder/browser/credentials.py)
- Full spec: [`DESIGN.md`](../DESIGN.md)
- AI-agent manual: [`wayfinder/browser/AGENTS.md`](../wayfinder/browser/AGENTS.md)

## The interview-friendly summary

> I took Playwright, which is optimised for humans writing imperative test
> scripts, and reshaped it into a surface an LLM can drive: handles instead
> of selectors, a 15-code error enum instead of exception messages, a
> structured diff on every action instead of a full re-parse, `allowed_domains`
> as a runtime invariant, and a refusal to accept literal credentials for
> credential-shaped fields. Safety becomes a property of the API, not a
> behaviour the prompt has to remember.

## Open questions (the honest list)

- **Vision fallback.** When the AX tree is empty (canvas UIs, aggressive
  shadow DOMs), we have no handles. We screenshot-and-defer to a VLM, but
  the integration is ugly. Is there a better primitive?
- **Multi-page flows.** One Session = one tab. OAuth popups and new-tab
  launches mostly work via a `popup` event, but "here are three related
  pages, coordinate across them" is not modelled yet.
- **The diff as the primary artifact.** I'd like to skip `observe()` entirely
  for agents that can stay coherent from diffs alone, but I haven't found
  the sweet spot yet — cold-start observation is still the right call.
