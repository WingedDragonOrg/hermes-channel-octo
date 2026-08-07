"""Progress-card lifecycle and concurrency contracts."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_octo_plugin import card_progress, cards, types
from hermes_octo_plugin.card_tools import TrustedOctoRoute
from hermes_octo_plugin.types import CardProfileManifest, ChannelType, SendMessageResult


class _Adapter:
    _api_url = "https://api.example.invalid"
    _bot_token = "test-token"
    _http_session = object()
    _disconnecting = False
    on_behalf_of: str | None = None

    def __init__(self) -> None:
        self.pending: list[Callable[[], Coroutine[Any, Any, None]]] = []
        self._card_profile_cache = cards.CardProfileCache()

    def _schedule_card_progress(
        self,
        factory: Callable[[], Coroutine[Any, Any, None]],
    ) -> bool:
        self.pending.append(factory)
        return True

    async def run_next(self) -> None:
        await self.pending.pop(0)()


_ROUTE = TrustedOctoRoute(
    chat_id="group-1",
    channel_id="group-1",
    channel_type=ChannelType.Group,
    requester_uid="user-1",
    session_key="octo:group-1:user-1",
)
_MANIFEST = CardProfileManifest(
    available=True,
    enabled=True,
    profiles=("octo/v1",),
    card_version="1.5",
    elements=("TextBlock",),
)


def test_progress_renderer_uses_only_safe_bounded_summaries() -> None:
    rendered = cards.build_progress_card(
        phase="running",
        tools=[
            {
                "tool_call_id": "call-1",
                "tool_name": "read",
                "args": {
                    "path": "/Users/example/project/config.py",
                    "token": "AKIA1234567890ABCDEF",
                },
                "status": "running",
            },
            {
                "tool_call_id": "call-2",
                "tool_name": "mcp__untrusted__tool-token-AKIA1234567890ABCDEF",
                "args": {"secret": "hidden"},
                "status": "failed",
                "error": "Authorization: Bearer hidden",
            },
        ],
    )

    assert "config.py" in rendered.plain
    assert "MCP tool" in rendered.plain
    assert "AKIA1234567890ABCDEF" not in rendered.plain
    assert "hidden" not in rendered.plain
    assert "reasoning" not in rendered.plain.lower()


@pytest.mark.asyncio
async def test_progress_lifecycle_sends_then_edits_transient_and_final() -> None:
    controller = card_progress.CardProgressController()
    adapter = _Adapter()
    send = AsyncMock(return_value=SendMessageResult(message_id="progress-1"))
    edit = AsyncMock(return_value={})
    with (
        patch.object(
            card_progress.api, "get_card_profile", AsyncMock(return_value=_MANIFEST)
        ),
        patch.object(card_progress.api, "send_card_message", send),
        patch.object(card_progress.api, "edit_card_message", edit),
    ):
        controller.begin(
            adapter=adapter,
            route=_ROUTE,
            session_id="session-1",
            turn_id="turn-1",
        )
        await adapter.run_next()

        controller.tool_started(
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-a",
            tool_name="read",
            args={"path": "/tmp/a.py"},
        )
        controller.tool_started(
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-b",
            tool_name="bash",
            args={"command": "pytest -q"},
        )
        controller.tool_finished(
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-b",
            tool_name="bash",
            status="error",
            error="secret-token-AKIA1234567890ABCDEF",
        )
        controller.tool_finished(
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-a",
            tool_name="read",
            status="ok",
        )
        await adapter.run_next()

        controller.complete(session_id="session-1", turn_id="turn-1")
        await adapter.run_next()

    assert send.await_count == 1
    assert edit.await_count == 1
    sent = send.await_args.kwargs
    final = edit.await_args.kwargs
    assert "read (/tmp/a.py): complete" in sent["plain"]
    assert "bash (pytest): failed" in sent["plain"]
    assert "AKIA1234567890ABCDEF" not in sent["plain"]
    assert final["card_seq"] == 1
    assert final["transient"] is False
    assert final["plain"].startswith("Completed")
    assert controller.state_count == 0


def test_progress_is_disabled_for_on_behalf_of_delivery() -> None:
    controller = card_progress.CardProgressController()
    adapter = _Adapter()
    adapter.on_behalf_of = "grantor-1"

    controller.begin(
        adapter=adapter,
        route=_ROUTE,
        session_id="session-1",
        turn_id="turn-1",
    )

    assert controller.state_count == 0
    assert adapter.pending == []

@pytest.mark.asyncio
async def test_progress_stays_silent_for_pure_model_turn_and_card_authoring_tools() -> None:
    controller = card_progress.CardProgressController()
    adapter = _Adapter()
    send = AsyncMock(return_value=SendMessageResult(message_id="unexpected"))
    with (
        patch.object(
            card_progress.api, "get_card_profile", AsyncMock(return_value=_MANIFEST)
        ),
        patch.object(card_progress.api, "send_card_message", send),
    ):
        controller.begin(
            adapter=adapter,
            route=_ROUTE,
            session_id="session-1",
            turn_id="turn-1",
        )
        controller.model_started(
            session_id="session-1", turn_id="turn-1", model_call_id="model-1"
        )
        controller.model_finished(
            session_id="session-1", turn_id="turn-1", model_call_id="model-1"
        )
        controller.tool_started(
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="card-1",
            tool_name="octo_send_display_card",
            args={"title": "Result"},
        )
        controller.complete(session_id="session-1", turn_id="turn-1")
        while adapter.pending:
            await adapter.run_next()

    send.assert_not_awaited()
    assert controller.state_count == 0




@pytest.mark.asyncio
async def test_progress_turns_are_isolated_and_session_cancel_drops_pending_work() -> (
    None
):
    controller = card_progress.CardProgressController()
    adapter = _Adapter()
    send = AsyncMock(
        side_effect=[
            SendMessageResult(message_id="progress-1"),
            SendMessageResult(message_id="progress-2"),
        ]
    )
    with (
        patch.object(
            card_progress.api, "get_card_profile", AsyncMock(return_value=_MANIFEST)
        ),
        patch.object(card_progress.api, "send_card_message", send),
    ):
        controller.begin(
            adapter=adapter,
            route=_ROUTE,
            session_id="session-1",
            turn_id="turn-1",
        )
        controller.begin(
            adapter=adapter,
            route=_ROUTE,
            session_id="session-2",
            turn_id="turn-2",
        )
        controller.tool_started(
            session_id="session-2",
            turn_id="turn-2",
            tool_call_id="call-2",
            tool_name="read",
            args={"path": "/tmp/two.py"},
        )
        assert controller.state_count == 2
        controller.cancel_session("session-1")
        assert controller.state_count == 1
        await adapter.run_next()
        await adapter.run_next()

    assert send.await_count == 1
    assert send.await_args.kwargs["client_msg_no"].endswith("turn-2")


@pytest.mark.asyncio
async def test_progress_delivery_failure_is_fail_soft() -> None:
    controller = card_progress.CardProgressController()
    adapter = _Adapter()
    with (
        patch.object(
            card_progress.api, "get_card_profile", AsyncMock(return_value=_MANIFEST)
        ),
        patch.object(
            card_progress.api,
            "send_card_message",
            AsyncMock(side_effect=RuntimeError("network down")),
        ),
    ):
        controller.begin(
            adapter=adapter,
            route=_ROUTE,
            session_id="session-1",
            turn_id="turn-1",
        )
        controller.tool_started(
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
            tool_name="read",
            args={"path": "/tmp/input.py"},
        )
        await adapter.run_next()

    assert controller.state_count == 0


def test_session_end_finalizes_the_exact_turn_instead_of_cancelling_it() -> None:
    controller = MagicMock()
    with patch.object(card_progress, "_CONTROLLER", controller):
        card_progress.on_session_end(
            session_id="session-1",
            task_id="task-1",
            turn_id="turn-1",
            completed=False,
            failed=False,
            interrupted=False,
            platform="octo",
        )

    controller.complete.assert_called_once_with(
        session_id="session-1",
        turn_id="turn-1",
        failed=True,
    )
    controller.cancel_session.assert_not_called()


def test_post_llm_waits_for_authoritative_session_end_status() -> None:
    controller = MagicMock()
    with patch.object(card_progress, "_CONTROLLER", controller):
        card_progress.on_post_llm_call(
            session_id="session-1",
            turn_id="turn-1",
            platform="octo",
        )
        controller.complete.assert_not_called()
        card_progress.on_session_end(
            session_id="session-1",
            turn_id="turn-1",
            completed=False,
            failed=True,
            interrupted=False,
            platform="octo",
        )

    controller.complete.assert_called_once_with(
        session_id="session-1",
        turn_id="turn-1",
        failed=True,
    )


@pytest.mark.asyncio
async def test_failed_turn_finalizes_as_stopped_and_closes_running_tools() -> None:
    controller = card_progress.CardProgressController()
    adapter = _Adapter()
    send = AsyncMock(return_value=SendMessageResult(message_id="progress-1"))
    edit = AsyncMock(return_value={})
    with (
        patch.object(
            card_progress.api,
            "get_card_profile",
            AsyncMock(return_value=_MANIFEST),
        ),
        patch.object(card_progress.api, "send_card_message", send),
        patch.object(card_progress.api, "edit_card_message", edit),
    ):
        controller.begin(
            adapter=adapter,
            route=_ROUTE,
            session_id="session-1",
            turn_id="turn-1",
        )
        controller.tool_started(
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
            tool_name="read",
            args={"path": "/tmp/input.txt"},
        )
        controller.complete(
            session_id="session-1",
            turn_id="turn-1",
            failed=True,
        )
        await adapter.run_next()

    edit.assert_not_awaited()
    final = send.await_args.kwargs
    assert final["plain"].startswith("Stopped")
    assert "failed" in final["plain"]
    assert controller.state_count == 0


@pytest.mark.asyncio
async def test_progress_keeps_rendering_after_more_than_32_tool_calls() -> None:
    controller = card_progress.CardProgressController()
    adapter = _Adapter()
    send = AsyncMock(return_value=SendMessageResult(message_id="progress-1"))
    edit = AsyncMock(return_value={})
    with (
        patch.object(
            card_progress.api,
            "get_card_profile",
            AsyncMock(return_value=_MANIFEST),
        ),
        patch.object(
            card_progress.api,
            "send_card_message",
            send,
        ),
        patch.object(card_progress.api, "edit_card_message", edit),
    ):
        controller.begin(
            adapter=adapter,
            route=_ROUTE,
            session_id="session-1",
            turn_id="turn-1",
        )
        for index in range(33):
            controller.tool_started(
                session_id="session-1",
                turn_id="turn-1",
                tool_call_id=f"call-{index}",
                tool_name="read",
                args={"path": f"/tmp/{index}.txt"},
            )
        controller.complete(session_id="session-1", turn_id="turn-1")
        await adapter.run_next()

    send.assert_awaited_once()
    edit.assert_not_awaited()
    assert controller.state_count == 0


def test_reasoning_process_data_matches_openclaw_summary_contract() -> None:
    data = cards.build_reasoning_process_data(
        phase="completed",
        tools=[
            {
                "tool_name": "__thinking__",
                "status": "complete",
                "thought": "Inspect the relevant implementation.",
                "duration_ms": 1_200,
            },
            {
                "tool_name": "read",
                "status": "complete",
                "summary": "…/src/cards.py",
                "result_summary": "42 results",
                "duration_ms": 300,
            },
            {
                "tool_name": "bash",
                "status": "failed",
                "summary": "pytest",
                "error": "command failed",
                "duration_ms": 800,
            },
        ],
        elapsed_ms=12_000,
        reasoning_id="session-1:turn-1",
    )

    assert data["state"] == "completed"
    assert data["statusLabel"] == "Done"
    assert data["traceExpanded"] is False
    assert data["traceCollapsed"] is True
    assert data["timerText"] == "12.0s · 1 phase · 2 tool calls"
    assert data["phases"] == [
        {
            "thought": "Inspect the relevant implementation.",
            "actions": [
                {
                    "tool": "read",
                    "detail": "…/src/cards.py · 42 results",
                    "statusGlyph": "●",
                    "statusTone": "Good",
                },
                {
                    "tool": "bash",
                    "detail": "pytest · command failed",
                    "statusGlyph": "●",
                    "statusTone": "Attention",
                },
            ],
        }
    ]


def test_reasoning_process_renderer_uses_collapsed_terminal_trace() -> None:
    rendered = cards.build_reasoning_process_card(
        phase="completed",
        tools=[
            {
                "tool_name": "__thinking__",
                "status": "complete",
                "thought": "Check the implementation.",
            },
            {
                "tool_name": "read",
                "status": "complete",
                "summary": "…/src/cards.py",
                "result_summary": "completed",
            },
        ],
        elapsed_ms=2_000,
        reasoning_id="session-1:turn-1",
        capabilities=cards.CardCapabilities(
            available=True,
            enabled=True,
            elements=frozenset({"TextBlock", "Container", "ColumnSet", "ActionSet"}),
            actions=frozenset({"Action.ToggleVisibility"}),
        ),
    )

    body = rendered.card["body"]
    assert rendered.card["metadata"] == {"octo_layout": "agent_progress_v1"}
    assert body[0]["id"] == "octo-surface-accent-header-reasoning-active"
    assert body[1]["id"] == "trace_panel"
    assert body[1]["isVisible"] is False
    assert body[2]["id"] == "collapsed_panel"
    assert body[2]["isVisible"] is True
    toggle = body[3]["items"][0]["actions"][0]
    assert toggle == {
        "type": "Action.ToggleVisibility",
        "id": "reasoning_toggle",
        "title": "Show / hide reasoning",
        "targetElements": ["trace_panel", "collapsed_panel"],
    }
    assert rendered.plain.startswith("Done · 2.0s · 1 phase · 1 tool call")
    assert "Check the implementation." in rendered.plain
    assert "read · …/src/cards.py · completed" in rendered.plain


def test_reasoning_summary_never_uses_raw_cot_or_tool_output() -> None:
    assert (
        cards.sanitize_reasoning_thought("Authorization: Bearer abcdefghijklmnop")
        == "Thinking through…"
    )
    assert (
        cards.summarize_tool_result(
            "bash",
            {
                "details": {"exitCode": 0},
                "content": [{"type": "text", "text": "Bearer raw-secret"}],
            },
        )
        == "exit 0"
    )
    assert (
        cards.summarize_tool_result(
            "read",
            {"content": "complete file contents"},
        )
        == "completed"
    )


def test_api_hooks_capture_only_public_reasoning_summary() -> None:
    controller = MagicMock()
    with (
        patch.object(card_progress, "_CONTROLLER", controller),
        patch.object(
            card_progress,
            "_reasoning_summaries_enabled",
            return_value=True,
        ),
    ):
        card_progress.on_pre_api_request(
            platform="octo",
            session_id="session-1",
            turn_id="turn-1",
            api_request_id="api-1",
        )
        card_progress.on_post_api_request(
            platform="octo",
            session_id="session-1",
            turn_id="turn-1",
            api_request_id="api-1",
            api_duration=1.25,
            assistant_message={
                "reasoning": "raw hidden chain of thought",
                "reasoning_content": "raw provider scratchpad",
                "reasoning_details": [
                    {
                        "type": "reasoning.summary",
                        "summary": "Inspecting the card lifecycle.",
                    }
                ],
                "codex_reasoning_items": [
                    {
                        "type": "reasoning",
                        "encrypted_content": "opaque-hidden-state",
                        "text": "raw hidden Codex reasoning",
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": "Checking the negotiated renderer.",
                            }
                        ],
                    }
                ],
                "content": None,
                "tool_calls": [{"id": "call-1"}],
            },
        )

    controller.model_started.assert_called_once_with(
        session_id="session-1",
        turn_id="turn-1",
        model_call_id="api-1",
    )
    controller.model_finished.assert_called_once_with(
        session_id="session-1",
        turn_id="turn-1",
        model_call_id="api-1",
        thought="Inspecting the card lifecycle. Checking the negotiated renderer.",
        duration_ms=1_250,
        answering=False,
        failed=False,
    )


@pytest.mark.asyncio
async def test_progress_prefers_compatible_registry_reasoning_template() -> None:
    controller = card_progress.CardProgressController()
    adapter = _Adapter()
    manifest = CardProfileManifest(
        available=True,
        enabled=True,
        templating=types.CardTemplatingCapability(
            supported=True,
            wire="template-ref/v1",
            templates=(
                types.CardTemplateCapability(
                    id="ai.reasoning-process",
                    version="0.3.0",
                    views=(
                        types.CardTemplateViewCapability(
                            name="active",
                            wire_profile="octo/v2",
                            states=("reasoning", "answering"),
                        ),
                        types.CardTemplateViewCapability(
                            name="error",
                            wire_profile="octo/v2",
                            states=("error",),
                        ),
                        types.CardTemplateViewCapability(
                            name="result",
                            wire_profile="octo/v1",
                            states=("completed", "stopped"),
                        ),
                    ),
                ),
            ),
        ),
    )


    send_template = AsyncMock(return_value=SendMessageResult(message_id="reasoning-1"))
    edit_template = AsyncMock(return_value={})
    with (
        patch.object(
            card_progress.api,
            "get_card_profile",
            AsyncMock(return_value=manifest),
        ),
        patch.object(
            card_progress,
            "send_template_card_message",
            send_template,
        ),
        patch.object(
            card_progress,
            "edit_template_card_message",
            edit_template,
        ),
        patch.object(card_progress.api, "send_card_message", AsyncMock()) as send,
    ):
        controller.begin(
            adapter=adapter,
            route=_ROUTE,
            session_id="session-1",
            turn_id="turn-1",
        )
        controller.model_started(
            session_id="session-1",
            turn_id="turn-1",
            model_call_id="api-1",
        )
        controller.model_finished(
            session_id="session-1",
            turn_id="turn-1",
            model_call_id="api-1",
            thought="Inspect the implementation.",
            duration_ms=400,
        )
        controller.tool_started(
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
            tool_name="read",
            args={"path": "/work/src/cards.py"},
        )
        await adapter.run_next()
        controller.tool_finished(
            session_id="session-1",
            turn_id="turn-1",
            tool_call_id="call-1",
            tool_name="read",
            status="ok",
            duration_ms=200,
            result={"match_count": 7, "content": "must not be rendered"},
        )
        await adapter.run_next()
        controller.complete(session_id="session-1", turn_id="turn-1")
        await adapter.run_next()

    send.assert_not_awaited()
    assert send_template.await_args.kwargs["template_ref"] == {
        "id": "ai.reasoning-process",
        "version": "0.3.0",
    }
    assert send_template.await_args.kwargs["data"]["state"] == "reasoning"
    assert edit_template.await_args_list[-1].kwargs["state"] == "completed"
    assert edit_template.await_args_list[-1].kwargs["transient"] is False
    final_data = edit_template.await_args_list[-1].kwargs["data"]
    assert final_data["timerText"].endswith("1 tool call")
    assert final_data["phases"][0]["actions"][0]["detail"].endswith("7 results")
@pytest.mark.parametrize("action", ["reasoning_stop", "reasoning_retry"])
def test_registry_reasoning_template_rejects_unhandled_submit_actions(
    action: str,
) -> None:
    templating = types.CardTemplatingCapability(
        supported=True,
        wire="template-ref/v1",
        templates=(
            types.CardTemplateCapability(
                id="ai.reasoning-process",
                version="0.3.0",
                views=(
                    types.CardTemplateViewCapability(
                        name="active",
                        wire_profile="octo/v2",
                        states=("reasoning", "answering"),
                        submit_actions=(action,) if action == "reasoning_stop" else (),
                    ),
                    types.CardTemplateViewCapability(
                        name="error",
                        wire_profile="octo/v2",
                        states=("error",),
                        submit_actions=(action,) if action == "reasoning_retry" else (),
                    ),
                    types.CardTemplateViewCapability(
                        name="result",
                        wire_profile="octo/v1",
                        states=("completed", "stopped"),
                    ),
                ),
            ),
        ),
    )

    assert cards.select_reasoning_process_template(templating) is None
