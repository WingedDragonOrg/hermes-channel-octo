"""Native Hermes clarify delivery and action integration."""

from __future__ import annotations

import asyncio
import logging
import json
import threading
import uuid
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version as package_version
from typing import Any

import aiohttp
from gateway.platforms.base import SendResult

from . import api, cards
from .card_sessions import CardSession, ClarifySession
from .types import CARD_PROFILE_V2, ChannelType, SendMessageResult

logger = logging.getLogger(__name__)



_NATIVE_CLARIFY_RELEASE = (0, 20)
_native_clarify_gate_log: set[tuple[str, bool]] = set()


def native_clarify_supported() -> bool:
    try:
        from packaging.version import InvalidVersion, Version
    except ModuleNotFoundError:
        logger.warning(
            "[Octo] native clarify disabled: packaging.version is unavailable"
        )
        return False
    try:
        installed = Version(package_version("hermes-agent"))
    except PackageNotFoundError:
        logger.debug(
            "[Octo] native clarify disabled: hermes-agent distribution not found"
        )
        return False
    except InvalidVersion as exc:
        logger.warning(
            "[Octo] native clarify disabled: invalid hermes-agent version",
            exc_info=exc,
        )
        return False
    enabled = (
        installed.release[:2] == _NATIVE_CLARIFY_RELEASE
        and installed.pre is None
        and installed.dev is None
        and installed.post is None
        and installed.local is None
    )
    decision = (str(installed), enabled)
    if decision not in _native_clarify_gate_log:
        _native_clarify_gate_log.add(decision)
        logger.info(
            "[Octo] native clarify %s for hermes-agent %s; requires stable 0.20.x",
            "enabled" if enabled else "disabled",
            installed,
        )
    return enabled




_occurrence_lock = threading.Lock()
_clarify_occurrences: dict[str, str] = {}



def _register_occurrence(clarify_id: str, entry: object) -> str:
    token = str(id(entry))
    with _occurrence_lock:
        _clarify_occurrences[clarify_id] = token
    return token


def _occurrence_is_current(clarify_id: str, token: str) -> bool:
    with _occurrence_lock:
        return _clarify_occurrences.get(clarify_id) == token

def _clear_occurrence(clarify_id: str, token: str) -> None:
    with _occurrence_lock:
        if _clarify_occurrences.get(clarify_id) == token:
            _clarify_occurrences.pop(clarify_id, None)


def clarify_send_is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (aiohttp.ClientError, TimeoutError)):
        return True
    return isinstance(exc, api.OctoApiError) and (
        exc.status is None or exc.status in {408, 409, 425, 429} or exc.status >= 500
    )


def selected_choices(
    clarify: ClarifySession,
    action: Any,
) -> list[str]:
    if action.action_id == clarify.other_action_id:
        return []
    if not clarify.multi_select:
        return [
            choice
            for action_id, choice in clarify.action_choices
            if action_id == action.action_id
        ]
    if action.action_id != clarify.confirm_action_id or clarify.input_id is None:
        return []
    raw_selection = action.inputs.get(clarify.input_id, "")
    selected_ids = raw_selection.split(",") if raw_selection else []
    if (
        not selected_ids
        or any(not value or value.strip() != value for value in selected_ids)
        or len(set(selected_ids)) != len(selected_ids)
    ):
        return []
    selected = set(selected_ids)
    known_ids = {choice_id for choice_id, _ in clarify.action_choices}
    if not selected.issubset(known_ids):
        return []
    return [
        choice
        for choice_id, choice in clarify.action_choices
        if choice_id in selected
    ]


def _resolve_current_occurrence(
    session: CardSession,
    clarify: ClarifySession,
    response: str,
) -> bool:
    from tools import clarify_gateway

    with _occurrence_lock:
        if _clarify_occurrences.get(clarify.clarify_id) != clarify.occurrence_token:
            return False
        entry = clarify_gateway.get_pending_for_session(
            session.session_key,
            include_choice_prompts=True,
        )
        if entry is None or str(id(entry)) != clarify.occurrence_token:
            return False
        resolved = clarify_gateway.resolve_gateway_clarify_entry(entry, response)
        if resolved:
            _clarify_occurrences.pop(clarify.clarify_id, None)
        return resolved




