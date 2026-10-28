# Agent Harness

A local-first personal agent harness, initially using AdaL's public Python Agent SDK as its execution runtime.

> This is a new, unpushed project in an early foundation stage. It is not a replacement for AdaL yet, does not reproduce AdaL's connectors or UI, and has not been verified against a live AdaL account.

## Current foundation

- An append-only, versioned local SQLite event ledger with ordered event IDs, idempotency keys, crash-safe SQLite transactions and derived job status.
- A bounded AdaL SDK turn adapter with streamed progress, explicit approval callbacks, cancellation on timeout, and resumable AdaL session IDs.
- A task model for optional, falsifiable acceptance criteria. It records the criteria, but does not yet run their verifiers.
- A CLI for one-turn runs, status, event inspection, resume, and explicit job-history purge.
- Tests use a fake AdaL boundary. They do not make billable model calls.

The ledger is local audit history, not cryptographically tamper-proof storage. By default it lives in `~/.agent-harness/runs.sqlite3`, with restrictive file permissions on the database. It stores task prompts, final assistant answers, AdaL session IDs, workspace paths, progress types, tool names/statuses, approval decisions and failure summaries. It intentionally omits raw model-thought text and tool arguments/results. Error messages can still include sensitive details, so inspect the local database and use `agent-harness purge` when appropriate. Purging is logical deletion; it cannot promise forensic erasure from filesystem snapshots, backups, or storage media. There is no encryption-at-rest or OS sandbox in this first cut. AdaL's tools run with the current user's permissions.

## Requirements and setup

- Python 3.12 or newer.
- AdaL CLI installed and authenticated separately.
- AdaL Agent SDK 0.3.0 from the pinned public source revision, installed as an optional extra.

```bash
cd /Volumes/990Pro2TB/MyProjects/agent-harness
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,adal]'
adal --version
```

The AdaL SDK launches the installed AdaL runtime as a local subprocess. The user authenticates with AdaL; this project does not read or store AdaL access tokens. The exact current SDK requirements should be checked against the pinned upstream revision when updating it.

## Use

```bash
# One new job, in the chosen project directory
agent-harness run "Explain how this project handles authentication" \
  --workspace /path/to/project \
  --model claude-sonnet-4-6

# Inspect the latest job list or one job
agent-harness status
agent-harness status <job-id>
agent-harness events <job-id>

# Resume a paused/failed AdaL conversation with the same signed-in account
agent-harness resume <job-id> --confirm-same-account
# If the prior process exited unexpectedly, confirm it has stopped too:
agent-harness resume <job-id> --confirm-same-account --confirm-no-active-run

# Permanently remove this job's local event history (requires the exact ID twice)
agent-harness purge <job-id> --confirm-job-id <job-id>
```

Use the same AdaL account when resuming a session. Account identity is not yet bound to a job in this prototype, so do not switch AdaL accounts and then resume an old session. Connector credentials are not copied or handled by this code.

The CLI asks before AdaL SDK tool calls marked as requiring human approval. Non-interactive runs deny those calls and exit with status 2. Review the exact proposed tool input before approving; approval decisions are logged without the raw arguments. Interactive prompts are not an OS security boundary.

Progress may show assistant output, tool names/statuses and a generic "thinking" indicator. Although upstream SDK event schemas may carry reasoning deltas, the harness deliberately does not display or store raw private reasoning. Whether AdaL exposes subagent lifecycle events through the SDK is not yet verified; generic subagent events are surfaced if the SDK emits them, but this project does not yet launch or supervise its own subagents.

## Architecture principles

- AdaL owns the inner model/tool loop. The harness owns job IDs, local event history, resume policy, presentation and future orchestration.
- Durable events and the prompt/context sent to a model are different representations. A future context builder should select bounded evidence from events instead of replaying the entire log.
- An acceptance claim is not closed by an assistant saying "done". A future verifier should record the exact evidence that satisfies each criterion.
- Memory facts must remain separate from raw workflow events, with explicit scope and source provenance. No memory provider is selected yet.
- Connectors, MCP servers, skills, plugins and built-in capabilities are distinct resource types. Credential storage and scope need an OS keychain boundary and account isolation before connectors are implemented.
- Dangerous or unattended side effects need deterministic authorization and, for untrusted code execution, OS-level isolation. Prompts and static scans do not sandbox processes.

## Project status and known gaps

This repository currently records one SDK turn. It does not yet provide a multi-phase task planner, deterministic acceptance-criterion verifier, worktree manager, sandbox, semantic memory, connector catalog, OAuth flow, marketplace, plugin runtime, capability router, subagent scheduler, desktop UI, web server, or remote orchestration. It does not control whether AdaL internally chooses built-in capabilities or delegates to workers. Progress handling is an adapter over documented SDK events and must be checked against a real SDK installation before production use.

Open `docs/product-brief.md` and `docs/architecture.md` for the direction and trade-offs. `docs/research-notes.md` records the public sources reviewed.

## Development

```bash
python -m pytest -q
python -m compileall -q src tests
python -m agent_harness.cli --help  # after the editable install above
```

The tests fake the AdaL runtime. They verify persistence, event ordering, idempotency, privacy filtering, approval handling and CLI status paths, not provider behavior. No real AdaL query has been run as part of the foundation.

## License

MIT. See [LICENSE](LICENSE).
