from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from konz.models import RunSpec
from konz.runner import AdalSdkRunner, ApprovalRequest, ProgressEvent
from konz.store import JobNotFoundError, SQLiteEventStore


def _default_database() -> Path:
    return Path.home() / ".konz" / "runs.sqlite3"


def _interactive_approval_sync(request: ApprovalRequest) -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    print(f"\nAdaL requests approval for tool: {request.tool_name}")
    print("Tool arguments are shown locally for review and are not stored in the event ledger.")
    print(json.dumps(request.tool_input, indent=2, ensure_ascii=False, default=str))
    if request.confirmation:
        print("\nEdit confirmation:")
        print(json.dumps(request.confirmation, indent=2, ensure_ascii=False, default=str))
    return input("Approve this exact call? [y/N] ").strip().casefold() in {"y", "yes"}


async def _interactive_approval(request: ApprovalRequest) -> bool:
    return await asyncio.to_thread(_interactive_approval_sync, request)


def _progress(event: ProgressEvent) -> None:
    if event.kind == "assistant_output":
        print(event.message, end="", flush=True)
    elif event.kind == "thinking":
        print("\n[thinking] Model is working; private reasoning is not displayed.", flush=True)
    elif event.kind == "tool_started":
        print(f"\n[tool] {event.tool_name or 'unknown'} started", flush=True)
    elif event.kind == "tool_completed":
        print(f"[tool] {event.tool_name or 'unknown'}: {event.status or 'finished'}", flush=True)
    elif event.kind in {"subagent", "progress", "notice", "runtime_event"}:
        print(f"\n[{event.kind}] {event.message}", flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="konz",
        description="Local-first, evidence-led harness around the AdaL Agent SDK.",
    )
    parser.add_argument("--database", type=Path, default=_default_database())
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="start one AdaL turn and record its local events")
    run.add_argument("task", help="task prompt for AdaL")
    run.add_argument("--workspace", type=Path, default=Path.cwd())
    run.add_argument("--model", default=None)
    run.add_argument("--timeout", type=float, default=300.0)

    resume = subparsers.add_parser("resume", help="continue an existing paused or interrupted job")
    resume.add_argument("job_id")
    resume.add_argument(
        "--confirm-same-account",
        action="store_true",
        help="confirm the currently signed-in AdaL account matches the job",
    )
    resume.add_argument(
        "--confirm-no-active-run",
        action="store_true",
        help="confirm an earlier process for a job marked running has stopped",
    )
    resume.add_argument("--prompt", help="optional follow-up instruction for the resumed session")
    resume.add_argument("--timeout", type=float, default=300.0)

    status = subparsers.add_parser("status", help="show local job status")
    status.add_argument("job_id", nargs="?")

    events = subparsers.add_parser("events", help="show stored event summaries for a job")
    events.add_argument("job_id")
    events.add_argument("--after", type=int, default=0)

    purge = subparsers.add_parser("purge", help="permanently remove one local job record")
    purge.add_argument("job_id")
    purge.add_argument("--confirm-job-id", required=True)
    return parser


def _record_result(store: SQLiteEventStore, job_id: str, result: Any) -> None:
    if result.session_id:
        print(f"\nAdaL session: {result.session_id}")
    if result.status == "completed":
        store.append_event(job_id, "run.completed", {})
        print(f"\nJob {job_id} completed.")
    elif result.status == "paused":
        store.append_event(
            job_id,
            "run.paused",
            {"reason": "approval_required", "session_id": result.session_id},
        )
        print(f"\nJob {job_id} paused. Inspect it with `konz status {job_id}`.")
        raise SystemExit(2)
    else:
        store.append_event(
            job_id,
            "run.failed",
            {"error": result.error or "AdaL turn failed", "session_id": result.session_id},
        )
        print(f"\nJob {job_id} failed: {result.error or 'AdaL turn failed'}", file=sys.stderr)
        raise SystemExit(1)


def _execute_turn(
    store: SQLiteEventStore,
    job_id: str,
    task: str,
    workspace: Path,
    model: str | None,
    session_id: str | None,
    timeout_seconds: float,
) -> None:
    store.append_event(job_id, "run.started", {"model": model, "resumed": session_id is not None})
    runner = AdalSdkRunner(store, approval_handler=_interactive_approval)
    try:
        result = asyncio.run(
            runner.run_turn(
                job_id,
                task,
                workspace,
                model=model,
                session_id=session_id,
                timeout_seconds=timeout_seconds,
                on_progress=_progress,
            )
        )
    except KeyboardInterrupt:
        store.append_event(job_id, "run.cancelled", {"reason": "keyboard_interrupt"})
        print(f"\nJob {job_id} cancelled.", file=sys.stderr)
        raise SystemExit(130) from None
    _record_result(store, job_id, result)