async def dispatch_action(
    session: CardSession,
    action: Any,
) -> bool | str | None:
    """Resolve an owned clarify card atomically without creating a user turn."""
    clarify = session.clarify
    if clarify is None:
        return None

    from tools import clarify_gateway

    if action.action_id == clarify.other_action_id:
        with _occurrence_lock:
            if (
                _clarify_occurrences.get(clarify.clarify_id)
                != clarify.occurrence_token
            ):
                return "expired"
            entry = clarify_gateway.get_pending_for_session(
                session.session_key,
                include_choice_prompts=True,
            )
            if entry is None or str(id(entry)) != clarify.occurrence_token:
                return "expired"
            return (
                "awaiting_text"
                if clarify_gateway.mark_awaiting_text_entry(entry)
                else "expired"
            )

    if not _occurrence_is_current(
        clarify.clarify_id,
        clarify.occurrence_token,
    ):
        return "expired"

    if not clarify.multi_select:
        for action_id, response in clarify.action_choices:
            if action.action_id == action_id:
                return (
                    "completed"
                    if _resolve_current_occurrence(session, clarify, response)
                    else "expired"
                )
        return False

    if action.action_id != clarify.confirm_action_id or clarify.input_id is None:
        return False
    raw_selection = action.inputs.get(clarify.input_id, "")
    selected_ids = raw_selection.split(",") if raw_selection else []
    if not selected_ids:
        return False
    if any(not value or value.strip() != value for value in selected_ids):
        return False
    selected = set(selected_ids)
    if len(selected) != len(selected_ids):
        return False
    known_ids = {choice_id for choice_id, _ in clarify.action_choices}
    if not selected.issubset(known_ids):
        return False
    response = json.dumps(
        [
            choice
            for choice_id, choice in clarify.action_choices
            if choice_id in selected
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "completed"
        if _resolve_current_occurrence(session, clarify, response)
        else "expired"
    )


def _delivery_result(result: SendMessageResult) -> SendResult:
    if result.message_id is None:
        return SendResult(
            success=False,
            error="Octo send response missing message_id",
        )
    raw_response: dict[str, object] = {"message_id": result.message_id}
    if result.message_seq is not None:
        raw_response["message_seq"] = result.message_seq
    if result.client_msg_no is not None:
        raw_response["client_msg_no"] = result.client_msg_no
    return SendResult(
        success=True,
        message_id=result.message_id,
        raw_response=raw_response,
    )


from .card_tools import TrustedOctoRoute

def _shared_multi_user_session(adapter: Any, route: TrustedOctoRoute) -> bool:
    extra = getattr(adapter.config, "extra", None) or {}
    if route.channel_type == ChannelType.Group:
        return not bool(extra.get("group_sessions_per_user", True))
    if route.channel_type == ChannelType.CommunityTopic:
        return not bool(extra.get("thread_sessions_per_user", False))
    return False


def _direct_reply_hint(
    *,
    channel_type: ChannelType,
    require_mention: bool,
    shared_multi_user_session: bool,
) -> str:
    if channel_type == ChannelType.DM:
        return "也可以直接发送文字回答。"
    if require_mention:
        return (
            "群内成员可以点击回答；发送文字回答时，请 @机器人。"
            if shared_multi_user_session
            else "发送文字回答时，请 @机器人。"
        )
    return (
        "群内成员可以点击或直接发送文字回答。"
        if shared_multi_user_session
        else "也可以直接发送文字回答。"
    )


async def deliver(
    *,
    adapter: Any,
    chat_id: str,
    question: str,
    choices: list[Any] | None,
    clarify_id: str,
    session_key: str,
    fallback: Callable[[], Awaitable[SendResult]],
) -> SendResult:
    """Deliver one native Type-17 clarify within its gateway deadline."""
    if (
        not adapter._native_clarify_enabled
        or adapter._disconnecting
        or adapter._http_session is None
        or adapter._on_behalf_of
    ):
        return await fallback()

    deadline = asyncio.get_running_loop().time() + 12.0

    async def within_deadline(awaitable: Any, *, cap: float | None = None) -> Any:
        remaining = deadline - asyncio.get_running_loop().time()
        if cap is not None:
            remaining = min(remaining, cap)
        if remaining <= 0:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise TimeoutError
        async with asyncio.timeout(remaining):
            return await awaitable
    occurrence_token = ""

    def still_pending() -> bool:
        current = clarify_gateway.get_pending_for_session(
            session_key,
            include_choice_prompts=True,
        )
        return current is entry

    def pending_failure(*, message_id: str | None = None) -> SendResult:
        _clear_occurrence(clarify_id, occurrence_token)
        if getattr(entry, "state", "pending") == "answered":
            return SendResult(success=True, message_id=message_id)
        return SendResult(
            success=False,
            message_id=message_id,
            error="Hermes clarify is no longer pending",
        )
    def deadline_failure() -> SendResult:
        if not still_pending():
            return pending_failure()
        return SendResult(
            success=False,
            error="Octo clarify card delivery timed out",
            retryable=True,
        )

    async def fallback_with_deadline(require_pending: bool = False) -> SendResult:
        if require_pending and not still_pending():
            return pending_failure()
        try:
            return await within_deadline(fallback())
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return deadline_failure()

    from .card_tools import _trusted_route
    from tools import clarify_gateway

    route = _trusted_route(adapter, require_session_key=True)
    entry = clarify_gateway.get_pending_for_session(
        session_key,
        include_choice_prompts=True,
    )
    if (
        route is None
        or route.chat_id != chat_id
        or route.session_key != session_key
        or entry is None
        or entry.clarify_id != clarify_id
        or entry.session_key != session_key
        or entry.question != question
        or entry.choices != choices
        or not choices
        or not all(
            isinstance(choice, str)
            and choice
            and choice == choice.strip()
            for choice in choices
        )
    ):
        return await fallback_with_deadline()

    if len(choices) > 4 or len(set(choices)) != len(choices):
        return await fallback_with_deadline(require_pending=True)
    multi_select = bool(getattr(entry, "multi_select", False))
    occurrence_token = _register_occurrence(clarify_id, entry)
    shared_multi_user_session = _shared_multi_user_session(adapter, route)

    try:
        manifest = adapter._card_profile_cache.get()
        if manifest is None:
            manifest = await within_deadline(
                api.get_card_profile(
                    adapter._http_session,
                    adapter._api_url,
                    adapter._bot_token,
                )
            )
            if not still_pending():
                return pending_failure()
            adapter._card_profile_cache.put(manifest)
        if not still_pending():
            return pending_failure()
        if (
            not manifest.available
            or not manifest.enabled
            or manifest.profiles is None
            or CARD_PROFILE_V2 not in manifest.profiles
        ):
            return await fallback_with_deadline(require_pending=True)
        capabilities = cards.derive_card_capabilities(manifest)
        binding_id = str(uuid.uuid4())
        action_choices = tuple(
            (f"clarify_choice_{index}", choice)
            for index, choice in enumerate(choices)
        )
        other_action_id = "clarify_other"
        input_id: str | None = None
        confirm_action_id: str | None = None
        if multi_select:
            input_id = "clarify_choices"
            confirm_action_id = "clarify_confirm"
            inputs: list[dict[str, object]] = [{
                "kind": "choice",
                "id": input_id,
                "label": "可多选",
                "multi_select": True,
                "choices": [
                    {"title": choice, "value": choice_id}
                    for choice_id, choice in action_choices
                ],
            }]
            buttons: list[dict[str, object]] = [
                {"id": confirm_action_id, "label": "提交"},
                {"id": other_action_id, "label": "其他"},
            ]
        else:
            inputs = []
            buttons = [
                {"id": choice_id, "label": choice}
                for choice_id, choice in action_choices
            ]
            buttons.append({"id": other_action_id, "label": "其他"})
        direct_reply_hint = _direct_reply_hint(
            channel_type=route.channel_type,
            require_mention=bool(getattr(adapter, "_require_mention", True)),
            shared_multi_user_session=shared_multi_user_session,
        )
        rendered = cards.build_interactive_card(
            title="需要确认",
            text=f"{question}\n\n{direct_reply_hint}",
            inputs=inputs,
            buttons=buttons,
            binding_id=binding_id,
            capabilities=capabilities,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return deadline_failure()
    except Exception:
        return await fallback_with_deadline(require_pending=True)

    # Scope transport idempotency to this delivery attempt. The same Hermes
    # clarify_id may be reused by a later, semantically distinct prompt.
    client_msg_no = str(uuid.uuid4())
    if not still_pending():
        return pending_failure()
    try:
        await within_deadline(
            adapter._wait_for_card_progress(session_key, timeout=5.0),
            cap=5.0,
        )
    except TimeoutError:
        return deadline_failure()
    if not still_pending():
        return pending_failure()
    try:
        result = await within_deadline(
            api.send_card_message(
                adapter._http_session,
                adapter._api_url,
                adapter._bot_token,
                channel_id=route.channel_id,
                channel_type=route.channel_type,
                card=rendered.card,
                plain=rendered.plain,
                client_msg_no=client_msg_no,
                profile=CARD_PROFILE_V2,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception as first_error:
        if not clarify_send_is_retryable(first_error):
            return await fallback_with_deadline(require_pending=True)
        try:
            result = await within_deadline(
                api.send_card_message(
                    adapter._http_session,
                    adapter._api_url,
                    adapter._bot_token,
                    channel_id=route.channel_id,
                    channel_type=route.channel_type,
                    card=rendered.card,
                    plain=rendered.plain,
                    client_msg_no=client_msg_no,
                    profile=CARD_PROFILE_V2,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as retry_error:
            if not still_pending():
                return pending_failure()
            return SendResult(
                success=False,
                error="Octo clarify card delivery failed",
                retryable=clarify_send_is_retryable(retry_error),
            )

    if result.message_id is None:
        if not still_pending():
            return pending_failure()
        return SendResult(
            success=False,
            error="Octo clarify card delivery missing message_id",
        )
    try:
        adapter._register_card_session(
            CardSession(
                message_id=result.message_id,
                binding_id=binding_id,
                session_key=session_key,
                chat_id=route.chat_id,
                channel_id=route.channel_id,
                channel_type=route.channel_type,
                requester_uid=route.requester_uid,
                card=rendered.card,
                plain=rendered.plain,
                action_labels=rendered.action_labels,
                input_ids=rendered.input_ids,
                action_channel_ids=(
                    tuple(
                        dict.fromkeys(
                            (route.channel_id, adapter._robot_id)
                        )
                    )
                    if route.channel_type == ChannelType.DM
                    else (route.channel_id,)
                ),
                max_input_text_bytes=capabilities.max_input_text_bytes,
                max_inputs_bytes=capabilities.max_inputs_bytes,
                clarify=ClarifySession(
                    clarify_id=clarify_id,
                    occurrence_token=occurrence_token,
                    multi_select=bool(multi_select),
                    question=question,
                    choices=tuple(choices),
                    action_choices=action_choices,
                    input_id=input_id,
                    confirm_action_id=confirm_action_id,
                    other_action_id=other_action_id,
                    shared_multi_user_session=shared_multi_user_session,
                ),
            )
        )
    except Exception:
        try:
            unavailable = cards.build_display_card(
                title="需要确认",
                blocks=[{"type": "text", "text": "该确认卡不可用，请重试。"}],
            )
            await within_deadline(
                api.edit_card_message(
                    adapter._http_session,
                    adapter._api_url,
                    adapter._bot_token,
                    channel_id=route.channel_id,
                    channel_type=route.channel_type,
                    message_id=result.message_id,
                    card=unavailable.card,
                    card_seq=1,
                    plain=unavailable.plain,
                    transient=False,
                    profile=CARD_PROFILE_V2,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if not still_pending():
            return pending_failure(message_id=result.message_id)
        return SendResult(
            success=False,
            message_id=result.message_id,
            error="Octo clarify card binding failed",
        )
    if not still_pending():
        _clear_occurrence(clarify_id, occurrence_token)
        return pending_failure(message_id=result.message_id)
    return _delivery_result(result)
