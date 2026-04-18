# The Sandboxed Build Loop

> Let the agent *build* wayfinders. Don't let the agent *hold* the
> credentials those wayfinders use.

## The observation

Most agent architectures collapse two jobs that shouldn't be collapsed:

1. **Designing and iterating on a tool** — writing code, spawning it,
   reading what happened, fixing it, spawning it again.
2. **Executing that tool with real credentials** — calling the API,
   driving the browser, opening the cookie jar.

If the same process does both, the agent ends up either (a) holding
tokens it shouldn't hold or (b) needing a human in the loop to approve
every secure-state transition. Neither is tenable when the point is
*agents that iterate on real systems*.

## The split

A wayfinder is **a process that runs authenticated, without the agent
having access to the authentication**.

- The **wayfinder** runs in its own process. It holds the live
  Playwright context, the session cookies, the in-memory token the
  OIDC refresh just minted. Its `SecretResolver` comes from warden;
  the agent never has that object.
- The **agent** (sandboxed — Claude, weaver, whoever) writes the
  wayfinder's code and launches it through a closed RPC contract. The
  contract is the agent's *only* way to touch the running process.

The contract is four verbs and nothing else:

| RPC | What it does |
|---|---|
| `wayfinder.spawn(type, inputs)`   | Start a run. Returns a spawn_id. |
| `wayfinder.status(spawn_id)`      | Running / completed / failed. |
| `wayfinder.events(spawn_id, since)` | Replay the structured event stream, scrubbed by warden's redactor. |
| `wayfinder.kill(spawn_id)`        | Stop it. |

No "read this token", no "call this API directly", no back door. The
agent can't escape the contract because there's nothing else to call.

## Why that's enough to iterate

The feedback loop looks like this:

```
agent  ──(writes wayfinder code)──▶ repo
agent  ──(spawn)──▶  warden  ──(run with secrets)──▶  wayfinder
agent  ◀──(events, scrubbed)──  warden  ◀──(events)──  wayfinder
agent  ──(refine code)──▶ repo
agent  ──(spawn again)──▶ ...
```

Every piece of information the agent needs to iterate — what happened,
in what order, what failed, what the structured error was — flows back
through `events` and `status`. What the agent doesn't get: the token,
the cookie, the page that contained the credential, the raw request.
The redactor between `events` (inside warden) and the agent strips
registered literals on the way out.

The agent can iterate *freely*. It doesn't need permission for each
try-again. The human isn't in the loop on every secure-state
transition; the sandbox is.

## Why it's structural, not procedural

This isn't "we told the agent not to touch credentials." The agent
*literally cannot* touch them — nothing in its address space resolves
to a token. The RPC surface is the only bridge, and the bridge
deliberately narrow.

That means the safety property survives:

- A prompt-injected agent still can't exfiltrate credentials. It can
  at worst produce a wayfinder spec that does something silly; warden
  rejects anything outside policy; the event stream shows what was
  attempted.
- A hallucinating agent still can't write a wayfinder that bypasses
  scope. `allowed_domains`, `ErrCode.scope_violation`, and the route
  interceptor live on the authenticated side of the wall.
- A compromised agent still can't log the token somewhere. The token
  isn't available to log.

Safety stays a property of the architecture, not of the model's good
behaviour on the day it was asked.

## The two-step watcher

Warden sits in the middle of every privileged call as a **two-step
supervisor**:

1. Agent → warden: "please do X." Warden validates against policy,
   checks the capability token, notes the call in the audit log.
2. Warden → worker: runs the privileged work with real secrets,
   scrubs the result, returns it.

Both steps are observable. Either step can refuse. Neither step trusts
the other. The whole architecture is designed to stay honest when any
one layer misbehaves.

## Where this shows up in the trio

- `wayfinder/base.py` — the `Wayfinder` protocol declares the
  `SecretResolver` as an *injected* parameter. The worker can only use
  whatever resolver the runtime gave it.
- `warden/workers/` — every privileged worker (mail, browser v2,
  capability discovery, submitter) runs inside warden and holds its
  own secrets; the resolver never crosses the RPC boundary.
- `wayfinder.spawn` / `.status` / `.events` / `.kill` RPC methods —
  the closed surface the agent gets.
- The redactor at the audit boundary — any literal registered as a
  secret gets masked out of events before they're visible to the
  caller.

## Why it matters for real workflows

The payoff is simple: **I can ask Claude to build a new wayfinder and
not also give Claude my cookies.** Claude drafts the code, calls
`spawn`, reads the events, sees what went wrong, edits the code, calls
`spawn` again. Thirty iterations in, it has a working wayfinder. I
never had to restart the secure process, hand it a token, or watch
what it was doing with one. The build loop ran on the AI side; the
execution ran on the warden side; the RPC contract carried just enough
information in each direction for both to do their jobs.

That's the whole pattern. It's the reason I think of wayfinder and
warden as two halves of one thing.
