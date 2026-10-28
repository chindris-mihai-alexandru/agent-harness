from __future__ import annotations

import pytest

from agent_harness import cli
from agent_harness.runner import TurnResult
from agent_harness.store import SQLiteEventStore


def test_paused_turn_returns_exit_code_two(tmp_path, monkeypatch, capsys):
    database = tmp_path / "runs.sqlite3"
    store = SQLiteEventStore(database)
    job = store.create_job("task", tmp_path)
    store.append_event(job.job_id, "run.started", {})

    with pytest.raises(SystemExit) as error:
        cli._record_result(
            store,
            job.job_id,
            TurnResult(
                status="paused",
                answer="",
                session_id="session-1",
                model=None,
                permission_denied=True,
            ),
        )

    assert error.value.code == 2
    assert store.get_snapshot(job.job_id).status.value == "paused"
    assert "paused" in capsys.readouterr().out
