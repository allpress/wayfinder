# Ideas

Short specs of the distinctive ideas this repo embodies. Not design docs
(those live in [DESIGN.md](../DESIGN.md)) — these are *theses*: the
shape-of-the-problem observations that made the code look the way it does.

Each idea is one file, ≤400 words of prose + a working code reference.

## Index

- [01 — The Agent-Native Browser](01-agent-native-browser.md)
  *Selectors are for humans. AI needs a different surface.* Five primitives
  (handles, closed-set errors, diff-on-action, scope-enforced sessions,
  credential-shape refusal) that reshape Playwright into a surface an LLM
  can drive, observe, and recover on without hallucinating.

- [02 — The Capability-Discovery Wayfinder](02-capability-discovery.md)
  *Before an agent builds a workflow, it needs to know what pieces the
  user already has access to — and someone else has to hold the
  credentials.* A runtime-discovered entitlement map surfaced to the AI
  as capability handles (never tokens) so agents can compose workflows
  across APIs + browsers + enterprise tools safely. Payoff: **boutique
  one-click apps** — one prompt, one standalone desktop app, no AI at
  runtime, no embedded secrets.

- [03 — The Sandboxed Build Loop](03-sandboxed-build-loop.md)
  *Let the agent build wayfinders. Don't let the agent hold the
  credentials those wayfinders use.* A four-verb RPC contract
  (`spawn` / `status` / `events` / `kill`) is the entire surface the
  agent gets. Authentication lives on the warden side; iteration lives
  on the agent side; the event stream is the iteration surface. The
  human doesn't need to approve every try-again.
