from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from konz.runner import AdalSdkRunner, ApprovalRequest, ProgressEvent
from konz.store import SQLiteEventStore


class FakePermissionResult:
    def __init__(self, behavior: str, reason: str = ""):
        self.behavior = behavior
        self.reason = reason


class FakeBindings:
    @staticmethod
    def allow():
        return FakePermissionResult("allow")

    @staticmethod
    def deny(reason: str):
        return FakePermissionResult("deny", reason)

    def __init__(self, events, session_id="session-123", approval_request=None):
        self.events = list(events)
        self.session_id = session_id
        self.approval_request = approval_request
        self.options = None
        self.client = None

    def options_factory(self, **kwargs):
        self.options = SimpleNamespace(**kwargs)
        return self.options

    def client_factory(self, options):
        self.client = FakeClient(options, self.events, self.session_id, self.approval_request)
        return self.client


class FakeClient:
    def __init__(self, options, events, session_id, approval_request=None):
        self.options = options
        self.events = list(events)
        self.session_id = session_id
        self.approval_request = approval_request
        self.queries = []
        self.cancelled = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def query(self, prompt):
        self.queries.append(prompt)
        if self.approval_request is not None:
            tool_name, tool_input, context = self.approval_request
            result = await self.options.can_use_tool(tool_name, tool_input, context)
            if result.behavior == "deny":
                self.events.extend(
                    [
                        {
                            "type": "tool.completed",
                            "name": tool_name,
                            "toolCallId": context.tool_call_id,
                            "status": "cancelled",
                            "result": {"metadata": {"permission": "unattended"}},
                        },
                        {"type": "command.failed", "error": {"message": "denied"}},
                    ]
                )
            else:
                self.events.append({"type": "command.completed"})

    async def receive_events(self, timeout=300.0):
        for event in self.events:
            yield event

    async def cancel(self):
        self.cancelled = True


@pytest.mark.asyncio
async def test_runner_records_safe_progress_and_resumable_session(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    job = store.create_job("Summarize this workspace", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    bindings = FakeBindings(
        [
            {"type": "thought.delta", "text": "secret private reasoning"},
            {"type": "assistant.delta", "text": "The answer"},
            {"type": "tool.started", "name": "read_file", "toolCallId": "c1", "args": {"file_path": "README.md"}},
            {"type": "tool.completed", "name": "read_file", "toolCallId": "c1", "status": "completed", "result": {"text": "private file body"}},
            {"type": "command.completed"},
        ]
    )
    seen: list[ProgressEvent] = []
    runner = AdalSdkRunner(store, sdk_bindings=bindings)
    result = await runner.run_turn(
        job.job_id,
        "Summarize this workspace",
        tmp_path,
        model="test-model",
        on_progress=seen.append,
    )

    assert result.status == "completed"
    assert result.answer == "The answer"
    assert result.session_id == "session-123"
    assert any(event.kind == "thinking" and "secret private reasoning" not in event.message for event in seen)
    assert any(event.kind == "tool_started" for event in seen)
    serialized_events = str([event.payload for event in store.get_events(job.job_id)])
    assert "secret private reasoning" not in serialized_events
    assert "private file body" not in serialized_events
    assert store.get_snapshot(job.job_id).adal_session_id == "session-123"


@pytest.mark.asyncio
async def test_human_approval_without_handler_fails_closed_and_pauses(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    job = store.create_job("Edit a file", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    context = SimpleNamespace(requires_human=True, tool_call_id="call-1", reason="prompt")
    proposal = {"file_path": "README.md", "new_string": "sensitive proposed edit"}
    bindings = FakeBindings([], approval_request=("rewrite_file", proposal, context))
    runner = AdalSdkRunner(store, sdk_bindings=bindings)

    result = await runner.run_turn(job.job_id, "Edit a file", tmp_path)
    assert result.status == "paused"
    assert result.permission_denied is True
    event_types = [event.event_type for event in store.get_events(job.job_id)]
    assert "approval.requested" in event_types
    assert "approval.decided" in event_types
    assert "sensitive proposed edit" not in str([event.payload for event in store.get_events(job.job_id)])


@pytest.mark.asyncio
async def test_approval_handler_receives_proposal_and_can_approve(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    job = store.create_job("Edit file", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    proposals: list[ApprovalRequest] = []
    context = SimpleNamespace(requires_human=True, tool_call_id="call-2", reason="prompt", confirmation={"fileName": "README.md"})
    proposal = {"file_path": "README.md", "new_string": "updated"}
    bindings = FakeBindings([], approval_request=("rewrite_file", proposal, context))
    runner = AdalSdkRunner(
        store,
        approval_handler=lambda request: proposals.append(request) or True,
        sdk_bindings=bindings,
    )

    result = await runner.run_turn(job.job_id, "Edit file", tmp_path)
    assert result.status == "completed"
    assert proposals[0].tool_name == "rewrite_file"
    assert proposals[0].tool_input == proposal
    assert proposals[0].confirmation == {"fileName": "README.md"}


@pytest.mark.asyncio
async def test_runner_rejects_invalid_prompt_workspace_and_cross_job_workspace(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    job = store.create_job("task", tmp_path)
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()
    runner = AdalSdkRunner(store, sdk_bindings=FakeBindings([]))
    with pytest.raises(ValueError, match="prompt"):
        await runner.run_turn(job.job_id, "  ", tmp_path)
    with pytest.raises(ValueError, match="existing directory"):
        await runner.run_turn(job.job_id, "task", tmp_path / "missing")
    with pytest.raises(ValueError, match="does not match"):
        await runner.run_turn(job.job_id, "task", other_workspace)
    store.append_event(job.job_id, "run.started", {})
    with pytest.raises(ValueError, match="does not match"):
        await runner.run_turn(job.job_id, "task", other_workspace)
