from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    """Materialized status derived from the append-only job event stream."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    """A falsifiable task-level claim and the kind of evidence that can close it."""

    criterion_id: str
    statement: str
    verifier: str
    required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.criterion_id, str) or not self.criterion_id.strip():
            raise ValueError("criterion_id must not be empty")
        if not isinstance(self.statement, str) or not self.statement.strip():
            raise ValueError("criterion statement must not be empty")
        if not isinstance(self.verifier, str) or not self.verifier.strip():
            raise ValueError("criterion verifier must not be empty")


@dataclass(frozen=True, slots=True)
class RunSpec:
    """User-owned task intent, workspace and optional acceptance criteria."""

    task: str
    workspace: Path
    model: str | None = None
    criteria: tuple[AcceptanceCriterion, ...] = ()
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not isinstance(self.criteria, (tuple, list)) or any(
            not isinstance(criterion, AcceptanceCriterion) for criterion in self.criteria
        ):
            raise ValueError("criteria must be a tuple or list of AcceptanceCriterion values")
        object.__setattr__(self, "criteria", tuple(self.criteria))
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("task must not be empty")
        if self.model is not None and not isinstance(self.model, str):
            raise ValueError("model must be a string or None")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite number greater than zero")
        try:
            workspace = Path(self.workspace).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError) as exc:
            raise ValueError("workspace must be an existing directory") from exc
        if not workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        object.__setattr__(self, "workspace", workspace)
        ids = [criterion.criterion_id for criterion in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("acceptance criterion IDs must be unique")


@dataclass(frozen=True, slots=True)
class JobEvent:
    """One versioned, ordered, append-only event for a local job."""

    event_id: str
    job_id: str
    sequence: int
    event_type: str
    occurred_at: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class JobSnapshot:
    """Small materialized view derived from a job's events."""

    job_id: str
    task: str
    status: JobStatus
    created_at: str
    updated_at: str
    last_sequence: int
    adal_session_id: str | None = None
    last_error: str | None = None


def reduce_job_events(job_id: str, events: list[JobEvent]) -> JobSnapshot:
    """Derive current job status from its event history without mutating events."""
    if not events:
        raise ValueError(f"job {job_id!r} has no events")
    if any(event.job_id != job_id for event in events):
        raise ValueError("cannot reduce events from multiple jobs")

    ordered = sorted(events, key=lambda event: event.sequence)
    if [event.sequence for event in ordered] != list(range(1, len(ordered) + 1)):
        raise ValueError("job event sequence must be contiguous and start at 1")
    first = ordered[0]
    if first.event_type != "job.created":
        raise ValueError("first job event must be job.created")
    task = first.payload.get("task")
    if not isinstance(task, str):
        raise ValueError("job.created event must contain a string task")

    status = JobStatus.CREATED
    session_id: str | None = None
    last_error: str | None = None
    turn_states: dict[str, str] = {}
    terminal_run_events = {
        "run.interrupted", "run.paused", "run.completed", "run.failed", "run.cancelled"
    }
    turn_events = {
        "turn.started", "turn.paused", "turn.completed", "turn.failed", "turn.cancelled", "turn.interrupted"
    }
    for event in ordered[1:]:
        event_type = event.event_type
        if status in {JobStatus.COMPLETED, JobStatus.CANCELLED} and event_type in {
            "run.started", *terminal_run_events
        }:
            raise ValueError(f"job has terminal status {status.value}; cannot apply {event_type}")

        if event_type == "run.started":
            if status == JobStatus.RUNNING:
                raise ValueError("run.started cannot be appended while a job is already running")
            if any(turn_status == "turn.started" for turn_status in turn_states.values()):
                raise ValueError("run.started cannot reopen a job with an active turn")
            status = JobStatus.RUNNING
            last_error = None
        elif event_type in terminal_run_events:
            if status != JobStatus.RUNNING:
                raise ValueError(f"{event_type} requires a running job")
            if any(turn_status == "turn.started" for turn_status in turn_states.values()):
                raise ValueError(f"{event_type} cannot close a job with an active turn")
            if event_type in {"run.interrupted", "run.paused"}:
                status = JobStatus.PAUSED
                reason = event.payload.get("reason")
                if isinstance(reason, str):
                    last_error = reason
            elif event_type == "run.completed":
                status = JobStatus.COMPLETED
            elif event_type == "run.failed":
                status = JobStatus.FAILED
                error = event.payload.get("error")
                last_error = error if isinstance(error, str) else "Run failed"
            else:
                status = JobStatus.CANCELLED
        elif event_type in turn_events:
            turn_id = event.payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id.strip():
                raise ValueError(f"{event_type} requires a non-empty turn_id")
            if event_type == "turn.started":
                if status != JobStatus.RUNNING:
                    raise ValueError("turn.started requires a running job")
                if any(turn_status == "turn.started" for turn_status in turn_states.values()):
                    raise ValueError("another turn is already active")
                if turn_id in turn_states:
                    raise ValueError("turn_id cannot be reused")
            elif status != JobStatus.RUNNING or turn_states.get(turn_id) != "turn.started":
                raise ValueError(f"{event_type} requires the matching active turn")
            turn_states[turn_id] = event_type
        elif event_type == "session.started":
            value = event.payload.get("session_id")
            if isinstance(value, str):
                session_id = value

    return JobSnapshot(
        job_id=job_id,
        task=task,
        status=status,
        created_at=first.occurred_at,
        updated_at=ordered[-1].occurred_at,
        last_sequence=ordered[-1].sequence,
        adal_session_id=session_id,
        last_error=last_error,
    )


def utc_now() -> str:
    """Return a timezone-aware ISO-8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()
