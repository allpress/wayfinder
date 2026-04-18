# The Capability-Discovery Wayfinder

> Before an agent builds a workflow, it needs to know what pieces the user
> already has access to. And someone else has to hold the credentials.

The payoff is **boutique one-click apps**: one prompt → a small
desktop app composed from the user's capability map, running on their
existing auth, with no AI in the loop at runtime. See *§ The payoff*
below.

## The observation

Ask an agent to "build me a dashboard that shows cross-team deployment
health" and it has two options: hallucinate the API shape, or ask the user
to paste tokens. Both fail. The agent doesn't know what the user has
access to, what APIs their AD groups authorize, what data already lives
in Salesforce or JIRA or their wiki — and it definitely shouldn't be
holding the credentials needed to find out.

The missing layer isn't the agent and isn't the credential store. It's
the **capability map**: a runtime-discovered picture of what the user
can already reach, surfaced to the agent *without* the tokens attached.

## What the wayfinder does

A capability-discovery wayfinder crawls the user's entitlement graph and
emits a typed capability map:

- **Source-derived entitlements.** Parses the company's own codebase —
  AD-group → API-route manifests, service-registry configs, IAM policy
  files, feature-flag definitions — to enumerate "X group has access to
  Y route".
- **Enterprise-tool scans.** Pulls project/space/board membership from
  Salesforce, JIRA, Confluence, GitLab, dashboards. Not the data inside
  them — just the *presence* of access.
- **Runtime probing.** For each candidate surface, issues a cheap
  authenticated HEAD/identity call through warden to confirm the access
  is live, then records the method + scope.
- **Token stays behind.** The token, cookie, or OAuth refresh secret is
  stored inside warden. The agent gets a capability handle back
  (`capability://gitlab/repo-read/team-platform`) plus the heuristic the
  wayfinder used to verify it — never the credential itself.

The AI calling side sees a structured list of reachable surfaces:

```
{
  "gitlab.company.com": {"scopes": ["read", "write:own-repos"], "groups": [...]},
  "salesforce":         {"objects": ["Opportunity", "Account"], "permissions": {...}},
  "jira":               {"projects": ["PLAT", "INGEST"], "create_issue": true},
  "internal-api:deployments": {"verbs": ["GET /deployments", "GET /services"]},
  ...
}
```

Plus, for each capability, the heuristics used to confirm it — so an
agent constructing a new wayfinder (e.g. "walk the deployments API and
build a dashboard") can reuse those heuristics as the starting point
instead of re-deriving them.

## Why it belongs in the trio

- **Weaver** owns the capability map as a first-class context — right
  next to the RAG index and knowledge graph. Agents query it the same
  way they query anything else the user's work has touched.
- **Warden** holds the real tokens and brokers every call. The agent
  never sees a secret; it just asks warden to act on its behalf against
  a capability handle. Safety stays structural, not procedural.
- **Wayfinder** is the runtime that *finds* the capabilities — and the
  primitive a future agent will spawn to extend the map when something
  new appears. Capabilities are discovered the same way feeds are:
  a supervised walk with halt rules, an event stream, a structured
  report.

## Why it matters

- **Agents build workflows that actually compose.** Given a capability
  map, an agent building "dashboard of my team's PR latency" picks
  GitLab read + JIRA sprint + Slack channel-post from a typed menu
  instead of guessing endpoints.
- **Credentials never leak into the prompt.** The heuristic that proves
  "you can call this" is surfaced; the token that authorises it isn't.
- **Workflow artefacts are portable.** Because capabilities are handles,
  not baked-in tokens, the workflow an agent builds for Doug works for
  Priya tomorrow — warden just resolves the same handle against her
  identity.

## The payoff — boutique one-click apps

The final move is packaging. A user types one prompt — *"daily dashboard
of PR health across my team's repos"* — and the system produces a
**boutique one-click app**: small, single-purpose, composed from the
user's capability map, running on their existing auth, that they click
and run every day with no AI in the loop.

What the user provides: one sentence.
What the agent does: reads the capability map, picks the reachable APIs
and browser surfaces needed, composes a workflow, emits the artefact.
What the packaged app does: runs.

The packaging is what makes the pattern load-bearing:

- **No embedded secrets.** Capability handles resolve at runtime through
  the local broker (warden, in this trio). The compiled artefact holds
  handles, never tokens. Tokens stay where they always did.
- **No LLM at runtime.** The workflow is an ordinary program — a REST
  loop, a browser-session scenario, a cron job — whatever the agent
  composed. No network call to a model provider, no ongoing cost, no
  ongoing dependency on the AI that designed it.
- **Same invariants as the browser layer.** Capabilities are typed and
  scope-checked exactly like browser sessions (`allowed_domains`,
  `ErrCode` taxonomy, diff-on-action). The packaged app inherits those
  invariants — it can't drift onto an endpoint the user never
  authorized.
- **Portable across identities.** Because the app holds capability
  handles, not baked-in tokens, the one Doug built for "my team's PR
  health" resolves cleanly for Priya tomorrow — the broker answers the
  same handle against her identity, her tokens.

Three clean separations:

| Role | Layer | When it runs |
|---|---|---|
| Designer | AI + capability map | Once, at compose time |
| Broker   | Warden (local)       | Every execution, silently |
| Executor | The one-click app    | Every day, alone |

The agent builds the thing, then steps out of the room.

## Where I've built this before

At Lincoln Financial: 85k-LOC platform, 700+ repos parsed for AD-group ↔
API-route linkage, pulled from Salesforce / JIRA / Confluence / GitLab,
surfaced as per-engineer dashboards of reachable APIs and data. The
output surface was **boutique one-click apps** — one prompt → a small
desktop app users ran on their existing auth with no AI at runtime.
That system is the reason I think of the trio the way I do: warden
brokering the credentials, weaver indexing the capability map, and
wayfinder as the spawnable runtime that keeps the map honest. This
open-source trio is the personal-scale distillation; the enterprise
version was first.

## Pointers (when implemented in this repo)

- Discovery runtime: `wayfinder/walkers/capability_discovery.py` (not
  yet built — this doc is the spec).
- Source-parse rules: `weaver/extraction/entitlement_*` (pluggable per
  code-idiom: Spring `@PreAuthorize`, Flask decorators, etc.).
- Capability handle resolution: warden policy block `capability.*` —
  same shape as `web.*` and `secret.*`.

## Open questions

- How aggressive should the probing be? A naïve HEAD on every
  discovered endpoint can look like reconnaissance to a WAF. The
  honest answer is "respect the scope, space out the probes, and
  document what you touched."
- Does the capability map belong per-identity or per-role? Per-identity
  is honest but doesn't share well; per-role is shareable but requires
  trust in the role definition. Default to per-identity, cache by role
  where the user opts in.
- Are capability handles stable across OAuth refreshes? The handle must
  be; the token behind it rotates. This is exactly the invariant the
  browser layer's ``identity`` names already enforce — reuse the
  pattern.
