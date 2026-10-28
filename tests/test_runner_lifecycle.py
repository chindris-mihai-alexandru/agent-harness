from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_harness.runner import AdalSdkRunner
from agent_harness.store import SQLiteEventStore


class Permission:
    @staticmethod
    def allow():
        return SimpleNamespace(behavior="allow")

    @staticmethod
    def deny(reason):
        return SimpleNamespace(behavior="deny", reason=reason)


class SlowClient:
    session_id = "session-slow"

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def query(self, prompt):
        self.prompt = prompt

    async def receive_events(self, timeout=300):
        await asyncio.sleep(0.1)
        yield {"type": "command.completed"}

    async def cancel(self):
        self.cancelled = True


class Bindings:
    allow = Permission.allow
    deny = Permission.deny

    def __init__(self, client_factory):
        self.client = None
        self.client_factory_impl = client_factory

    @staticmethod
    def options_factory(**kwargs):
        return SimpleNamespace(**kwargs)

    def client_factory(self, options):
        self.client = self.client_factory_impl(options)
        return self.client


@pytest.mark.asyncio
async def test_approval_callback_timeout_is_denied_by_the_sdk(tmp_path):
    store = SQLiteEventStore(tmp_path / "approval-timeout.sqlite3")
    job = store.create_job("approval task", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    context = SimpleNamespace(requires_human=True, tool_call_id="call-timeout", reason="prompt")

    class ApprovalClient(SlowClient):
        async def query(self, prompt):
            decision = await self.options.can_use_tool(
                "bash", {"command": "true"}, context
            )
            assert decision.behavior == "deny"

        async def receive_events(self, timeout=300):
            yield {"type": "tool.completed", "status": "cancelled"}
            yield {"type": "command.failed", "error": {"message": "approval timed out"}}

    async def slow_approval(_request):
        await asyncio.sleep(0.1)
        return True

    runner = AdalSdkRunner(
        store,
        approval_handler=slow_approval,
        sdk_bindings=Bindings(lambda options: _with_options(ApprovalClient(), options)),
        approval_timeout_seconds=0.01,
    )
    result = await runner.run_turn(job.job_id, "approval task", tmp_path)
    assert result.status == "paused"
    assert result.permission_denied
    decisions = [event.payload for event in store.get_events(job.job_id) if event.event_type == "approval.decided"]
    assert decisions[0]["decision"] == "approval_handler_timeout"


@pytest.mark.asyncio
async def test_timeout_is_classified_and_runtime_is_cancelled(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    job = store.create_job("long job", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    bindings = Bindings(lambda _options: SlowClient())
    runner = AdalSdkRunner(store, sdk_bindings=bindings)

    result = await runner.run_turn(job.job_id, "long job", tmp_path, timeout_seconds=0.01)

    assert result.status == "failed"
    assert "timeout" in (result.error or "").casefold()
    assert bindings.client.cancelled is True
    assert any(event.event_type == "turn.failed" for event in store.get_events(job.job_id))


@pytest.mark.asyncio
async def test_stream_without_terminal_event_fails_closed(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    job = store.create_job("incomplete", tmp_path)
    store.append_event(job.job_id, "run.started", {})

    class IncompleteClient(SlowClient):
        async def receive_events(self, timeout=300):
            yield {"type": "assistant.delta", "text": "Partial answer"}

    runner = AdalSdkRunner(store, sdk_bindings=Bindings(lambda _options: IncompleteClient()))
    result = await runner.run_turn(job.job_id, "incomplete", tmp_path)

    assert result.status == "failed"
    assert result.error == "AdaL event stream ended without a terminal event"
    assert result.answer == "Partial answer"


@pytest.mark.asyncio
async def test_approval_handler_exception_denies_and_records_only_error_type(tmp_path):
    store = SQLiteEventStore(tmp_path / "runs.sqlite3")
    job = store.create_job("approval task", tmp_path)
    store.append_event(job.job_id, "run.started", {})
    context = SimpleNamespace(requires_human=True, tool_call_id="call-safe", reason="prompt")

    class ApprovalClient(SlowClient):
        async def query(self, prompt):
            decision = await self.options.can_use_tool(
                "bash", {"command": "private command value"}, context
            )
            assert decision.behavior == "deny"

        async def receive_events(self, timeout=300):
            yield {"type": "tool.completed", "status": "cancelled"}
            yield {"type": "command.failed", "error": {"message": "denied"}}

    async def broken_approval(_request):
        raise RuntimeError("sensitive UI detail")

    runner = AdalSdkRunner(
        store,
        approval_handler=broken_approval,
        sdk_bindings=Bindings(lambda options: _with_options(ApprovalClient(), options)),
    )
    result = await runner.run_turn(job.job_id, "approval task", tmp_path)

    assert result.status == "paused"
    assert result.permission_denied is True
    payloads = [event.payload for event in store.get_events(job.job_id)]
    assert "approval_handler_error:RuntimeError" in str(payloads)
    assert "sensitive UI detail" not in str(payloads)
    assert "private command value" not in str(payloads)


def _with_options(client, options):
    client.options = options
    return client
