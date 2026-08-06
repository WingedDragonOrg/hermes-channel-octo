"""
Octo Bot HTTP API client.

All API calls use aiohttp with Bearer token authentication.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import time
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

import aiohttp

from .types import (
    BotRegisterResp,
    ChannelType,
    GroupInfo,
    GroupMember,
    MentionEntity,
    MessageType,
    RichTextBlock,
    SendMessageResult,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)
_API_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _api_path_segment(value: str, name: str) -> str:
    """Validate and encode one server resource identifier for a URL path."""
    if not isinstance(value, str) or not _API_PATH_SEGMENT_RE.fullmatch(value):
        raise ValueError(f"invalid Octo {name}")
    return quote(value, safe="")


class OctoApiError(RuntimeError):
    """A safe, structured failure from an authenticated Octo API request.

    Response bodies can contain implementation details and should never be
    copied into agent-tool output.  Callers that need compatibility handling
    can inspect ``status`` without parsing a response body.
    """

    def __init__(
        self,
        path: str,
        *,
        status: int | None = None,
        reason: str | None = None,
    ) -> None:
        self.path = path
        self.status = status
        suffix = f" (HTTP {status})" if status is not None else ""
        detail = f": {reason}" if reason else ""
        super().__init__(f"Octo API request failed{suffix}{detail}")


def _response_error(path: str, response: aiohttp.ClientResponse) -> OctoApiError:
    """Create a secret-free error for a non-success HTTP response."""
    return OctoApiError(path, status=response.status)


# ─── MIME Type Helpers ───────────────────────────────────────────────────────

_MIME_MAP: dict[str, str] = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".ico": "image/x-icon",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".opus": "audio/opus",
    ".pdf": "application/pdf", ".zip": "application/zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain", ".md": "text/markdown",
    ".csv": "text/csv", ".html": "text/html",
    ".json": "application/json",
}


def infer_content_type(filename: str) -> str:
    """Infer MIME type from filename extension."""
    ext = os.path.splitext(filename)[1].lower()
    return _MIME_MAP.get(ext, "application/octet-stream")


def parse_image_dimensions(data: bytes, mime: str) -> tuple[int, int] | None:
    """
    Parse image dimensions from buffer header bytes (PNG/JPEG/GIF/WebP).

    Returns:
        (width, height) or None if parsing fails.
    """
    try:
        if mime == "image/png" and len(data) > 24:
            width = struct.unpack(">I", data[16:20])[0]
            height = struct.unpack(">I", data[20:24])[0]
            return width, height
        if mime in ("image/jpeg", "image/jpg") and len(data) > 2:
            offset = 2
            while offset < len(data) - 8:
                if data[offset] != 0xFF:
                    break
                marker = data[offset + 1]
                if marker in (0xC0, 0xC2):
                    height = struct.unpack(">H", data[offset + 5:offset + 7])[0]
                    width = struct.unpack(">H", data[offset + 7:offset + 9])[0]
                    return width, height
                seg_len = struct.unpack(">H", data[offset + 2:offset + 4])[0]
                offset += 2 + seg_len
        if mime == "image/gif" and len(data) > 10:
            width = struct.unpack("<H", data[6:8])[0]
            height = struct.unpack("<H", data[8:10])[0]
            return width, height
        if mime == "image/webp" and len(data) > 30:
            if data[12:16] == b"VP8 " and len(data) > 29:
                width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
                height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
                return width, height
    except Exception:
        pass
    return None


# ─── Core HTTP Helpers ───────────────────────────────────────────────────────


async def post_json(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    path: str,
    payload: dict[str, Any],
) -> Any | None:
    """
    POST JSON to a Octo API endpoint with Bearer auth.

    Args:
        session: aiohttp client session.
        api_url: Base API URL (e.g. https://api.botgate.cn).
        bot_token: Bot authentication token.
        path: API path (e.g. /v1/bot/sendMessage).
        payload: JSON body dict.

    Returns:
        Parsed JSON response dict, or None if empty response.

    Raises:
        OctoApiError: On non-2xx responses, without exposing response text.
    """
    url = f"{api_url.rstrip('/')}{path}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {bot_token}",
    }
    async with session.post(url, json=payload, headers=headers, timeout=DEFAULT_TIMEOUT) as resp:
        if not resp.ok:
            raise _response_error(path, resp)
        text = await resp.text()
        if not text:
            return None
        return await resp.json(content_type=None)


async def get_json(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    path: str,
) -> Any | None:
    """
    GET JSON from a Octo API endpoint with Bearer auth.

    Returns:
        Parsed JSON response value, or None for an empty successful response.
    """
    url = f"{api_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {bot_token}"}
    async with session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT) as resp:
        if not resp.ok:
            raise _response_error(path, resp)
        text = await resp.text()
        if not text:
            return None
        return await resp.json(content_type=None)


# ─── Bot Registration ────────────────────────────────────────────────────────


async def register_bot(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    force_refresh: bool = False,
) -> BotRegisterResp:
    """
    Register bot and obtain WS connection credentials.

    Args:
        session: aiohttp client session.
        api_url: Base API URL.
        bot_token: Bot authentication token.
        force_refresh: If True, force token refresh.

    Returns:
        BotRegisterResp with ws_url, im_token, robot_id, etc.
    """
    path = "/v1/bot/register"
    if force_refresh:
        path += "?force_refresh=true"

    result = await post_json(session, api_url, bot_token, path, {})
    if not result:
        raise RuntimeError("Octo bot registration returned empty response")

    return BotRegisterResp(
        robot_id=result["robot_id"],
        im_token=result["im_token"],
        ws_url=result["ws_url"],
        api_url=result.get("api_url", api_url),
        owner_uid=result["owner_uid"],
        owner_channel_id=result["owner_channel_id"],
    )


# ─── Message Sending ─────────────────────────────────────────────────────────


async def send_message(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    channel_id: str,
    channel_type: ChannelType,
    content: str,
    mention_uids: list[str] | None = None,
    mention_entities: list[MentionEntity] | None = None,
    mention_all: bool = False,
    stream_no: str | None = None,
    reply_msg_id: str | None = None,
) -> SendMessageResult:
    """
    Send a text message to a channel.

    Args:
        session: aiohttp client session.
        api_url: Base API URL.
        bot_token: Bot authentication token.
        channel_id: Target channel ID.
        channel_type: DM (1) or Group (2).
        content: Message text content.
        mention_uids: UIDs to @mention.
        mention_entities: Precise mention entities.
        mention_all: If True, @all.
        stream_no: Optional stream number for streaming messages.
        reply_msg_id: Optional message ID to reply to.
    """
    payload: dict[str, Any] = {
        "type": MessageType.Text,
        "content": content,
    }

    # Build mention field
    if mention_uids or mention_entities or mention_all:
        mention: dict[str, Any] = {}
        if mention_uids:
            mention["uids"] = mention_uids
        if mention_entities:
            mention["entities"] = [
                {"uid": e.uid, "offset": e.offset, "length": e.length}
                for e in mention_entities
            ]
        if mention_all:
            mention["all"] = 1
        payload["mention"] = mention

    # Add reply field
    if reply_msg_id:
        payload["reply"] = {"message_id": reply_msg_id}

    body: dict[str, Any] = {
        "channel_id": channel_id,
        "channel_type": channel_type,
        "payload": payload,
    }
    if stream_no:
        body["stream_no"] = stream_no

    result = await post_json(session, api_url, bot_token, "/v1/bot/sendMessage", body)
    data = result if isinstance(result, dict) else {}

    raw_message_id = data.get("message_id")
    message_id = (
        str(raw_message_id)
        if isinstance(raw_message_id, (str, int)) and not isinstance(raw_message_id, bool)
        else None
    )
    if not message_id:
        raise OctoApiError(
            "/v1/bot/sendMessage",
            reason="missing message_id in success response",
        )
    raw_message_seq = data.get("message_seq")
    try:
        message_seq = (
            int(raw_message_seq)
            if isinstance(raw_message_seq, (str, int)) and not isinstance(raw_message_seq, bool)
            else None
        )
    except (TypeError, ValueError):
        message_seq = None
    raw_client_msg_no = data.get("client_msg_no")
    client_msg_no = (
        str(raw_client_msg_no)
        if isinstance(raw_client_msg_no, (str, int)) and not isinstance(raw_client_msg_no, bool)
        else None
    )
    return SendMessageResult(
        message_id=message_id,
        message_seq=message_seq,
        client_msg_no=client_msg_no,
    )


async def edit_message(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    channel_id: str,
    channel_type: ChannelType,
    message_id: str,
    content: str,
    finalize: bool = False,
) -> Any | None:
    """Edit a text message using Octo's native edit envelope.

    ``finalize`` is a Hermes lifecycle argument.  The current Octo contract
    has no corresponding wire field, so it is deliberately accepted but not
    serialized.
    """
    del finalize
    return await post_json(
        session,
        api_url,
        bot_token,
        "/v1/bot/message/edit",
        {
            "message_id": str(message_id),
            "channel_id": channel_id,
            "channel_type": channel_type,
            "content_edit": json.dumps({"type": MessageType.Text, "content": content}),
        },
    )


async def send_typing(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    channel_id: str,
    channel_type: ChannelType,
) -> None:
    """Send typing indicator to a channel."""
    await post_json(session, api_url, bot_token, "/v1/bot/typing", {
        "channel_id": channel_id,
        "channel_type": channel_type,
    })


async def send_heartbeat(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
) -> Any | None:
    """Record an authenticated Bot API heartbeat (empty envelope by contract)."""
    return await post_json(session, api_url, bot_token, "/v1/bot/heartbeat", {})


async def send_media_message(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    channel_id: str,
    channel_type: ChannelType,
    msg_type: MessageType,
    url: str,
    name: str | None = None,
    size: int | None = None,
    width: int | None = None,
    height: int | None = None,
    duration: int | None = None,
    reply_msg_id: str | None = None,
) -> None:
    """
    Send a media message (image, file, etc.) to a channel.

    Args:
        msg_type: MessageType.Image, MessageType.File, etc.
        url: Media file URL.
        name: Filename (for File type).
        size: File size in bytes (for File type).
        width: Image/GIF/Video width.
        height: Image/GIF/Video height.
        duration: Voice/Video duration, when the caller has it.
        reply_msg_id: Optional message ID to reply to.
    """
    dimension_types = {MessageType.Image, MessageType.GIF, MessageType.Video}
    duration_types = {MessageType.Voice, MessageType.Video}
    if (width is not None or height is not None) and msg_type not in dimension_types:
        raise ValueError(f"width/height are not supported for media type {msg_type}")
    if duration is not None and msg_type not in duration_types:
        raise ValueError(f"duration is not supported for media type {msg_type}")

    payload: dict[str, Any] = {
        "type": msg_type,
        "url": url,
    }
    if name:
        payload["name"] = name
    if size is not None:
        payload["size"] = size
    if width is not None:
        payload["width"] = width
    if height is not None:
        payload["height"] = height
    if duration is not None:
        payload["duration"] = duration
    if reply_msg_id:
        payload["reply"] = {"message_id": reply_msg_id}

    await post_json(session, api_url, bot_token, "/v1/bot/sendMessage", {
        "channel_id": channel_id,
        "channel_type": channel_type,
        "payload": payload,
    })


async def send_rich_text_message(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    channel_id: str,
    channel_type: ChannelType,
    blocks: list[RichTextBlock],
    plain: str | None = None,
    mention_uids: list[str] | None = None,
    mention_entities: list[MentionEntity] | None = None,
    mention_all: bool = False,
    reply_msg_id: str | None = None,
) -> None:
    """
    Send a RichText(=14) 图文混排 message.

    Replaces the "text sendMessage + loop uploadMedia" pattern with a
    single POST carrying an ordered ``content`` block array — the caller
    uploads images first (to obtain url + width + height), assembles a
    mix of text and image blocks in display order, and passes them here.

    Contract (see octo-lib common/richtext.go):
      - `blocks` MUST be non-empty; text blocks require non-empty `text`,
        image blocks require http/https `url` and positive `width`/`height`.
        Validation is enforced by octo-server; this function only assembles.
      - `plain` is an optional redundant plain-text rendering for legacy
        clients; the server regenerates it authoritatively from `content`.

    Args:
        blocks: Ordered RichText block list (text/image interleaved).
        plain: Optional redundant plain-text (server-overridden).
        mention_uids/mention_entities/mention_all: Same shape as send_message.
        reply_msg_id: Optional message ID to reply to.
    """
    payload: dict[str, Any] = {
        "type": MessageType.RichText,
        "content": [b.to_dict() for b in blocks],
    }
    if plain is not None:
        payload["plain"] = plain

    if mention_uids or mention_entities or mention_all:
        mention: dict[str, Any] = {}
        if mention_uids:
            mention["uids"] = mention_uids
        if mention_entities:
            mention["entities"] = [
                {"uid": e.uid, "offset": e.offset, "length": e.length}
                for e in mention_entities
            ]
        if mention_all:
            mention["all"] = 1
        payload["mention"] = mention

    if reply_msg_id:
        payload["reply"] = {"message_id": reply_msg_id}

    body: dict[str, Any] = {
        "channel_id": channel_id,
        "channel_type": channel_type,
        "payload": payload,
    }
    await post_json(session, api_url, bot_token, "/v1/bot/sendMessage", body)


async def send_read_receipt(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    channel_id: str,
    channel_type: ChannelType,
    message_ids: list[str] | None = None,
) -> None:
    """
    Send a read receipt to a channel.

    Args:
        channel_id: Channel to mark as read.
        channel_type: DM or Group.
        message_ids: Optional specific message IDs to acknowledge.
    """
    body: dict[str, Any] = {
        "channel_id": channel_id,
        "channel_type": channel_type,
    }
    if message_ids:
        body["message_ids"] = message_ids
    await post_json(session, api_url, bot_token, "/v1/bot/readReceipt", body)


# ─── Stream API ──────────────────────────────────────────────────────────────


async def stream_start(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    channel_id: str,
    channel_type: ChannelType,
    initial_content: str,
) -> str | None:
    """
    Start a streaming message.

    The initial content is sent as a base64-encoded JSON payload.

    Args:
        channel_id: Target channel.
        channel_type: DM or Group.
        initial_content: Initial text content.

    Returns:
        stream_no (stream identifier) or None on failure.
    """
    import json
    payload_b64 = base64.b64encode(
        json.dumps({"type": 1, "content": initial_content}).encode("utf-8")
    ).decode("ascii")

    result = await post_json(session, api_url, bot_token, "/v1/bot/stream/start", {
        "channel_id": channel_id,
        "channel_type": channel_type,
        "payload": payload_b64,
    })
    return result.get("stream_no") if result else None


async def stream_end(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    stream_no: str,
    channel_id: str,
    channel_type: ChannelType,
) -> None:
    """
    End a streaming message.

    Args:
        stream_no: Stream identifier from stream_start.
        channel_id: Target channel.
        channel_type: DM or Group.
    """
    await post_json(session, api_url, bot_token, "/v1/bot/stream/end", {
        "stream_no": stream_no,
        "channel_id": channel_id,
        "channel_type": channel_type,
    })


# ─── COS Upload ──────────────────────────────────────────────────────────────


async def get_upload_credentials(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    filename: str,
) -> dict[str, Any]:
    """
    Get STS temporary credentials for COS upload.

    Returns:
        Dict with bucket, region, key, credentials, startTime, expiredTime, cdnBaseUrl.

    Raises:
        RuntimeError: If the API returns incomplete data.
    """
    encoded_filename = quote(filename)
    path = f"/v1/bot/upload/credentials?filename={encoded_filename}"
    url = f"{api_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {bot_token}"}

    async with session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT) as resp:
        if not resp.ok:
            raise _response_error(path, resp)
        data = await resp.json()

    # Validate required fields
    for field in ("bucket", "region", "key", "credentials"):
        if not data.get(field):
            raise RuntimeError(
                f"Octo API /v1/bot/upload/credentials returned incomplete response: missing {field}"
            )
    creds = data["credentials"]
    for field in ("tmpSecretId", "tmpSecretKey", "sessionToken"):
        if not creds.get(field):
            raise RuntimeError(
                "Octo API /v1/bot/upload/credentials returned incomplete credentials: "
                f"missing {field}"
            )
    return data


_CD_UNSAFE_RE = re.compile(r'["\\\x00-\x1f\x7f;]')


def _build_content_disposition(
    filename: str,
    disposition_type: str = "attachment",
) -> str:
    """Build RFC 5987 Content-Disposition header value with safe ASCII fallback."""
    is_ascii_safe = (
        bool(re.match(r'^[\x20-\x7e]+$', filename))
        and not _CD_UNSAFE_RE.search(filename)
    )
    encoded = quote(filename, safe='')

    if is_ascii_safe:
        return f'{disposition_type}; filename="{filename}"'

    ext = '.' + filename.rsplit('.', 1)[1] if '.' in filename else ''
    return f"{disposition_type}; filename=\"download{ext}\"; filename*=UTF-8''{encoded}"


async def upload_file_to_cos(
    session: aiohttp.ClientSession,
    credentials: dict[str, str],
    bucket: str,
    region: str,
    key: str,
    file_data: bytes,
    content_type: str,
    cdn_base_url: str | None = None,
    filename: str | None = None,
) -> str:
    """
    Upload a file to COS using STS temporary credentials via HTTP PUT.

    Uses direct HTTP PUT with authorization header instead of the COS SDK,
    keeping dependencies minimal.

    Args:
        session: aiohttp client session.
        credentials: Dict with tmpSecretId, tmpSecretKey, sessionToken.
        bucket: COS bucket name.
        region: COS region.
        key: Object key (path in bucket).
        file_data: File content bytes.
        content_type: MIME type of the file.
        cdn_base_url: Optional CDN base URL for the result URL.

    Returns:
        Public URL of the uploaded file.
    """
    secret_id = credentials["tmpSecretId"]
    secret_key = credentials["tmpSecretKey"]
    session_token = credentials["sessionToken"]

    # Build COS endpoint
    host = f"{bucket}.cos.{region}.myqcloud.com"
    url = f"https://{host}/{key}"

    # Generate authorization signature
    # COS uses a simplified HMAC-SHA1 signature for PUT requests
    now = int(time.time())
    sign_time = f"{now - 60};{now + 3600}"  # valid for 1 hour

    # Build string to sign (simplified COS auth)
    http_string = f"put\n/{key}\n\nhost={host.lower()}\n"
    sha1_content = hashlib.sha1(http_string.encode("utf-8")).hexdigest()
    string_to_sign = f"sha1\n{sign_time}\n{sha1_content}\n"

    # Sign
    sign_key = hmac.new(
        secret_key.encode("utf-8"),
        sign_time.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()
    signature = hmac.new(
        sign_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest()

    authorization = (
        f"q-sign-algorithm=sha1"
        f"&q-ak={secret_id}"
        f"&q-sign-time={sign_time}"
        f"&q-key-time={sign_time}"
        f"&q-header-list=host"
        f"&q-url-param-list="
        f"&q-signature={signature}"
    )

    headers = {
        "Host": host,
        "Content-Type": content_type,
        "Content-Length": str(len(file_data)),
        "Authorization": authorization,
        "x-cos-security-token": session_token,
    }
    if filename:
        if content_type.startswith("video/") or content_type.startswith("audio/"):
            headers["Content-Disposition"] = _build_content_disposition(filename, "inline")
        elif not content_type.startswith("image/"):
            headers["Content-Disposition"] = _build_content_disposition(filename, "attachment")

    upload_timeout = aiohttp.ClientTimeout(total=300)  # 5 min for large files
    async with session.put(url, data=file_data, headers=headers, timeout=upload_timeout) as resp:
        if not resp.ok:
            # COS responses may echo signed request diagnostics.  Preserve the
            # status for operators, never the body or authorization material.
            raise RuntimeError(f"COS upload failed (HTTP {resp.status})")

    # Build result URL
    if cdn_base_url:
        base = cdn_base_url.rstrip("/")
        # Re-encode path segments for CDN URL
        re_encoded_key = "/".join(quote(seg) for seg in key.split("/"))
        return f"{base}/{re_encoded_key}"
    else:
        return f"https://{host}/{key}"


async def upload_and_get_url(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    filename: str,
    file_data: bytes,
    content_type: str,
) -> str:
    """
    High-level: get credentials, upload to COS, return URL.

    Args:
        filename: Original filename.
        file_data: File content bytes.
        content_type: MIME type.

    Returns:
        Public URL of the uploaded file.
    """
    creds_data = await get_upload_credentials(session, api_url, bot_token, filename)

    return await upload_file_to_cos(
        session,
        credentials=creds_data["credentials"],
        bucket=creds_data["bucket"],
        region=creds_data["region"],
        key=creds_data["key"],
        file_data=file_data,
        content_type=content_type,
        cdn_base_url=creds_data.get("cdnBaseUrl"),
        filename=filename,
    )


_DOWNLOAD_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.goog",
    "100.100.100.200",
    "fd00:ec2::254",
}
_DOWNLOAD_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_DOWNLOAD_REDIRECTS = 5


def _canonical_download_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    normalized = host.lower().strip("[]").rstrip(".")
    try:
        return ipaddress.ip_address(normalized)
    except ValueError:
        try:
            return ipaddress.ip_address(socket.inet_aton(normalized))
        except OSError:
            return None


def _validate_download_url(
    url: str,
    *,
    trusted_private_hosts: frozenset[str] = frozenset(),
) -> str:
    """Validate one download hop before any network I/O."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("unsafe download URL") from exc
    if parsed.scheme not in {"http", "https"} or not host:
        raise RuntimeError("unsafe download URL")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeError("unsafe download URL")
    if host in _DOWNLOAD_METADATA_HOSTS:
        raise RuntimeError("unsafe download URL")

    literal = _canonical_download_ip(host)
    if literal is not None and (
        literal.is_link_local
        or literal.is_multicast
        or literal.is_reserved
        or literal.is_unspecified
    ):
        raise RuntimeError("unsafe download URL")
    if (
        literal is not None
        and (literal.is_loopback or literal.is_private)
        and host not in trusted_private_hosts
    ):
        raise RuntimeError("unsafe download URL")
    return url


def _download_trusted_private_hosts(session: aiohttp.ClientSession) -> frozenset[str]:
    connector = getattr(session, "connector", None)
    resolver = getattr(connector, "_ssrf_resolver", None)
    hosts = getattr(resolver, "_trusted_hosts", None)
    if not isinstance(hosts, set):
        return frozenset()
    return frozenset(str(host).lower().rstrip(".") for host in hosts)


async def download_file(
    session: aiohttp.ClientSession,
    url: str,
    max_size: int = 500 * 1024 * 1024,
    timeout_seconds: int = 300,
) -> tuple[bytes, str, str]:
    """
    Download a file from a URL.

    Args:
        url: URL to download.
        max_size: Maximum file size in bytes.
        timeout_seconds: Download timeout.

    Returns:
        (file_data, content_type, filename)

    Raises:
        RuntimeError: If file is too large or download fails.
    """
    dl_timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    current_url = url
    trusted_private_hosts = _download_trusted_private_hosts(session)
    for redirect_count in range(_MAX_DOWNLOAD_REDIRECTS + 1):
        _validate_download_url(
            current_url,
            trusted_private_hosts=trusted_private_hosts,
        )
        async with session.get(
            current_url,
            timeout=dl_timeout,
            allow_redirects=False,
        ) as resp:
            if resp.status in _DOWNLOAD_REDIRECT_STATUSES:
                location = resp.headers.get("Location")
                if not location or redirect_count >= _MAX_DOWNLOAD_REDIRECTS:
                    raise RuntimeError("Download failed (invalid redirect)")
                current_url = urljoin(current_url, location)
                _validate_download_url(
                    current_url,
                    trusted_private_hosts=trusted_private_hosts,
                )
                continue
            if not resp.ok:
                # Source URLs are frequently pre-signed and must not be copied into
                # exceptions that can reach logs or SendResult.error.
                raise RuntimeError(f"Download failed (HTTP {resp.status})")

            content_type = resp.headers.get(
                "Content-Type", "application/octet-stream"
            )

            # Extract filename from URL or Content-Disposition.
            filename = "file"
            cd = resp.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                filename = cd.split("filename=")[-1].strip('"').strip("'")
            else:
                path = urlparse(current_url).path
                filename = unquote(path.split("/")[-1]) or "file"

            cl = resp.headers.get("Content-Length")
            if cl and int(cl) > max_size:
                raise RuntimeError(f"File too large ({cl} bytes, max {max_size})")

            data = bytearray()
            async for chunk in resp.content.iter_any():
                data.extend(chunk)
                if len(data) > max_size:
                    raise RuntimeError(f"File too large (>{max_size} bytes)")

            return bytes(data), content_type, filename

    raise RuntimeError("Download failed (too many redirects)")


# ─── Channel History ─────────────────────────────────────────────────────────


async def get_channel_messages(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    channel_id: str,
    channel_type: ChannelType,
    limit: int = 20,
    start_message_seq: int = 0,
    end_message_seq: int = 0,
) -> list[dict[str, Any]]:
    """
    Fetch channel history messages.

    Uses /v1/bot/messages/sync API. Payloads are base64-encoded JSON.

    Args:
        channel_id: Channel to fetch history from.
        channel_type: DM or Group.
        limit: Max messages to fetch (default 20).
        start_message_seq: Start sequence (0 = from beginning).
        end_message_seq: End sequence (0 = to latest).

    Returns:
        List of dicts with from_uid, content, timestamp, type, url, name, payload.
    """
    result = await post_json(session, api_url, bot_token, "/v1/bot/messages/sync", {
        "channel_id": channel_id,
        "channel_type": channel_type,
        "limit": limit,
        "start_message_seq": start_message_seq,
        "end_message_seq": end_message_seq,
        "pull_mode": 1,  # 1 = pull up (newer messages)
    })

    if not result:
        return []

    messages = result.get("messages", [])
    parsed = []
    for m in messages:
        payload: dict[str, Any] = {}
        raw_payload = m.get("payload")
        if raw_payload:
            try:
                decoded = base64.b64decode(raw_payload).decode("utf-8")
                import json
                payload = json.loads(decoded)
            except Exception:
                if isinstance(raw_payload, dict):
                    payload = raw_payload

        parsed.append({
            "from_uid": m.get("from_uid", "unknown"),
            "type": payload.get("type"),
            "url": payload.get("url"),
            "name": payload.get("name"),
            "content": payload.get("content", ""),
            "payload": payload,
            # API returns seconds, convert to ms
            "timestamp": (m.get("timestamp", int(time.time()))) * 1000,
        })
    return parsed


# ─── Group API ────────────────────────────────────────────────────────────────


async def fetch_bot_groups(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
) -> list[dict[str, str]]:
    """
    Fetch the list of groups the bot belongs to.

    Returns:
        List of dicts with 'group_no' and 'name' keys.
    """
    data = await get_json(session, api_url, bot_token, "/v1/bot/groups")
    return data if isinstance(data, list) else []


async def get_group_members(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    group_no: str,
) -> list[GroupMember]:
    """
    Get members of a group.

    Args:
        group_no: Group ID (channel_id).

    Returns:
        List of GroupMember objects.
    """
    data = await get_json(
        session, api_url, bot_token, f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/members"
    )
    # Normalize: API may return {members: [...]} or bare [...].  A successful
    # but structurally empty response remains a legitimate empty roster.
    members_raw = data.get("members", data) if isinstance(data, dict) else data
    if not isinstance(members_raw, list):
        return []
    return [
        GroupMember(
            uid=m.get("uid", ""),
            name=m.get("name", ""),
            role=m.get("role"),
            robot=m.get("robot"),
        )
        for m in members_raw
    ]


async def get_group_info(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    group_no: str,
) -> GroupInfo:
    """
    Get information about a group.

    Args:
        group_no: Group ID (channel_id).

    Returns:
        GroupInfo with group_no, name, and extra fields.

    Raises:
        RuntimeError: If the API call fails.
    """
    data = await get_json(
        session, api_url, bot_token, f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}"
    )
    if not isinstance(data, dict):
        raise RuntimeError("Octo group info returned an invalid response")
    known_keys = {"group_no", "name"}
    return GroupInfo(
        group_no=data.get("group_no", group_no),
        name=data.get("name", ""),
        extra={k: v for k, v in data.items() if k not in known_keys},
    )


