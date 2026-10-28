# Research notes: harness design sources

**Date:** 2026-10-17  
**POC:** Mihai Chindris  
**TL;DR:** Public documentation suggests a small, evidence-first AdaL adapter plus local, replaceable data boundaries. None of the reviewed products is a universal winner; the prototypes and marketing pages do not prove production safety or performance.

## How to read these notes

The recommendations below are my synthesis of the cited public sources, not direct inspection of private product backends or an independent benchmark. Treat upstream code, instructions and marketplace packages as untrusted input. No external repository code has been copied into this project. HumanLayer's 12-factor repository distinguishes Apache-2.0 code from CC BY-SA 4.0 prose/images; this file paraphrases principles and links to sources rather than reproducing their content.

## AdaL: the first execution provider

AdaL's documented Python SDK embeds its agent runtime through a local subprocess and stdio NDJSON, and provides streaming, session resume, programmatic tool approvals and model selection. [1] [SDK overview](https://docs.sylph.ai/integration/sdk/overview) [API reference](https://docs.sylph.ai/integration/sdk/api-reference/)

The public SDK surface includes `AdalAgentClient`, `AdalAgentOptions`, `query`, per-tool permission callbacks, cancellation, session/model controls and typed SDK errors. [2] The source adapter should depend on those documented types only. [API reference](https://docs.sylph.ai/integration/sdk/api-reference/)

The SDK approval callback runs before every tool call when configured; it can allow, deny or modify arguments, and callback timeout/error denies the call. [3] The project uses the default permission mode and denies calls requiring a person when no approval handler answers. The callback must be treated as policy and audit boundary, not cosmetic UI.

The persistent SDK client supports multiple queries, explicit session resume, model switching, cancellation and a context-managed subprocess lifecycle. [4] A saved session ID must be associated with its AdaL identity/profile in a future version. Current docs do not specify cross-account session behavior.

AdaL's Engineer feature is documented as experimental and CLI-only, with workers in separate worktrees; the page does not document an external SDK API for controlling or observing those workers. [5] The initial harness reports subagent events if available but does not claim it can launch subagents through the SDK. [AdaL Engineer docs](https://docs.sylph.ai/features/engineer-mode)

## AdaL connectors, scopes and identity

AdaL documents connectors as attached to an AdaL login, shared across surfaces for that login, and capable of multiple accounts per provider; disconnect removes the connection from AdaL but does not revoke the provider grant. [6] [Connectors docs](https://docs.sylph.ai/features/connectors/)

AdaL's 24-tool directory distinguishes local from remote services and documents a mix of OAuth, API-key, existing-account-permission, read-only and confirmation-gated integrations. [7] The public connector docs do not specify a credential export, account-to-account import, or exact secret retention/deletion behavior. A safe harness feature is to copy only connection metadata (provider, label, scopes, workspace selection) and reauthorize under the destination identity. Never silently move tokens, browser cookies or someone else's provider authorization.

This is also a reasonable feature request for AdaL: ask for an opt-in “copy connector setup and reconnect” wizard on account switch, with explicit warnings about scopes and provider-side revocation. Avoid requesting automatic credential migration. AdaL's docs establish account-bound connections, but do not tell us how logout changes the backend connection list or token lifecycle.

## HumanLayer: principles, not a second runtime

HumanLayer's 12-factor guide recommends modular agent engineering patterns, including explicit control flow, context ownership, pause/resume and structured tool outcomes; prefetching appears as honorable advice, not a core factor. [8] [12-factor-agents](https://github.com/humanlayer/12-factor-agents)

Dex Horthy's 2025 context-engineering article emphasizes correctness and completeness of context, intentional compaction, focused research/plan/implementation phases, subagents for context isolation, and human review of high-leverage research and plans. [9] His performance stories are practitioner experience, not controlled benchmark evidence.

HumanLayer's later harness-engineering article recommends starting simply, adding configuration only in response to observed failures, and using verification/back-pressure, skills, tools and subagents deliberately. [10] [Harness Engineering](https://www.humanlayer.dev/blog/skill-issue-harness-engineering-for-coding-agents)

The back-pressure article recommends surfacing concise success signals while retaining full diagnostic output for failures, rather than truncating output blindly. [11]

HumanLayer's 2026 resource roundup says its guidance moved from RPI toward QRSPI after the team reported that a long-running unattended coding factory produced unmaintainable slop; the page does not expand QRSPI or provide a transcript. [12] This is a reason to keep humans in charge of high-leverage decisions, not a universal proof that autonomous loops cannot work.

HumanLayer AgentLayer demonstrates serializable resumable agent state, approval hooks, subagents, stop conditions and streamed events, but is built on Vercel AI SDK rather than AdaL's SDK. [13] We borrow the boundary ideas, not the dependency or framework.

## LifeOS: useful concepts, not a product dependency

LifeOS Algorithm v8.20.2 frames a run as a claim-and-evidence process against an Ideal State Artifact, with claims closed by modality-appropriate tool evidence and explicit handling for deferred verification. [14]

LifeOS ISA v2.21.0 is a Markdown artifact with YAML frontmatter and optional ordered sections; atomic claims and verifiers are useful design inspiration, but the project's own neighboring ISA system doc describes an older format. [15] The foundation uses a small in-code acceptance criterion, not the LifeOS format.

The public Cortex contract describes local file-backed records with stable IDs, source paths, metadata-only search results and explicit full-record retrieval; it also documents limits on the coverage of privacy sanitization. [16] For this harness, raw events, derived memories and prompt context should remain separate.

LifeOS hooks run on Claude Code's native event infrastructure, with some blocking and many advisory/asynchronous behaviors; the public hook system is Claude-specific. [17] There is no AdaL adapter in the public source. Use SDK-native callbacks instead.

LifeOS data classification describes deterministic routing ceilings and also lists what its scanner cannot inspect or control, including some runtime file contents and non-inference egress. [18] Static scanning should never be presented as comprehensive DLP or a sandbox.

LifeOS observability is a local file-backed event pipeline read by Pulse, but its docs note that some events are absent and some sources/routes are retired. [19] This inspires local events; Pulse itself is an unnecessary daemon/UI dependency for the current CLI.

LifeOS's published eval guide is explicitly historical and points to a separate assertion-first Evals skill as its current home. [20] No standalone public “Assay” package was verified in the reviewed payload.

## Memory candidates

Hindsight is a self-hostable memory engine with retain/recall/reflect and several memory categories; its public repo describes production PostgreSQL/pgvector or Oracle AI Database backends, with lighter local options. [21] It is a future semantic-memory candidate, not the current event store.

Cognee describes a memory layer with graph-oriented ingestion and remember/recall/improve/forget operations, exposed through Python, CLI, REST and MCP surfaces. [22]

Mem0's open-source library and self-hosted server use different defaults, and the documented defaults include OpenAI model/embedding services unless reconfigured. [23] “Self-hosted” therefore does not automatically mean “all inference local.”

Graphiti is a temporal graph framework with episode provenance and requires users to operate a supported graph database; its README describes OpenAI as the default inference/embedding provider. [24]

Letta self-hosting includes an agent runtime and persistent agent state, rather than providing only a memory backend. [25] It is a larger runtime choice and should only be considered if replacing the AdaL execution layer is intended.

LongMemEval evaluates information extraction, multi-session reasoning, temporal reasoning, knowledge updates and abstention across 500 curated questions. [26] It is a useful source for evaluation dimensions, not evidence that any one vendor is best for this harness.

No apples-to-apples comparison has established a universal winner. Start with a harness-owned event store; prototype semantic memory only after a small, labeled corpus exposes retrieval failures. Preserve source IDs, scope, observed time, valid time, confidence/status and explicit deletion/retention behavior.

## Other systems: patterns to adapt

Accio's public Agent Desktop Harness describes consistent CLI/MCP/Python/Robot Framework contracts over a common runtime and event model, but targets an alpha visible Windows desktop runtime and is not evidence of the proprietary Accio desktop app internals.

Commerce Agent Bench emphasizes pinned task definitions, harness/runtime versions and run metadata, and warns that displayed model names alone do not make results comparable. These reproducibility practices are useful for our own evals, not a reason to install the benchmark suite.

LUML's Python SDK/Flow area offers local experiment tracking, traces, evaluation examples and annotations, while Prisma targets metric-driven ML experiments in isolated Git branches. The former is a useful future run-comparison reference; the latter and model deployment/registry platform are MLOps-specific overkill for v0.1.

Octavian's Autonomous-Coding-Agent is a narrow Python/pytest repair prototype with workspace copies, SQLite plus JSONL traces and patch/test/reflection steps; its README explicitly says it has no Docker/VM sandbox. [27] No license was found in its public tree; do not copy its code.

Octavian's memory prototype uses linear dot-product retrieval over normalized embeddings, returns a fixed top-three result, and filters by a threshold. [28] This is a useful teaching example, not production code; the public repo has no tests or license.

## Archive, integration and account-switch decisions

- Keep `loop-harness` and `OpenBrain` as separate historical projects for now. The existing `loop-harness` is a coding workflow prototype; OpenBrain is primarily configuration for an external MCP memory package. Neither needs to be archived or merged before this new repository proves its direction.
- Keep LifeOS, HumanLayer, LUML and Octavian as references. Do not depend on their runtimes in the first milestone.
- Ask AdaL about a consented “copy setup and reconnect” flow for connectors across AdaL accounts. Do not ask for silent token migration. A new harness should bind connections and sessions to explicit account profiles, and a switch should require fresh provider authorization.

## Source index

Primary URLs are attached to citations above. Main source pages include:

- AdaL: https://docs.sylph.ai/integration/sdk/overview · https://docs.sylph.ai/integration/sdk/api-reference/ · https://docs.sylph.ai/features/connectors/ · https://docs.sylph.ai/features/engineer-mode
- HumanLayer: https://github.com/humanlayer/12-factor-agents · https://www.humanlayer.dev/blog/advanced-context-engineering · https://www.humanlayer.dev/blog/skill-issue-harness-engineering-for-coding-agents
- LifeOS: https://github.com/danielmiessler/LifeOS
- Memory projects: https://github.com/vectorize-io/hindsight · https://docs.cognee.ai/ · https://docs.mem0.ai/ · https://github.com/getzep/graphiti · https://docs.letta.com/
- LUML: https://github.com/luml-ai/luml
- Octavian prototypes: https://github.com/OctavianBoji/Autonomous-Coding-Agent · https://github.com/OctavianBoji/llm-memory-system
- Accio public projects: https://github.com/Accio-org/CommerceAgentBench · https://github.com/Accio-org/accio-cu · https://github.com/Accio-org/agent-desktop-harness

## References

- [1] AdaL Agent SDK | https://docs.sylph.ai/integration/sdk/overview
- [2] API Reference | AdaL | https://docs.sylph.ai/integration/sdk/api-reference/
- [3] Permissions | AdaL | https://docs.sylph.ai/integration/sdk/permissions/
- [4] Client API | AdaL | https://docs.sylph.ai/integration/sdk/client/
- [5] AdaL Engineer (Experimental) | AdaL | https://docs.sylph.ai/features/engineer-mode
- [6] Connectors | AdaL | https://docs.sylph.ai/features/connectors/
- [7] Tools Directory | AdaL | https://docs.sylph.ai/features/tools
- [8] 12-Factor Agents – Principles for Building Reliable LLM Applications | https://github.com/humanlayer/12-factor-agents/blob/main/README.md
- [9] Advanced Context Engineering for Coding Agents | https://www.humanlayer.dev/blog/advanced-context-engineering
- [10] Skill Issue: Harness Engineering for Coding Agents | https://www.humanlayer.dev/blog/skill-issue-harness-engineering-for-coding-agents
- [11] Context-Efficient Backpressure for Coding Agents | https://www.humanlayer.dev/blog/context-efficient-backpressure
- [12] HumanLayer - All Resources | https://www.humanlayer.dev/blog/humanlayer-resources
- [13] AgentLayer Core README | https://github.com/humanlayer/agentlayer/blob/main/packages/agentlayer-core/README.md
- [14] The Algorithm 8.20.2 | https://raw.githubusercontent.com/danielmiessler/LifeOS/main/LifeOS/install/LIFEOS/ALGORITHM/v8.20.2.md
- [15] LifeOS ISA Format Specification v2.21.0 (Algorithm v8.20.0) | https://raw.githubusercontent.com/danielmiessler/LifeOS/main/LifeOS/install/LIFEOS/DOCUMENTATION/ISA/ISAFormat.md
- [16] Cortex Local Contract, Privacy Boundary, and Evidence Gates | https://raw.githubusercontent.com/danielmiessler/LifeOS/main/LifeOS/install/LIFEOS/DOCUMENTATION/Memory/CortexContract.md
- [17] Hook System | https://raw.githubusercontent.com/danielmiessler/LifeOS/main/LifeOS/install/LIFEOS/DOCUMENTATION/Hooks/HookSystem.md
- [18] LifeOS Data Classification & Inference-Source Routing | https://raw.githubusercontent.com/danielmiessler/LifeOS/main/LifeOS/install/LIFEOS/DOCUMENTATION/Security/DataClassification.md
- [19] The Observability System | https://raw.githubusercontent.com/danielmiessler/LifeOS/main/LifeOS/install/LIFEOS/DOCUMENTATION/Observability/ObservabilitySystem.md
- [20] Eval Guide — LLM-as-Judge Evaluation for Optimize Mode | https://raw.githubusercontent.com/danielmiessler/LifeOS/main/LifeOS/install/LIFEOS/ALGORITHM/eval-guide.md
- [21] Hindsight: Agent Memory That Learns | https://github.com/vectorize-io/hindsight
- [22] Introduction - Cognee Documentation | https://docs.cognee.ai/getting-started/introduction
- [23] Open Source Overview - Mem0 | https://docs.mem0.ai/open-source/overview
- [24] GitHub - getzep/graphiti: Build Real-Time Knowledge Graphs for AI Agents | https://github.com/getzep/graphiti
- [25] Self-hosting | https://docs.letta.com/self-hosting/
- [26] LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory | https://arxiv.org/abs/2410.10813
- [27] Self-Improving Coding Agent | https://raw.githubusercontent.com/OctavianBoji/Autonomous-Coding-Agent/main/README.md
- [28] Memory Vector Retriever | https://raw.githubusercontent.com/OctavianBoji/llm-memory-system/master/src/memory/retriever.py