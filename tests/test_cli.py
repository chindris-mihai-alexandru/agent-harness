from __future__ import annotations

import json

import pytest

from agent_harness import cli
from agent_harness.store import SQLiteEventStore


def test_cli_status_and_events_read_job(tmp_path, monkeypatch, capsys):
    database = tmp_path / "runs.sqlite3"
    store = SQLiteEventStore(database)
    job = store.create_job("inspect a task", tmp_path)
    store.append_event(job.job_id, "run.started", {})

    monkeypatch.setattr("sys.argv", ["agent-harness", "--database", str(database), "status", job.job_id])
    cli.main()
    status = json.loads(capsys.readouterr().out)
    assert status["job_id"] == job.job_id
    assert status["status"] == "running"

    monkeypatch.setattr("sys.argv", ["agent-harness", "--database", str(database), "events", job.job_id])
    cli.main()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["sequence"] for event in events] == [1, 2]


def test_cli_events_unknown_job_is_actionable(tmp_path, monkeypatch, capsys):
    database = tmp_path / "runs.sqlite3"
    monkeypatch.setattr("sys.argv", ["agent-harness", "--database", str(database), "events", "missing"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert "Unknown job" in capsys.readouterr().err


def test_cli_resume_missing_job_is_actionable(tmp_path, monkeypatch):
    database = tmp_path / "runs.sqlite3"
    monkeypatch.setattr("sys.argv", ["agent-harness", "--database", str(database), "resume", "missing"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
