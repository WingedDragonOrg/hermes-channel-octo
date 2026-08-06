"""Standalone sender contract tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes_octo_plugin import adapter as adapter_module
from hermes_octo_plugin import api
from hermes_octo_plugin.adapter import _standalone_send
from hermes_octo_plugin.types import SendMessageResult


class _Session:
    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_standalone_sender_returns_real_server_message_identity():
    config = SimpleNamespace(
        extra={"api_url": "https://api.example.invalid", "bot_token": "test-token"},
        token="",
    )
    with (
        patch.object(
            adapter_module,
            "_new_guarded_http_session",
            return_value=_Session(),
        ) as guarded_session,
        patch.object(
            api,
            "send_message",
            AsyncMock(
                return_value=SendMessageResult(
                    message_id="9223372036854775807", message_seq=4
                )
            ),
        ),
    ):
        result = await _standalone_send(config, "group-1", "hello")

    assert result == {"success": True, "message_id": "9223372036854775807"}
    guarded_session.assert_called_once_with("https://api.example.invalid")
