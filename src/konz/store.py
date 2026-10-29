from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from math import isfinite
from pathlib import Path
from typing import Any, Iterator

from konz.models import JobEvent, JobSnapshot, JobStatus, reduce_job_events, utc_now


_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SCHEMA_VERSION = 1


class JobNotFoundError(KeyError):
    """Raised when an event references a job that is not in this ledger."""


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused for a different event."""


class SQLiteEventStore:
    """Local SQLite event ledger; events are append-only through this API and triggers.

    This is a local audit record, not tamper-evident storage: a user with direct
    database access can still replace the database or its schema.
    """

    def __init__(self, database_path: str | Path | None = None, timeout: float = 5.0) -> None:
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a finite number greater than zero")
        self._restrict_permissions = database_path is None
        default_directory = Path.home() / ".konz"
        self.database_path = Path(
            database_path if database_path is not None else default_directory / "runs.sqlite3"
        ).expanduser()
        if self._restrict_permissions and default_directory.is_symlink():
            raise ValueError("default data directory must not be a symbolic link")
        if self._restrict_permissions and default_directory.exists() and not default_directory.is_dir():
            raise ValueError("default data directory exists but is not a directory")
        if self.database_path.is_symlink():
            raise ValueError("database_path must not be a symbolic link")
        if self.database_path.exists() and not self.database_path.is_file():
            raise ValueError("database_path must name a file")
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._restrict_permissions and self.database_path.parent == default_directory:
            os.chmod(default_directory, 0o700)
        if not self.database_path.exists():
            try:
                descriptor = os.open(
                    self.database_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if not self.database_path.is_file() or self.database_path.is_symlink():
                    raise ValueError("database_path must name a regular file")
            else:
                os.close(descriptor)
        elif self._restrict_permissions:
            os.chmod(self.database_path, 0o600)
        self.timeout = float(timeout)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.timeout,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {max(1, int(self.timeout * 1000))}")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Close every SQLite handle deterministically after its operation."""
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                current_version = connection.execute("PRAGMA user_version").fetchone()[0]
                if current_version > _SCHEMA_VERSION:
                    raise RuntimeError(
                        f"database schema {current_version} is newer than supported {_SCHEMA_VERSION}"
                    )
                if current_version == _SCHEMA_VERSION:
                    connection.commit()
                    return
                if current_version != 0:
                    raise RuntimeError(f"no migration path from database schema {current_version}")
                connection.execute(
                    """CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """CREATE TABLE events (
                        event_id TEXT PRIMARY KEY,
                        job_id TEXT NOT NULL REFERENCES jobs(job_id),
                        sequence INTEGER NOT NULL CHECK(sequence > 0),
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                        payload_json TEXT NOT NULL,
                        idempotency_key TEXT,
                        UNIQUE(job_id, sequence)
                    )"""
                )
                connection.execute(
                    "CREATE UNIQUE INDEX events_idempotency ON events(job_id, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
                )
                connection.execute(
                    """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
                    BEGIN SELECT RAISE(ABORT, 'job events are append-only'); END"""
                )
                connection.execute(
                    """CREATE TABLE purge_authorizations (
                        job_id TEXT PRIMARY KEY REFERENCES jobs(job_id)
                    )"""
                )
                connection.execute(
                    """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
                    WHEN NOT EXISTS (
                        SELECT 1 FROM purge_authorizations
                        WHERE job_id = OLD.job_id
                    )
                    BEGIN SELECT RAISE(ABORT, 'job events are append-only'); END"""
                )
                connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def create_job(
        self,
        task: str,
        workspace: str | Path,
        *,
        job_id: str | None = None,
        model: str | None = None,
        criteria: list[dict[str, Any]] | None = None,
    ) -> JobEvent:
        """Create a job and its first event in a single transaction."""
        if not isinstance(task, str) or not task.strip():
            raise ValueError("task must not be empty")
        if model is not None and not isinstance(model, str):
            raise ValueError("model must be a string or None")
        if criteria is not None and (
            not isinstance(criteria, list)
            or any(not isinstance(criterion, dict) for criterion in criteria)
        ):
            raise ValueError("criteria must be a list of objects")
        try:
            workspace_path = Path(workspace).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError) as exc:
            raise ValueError("workspace must be an existing directory") from exc
        if not workspace_path.is_dir():
            raise ValueError("workspace must be an existing directory")
        chosen_job_id = uuid.uuid4().hex if job_id is None else job_id
        self._validate_job_id(chosen_job_id)
        payload: dict[str, Any] = {
            "task": task,
            "workspace": str(workspace_path),
            "model": model,
            "criteria": criteria or [],
        }
        payload_json = self._serialize_payload(payload)
        occurred_at = utc_now()
        event_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO jobs(job_id, created_at) VALUES(?, ?)",
                    (chosen_job_id, occurred_at),
                )
                connection.execute(
                    """INSERT INTO events
                    (event_id, job_id, sequence, event_type, occurred_at,
                     schema_version, payload_json, idempotency_key)
                    VALUES(?, ?, 1, 'job.created', ?, ?, ?, NULL)""",
                    (event_id, chosen_job_id, occurred_at, _SCHEMA_VERSION, payload_json),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return JobEvent(
            event_id=event_id,
            job_id=chosen_job_id,
            sequence=1,
            event_type="job.created",
            occurred_at=occurred_at,
            payload=payload,
            schema_version=_SCHEMA_VERSION,
        )

    def append_event(
        self,
        job_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> JobEvent:
        """Append an event atomically; replaying the same key and payload is safe."""
        self._validate_job_id(job_id)
        if not isinstance(event_type, str) or not _EVENT_TYPE.fullmatch(event_type):
            raise ValueError("event_type must be a lowercase dotted identifier")
        normalized_payload = {} if payload is None else payload
        if not isinstance(normalized_payload, dict):
            raise ValueError("event payload must be an object")
        payload_json = self._serialize_payload(normalized_payload)
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            raise ValueError("idempotency_key must be a non-empty string")

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                job = connection.execute(
                    "SELECT job_id FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if job is None:
                    raise JobNotFoundError(job_id)
                if idempotency_key is not None:
                    existing = connection.execute(
                        "SELECT * FROM events WHERE job_id = ? AND idempotency_key = ?",
                        (job_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        if (
                            existing["event_type"] != event_type
                            or existing["payload_json"] != payload_json
                        ):
                            raise IdempotencyConflictError(
                                "idempotency key already belongs to a different event"
                            )
                        connection.commit()
                        return self._row_to_event(existing)

                self._validate_lifecycle_transition(connection, job_id, event_type, normalized_payload)
                sequence = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
                occurred_at = utc_now()
                event_id = uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO events
                    (event_id, job_id, sequence, event_type, occurred_at,
                     schema_version, payload_json, idempotency_key)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id,
                        job_id,
                        sequence,
                        event_type,
                        occurred_at,
                        _SCHEMA_VERSION,
                        payload_json,
                        idempotency_key,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return JobEvent(
            event_id=event_id,
            job_id=job_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            payload=normalized_payload,
            schema_version=_SCHEMA_VERSION,
            idempotency_key=idempotency_key,
        )

    def get_events(self, job_id: str, *, after_sequence: int = 0) -> list[JobEvent]:
        """Read events in sequence order after an exclusive cursor."""
        self._validate_job_id(job_id)
        if (
            not isinstance(after_sequence, int)
            or isinstance(after_sequence, bool)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")
        with self._connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if exists is None:
                raise JobNotFoundError(job_id)
            rows = connection.execute(
                "SELECT * FROM events WHERE job_id = ? AND sequence > ? ORDER BY sequence ASC",
                (job_id, after_sequence),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def get_creation_event(self, job_id: str) -> JobEvent:
        """Read only the first event, which contains the immutable job request."""
        self._validate_job_id(job_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE job_id = ? AND sequence = 1", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        event = self._row_to_event(row)
        if event.event_type != "job.created":
            raise ValueError("first event is not job.created")
        return event

    def get_snapshot(self, job_id: str) -> JobSnapshot:
        """Replay a job's events to derive the current status/session view."""
        self._validate_job_id(job_id)
        return reduce_job_events(job_id, self.get_events(job_id))

    def purge_job(self, job_id: str, *, confirm_job_id: str) -> bool:
        """Remove one job's logical history after an explicit ID confirmation.

        This cannot promise forensic erasure from WAL files, backups or snapshots.
        """
        self._validate_job_id(job_id)
        if confirm_job_id != job_id:
            raise ValueError("confirm_job_id must exactly match job_id")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                exists = connection.execute(
                    "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if exists is None:
                    connection.rollback()
                    return False
                connection.execute("INSERT INTO purge_authorizations(job_id) VALUES(?)", (job_id,))
                connection.execute("DELETE FROM events WHERE job_id = ?", (job_id,))
                connection.execute("DELETE FROM purge_authorizations WHERE job_id = ?", (job_id,))
                connection.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return True

    def list_jobs(self) -> list[JobSnapshot]:
        """List job snapshots in creation order."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT job_id FROM jobs ORDER BY created_at ASC, job_id ASC"
            ).fetchall()
        return [self.get_snapshot(row["job_id"]) for row in rows]

    def _validate_lifecycle_transition(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Serialize run and turn lifecycle changes inside the append transaction."""
        run_events = {
            "run.started", "run.interrupted", "run.paused", "run.completed", "run.failed", "run.cancelled"
        }
        turn_events = {
            "turn.started", "turn.paused", "turn.completed", "turn.failed", "turn.cancelled", "turn.interrupted"
        }
        if event_type not in run_events and event_type not in turn_events:
            return
        rows = connection.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY sequence ASC", (job_id,)
        ).fetchall()
        events = [self._row_to_event(row) for row in rows]
        current = reduce_job_events(job_id, events)

        if event_type == "run.started":
            if current.status in {JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.CANCELLED}:
                raise ValueError(f"cannot start a job in {current.status.value} state")
            return
        if event_type == "run.interrupted":
            if current.status != JobStatus.RUNNING:
                raise ValueError("run.interrupted requires a running job")
            active_turn_ids = self._active_turn_ids(events)
            for active_turn_id in active_turn_ids:
                self._validate_turn_transition(
                    connection,
                    job_id,
                    "turn.interrupted",
                    {"turn_id": active_turn_id},
                    events,
                    current,
                )
                self._insert_interrupted_turn_event(
                    connection, job_id, active_turn_id, payload
                )
            return
        if event_type in run_events:
            if current.status != JobStatus.RUNNING:
                raise ValueError(f"{event_type} requires a running job, got {current.status.value}")
            if event_type != "run.interrupted" and self._active_turn_ids(events):
                raise ValueError(f"{event_type} cannot close a job with an active turn")
            return

        self._validate_turn_transition(connection, job_id, event_type, payload, events, current)

    @staticmethod
    def _turn_states(events: list[JobEvent]) -> dict[str, str]:
        states: dict[str, str] = {}
        for event in events:
            if event.event_type in {
                "turn.started", "turn.paused", "turn.completed", "turn.failed", "turn.cancelled", "turn.interrupted"
            }:
                turn_id = event.payload.get("turn_id")
                if isinstance(turn_id, str):
                    states[turn_id] = event.event_type
        return states

    def _active_turn_ids(self, events: list[JobEvent]) -> list[str]:
        return [turn_id for turn_id, state in self._turn_states(events).items() if state == "turn.started"]

    def _validate_turn_transition(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        events: list[JobEvent],
        current: JobSnapshot,
    ) -> None:
        """Validate a per-turn transition while the write transaction is held."""
        turn_id = payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError(f"{event_type} requires a non-empty turn_id")
        states = self._turn_states(events)
        if event_type == "turn.started":
            if current.status != JobStatus.RUNNING:
                raise ValueError(f"turn.started requires a running job, got {current.status.value}")
            if any(state == "turn.started" for state in states.values()):
                raise ValueError("another AdaL turn is already active for this job")
            if turn_id in states:
                raise ValueError("turn_id has already been used")
            return
        if event_type == "turn.interrupted":
            if states.get(turn_id) != "turn.started":
                raise ValueError("turn.interrupted requires the same active turn_id")
            return
        if states.get(turn_id) != "turn.started":
            raise ValueError(f"{event_type} requires an active turn with the same turn_id")

    @staticmethod
    def _insert_interrupted_turn_event(
        connection: sqlite3.Connection,
        job_id: str,
        turn_id: str,
        payload: dict[str, Any],
    ) -> None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
        occurred_at = utc_now()
        connection.execute(
            """INSERT INTO events
            (event_id, job_id, sequence, event_type, occurred_at, schema_version,
             payload_json, idempotency_key)
            VALUES(?, ?, ?, 'turn.interrupted', ?, ?, ?, NULL)""",
            (
                uuid.uuid4().hex,
                job_id,
                sequence,
                occurred_at,
                _SCHEMA_VERSION,
                SQLiteEventStore._serialize_payload(
                    {
                        "turn_id": turn_id,
                        "reason": payload.get("reason", "previous process was confirmed stopped"),
                    }
                ),
            ),
        )

    @staticmethod
    def _serialize_payload(payload: dict[str, Any]) -> str:
        try:
            return json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"event payload must contain only finite JSON values: {exc}"
            ) from exc

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> JobEvent:
        payload = json.loads(row["payload_json"])
        if not isinstance(payload, dict):
            raise ValueError("stored event payload is not a JSON object")
        return JobEvent(
            event_id=row["event_id"],
            job_id=row["job_id"],
            sequence=row["sequence"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            payload=payload,
            schema_version=row["schema_version"],
            idempotency_key=row["idempotency_key"],
        )

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("job_id must be 1-64 letters, digits, underscores, or hyphens")
