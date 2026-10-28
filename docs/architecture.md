# Architecture foundation

**Date:** 2026-10-17  
**POC:** Mihai Chindris  
**TL;DR:** Start with a small AdaL-SDK adapter and a local, append-only run ledger. Keep task intent, event history, model context, semantic memory and integrations as separate layers. Add features only when daily use exposes a concrete gap.

## Product boundary

The project is a personal, local-first agent harness. It may eventually offer a desktop interface, but no UI shell is selected. AdaL is the initial model/runtime provider through its documented Python SDK. The SDK starts AdaL as a local subprocess and exposes streamed events, persistent sessions, model selection, cancellation and a per-tool permission callback. The harness owns the outer workflow and local run record. [Official AdaL SDK overview](https://docs.sylph.ai/integration/sdk/overview) [API reference](https://docs.sylph.ai/integration/sdk/api-reference/)

The public AdaL integration surface is a client contract, not disclosure of AdaL's private server implementation. We should depend only on its documented SDK interface and version, not copy assumptions about internal desktop/app architecture. The pinned upstream source revision is recorded in `pyproject.toml`; live compatibility is still unverified.

## Layers and ownership

```text
CLI (current) / future UI
        │ RunSpec, status, progress, approval
        ▼
Application workflow
  job lifecycle, resume, budgets, criteria and future subagent policy
        │
        ├── durable local event ledger (SQLite, current)
        ├── context builder (future, bounded view over events/evidence)
        ├── memory provider interface (future; no semantic backend chosen)
        ├── connector / MCP / skill / plugin / capability registries (future)
        └── AdaL SDK adapter (current)
                │ local SDK subprocess / NDJSON
                ▼
         AdaL runtime, models and tools
```

Provider details stop at the adapter. Workflow state remains provider-neutral. No provider-specific protocol belongs in the event reducer or acceptance-criterion model.

### Durable facts versus working context

The event store is the local source for what the harness observed: job creation, turns, tool start/completion summaries, approval decisions, session IDs and terminal outcomes. The SDK conversation is AdaL's working memory. A future context builder will select and bound the events/evidence supplied to a later turn. Semantic memory will be derived from records with explicit provenance, scope, validity and review state; it will not become a substitute for the event history.

This follows useful ideas from HumanLayer's 12-factor guide and later context-engineering writing: own context construction, own outer control flow, persist pause/resume state, and review high-leverage research/plans. The guide itself is principles and teaching code, not an AdaL adapter; its code/content have distinct licenses. [12-factor-agents](https://github.com/humanlayer/12-factor-agents) [Advanced Context Engineering](https://www.humanlayer.dev/blog/advanced-context-engineering) [Harness Engineering](https://www.humanlayer.dev/blog/skill-issue-harness-engineering-for-coding-agents)

## Current data flow

1. `RunSpec` validates task, workspace and optional acceptance criteria.
2. The CLI creates a job and first `job.created` event in one SQLite transaction.
3. The runner calls the AdaL SDK with explicit workspace, model, session ID and `default` permission mode.
4. SDK events become safe progress messages. The event store records structured summaries, not model reasoning text or raw tool inputs/results.
5. A per-call callback denies tool actions needing human approval unless an interactive approval handler allows that exact call. Errors, timeouts and cancellation become explicit outcomes.
6. A later resume reuses the stored AdaL session ID, which is valid only while the same AdaL account is active. The CLI asks the user to attest that account; it cannot verify the account identity itself. If a prior process left the job marked running, the user must separately confirm that process has stopped. This is not a process lock and cannot detect another live runner.

The `thought.delta` event is represented to the user as a generic thinking/progress indicator, not exposed as raw chain-of-thought. Visible transparency should come from the task/plan, tool activity, intermediate artifacts, concise decision summaries, verifier results, and unresolved questions. Private reasoning traces are neither required to show useful progress nor stored by this harness.

AdaL's public docs describe its CLI Engineer feature as experimental and CLI-only. The public SDK reference documents streamed events and persistent sessions but does not define an external API to start, control or join Engineer subagents. The adapter recognizes future `subagent.*` event names generically, but this repository does not create or supervise subagents. An eventual native subagent coordinator should use separate session/worktree boundaries, small contracts, resource limits and verified result handoffs.

## Event storage

SQLite uses a per-job increasing sequence, opaque event ID, UTC time, schema version and finite JSON payload. A transaction allocates sequence and appends an event atomically. Idempotency keys are available for retry-safe writes. SQLite triggers prevent update/delete through ordinary SQL; an explicit purge operation uses a separate authorization row and exact job-ID confirmation. Status is a materialized projection reduced from events, not a second independently edited truth.

Default location: `~/.agent-harness/runs.sqlite3`; the default directory/file are created with owner-only permissions. A custom database path is caller-controlled. This is not tamper-evident or encrypted storage. The default database includes the task prompt, assistant final response, model ID, absolute workspace path and AdaL session ID. It excludes ambient environment variables, thought text, raw tool args and tool results. Error strings can still carry sensitive details. Purge is logical deletion and cannot guarantee removal from SQLite WAL files, OS snapshots, backups or storage media.

No schema migration path is shipped yet beyond rejecting newer schema versions. Schema versions must be bumped deliberately and migrations tested before long-lived local data depends on the format.

## Acceptance criteria and evidence

`AcceptanceCriterion` represents one testable claim with a named verifier. The foundation stores optional criteria when a job is created, but it does **not** yet run the verifier or mark criteria closed. A future verifier result must carry the criterion ID, evidence source/tool, exact command or probe, exit/status and captured evidence reference. Assistant text alone must never close a criterion. Keep derived, inferred, observed and unknown statuses distinct.

The ISA idea from LifeOS is useful here: define what done means as claims that can be falsified, specify a probe/evidence type and keep unresolved questions visible. LifeOS's file format and hooks are Claude-oriented and are not imported. Its public repo is generated from private source; some showcased components (notably Bunker) are explicitly not shipped in the public payload. [ISA system](https://github.com/danielmiessler/LifeOS/blob/main/LifeOS/install/LIFEOS/DOCUMENTATION/ISA/ISASystem.md) [ISA format](https://github.com/danielmiessler/LifeOS/blob/main/LifeOS/install/LIFEOS/DOCUMENTATION/ISA/ISAFormat.md)

## Identity, connectors and accounts

An integration catalog entry is metadata, not an authorized connection. Future types should remain separate:

- connector/provider catalog item and action schema;
- account-scoped connection and its granted scopes;
- workspace/project enablement;
- local MCP server configuration and transport;
- secret reference in OS keychain/credential vault;
- skill, plugin or built-in capability declaration.

Each connection should bind to a harness profile/account and workspace scope. On account switch, offer an opt-in **copy setup/reconnect** flow that copies provider, labels, workspace scope and required scopes, then asks the new account to authorize again. Do not copy OAuth refresh/access tokens, provider cookies, or another person's authorization. An API key may only be re-entered or transferred after the user explicitly confirms they own it and the provider permits reuse. Disconnecting from one account and revoking a provider grant are separate actions.

AdaL's connector docs say its catalog is backend-served, connector lists belong to an AdaL login, multiple accounts per provider are supported, and disconnect does not revoke the provider-side grant. They do not document connector import across AdaL accounts or the exact storage/deletion semantics of credentials. A GitHub feature request should therefore ask about a consented reconnect/setup-template flow, not automatic credential transfer. [AdaL connectors](https://docs.sylph.ai/features/connectors/)

## Skills, marketplaces, capabilities and plugins

Keep these concepts distinct:

- **Skill:** declarative, on-demand instructions/references, often filesystem-based. Must show publisher/source, version or immutable revision, content digest and compatibility. It does not itself grant tool authority.
- **Plugin:** a package that may install skills, tools, hooks, integrations or executable code. Show requested permissions and require review/approval before install or update.
- **Capability:** a first-party/built-in bundle of instructions and tools, activated when needed. Record the activation reason and actual tools made available; avoid unnecessary context loading.
- **Marketplace:** a discovery/catalog service. Discovery is not a trust guarantee; pin versions, verify provenance, present diffs and allow rollback.

AdaL documents filesystem personal/project skills and plugin-installed marketplace skills; its public docs do not specify their trust sandbox or persistence across account changes. LifeOS likewise demonstrates progressive disclosure and warns through its security architecture that prompt instructions and code hooks are separate surfaces. We should not accept executable scripts as harmless markdown.

## Memory-provider decision

No semantic memory backend is selected. OpenBrain is a small, separate macOS configuration/LaunchAgent repo around an external MCP package; it does not currently implement its own memory server. Keep it separate and do not archive or merge it until its user and data needs are known. The existing `loop-harness` is a separate coding-workflow prototype and remains untouched on `main`; it is a source of product history, not the destination repo.

Memory candidates reviewed:

- Hindsight: self-hostable, structured semantic/episodic memory; more dependencies and model-assisted extraction.
- Cognee: graph-oriented ingestion and knowledge-graph memory; useful if repo/project graph retrieval becomes a real need.
- Mem0: memory CRUD/history/scopes/expiration; its open-source defaults call hosted LLM/embedding services unless configured otherwise.
- Graphiti: temporal knowledge graph with episode provenance; requires a graph database and typically extraction models.
- Letta: a stateful agent runtime, not merely a pluggable store; adopting it would replace more of the AdaL execution boundary.

The personal use case needs auditable events, source provenance, account/project isolation, deliberate update/deletion, local operation and manageable backup/restore. There is no fair cross-provider benchmark on all of those needs. First collect representative questions and evaluate candidate backends later. The current SQLite run ledger must remain independent from any semantic-memory service.

### Decided policy: memory (provisional)

- Memory is opt-in per user and per project. Nothing is sent to a memory service unless the user has turned it on.
- The harness may propose memories, but each proposal sits in a review queue with its source event until the user accepts, edits or rejects it. Only accepted entries are written.
- Default provider candidate: Waku Memory (hosted MCP at `https://api.waku.one/mcp`, OAuth 2.1, or a Waku API key as bearer token). It draws on the user's own Waku credits (Free: 100,000 credits per month; Pro: $20 per seat per month for 500,000; write 10 credits, read 1). Waku's terms say prompts and final replies go to its servers and AI providers, so the opt-in prompt must say so. Unverified in this repo: live behavior, and whether credits can be topped up beyond the plans.
- The provider sits behind an interface so a local backend can replace it without touching the ledger.

### Decided policy: subagents (provisional)

- Subagents start read-only: they can read the workspace and report findings, with no write tools.
- Write access is a later, explicit grant. A writing subagent works in its own git worktree on its own branch, never in the main checkout, and its result returns as a diff for review or verified merge.
- Parallel writers need non-overlapping worktrees and a budget on each.

## Research-to-product extraction

- **HumanLayer 12-factor / Dex Horthy:** adapt the principles (own context and control flow, pause/resume, small focused delegation, deterministic back-pressure, review high-leverage artifacts), not the tutorial framework or licensed text. AgentLayer's concrete state/hooks/approval ideas are useful, but it is a separate Vercel AI SDK/Bun runtime and is not imported.
- **Octavian's Autonomous-Coding-Agent:** learn from its separate run state, patch/test/reflection trace and circuit breaker; do not copy unlicensed code or mistake a copied workspace/static blocklist for a sandbox.
- **Octavian's llm-memory-system:** its key lessons are explicit context budgets and exact-token prefix checks for KV-cache reuse. The small repo has no tests and no license; no model cache or code is reused.
- **LUML:** a local experiment/eval tracker linking run IDs, traces, metrics and human annotations could inform a later comparison UI. Prisma's autonomous branch-per-experiment optimizer is MLOps-specific and its bypass-permission behavior is not a safe default. The entire platform is not a dependency.
- **LifeOS:** borrow only evidence-bearing done criteria, curated local memory with provenance, deterministic policy gates and append-only local observability. Do not install its Claude-specific hooks/Pulse or depend on private Bunker code.

All research is summarized in `docs/research-notes.md` with primary-source citations and known limitations.

## Non-goals for the first milestone

- bypassing provider quotas, account limits or licensing controls;
- storing or copying a user's authentication tokens across accounts;
- displaying private chain-of-thought;
- an automatic free-running agent loop with unbounded tools;
- executing arbitrary repository code without a declared isolation boundary;
- replacing AdaL or cloning its proprietary internal architecture;
- a desktop shell, marketplace, OAuth connector service or multi-user server before the single-user local workflow is proven.
