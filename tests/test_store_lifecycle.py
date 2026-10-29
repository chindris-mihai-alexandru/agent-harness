from __future__ import annotations

import sqlite3
import threading

import pytest

from konz.store import SQLiteEventStore


def test_unknown_future_schema_is_rejected_without_overwriting(tmp_path):
    database = tmp_path / "future.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()

    with pytest.raises(RuntimeError, match="newer than supported"):
        SQLiteEventStore(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99


def test_concurrent_event_writes_keep_a_contiguous_sequence(tmp_path):
    store = SQLiteEventStore(tmp_path / "concurrent.sqlite3", timeout=10)
    job = store.create_job("parallel workers", tmp_path)
    barrier = threading.Barrier(6)
    errors: list[Exception] = []

    def append(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            store.append_event(job.job_id, "worker.finished", {"worker": index})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert [event.sequence for event in store.get_events(job.job_id)] == list(range(1, 8))
