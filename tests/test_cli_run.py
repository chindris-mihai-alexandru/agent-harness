from __future__ import annotations

import pytest

from konz import cli
from konz.runner import TurnResult
from konz.store import SQLiteEventStore


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