def main() -> None:
    args = _parser().parse_args()
    store = SQLiteEventStore(args.database)
    if args.command == "run":
        spec = RunSpec(
            task=args.task,
            workspace=args.workspace,
            model=args.model,
            timeout_seconds=args.timeout,
        )
        created = store.create_job(spec.task, spec.workspace, model=spec.model)
        _execute_turn(
            store,
            created.job_id,
            spec.task,
            spec.workspace,
            spec.model,
            None,
            spec.timeout_seconds,
        )
        return

    if args.command == "resume":
        try:
            snapshot = store.get_snapshot(args.job_id)
        except JobNotFoundError:
            print(f"Unknown job: {args.job_id}", file=sys.stderr)
            raise SystemExit(1) from None
        if snapshot.status.value in {"completed", "cancelled"}:
            print(f"Job {args.job_id} is {snapshot.status.value} and cannot be resumed.", file=sys.stderr)
            raise SystemExit(1)
        if not args.confirm_same_account:
            print(
                "Before resuming, verify you are signed into the same AdaL account; "
                "then pass --confirm-same-account.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        if snapshot.status.value == "running" and not args.confirm_no_active_run:
            print(
                "This job is marked running. Confirm its earlier process has stopped "
                "with --confirm-no-active-run before resuming; this check cannot detect "
                "another process automatically.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        created = store.get_creation_event(args.job_id)
        task = created.payload.get("task")
        workspace_value = created.payload.get("workspace")
        model = created.payload.get("model")
        if not isinstance(task, str) or not isinstance(workspace_value, str):
            print("Stored job is missing its task or workspace.", file=sys.stderr)
            raise SystemExit(1)
        try:
            spec = RunSpec(task=task, workspace=Path(workspace_value), model=model if isinstance(model, str) else None, timeout_seconds=args.timeout)
        except (ValueError, OSError) as exc:
            print(f"Cannot resume stored job: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        workspace = spec.workspace
        if not snapshot.adal_session_id:
            print("This job has no AdaL session to resume.", file=sys.stderr)
            raise SystemExit(1)
        if snapshot.status.value == "running":
            store.append_event(
                args.job_id,
                "run.interrupted",
                {"reason": "user_confirmed_previous_process_stopped"},
            )
        continuation = args.prompt or (
            "Continue the original task from the current state. First inspect what is "
            "already complete and what remains, then proceed only with actions the "
            "user has approved."
        )
        _execute_turn(
            store,
            args.job_id,
            continuation,
            workspace,
            model if isinstance(model, str) else None,
            snapshot.adal_session_id,
            args.timeout,
        )
        return

    if args.command == "status":
        try:
            snapshots = [store.get_snapshot(args.job_id)] if args.job_id else store.list_jobs()
        except JobNotFoundError:
            print(f"Unknown job: {args.job_id}", file=sys.stderr)
            raise SystemExit(1) from None
        for snapshot in snapshots:
            print(
                json.dumps(
                    {
                        "job_id": snapshot.job_id,
                        "task": snapshot.task,
                        "status": snapshot.status.value,
                        "adal_session_id": snapshot.adal_session_id,
                        "last_sequence": snapshot.last_sequence,
                        "last_error": snapshot.last_error,
                    },
                    ensure_ascii=False,
                )
            )
        return

    if args.command == "events":
        try:
            job_events = store.get_events(args.job_id, after_sequence=args.after)
        except JobNotFoundError:
            print(f"Unknown job: {args.job_id}", file=sys.stderr)
            raise SystemExit(1) from None
        except ValueError as exc:
            print(f"Invalid event cursor: {exc}", file=sys.stderr)
            raise SystemExit(2) from None
        for event in job_events:
            print(
                json.dumps(
                    {
                        "sequence": event.sequence,
                        "type": event.event_type,
                        "occurred_at": event.occurred_at,
                        "payload": event.payload,
                    },
                    ensure_ascii=False,
                )
            )
        return

    if args.command == "purge":
        if store.purge_job(args.job_id, confirm_job_id=args.confirm_job_id):
            print(f"Purged local event history for job {args.job_id}.")
        else:
            print(f"Unknown job: {args.job_id}", file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
