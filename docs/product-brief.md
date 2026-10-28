# Product brief: a personal agent harness

**Date:** 2026-10-17  
**POC:** Mihai Chindris  
**TL;DR:** Build a local-first, reviewable harness that helps one person use AI models for recurring work, starting with AdaL's documented SDK. Keep the provider replaceable, evidence visible, account boundaries explicit and every automatic side effect bounded.

## Vision

A daily-use personal harness that can grow from a reliable CLI into a desktop workspace. It should let the user bring their own workflows, skills and integrations, inspect what the agent did, resume work safely and improve through measured feedback. The first release is a learning foundation, not an attempt to recreate AdaL, Accio, LifeOS or a universal “agent operating system.”

## Early users and jobs

- Mihai, working locally on personal projects with AdaL as the first model/runtime provider.
- Later: other users who can bring their own AdaL account/model access and local workspace.
- Initial outcome: run one bounded task in a chosen workspace, see safe progress, approve or deny calls that require a human, resume the AdaL session, and inspect/purge local history.

## Principles

1. Keep user goals, constraints, task criteria and evidence explicit.
2. Show observable progress: current phase, tool call names/statuses, concise output, tests, approval requests, session ID and unresolved items.
3. Do not surface raw private chain-of-thought as a transparency feature. Offer a concise rationale or decision summary when useful, alongside the evidence and artifacts that support it.
4. Separate persistent audit events, AdaL conversation history, curated memory and the context sent to the model.
5. Treat model output, retrieved memory, marketplace content, connectors and skills as untrusted inputs until validated.
6. Make authorization deterministic and least-privilege. A prompt, skill or static scanner is not a sandbox.
7. Add architecture only after repeated use shows a gap; measure quality, safety, latency, cost and review effort.
8. Keep CLI/MCP/UI adapters thin around shared workflows.

## Feature direction (not all in v0.1)

### Models and execution

- AdaL SDK integration first; record SDK/runtime version and selected model when available.
- Safe streaming/progress, cancellation, session resume and same-account checks.
- Later provider adapters only if users need non-AdaL runtimes or local models.

### Connectors and accounts

- A provider-neutral, dynamically versioned connector catalog, initially aiming for parity with integrations users actually need.
- Connections scoped to identity/profile, workspace and provider-account ID. Keep provider authorization secret references in the OS credential vault, never in prompts, project config or committed files.
- On switching profiles, ask whether to copy the *connection setup* into the new profile. Copy labels, provider, endpoint, requested scopes and workspace selection, then reauthorize under the new identity. Never silently copy OAuth tokens/cookies or assume API keys can be reused.
- Separate hosted account connectors from user-configured local/remote MCP servers.
- Show requested scopes, action schemas, source, health, expiration, disconnect and provider-revocation state.

### Skills, marketplace, capabilities and plugins

- Skills are on-demand instructions/references; capabilities are first-party task bundles; plugins can add code, tools, hooks or integrations; marketplaces discover and distribute packages. These have separate manifests and risk labels.
- Version pinning, content hashes, publisher/source metadata, compatibility checks, update diff and rollback.
- Installing a package that can execute code requires review and explicit approval. A package’s instructions do not authorize tools.
- A future user skill marketplace must include trust/provenance and permissions, not only browse/install UX.

### Memory and context

- First maintain a durable event ledger and concise run artifacts. Add personal/project semantic memory only after defining scope, provenance, validity/expiry, review, correction and deletion.
- Distinguish observed facts, user-provided facts, derived summaries, model inferences and unknowns.
- Offer opt-in memory collection and a clear view/edit/export/delete path.
- Never automatically promote a model summary to trusted user identity or a system instruction.

### Evaluation and continuous improvement

- A task suite should check deterministic outcomes, evidence coverage, safety/approval correctness, recovery, and user review time.
- Keep model/runtime, prompt, skill/plugin and dataset versions in experiment metadata.
- Use LLM-as-judge only for criteria deterministic checks cannot measure; retain judge model/prompt and sample-level evidence.

### Subagents

- Delegate only when the task benefits from context isolation or independent work.
- Each child gets a narrow request, bounded tools/time/cost, isolated session/worktree as needed, and a required evidence-bearing result.
- The parent verifies the result before using it. Avoid parallel writes to a shared worktree.
- AdaL CLI’s experimental Engineer mode is not documented as an externally controllable SDK subagent API; first implementation should not promise independent workers until a tested integration boundary exists.

## Explicit non-goals

- Circumventing quotas, subscriptions, app entitlements, licensing or identity controls.
- Copying credentials or provider-side authorization from one user to another.
- Revealing raw private model reasoning.
- Fully autonomous high-impact actions without user-defined limits and approval.
- A server or social marketplace before local single-user workflows are sound.
- A general business benchmark platform or MLOps control plane.

## Success measures for the first useful release

- User can create, inspect, cancel, resume and purge a local job without losing evidence or mixing accounts.
- A process crash or SDK timeout leaves a recoverable, clearly classified status.
- Tool approvals bind to the SDK's exact proposed call; denied/unattended actions never masquerade as success.
- No raw reasoning, raw tool arguments/results, ambient environment or credentials are written to the event ledger.
- Acceptance criteria close only with their named evidence, not because an agent says the task is complete.
- A representative task set reports pass rate, false completion rate, approval errors, retries, token/cost/latency where available, and human review time.

## Questions to decide together

1. What are your first three daily tasks: coding, research, writing, personal admin, browser/desktop work, or a mix?
2. Do you want the harness to be a thin AdaL session/workflow manager, or should it eventually be able to replace the model runtime entirely?
3. What data may become durable memory: explicit “remember this” only, approved summaries, or both? Should memory be separate per project, profile and personal identity?
4. On switching accounts, should the default be “keep separate,” with a one-time copy-and-reauthorize option? Should all connectors be copied or only selected ones?
5. Which actions should require human approval even if AdaL marks them safe: network egress, account changes, publishing/sending, deleting data, spending money, or running code outside the workspace?
6. Should subagents initially be read-only researchers, or may they edit isolated worktrees? What concurrency/cost limits feel acceptable?
7. What does a “great answer” mean to you: factual citations, working code/tests, speed, voice, privacy, low cost, or a weighted combination?
8. Do you want any local model capability in the first version, or should AdaL remain the only model provider until the workflow is proven?
