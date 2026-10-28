from __future__ import annotations

import pytest

from agent_harness.models import AcceptanceCriterion, JobEvent, JobStatus, reduce_job_events


def event(seq, kind, payload, job_id="job-1"):
    return JobEvent(
        event_id=f"e{seq}",
        job_id=job_id,
        sequence=seq,
        event_type=kind,
        occurred_at=f"2026-01-01T00:00:0{seq}+00:00",
        payload=payload,
    )


def test_reducer_tracks_terminal_status_and_session():
    snapshot = reduce_job_events(
        "job-1",
        [
            event(1, "job.created", {"task": "Ship change"}),
            event(2, "run.started", {}),
            event(3, "session.started", {"session_id": "adal-1"}),
            event(4, "run.failed", {"error": "timeout"}),
        ],
    )
    assert snapshot.status == JobStatus.FAILED
    assert snapshot.last_error == "timeout"
    assert snapshot.adal_session_id == "adal-1"


def test_reducer_rejects_sequence_gaps_and_terminal_restarts():
    with pytest.raises(ValueError, match="contiguous"):
        reduce_job_events(
            "job-1",
            [event(1, "job.created", {"task": "x"}), event(3, "run.started", {})],
        )
    with pytest.raises(ValueError, match="terminal"):
        reduce_job_events(
            "job-1",
            [
                event(1, "job.created", {"task": "x"}),
                event(2, "run.started", {}),
                event(3, "run.completed", {}),
                event(4, "run.started", {}),
            ],
        )


def test_reducer_rejects_invalid_or_mixed_history():
    with pytest.raises(ValueError, match="first"):
        reduce_job_events("job-1", [event(1, "run.started", {})])
    with pytest.raises(ValueError, match="multiple jobs"):
        reduce_job_events(
            "job-1",
            [event(1, "job.created", {"task": "x"}), event(2, "run.started", {}, job_id="job-2")],
        )


def test_acceptance_criterion_requires_statement_and_verifier():
    with pytest.raises(ValueError):
        AcceptanceCriterion("test", "", "pytest")
