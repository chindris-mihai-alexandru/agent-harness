from __future__ import annotations

import sqlite3

import pytest

from konz.models import JobStatus, RunSpec
from konz.store import (
    IdempotencyConflictError,
    JobNotFoundError,
    SQLiteEventStore,
)


def test_default_store_uses_restrictive_permissions(tmp_path, monkeypatch):
    import os
    import stat

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    store = SQLiteEventStore()
    assert stat.S_IMODE(store.database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.database_path.parent.stat().st_mode) == 0o700


def test_create_job_and_derive_snapshot(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    created = store.create_job("Implement a feature", tmp_path)
    store.append_event(created.job_id, "run.started", {"model": "example-model"})
    store.append_event(created.job_id, "session.started", {"session_id": "session-1"})
    store.append_event(created.job_id, "run.paused", {"reason": "approval_required"})

    snapshot = store.get_snapshot(created.job_id)
    assert snapshot.task == "Implement a feature"
    assert snapshot.status == JobStatus.PAUSED
    assert snapshot.adal_session_id == "session-1"
    assert snapshot.last_sequence == 4


def test_event_sequence_is_ordered_and_cursor_is_exclusive(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    created = store.create_job("task", tmp_path)
    first = store.append_event(created.job_id, "run.started")
    second = store.append_event(created.job_id, "turn.started", {"turn_id": "t1"})

    assert (first.sequence, second.sequence) == (2, 3)
    assert [event.sequence for event in store.get_events(created.job_id, after_sequence=1)] == [2, 3]


def test_idempotent_append_returns_original_event(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    created = store.create_job("task", tmp_path)
    original = store.append_event(
        created.job_id, "run.started", {"model": "m"}, idempotency_key="start-1"
    )
    replay = store.append_event(
        created.job_id, "run.started", {"model": "m"}, idempotency_key="start-1"
    )

    assert replay == original
    assert len(store.get_events(created.job_id)) == 2


def test_idempotency_key_cannot_be_reused_for_different_event(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    created = store.create_job("task", tmp_path)
    store.append_event(created.job_id, "run.started", {"model": "m"}, idempotency_key="same")

    with pytest.raises(IdempotencyConflictError):
        store.append_event(created.job_id, "run.failed", {"error": "changed"}, idempotency_key="same")


def test_append_and_snapshot_require_existing_job(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    with pytest.raises(JobNotFoundError):
        store.append_event("missing", "run.started")
    with pytest.raises(JobNotFoundError):
        store.get_snapshot("missing")


def test_job_id_and_event_type_are_validated(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    with pytest.raises(ValueError):
        store.create_job("task", tmp_path, job_id="../escape")
    created = store.create_job("task", tmp_path)
    with pytest.raises(ValueError):
        store.append_event(created.job_id, "Run Started")


def test_event_type_and_cursor_inputs_are_validated(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    created = store.create_job("task", tmp_path)
    with pytest.raises(ValueError, match="event_type"):
        store.append_event(created.job_id, 17)
    with pytest.raises(ValueError, match="after_sequence"):
        store.get_events(created.job_id, after_sequence="bad")
    with pytest.raises(JobNotFoundError):
        store.get_events("missing")


def test_payload_must_be_finite_json_and_falsey_non_objects_are_rejected(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    created = store.create_job("task", tmp_path)
    with pytest.raises(ValueError):
        store.append_event(created.job_id, "metric.recorded", {"value": float("nan")})
    with pytest.raises(ValueError, match="object"):
        store.append_event(created.job_id, "metric.recorded", [])


def test_purge_requires_exact_confirmation_and_removes_job_events(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    created = store.create_job("private task", tmp_path)
    store.append_event(created.job_id, "run.started")

    with pytest.raises(ValueError, match="exactly match"):
        store.purge_job(created.job_id, confirm_job_id="other")
    assert store.purge_job(created.job_id, confirm_job_id=created.job_id)
    assert store.purge_job(created.job_id, confirm_job_id=created.job_id) is False
    assert store.list_jobs() == []


def test_stale_run_interruption_closes_active_turn_before_resume(tmp_path):
    store = SQLiteEventStore(tmp_path / "stale.sqlite3")
    job = store.create_job("task", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    store.append_event(job.job_id, "turn.started", {"turn_id": "old-turn"})

    store.append_event(job.job_id, "run.interrupted", {"reason": "confirmed stopped"})
    stale_turns = [event for event in store.get_events(job.job_id) if event.event_type == "turn.interrupted"]
    assert len(stale_turns) == 1
    assert stale_turns[0].payload["turn_id"] == "old-turn"
    assert store.get_snapshot(job.job_id).status == JobStatus.PAUSED

    store.append_event(job.job_id, "run.started", {"resumed": True})
    store.append_event(job.job_id, "turn.started", {"turn_id": "new-turn"})
    store.append_event(job.job_id, "turn.completed", {"turn_id": "new-turn"})
    store.append_event(job.job_id, "run.completed", {})
    assert store.get_snapshot(job.job_id).status == JobStatus.COMPLETED


def test_run_cannot_finish_while_turn_is_active(tmp_path):
    store = SQLiteEventStore(tmp_path / "unfinished-turn.sqlite3")
    job = store.create_job("task", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    store.append_event(job.job_id, "turn.started", {"turn_id": "active-turn"})

    with pytest.raises(ValueError, match="active turn"):
        store.append_event(job.job_id, "run.completed", {})


def test_turn_lifecycle_prevents_parallel_duplicate_turns(tmp_path):
    store = SQLiteEventStore(tmp_path / "turns.sqlite3")
    job = store.create_job("task", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    store.append_event(job.job_id, "turn.started", {"turn_id": "turn-1"})

    with pytest.raises(ValueError, match="already active"):
        store.append_event(job.job_id, "turn.started", {"turn_id": "turn-2"})
    with pytest.raises(ValueError, match="same turn_id"):
        store.append_event(job.job_id, "turn.completed", {"turn_id": "turn-2"})

    store.append_event(job.job_id, "turn.failed", {"turn_id": "turn-1", "error": "test"})
    store.append_event(job.job_id, "turn.started", {"turn_id": "turn-2"})


def test_events_are_append_only_at_sqlite_boundary(tmp_path):
    database = tmp_path / "runs.sqlite3"
    store = SQLiteEventStore(database)
    event = store.create_job("task", tmp_path)
    other_job = store.create_job("other", tmp_path)
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE events SET event_type = 'run.failed' WHERE event_id = ?", (event.event_id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events WHERE event_id = ?", (event.event_id,))
    assert store.purge_job(event.job_id, confirm_job_id=event.job_id)
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM events WHERE job_id = ?", (other_job.job_id,))


def test_run_spec_validates_intent_timeout_and_criterion_identity(tmp_path):
    with pytest.raises(ValueError):
        RunSpec("  ", tmp_path)
    with pytest.raises(ValueError):
        RunSpec("task", tmp_path, timeout_seconds=0)

    criterion = {"criterion_id": "tests", "statement": "Tests pass", "verifier": "pytest"}
    from konz.models import AcceptanceCriterion

    with pytest.raises(ValueError, match="unique"):
        RunSpec("task", tmp_path, criteria=(AcceptanceCriterion(**criterion), AcceptanceCriterion(**criterion)))