async def fetch_user_info(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    uid: str,
) -> dict[str, str] | None:
    """
    Fetch user info by UID.

    Returns:
        Dict with uid, name, avatar keys, or None if unavailable.
    """
    url = f"{api_url.rstrip('/')}/v1/bot/user/info"
    headers = {"Authorization": f"Bearer {bot_token}"}
    try:
        async with session.get(
            url,
            headers=headers,
            params={"uid": uid},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 404:
                return None
            if not resp.ok:
                logger.error("octo: fetchUserInfo(%s) failed: %d", uid, resp.status)
                return None
            data = await resp.json()
            if data and data.get("name"):
                return {
                    "uid": data.get("uid", uid),
                    "name": data["name"],
                    "avatar": data.get("avatar", ""),
                }
            return None
    except Exception as e:
        logger.error("octo: fetchUserInfo(%s) error: %s", uid, e)
        return None


# ─── GROUP.md API ─────────────────────────────────────────────────────────────


async def get_group_md(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    group_no: str,
) -> dict[str, Any] | None:
    """
    Fetch GROUP.md content for a group.

    Returns:
        Dict with content, version, updated_at, updated_by, or None on 404.
    """
    path = f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/md"
    url = f"{api_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {bot_token}"}
    async with session.get(url, headers=headers, timeout=DEFAULT_TIMEOUT) as resp:
        if resp.status == 404:
            return None
        if not resp.ok:
            raise _response_error(path, resp)
        return await resp.json()


