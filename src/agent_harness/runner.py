from __future__ import annotations

import asyncio
import copy
import inspect
import math
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent_harness.models import JobStatus
from agent_harness.store import SQLiteEventStore


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Safe UI progress; private reasoning and raw tool I/O are omitted."""

    kind: str
    message: str
    tool_name: str | None = None
    tool_call_id: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """A copied tool proposal presented to the caller's approval surface."""

    tool_name: str
    tool_input: dict[str, Any]
    call_id: str
    confirmation: dict[str, Any] | None = None


ApprovalHandler = Callable[[ApprovalRequest], bool | Awaitable[bool]]
ProgressHandler = Callable[[ProgressEvent], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Outcome of one AdaL SDK query."""

    status: str
    answer: str
    session_id: str | None
    model: str | None
    error: str | None = None
    permission_denied: bool = False
    duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _SdkBindings:
    client_factory: Callable[..., Any]
    options_factory: Callable[..., Any]
    allow: Callable[[], Any]
    deny: Callable[[str], Any]


class AdalSdkRunner:
    """Thin outer-loop adapter around AdaL's supported Python SDK.

    The SDK owns model/tool execution and its conversation. This adapter owns
    job correlation, bounded progress, approval policy and local run events.
    """

    def __init__(
        self,
        event_store: SQLiteEventStore,
        *,
        approval_handler: ApprovalHandler | None = None,
        sdk_bindings: Any | None = None,
        approval_timeout_seconds: float = 300.0,
    ) -> None:
        if (
            not isinstance(approval_timeout_seconds, (int, float))
            or isinstance(approval_timeout_seconds, bool)
            or not math.isfinite(approval_timeout_seconds)
            or approval_timeout_seconds <= 0
        ):
            raise ValueError("approval_timeout_seconds must be a finite number greater than zero")
        self.event_store = event_store
        self.approval_handler = approval_handler
        self._sdk_bindings = sdk_bindings
        self.approval_timeout_seconds = approval_timeout_seconds

    async def run_turn(
        self,
        job_id: str,
        prompt: str,
        workspace: str | Path,
        *,
        model: str | None = None,
        session_id: str | None = None,
        timeout_seconds: float = 300.0,
        on_progress: ProgressHandler | None = None,
    ) -> TurnResult:
        """Run one bounded AdaL turn and append a redacted event projection.

        The ledger stores the task, final answer and failure message locally. It
        does not store model reasoning, tool arguments/results or ambient env.
        The owner must still protect the local database and back it up carefully.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must not be empty")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a finite number greater than zero")
        try:
            workspace_path = Path(workspace).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError) as exc:
            raise ValueError("workspace must be an existing directory") from exc
        if not workspace_path.is_dir():
            raise ValueError("workspace must be an existing directory")

        if model is not None and not isinstance(model, str):
            raise ValueError("model must be a string or None")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            raise ValueError("session_id must be a non-empty string or None")
        created_payload = self.event_store.get_creation_event(job_id).payload
        recorded_workspace = created_payload.get("workspace")
        if not isinstance(recorded_workspace, str):
            raise ValueError("job is missing its recorded workspace")
        try:
            expected_workspace = Path(recorded_workspace).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, TypeError) as exc:
            raise ValueError("job workspace no longer exists") from exc
        if expected_workspace != workspace_path:
            raise ValueError("workspace does not match the workspace recorded for this job")
        snapshot = self.event_store.get_snapshot(job_id)
        if snapshot.status != JobStatus.RUNNING:
            raise ValueError(
                f"job must be running before an AdaL turn starts, got {snapshot.status.value}"
            )

        turn_id = uuid.uuid4().hex
        approval_state = {"denied": False}
        started_at = time.monotonic()
        active_session_id = session_id
        answer_deltas: list[str] = []
        completed_messages: list[str] = []
        displayed_delta_text = ""
        error: str | None = None
        status = "failed"
        runtime_error_seen = False
        terminal_type: str | None = None
        client: Any | None = None
        events_written = False
        self.event_store.append_event(
            job_id,
            "turn.started",
            {"turn_id": turn_id, "model": model, "workspace": str(workspace_path)},
            idempotency_key=f"turn:{turn_id}:started",
        )
        events_written = True

        try:
            bindings = self._sdk_bindings or self._load_sdk()

            async def can_use_tool(
                tool_name: str, tool_input: dict[str, Any], context: Any
            ) -> Any:
                return await self._authorize(
                    job_id,
                    turn_id,
                    tool_name,
                    tool_input,
                    context,
                    bindings,
                    approval_state,
                )

            options = bindings.options_factory(
                workspace=str(workspace_path),
                model=model,
                session_id=session_id,
                permission_mode="default",
                can_use_tool=can_use_tool,
                # Let the harness return a fail-closed decision just before the SDK timeout.
                can_use_tool_timeout=self.approval_timeout_seconds + 1.0,
            )
            client = bindings.client_factory(options)

            async with asyncio.timeout(timeout_seconds):
                async with client as active_client:
                    active_session_id = active_client.session_id or session_id
                    if active_session_id:
                        self.event_store.append_event(
                            job_id,
                            "session.started",
                            {"session_id": active_session_id, "model": model},
                        )
                    await active_client.query(prompt)
                    async for raw_event in active_client.receive_events(timeout=timeout_seconds):
                        if not isinstance(raw_event, dict):
                            raise ValueError("AdaL SDK yielded a non-object event")
                        event_type = raw_event.get("type")
                        if not isinstance(event_type, str):
                            raise ValueError("AdaL SDK event has no string type")

                        if event_type in {"thought.delta", "reasoning.delta"}:
                            await self._publish(
                                ProgressEvent(
                                    "thinking",
                                    "Model is working; private reasoning text is not displayed.",
                                ),
                                on_progress,
                            )
                            continue

                        if event_type == "assistant.delta":
                            text = self._assistant_text(raw_event)
                            if text:
                                answer_deltas.append(text)
                                displayed_delta_text += text
                                await self._publish(
                                    ProgressEvent("assistant_output", text), on_progress
                                )
                            continue

                        if event_type == "assistant.message.completed":
                            text = self._assistant_text(raw_event)
                            if text:
                                completed_messages.append(text)
                                if text not in displayed_delta_text:
                                    await self._publish(
                                        ProgressEvent("assistant_output", text), on_progress
                                    )
                            continue

                        if event_type == "tool.started":
                            name = raw_event.get("name")
                            call_id = raw_event.get("toolCallId") or raw_event.get("tool_call_id")
                            safe_name = name if isinstance(name, str) else "unknown"
                            safe_id = call_id if isinstance(call_id, str) else None
                            self.event_store.append_event(
                                job_id,
                                "tool.started",
                                {"turn_id": turn_id, "tool_name": safe_name, "call_id": safe_id},
                            )
                            await self._publish(
                                ProgressEvent(
                                    "tool_started",
                                    f"Running {safe_name}",
                                    safe_name,
                                    safe_id,
                                    "running",
                                ),
                                on_progress,
                            )
                            continue

                        if event_type == "tool.completed":
                            name = raw_event.get("name")
                            call_id = raw_event.get("toolCallId") or raw_event.get("tool_call_id")
                            tool_status = raw_event.get("status")
                            safe_name = name if isinstance(name, str) else "unknown"
                            safe_id = call_id if isinstance(call_id, str) else None
                            safe_status = tool_status if isinstance(tool_status, str) else "unknown"
                            result_payload = raw_event.get("result")
                            metadata = (
                                result_payload.get("metadata", {})
                                if isinstance(result_payload, dict)
                                else {}
                            )
                            if (
                                isinstance(metadata, dict)
                                and metadata.get("permission") == "unattended"
                            ):
                                approval_state["denied"] = True
                            self.event_store.append_event(
                                job_id,
                                "tool.completed",
                                {
                                    "turn_id": turn_id,
                                    "tool_name": safe_name,
                                    "call_id": safe_id,
                                    "status": safe_status,
                                },
                            )
                            await self._publish(
                                ProgressEvent(
                                    "tool_completed",
                                    f"{safe_name} {safe_status}",
                                    safe_name,
                                    safe_id,
                                    safe_status,
                                ),
                                on_progress,
                            )
                            continue

                        if event_type.startswith("subagent."):
                            stage = event_type.removeprefix("subagent.")
                            self.event_store.append_event(
                                job_id,
                                "runtime.event",
                                {"turn_id": turn_id, "event_type": event_type},
                            )
                            await self._publish(
                                ProgressEvent(
                                    "subagent",
                                    f"AdaL reported subagent event: {stage}",
                                    status=stage,
                                ),
                                on_progress,
                            )
                            continue

                        if event_type == "command.progress":
                            message = raw_event.get("message")
                            if isinstance(message, str):
                                await self._publish(
                                    ProgressEvent("progress", message[:1000]), on_progress
                                )
                            continue

                        if event_type == "command.completed":
                            if terminal_type is not None:
                                error = "AdaL emitted more than one terminal command event"
                                status = "failed"
                                continue
                            terminal_type = event_type
                            status = "paused" if approval_state["denied"] else "completed"
                            continue

                        if event_type == "command.failed":
                            if terminal_type is not None:
                                error = "AdaL emitted more than one terminal command event"
                                status = "failed"
                                continue
                            terminal_type = event_type
                            error = self._event_error(raw_event.get("error")) or "AdaL query failed"
                            status = "paused" if approval_state["denied"] else "failed"
                            continue

                        if event_type == "ui.message.appended":
                            message = raw_event.get("message")
                            if isinstance(message, dict):
                                notice = message.get("content") or message.get("text")
                                if isinstance(notice, str):
                                    await self._publish(
                                        ProgressEvent("notice", notice[:1000]), on_progress
                                    )
                            continue

                        if event_type == "error":
                            runtime_error_seen = True
                            error = self._event_error(raw_event.get("error")) or self._event_error(
                                raw_event.get("message")
                            ) or "AdaL runtime reported an error"
                            continue

                        # Future SDK events remain visible by type, not raw payload.
                        self.event_store.append_event(
                            job_id,
                            "runtime.event",
                            {"turn_id": turn_id, "event_type": event_type},
                        )
                        await self._publish(
                            ProgressEvent("runtime_event", f"AdaL event: {event_type}"),
                            on_progress,
                        )

                    active_session_id = active_client.session_id or active_session_id
                    if terminal_type is None:
                        error = error or "AdaL event stream ended without a terminal event"
                        status = "paused" if approval_state["denied"] else "failed"
                    elif runtime_error_seen and status == "completed":
                        status = "failed"
        except TimeoutError:
            error = f"AdaL turn exceeded its {timeout_seconds:g}s timeout"
            status = "paused" if approval_state["denied"] else "failed"
            await self._cancel_safely(client)
        except asyncio.CancelledError:
            await self._cancel_safely(client)
            if events_written:
                self.event_store.append_event(
                    job_id,
                    "turn.cancelled",
                    {"turn_id": turn_id, "session_id": active_session_id},
                )
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            status = "paused" if approval_state["denied"] else "failed"
            await self._cancel_safely(client)

        answer = (
            "\n".join(completed_messages)
            if completed_messages
            else "".join(answer_deltas)
        )
        if status == "completed":
            self.event_store.append_event(
                job_id,
                "turn.completed",
                {
                    "turn_id": turn_id,
                    "answer": answer,
                    "session_id": active_session_id,
                    "model": model,
                    "duration_seconds": time.monotonic() - started_at,
                },
            )
        elif status == "paused":
            self.event_store.append_event(
                job_id,
                "turn.paused",
                {
                    "turn_id": turn_id,
                    "session_id": active_session_id,
                    "reason": "approval_required",
                },
            )
        else:
            self.event_store.append_event(
                job_id,
                "turn.failed",
                {
                    "turn_id": turn_id,
                    "session_id": active_session_id,
                    "error": error or "AdaL turn failed",
                    "error_type": "timeout" if error and "timeout" in error.casefold() else "runtime",
                },
            )
        return TurnResult(
            status=status,
            answer=answer,
            session_id=active_session_id,
            model=model,
            error=error,
            permission_denied=approval_state["denied"],
            duration_seconds=time.monotonic() - started_at,
        )

    async def _authorize(
        self,
        job_id: str,
        turn_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        context: Any,
        bindings: Any,
        approval_state: dict[str, bool],
    ) -> Any:
        requires_human = getattr(context, "requires_human", None)
        call_id = getattr(context, "tool_call_id", "")
        call_id = call_id[:256] if isinstance(call_id, str) else ""
        reason = getattr(context, "reason", "unknown")
        reason = reason[:64] if isinstance(reason, str) else "unknown"
        if requires_human is False:
            self.event_store.append_event(
                job_id,
                "tool.policy_allowed",
                {"turn_id": turn_id, "call_id": call_id, "tool_name": tool_name, "reason": reason},
            )
            return bindings.allow()

        confirmation = getattr(context, "confirmation", None)
        confirmation = confirmation if isinstance(confirmation, dict) else None
        self.event_store.append_event(
            job_id,
            "approval.requested",
            {
                "turn_id": turn_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "reason": reason,
            },
        )

        allowed = False
        decision = "no_approval_handler"
        if self.approval_handler is not None:
            try:
                request = ApprovalRequest(
                    tool_name=tool_name,
                    tool_input=copy.deepcopy(tool_input),
                    call_id=call_id,
                    confirmation=copy.deepcopy(confirmation),
                )
                async with asyncio.timeout(self.approval_timeout_seconds):
                    response = self.approval_handler(request)
                    if inspect.isawaitable(response):
                        response = await response
                if type(response) is bool:
                    allowed = response
                    decision = "approved" if allowed else "denied"
                else:
                    decision = "approval_handler_invalid_result"
            except TimeoutError:
                decision = "approval_handler_timeout"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                decision = f"approval_handler_error:{type(exc).__name__}"

        self.event_store.append_event(
            job_id,
            "approval.decided",
            {"turn_id": turn_id, "call_id": call_id, "tool_name": tool_name, "decision": decision},
        )
        if allowed:
            return bindings.allow()
        approval_state["denied"] = True
        return bindings.deny("Tool call was not approved by the user.")

    @staticmethod
    async def _publish(event: ProgressEvent, handler: ProgressHandler | None) -> None:
        if handler is None:
            return
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # A disconnected view cannot cancel a user-requested tool run.
            return

    @staticmethod
    def _assistant_text(event: dict[str, Any]) -> str:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else event.get("text")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        return ""

    @staticmethod
    def _event_error(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("message"), str):
            return value["message"]
        return ""

    @staticmethod
    def _load_sdk() -> _SdkBindings:
        try:
            from adal_agent_sdk import AdalAgentClient, AdalAgentOptions, PermissionResult
        except ImportError as exc:
            raise RuntimeError(
                "AdaL SDK is not installed; install this project with `pip install -e '.[adal]'`."
            ) from exc
        return _SdkBindings(
            client_factory=AdalAgentClient,
            options_factory=AdalAgentOptions,
            allow=PermissionResult.allow,
            deny=PermissionResult.deny,
        )

    @staticmethod
    async def _cancel_safely(client: Any | None) -> None:
        if client is None:
            return
        try:
            await client.cancel()
        except Exception:
            return
