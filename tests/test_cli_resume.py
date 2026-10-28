from __future__ import annotations

import pytest

from agent_harness import cli
from agent_harness.store import SQLiteEventStore


def make_paused_job(database, workspace):
    store = SQLiteEventStore(database)
    job = store.create_job("task", workspace, model="model-a")
    store.append_event(job.job_id, "run.started", {"model": "model-a"})
    store.append_event(job.job_id, "session.started", {"session_id": "session-1"})
    store.append_event(job.job_id, "run.paused", {"reason": "approval_required"})
    return job


def test_resume_requires_explicit_same_account_confirmation(tmp_path, monkeypatch, capsys):
    database = tmp_path / "runs.sqlite3"
    store = SQLiteEventStore(database)
    job = store.create_job("task", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    store.append_event(job.job_id, "session.started", {"session_id": "session-1"})
    store.append_event(job.job_id, "run.paused", {"reason": "approval_required"})

    monkeypatch.setattr("sys.argv", ["agent-harness", "--database", str(database), "resume", job.job_id])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "same AdaL account" in capsys.readouterr().err


def test_resume_uses_stored_session_and_workspace_after_confirmation(tmp_path, monkeypatch):
    database = tmp_path / "runs.sqlite3"
    job = make_paused_job(database, tmp_path)
    calls = []
    monkeypatch.setattr(cli, "_execute_turn", lambda *args: calls.append(args))
    monkeypatch.setattr(
        "sys.argv",
        ["agent-harness", "--database", str(database), "resume", job.job_id, "--confirm-same-account"],
    )

    cli.main()

    assert calls[0][1] == job.job_id
    assert calls[0][2].startswith("Continue the original task")
    assert calls[0][3] == tmp_path.resolve()
    assert calls[0][4] == "model-a"
    assert calls[0][5] == "session-1"


def test_running_resume_requires_confirmation_that_old_process_stopped(tmp_path, monkeypatch, capsys):
    database = tmp_path / "runs.sqlite3"
    store = SQLiteEventStore(database)
    job = store.create_job("task", tmp_path, model="model-a")
    store.append_event(job.job_id, "run.started", {})
    store.append_event(job.job_id, "session.started", {"session_id": "session-1"})
    store.append_event(job.job_id, "turn.started", {"turn_id": "old-turn"})
    monkeypatch.setattr(
        "sys.argv",
        ["agent-harness", "--database", str(database), "resume", job.job_id, "--confirm-same-account"],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    assert "confirm-no-active-run" in capsys.readouterr().err

    calls = []

    def fake_execute_turn(event_store, job_id, *args):
        calls.append(args)
        event_store.append_event(job_id, "run.started", {"resumed": True})

    monkeypatch.setattr(cli, "_execute_turn", fake_execute_turn)
    monkeypatch.setattr(
        "sys.argv",
        ["agent-harness", "--database", str(database), "resume", job.job_id,
         "--confirm-same-account", "--confirm-no-active-run"],
    )
    cli.main()
    assert calls[0][0].startswith("Continue the original task")
    assert calls[0][1] == tmp_path.resolve()
    assert calls[0][2] == "model-a"
    assert calls[0][3] == "session-1"
    assert store.get_snapshot(job.job_id).status.value == "running"
    events = store.get_events(job.job_id)
    interrupted_turns = [event for event in events if event.event_type == "turn.interrupted"]
    assert len(interrupted_turns) == 1
    assert interrupted_turns[0].payload["turn_id"] == "old-turn"
    assert any(event.event_type == "run.interrupted" for event in events)


def test_resume_existing_completed_job_is_refused_even_if_confirmed(tmp_path, monkeypatch, capsys):
    database = tmp_path / "runs.sqlite3"
    store = SQLiteEventStore(database)
    job = store.create_job("task", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    store.append_event(job.job_id, "run.completed", {})

    monkeypatch.setattr("sys.argv", ["agent-harness", "--database", str(database), "resume", job.job_id, "--confirm-same-account"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert "cannot be resumed" in capsys.readouterr().err