async def update_group_md(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    group_no: str,
    content: str,
) -> dict[str, Any] | None:
    """
    Update GROUP.md content for a group.

    Returns:
        Parsed response dict, or None for a successful empty response.

    Raises:
        OctoApiError: The server returned a non-2xx response.
    """
    return await put_json(
        session,
        api_url,
        bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/md",
        {"content": content},
    )


# ─── Bot DELETE / generic helpers ────────────────────────────────────────────


async def delete_json(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """DELETE on a Octo endpoint, raising on non-2xx."""
    url = f"{api_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }
    kwargs: dict[str, Any] = {"headers": headers, "timeout": DEFAULT_TIMEOUT}
    if payload is not None:
        kwargs["data"] = json.dumps(payload)
    async with session.delete(url, **kwargs) as resp:
        if not resp.ok:
            raise _response_error(path, resp)
        text = await resp.text()
        return json.loads(text) if text else None


async def put_json(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    path: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """PUT JSON on a Octo endpoint, raising on non-2xx."""
    url = f"{api_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json",
    }
    async with session.put(
        url, data=json.dumps(payload), headers=headers, timeout=DEFAULT_TIMEOUT
    ) as resp:
        if not resp.ok:
            raise _response_error(path, resp)
        text = await resp.text()
        return json.loads(text) if text else None


# ─── Space-level operations ──────────────────────────────────────────────────


async def search_space_members(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    keyword: str | None = None,
    space_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fuzzy-search members of the bot's space (org).

    Returns ``[{uid, name, robot}, ...]`` (empty list when no matches).
    """
    qs: list[str] = []
    if keyword:
        qs.append(f"keyword={quote(keyword)}")
    if space_id:
        qs.append(f"space_id={quote(space_id)}")
    qs.append(f"limit={int(max(1, min(limit, 500)))}")
    path = "/v1/bot/space/members?" + "&".join(qs)
    result = await get_json(session, api_url, bot_token, path)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        members = result.get("members")
        return members if isinstance(members, list) else []
    return []


# ─── Group create / update / membership ──────────────────────────────────────


async def create_group(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    members: list[str],
    creator: str,
    name: str | None = None,
    space_id: str | None = None,
) -> dict[str, Any]:
    """Create a new group with the given members.

    *creator* is the uid that becomes the group owner.
    """
    payload: dict[str, Any] = {"members": members, "creator": creator}
    if name is not None:
        payload["name"] = name
    if space_id is not None:
        payload["space_id"] = space_id
    result = await post_json(session, api_url, bot_token, "/v1/bot/createGroup", payload)
    return result or {}


async def update_group(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    name: str | None = None,
    notice: str | None = None,
) -> None:
    """Update a group's metadata (name/notice).

    Raises ``ValueError`` rather than reporting a successful no-op when the
    caller supplies no mutable fields.
    """
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if notice is not None:
        payload["notice"] = notice
    if not payload:
        raise ValueError("update_group requires at least one of: name, notice")
    await put_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/info", payload,
    )


async def add_group_members(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    members: list[str],
) -> dict[str, Any]:
    result = await post_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/members/add", {"members": members},
    )
    return result or {}


async def remove_group_members(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    members: list[str],
) -> dict[str, Any]:
    result = await post_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/members/remove", {"members": members},
    )
    return result or {}


# ─── Threads ─────────────────────────────────────────────────────────────────


async def create_thread(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    name: str,
    source_message_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name}
    if source_message_id is not None:
        payload["source_message_id"] = source_message_id
    result = await post_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/threads", payload,
    )
    return result or {}


async def list_threads(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
) -> list[dict[str, Any]]:
    result = await get_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/threads",
    )
    if not result:
        return []
    threads = result.get("threads")
    return threads if isinstance(threads, list) else []


async def get_thread(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    short_id: str,
) -> dict[str, Any]:
    result = await get_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/threads/{_api_path_segment(short_id, 'short_id')}",
    )
    return result or {}


async def delete_thread(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    short_id: str,
) -> None:
    await delete_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/threads/{_api_path_segment(short_id, 'short_id')}",
    )


async def list_thread_members(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    short_id: str,
) -> list[dict[str, Any]]:
    result = await get_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/threads/{_api_path_segment(short_id, 'short_id')}/members",
    )
    if not result:
        return []
    members = result.get("members")
    return members if isinstance(members, list) else []


async def join_thread(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    short_id: str,
) -> None:
    await post_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/threads/{_api_path_segment(short_id, 'short_id')}/join", {},
    )


async def leave_thread(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    short_id: str,
) -> None:
    await post_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/threads/{_api_path_segment(short_id, 'short_id')}/leave", {},
    )


async def get_thread_md(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    short_id: str,
) -> dict[str, Any]:
    """Read THREAD.md. Returns {} on 404."""
    try:
        result = await get_json(
            session, api_url, bot_token,
            f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/threads/{_api_path_segment(short_id, 'short_id')}/md",
        )
    except OctoApiError as e:
        if e.status == 404:
            return {"content": "", "version": 0, "updated_at": None, "updated_by": ""}
        raise
    return result or {}


async def update_thread_md(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    group_no: str,
    short_id: str,
    content: str,
) -> dict[str, Any]:
    result = await put_json(
        session, api_url, bot_token,
        f"/v1/bot/groups/{_api_path_segment(group_no, 'group_no')}/threads/{_api_path_segment(short_id, 'short_id')}/md", {"content": content},
    )
    return result or {}


# ─── Voice context ───────────────────────────────────────────────────────────


async def get_voice_context(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
) -> dict[str, Any]:
    """Bot owner's personal voice correction context.

    Returns ``{has_context, context, updated_at}`` (empty when none set).
    """
    try:
        result = await get_json(session, api_url, bot_token, "/v1/bot/voice/context")
    except OctoApiError as e:
        if e.status == 404:
            return {"has_context": False, "context": "", "updated_at": None}
        raise
    return result or {"has_context": False, "context": "", "updated_at": None}


async def update_voice_context(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
    *,
    content: str,
) -> None:
    # Server expects {"context": "..."} (NOT "content").
    await put_json(session, api_url, bot_token, "/v1/bot/voice/context", {"context": content})


async def delete_voice_context(
    session: aiohttp.ClientSession,
    api_url: str,
    bot_token: str,
) -> None:
    await delete_json(session, api_url, bot_token, "/v1/bot/voice/context")
