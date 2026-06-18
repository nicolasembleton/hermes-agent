"""Zulip gateway adapter.

Connects to any Zulip server (cloud or self-hosted) via the official
``zulip`` Python package.  Uses the long-polling event queue for
real-time message delivery and the REST API for sending.

Authentication uses the bot's email + API key + server URL — no OAuth
tokens required.

Environment variables:
    ZULIP_SITE_URL           Server URL (e.g. https://your-org.zulipchat.com)
    ZULIP_BOT_EMAIL          Bot's email address
    ZULIP_API_KEY            Bot's API key (from Zulip bot settings)
    ZULIP_ALLOWED_USERS      Comma-separated email addresses
    ZULIP_ALLOW_ALL_USERS    If "true", allow all Zulip users (skip allowlist)
    ZULIP_DEFAULT_STREAM     Default stream for outbound messages
    ZULIP_HOME_TOPIC         Default topic for cron/notification delivery
    ZULIP_HOME_CHANNEL       Home stream:topic for cron/notification delivery
    ZULIP_CERT_BUNDLE        Path to a CA bundle for self-hosted/self-signed TLS
    ZULIP_ALLOW_INSECURE     If "true", disable TLS verification (dev only)
    ZULIP_REQUIRE_MENTION    Require @mention in streams (default: "true")
    ZULIP_FREE_RESPONSE_STREAMS  Comma-separated stream names or IDs that
                             don't require @mention
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_document_from_bytes,
)

logger = logging.getLogger(__name__)

# Zulip message size limit — server default is 10000, but 4000 matches
# the practical limit used by other adapters in this codebase.
MAX_MESSAGE_LENGTH = 4000

# Event-queue reconnect parameters (exponential backoff).
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER = 0.2


def _is_retryable_error(exc: Exception) -> bool:
    """Determine if a Zulip event queue error is worth retrying.

    Network errors, timeouts, and server errors (5xx) are retryable.
    Authentication failures (401/403) and other client errors (4xx)
    are not — the configuration or credentials need to be fixed first.

    Falls back to *retryable* for unrecognized error shapes so the
    event queue keeps trying on transient issues.
    """
    exc_name = type(exc).__name__

    # Network-level errors are always retryable.
    if any(keyword in exc_name for keyword in ("ConnectionError", "Timeout", "SSLError")):
        return True

    # Check for Zulip ``ClientError`` that carries an HTTP status.
    if hasattr(exc, "http_status"):
        status = getattr(exc, "http_status", 0)
        if status in (401, 403):
            return False
        if 400 <= status < 500:
            return False  # Client errors — user must fix config.

    return True


# ---------------------------------------------------------------------------
# Chat-ID helpers
#
# Zulip uses two distinct message types:
#   * Stream messages live in a stream and have a topic.
#   * Direct messages (DMs) are between exactly two users.
#
# We encode both into a single *chat_id* string that the gateway session
# layer can round-trip without understanding Zulip internals.
# ---------------------------------------------------------------------------

_DM_PREFIX = "dm:"
_GROUP_DM_PREFIX = "group_dm:"


def _build_zform_widget_content(heading: str, choices: List[Dict[str, Any]]) -> str:
    """Serialize a Zulip ``zform`` choices widget for ``send_message``.

    Intended use:
        Zulip does not have Telegram-style hidden callback buttons for normal
        bot messages.  Its native button-like primitive is a message widget
        called ``zform``.  Each zform choice renders as a button in compatible
        Zulip clients, and clicking it sends a normal visible reply message
        containing that choice's ``reply`` string.

    How it works:
        The Zulip send-message API accepts a ``widget_content`` form field that
        must be a JSON string.  For zform choices, the JSON object has
        ``widget_type: "zform"`` and ``extra_data.type: "choices"``.  Hermes
        chooses reply strings that existing gateway text handlers already
        understand (for example ``/approve`` or a literal clarify answer), so
        zform remains a richer UI over the same audited text-command paths.

    All fields are normalized to strings because Zulip's validator expects
    string values for the choice labels and reply payloads.
    """
    normalized_choices = [
        {
            "type": str(choice.get("type", "multiple_choice")),
            "short_name": str(choice.get("short_name", "")),
            "long_name": str(choice.get("long_name", "")),
            "reply": str(choice.get("reply", "")),
        }
        for choice in choices
    ]
    return json.dumps(
        {
            "widget_type": "zform",
            "extra_data": {
                "type": "choices",
                "heading": str(heading),
                "choices": normalized_choices,
            },
        },
        ensure_ascii=False,
    )


def _build_stream_chat_id(stream_id: int, topic: str) -> str:
    """Encode a stream message's origin as a stable chat ID.

    Format: ``"{stream_id}:{topic}"``
    """
    return f"{stream_id}:{topic}"


def _parse_stream_chat_id(chat_id: str) -> Optional[Tuple[int, str]]:
    """Parse a canonical stream chat ID back into ``(stream_id, topic)``.

    Returns ``None`` if *chat_id* does not look like a canonical stream chat
    ID with a numeric stream ID prefix.
    """
    # Canonical stream chat IDs look like "123:some topic" — the part before
    # the first colon must be a plain integer.
    colon = chat_id.find(":")
    if colon < 1:
        return None
    stream_part = chat_id[:colon]
    if not stream_part.isdigit():
        return None
    topic = chat_id[colon + 1:] or "(no topic)"
    return (int(stream_part), topic)


def _parse_stream_name_topic(chat_id: str) -> Optional[Tuple[str, str]]:
    """Parse a documented ``stream_name:topic`` target.

    This is intentionally separate from :func:`_parse_stream_chat_id` because
    the canonical internal format uses numeric stream IDs, while config/docs
    may use human-friendly stream names.
    """
    colon = chat_id.find(":")
    if colon < 1:
        return None
    stream_name = chat_id[:colon].strip()
    topic = chat_id[colon + 1:] or "(no topic)"
    if not stream_name or stream_name.isdigit():
        return None
    if chat_id.startswith(_DM_PREFIX) or chat_id.startswith(_GROUP_DM_PREFIX):
        return None
    if stream_name in {"dm", "group_dm"}:
        return None
    if "@" in stream_name:
        return None
    return stream_name, topic


def _build_dm_chat_id(sender_email: str) -> str:
    """Encode a DM origin as a stable chat ID.

    Format: ``"dm:{sender_email}"``
    """
    # Defensive: strip existing prefix so stale/cached IDs don't double-prefix.
    if sender_email.startswith(_DM_PREFIX):
        sender_email = sender_email[len(_DM_PREFIX):]
    return f"{_DM_PREFIX}{sender_email}"


def _parse_dm_chat_id(chat_id: str) -> Optional[str]:
    """Parse a DM chat ID back into the sender email.

    Returns ``None`` if *chat_id* does not look like a DM chat ID.
    """
    if chat_id.startswith(_DM_PREFIX) and "@" in chat_id:
        return chat_id[len(_DM_PREFIX):]
    return None


def is_dm_chat_id(chat_id: str) -> bool:
    """Return True if *chat_id* represents a DM conversation."""
    return chat_id.startswith(_DM_PREFIX)


def _build_group_dm_chat_id(participant_emails: list) -> str:
    """Encode a group DM (3+ participants) as a stable chat ID.

    Sorts emails for deterministic round-tripping regardless of the order
    in which Zulip delivers the participant list.

    Format: ``"group_dm:email1@example.com,email2@example.com,..."``
    """
    sorted_emails = sorted(participant_emails)
    return f"{_GROUP_DM_PREFIX}{','.join(sorted_emails)}"


def _parse_group_dm_chat_id(chat_id: str) -> Optional[list]:
    """Parse a group DM chat ID back into a sorted list of emails.

    Returns ``None`` if *chat_id* does not look like a group DM chat ID.
    """
    if not chat_id.startswith(_GROUP_DM_PREFIX):
        return None
    emails_str = chat_id[len(_GROUP_DM_PREFIX):]
    if not emails_str:
        return None
    return emails_str.split(",")


def is_group_dm_chat_id(chat_id: str) -> bool:
    """Return True if *chat_id* represents a group DM conversation."""
    return chat_id.startswith(_GROUP_DM_PREFIX)


def _build_stream_typing_request(stream_id: int, topic: str, op: str) -> Dict[str, Any]:
    """Build Zulip's channel typing payload using the modern stream_id + topic shape.

    Zulip's current typing API for streams/channels requires the numeric
    ``stream_id`` together with the ``topic``. The older ``{"to": [stream_name]}``
    form is only valid for direct-message recipients and is no longer reliable
    (or accepted) for channel typing notifications.
    """
    return {
        "stream_id": stream_id,
        "topic": topic,
        "type": "stream",
        "op": op,
    }


def _extract_dm_recipients(
    display_recipient: Any, bot_email: str, sender_email: str
) -> list:
    """Extract DM participant emails from ``display_recipient``.

    For 1:1 DMs, returns ``[other_user_email]``.
    For group DMs (3+ users), returns all emails except the bot's.
    Falls back to ``[sender_email]`` if the payload is malformed.
    """
    if isinstance(display_recipient, list):
        emails = [
            u.get("email", "")
            for u in display_recipient
            if isinstance(u, dict) and u.get("email") != bot_email
        ]
        if emails:
            return emails

    return [sender_email]


def _resolve_stream_name(
    message: Dict[str, Any],
    stream_id: int,
    stream_name_cache: Dict[int, str],
) -> str:
    """Get the stream name from cache or fall back to the message payload.

    Zulip's ``display_recipient`` for stream messages is either:
    - A string with the stream name (modern Zulip).
    - A dict with a ``name`` key (legacy Zulip).

    Falls back to ``str(stream_id)`` if nothing is available.
    """
    if stream_id in stream_name_cache:
        return stream_name_cache[stream_id]

    # Try display_recipient from the message payload.
    dr = message.get("display_recipient")
    if isinstance(dr, str) and dr:
        return dr
    if isinstance(dr, dict):
        name = dr.get("name", "")
        if name:
            return name

    return str(stream_id)


def _strip_bot_mention(
    content: str,
    mention_patterns: List[str],
) -> str:
    """Remove bot mention patterns from message content.

    Strips each pattern from the content (case-insensitive), then
    normalizes whitespace (collapses double spaces, strips edges).

    Zulip renders ``@**Full Name**`` and ``@email@example.com`` as
    clickable mentions.  We remove them so the agent doesn't see its
    own name as part of the user's message.
    """
    cleaned = content
    for pattern in mention_patterns:
        # Case-insensitive removal.
        cleaned = re.sub(
            re.escape(pattern), "", cleaned, count=1, flags=re.IGNORECASE
        )
    # Collapse any double spaces left by mention removal and strip edges.
    cleaned = re.sub(r"  +", " ", cleaned).strip()
    return cleaned


def _format_context_block(context_lines: list) -> str:
    """Format fetched context messages as a readable block prepended to the
    user's message.  The block is separated from the user's current message
    by a ``---`` delimiter so the agent can clearly distinguish context
    from the question being asked.
    """
    if not context_lines:
        return ""
    header = "Recent messages in this topic:"
    body = "\n".join(context_lines)
    return f"{header}\n{body}\n---\n\n"


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------


def check_zulip_requirements(config: Optional[PlatformConfig] = None) -> bool:
    """Return True if the Zulip adapter can be used.

    Uses the standard lazy-install path (via ``tools.lazy_deps.ensure``)
    so that users who have configured ZULIP_* environment variables get the
    package installed automatically on first gateway start — exactly like
    Slack, Matrix, DingTalk, Feishu, etc. This avoids scary "package not
    installed" warnings for new users while still respecting the "don't pull
    it in for people who never use Zulip" rule.
    """
    extra = config.extra if config else {}
    configured_key = (config.token or config.api_key) if config else ""
    api_key = configured_key or os.getenv("ZULIP_API_KEY", "")
    email = extra.get("bot_email") or os.getenv("ZULIP_BOT_EMAIL", "")
    site = extra.get("site_url") or os.getenv("ZULIP_SITE_URL", "")

    if not api_key:
        logger.debug("Zulip: ZULIP_API_KEY not set")
        return False
    if not email:
        logger.warning("Zulip: ZULIP_BOT_EMAIL not set")
        return False
    if not site:
        logger.warning("Zulip: ZULIP_SITE_URL not set")
        return False

    try:
        import zulip  # noqa: F401
        return True
    except ImportError:
        pass  # fall through to lazy-install attempt

    # Standard lazy-install path (prompt=False to avoid double-prompting
    # in the gateway startup flow).
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("platform.zulip", prompt=False)
        import zulip  # noqa: F401
        return True
    except Exception:
        # Let the caller (gateway/run.py factory) emit the single clean
        # user-facing message suggesting the [zulip] extra.
        return False


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ZulipAdapter(BasePlatformAdapter):
    """Gateway adapter for Zulip (cloud or self-hosted)."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.ZULIP)

        self._site_url: str = (
            config.extra.get("site_url", "")
            or os.getenv("ZULIP_SITE_URL", "")
        ).rstrip("/")
        self._bot_email: str = (
            config.extra.get("bot_email", "")
            or os.getenv("ZULIP_BOT_EMAIL", "")
        )
        self._api_key: str = (
            config.token
            or config.api_key
            or os.getenv("ZULIP_API_KEY", "")
        )
        self._default_stream: str = (
            config.extra.get("default_stream", "")
            or os.getenv("ZULIP_DEFAULT_STREAM", "")
        )
        self._home_topic: str = (
            config.extra.get("home_topic", "")
            or os.getenv("ZULIP_HOME_TOPIC", "")
        )
        self._cert_bundle: str = os.getenv("ZULIP_CERT_BUNDLE", "")
        self._allow_insecure: bool = os.getenv(
            "ZULIP_ALLOW_INSECURE", "false"
        ).lower() in ("true", "1", "yes")

        # Mention gating configuration (follows Discord's pattern).
        self._require_mention: bool = os.getenv(
            "ZULIP_REQUIRE_MENTION", "true"
        ).lower() not in ("false", "0", "no")

        free_streams_raw = os.getenv("ZULIP_FREE_RESPONSE_STREAMS", "")
        self._free_response_streams: set = {
            s.strip().lower()
            for s in free_streams_raw.split(",")
            if s.strip()
        }

        # Historical context: when the bot is @mentioned in a stream, fetch
        # the last N messages from that stream+topic via Zulip's /messages API
        # and inject them as context before the user's message.  Survives
        # disconnects — the bot uses Zulip as the source of truth.
        self._context_depth: int = int(
            os.getenv("ZULIP_CONTEXT_DEPTH", "0")
        )

        # Background thread running the event queue.
        self._event_thread: Optional[threading.Thread] = None
        self._closing = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._event_futures: set[Any] = set()

        # Bot identity (resolved on connect)
        self._bot_user_id: int = -1
        self._bot_full_name: str = ""

        # Dedup cache: event_id → timestamp
        self._seen_events: Dict[str, float] = {}
        self._SEEN_MAX = 2000
        self._SEEN_TTL = 300  # 5 minutes

        # Stream name → stream_id cache (populated on connect)
        self._stream_id_cache: Dict[str, int] = {}
        # stream_id → stream_name reverse cache
        self._stream_name_cache: Dict[int, str] = {}

        # Email.lower() → user_id cache for DM typing.
        #
        # CRITICAL Zulip API requirement (DM / direct-message typing):
        #   set_typing_status for type="direct" REQUIRES an array of *integer*
        #   user_ids in the "to" field.  Passing email strings (or any non-int)
        #   is rejected by the server (the client library may appear to accept
        #   it but the wire payload fails validation on the Zulip side).
        #
        # We populate this cache from every inbound private message's
        # "sender_id" (always present and numeric in Zulip events) and fall
        # back to on-demand lookup via client.get_user(email=...) for the
        # (rare) case of an outbound DM typing indicator before any inbound
        # traffic from that user has been seen in the current process.
        self._user_id_cache: Dict[str, int] = {}

        # Graceful shutdown: event that wakes the event-queue thread
        # immediately when disconnect() is called, instead of waiting
        # for the full backoff sleep to elapse.
        self._shutdown_event = threading.Event()
        self._consecutive_failures = 0

        # Zulip client — created in connect(), used by the event-queue thread.
        # Send operations use _build_send_client() instead (thread safety).
        self._client: Any = None

    def _build_send_client(self) -> Any:
        """Create a fresh Zulip client for a send operation.

        The event-queue thread holds ``self._client`` for long-polling.
        Sharing a ``requests.Session`` across threads corrupts SSL state,
        so send operations get their own ephemeral client.
        """
        import zulip
        kwargs: Dict[str, Any] = {
            "site": self._site_url,
            "email": self._bot_email,
            "api_key": self._api_key,
        }
        if self._cert_bundle:
            kwargs["cert_bundle"] = self._cert_bundle
        if self._allow_insecure:
            kwargs["insecure"] = True
        return zulip.Client(**kwargs)

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to Zulip, verify auth, and start the event queue."""
        if not self._site_url or not self._api_key or not self._bot_email:
            logger.error(
                "Zulip: missing configuration (site_url, api_key, or bot_email)"
            )
            return False

        import zulip

        # Create the synchronous Zulip client.
        client_kwargs: Dict[str, Any] = {
            "site": self._site_url,
            "email": self._bot_email,
            "api_key": self._api_key,
        }
        if self._cert_bundle:
            client_kwargs["cert_bundle"] = self._cert_bundle
        if self._allow_insecure:
            client_kwargs["insecure"] = True

        self._client = zulip.Client(**client_kwargs)

        # Verify credentials by fetching the bot's own profile.
        try:
            result = self._client.get_profile()
        except Exception as exc:
            logger.error("Zulip: failed to authenticate — %s", exc)
            return False

        if result.get("result") != "success":
            msg = result.get("msg", "unknown error")
            logger.error(
                "Zulip: authentication failed — %s. "
                "Check ZULIP_API_KEY, ZULIP_BOT_EMAIL, and ZULIP_SITE_URL.",
                msg,
            )
            return False

        profile = result.get("profile") if isinstance(result.get("profile"), dict) else result
        self._bot_user_id = profile.get("user_id", -1)
        self._bot_full_name = profile.get("full_name", "")
        logger.info(
            "Zulip: authenticated as %s (user_id=%d) on %s",
            self._bot_email,
            self._bot_user_id,
            self._site_url,
        )

        # Populate stream-id cache early (helps typing indicators on first messages).
        self._refresh_stream_cache()
        logger.debug("Zulip: adapter fully connected and ready (stream cache has %d entries so far)", len(self._stream_id_cache))

        # Start the event queue in a background thread.
        self._loop = asyncio.get_running_loop()
        self._closing = False
        self._shutdown_event.clear()
        self._consecutive_failures = 0
        self._event_thread = threading.Thread(
            target=self._run_event_queue,
            name="zulip-event-queue",
            daemon=True,
        )
        self._event_thread.start()

        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        """Stop the event queue, cancel background tasks, and close the client."""
        self._closing = True
        self._shutdown_event.set()  # Wake up the event thread immediately.

        # Wait for the event-queue thread to exit.
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=10.0)

        # Cancel any in-flight message-processing tasks that were
        # scheduled on the asyncio event loop.
        try:
            await self.cancel_background_tasks()
        except Exception:
            pass

        for future in list(self._event_futures):
            future.cancel()
        self._event_futures.clear()

        self._client = None
        self._loop = None

        # Clear caches to free memory and avoid stale data on reconnect.
        self._seen_events.clear()
        self._stream_id_cache.clear()
        self._stream_name_cache.clear()
        self._user_id_cache.clear()
        self._consecutive_failures = 0

        self._mark_disconnected()
        logger.info("Zulip: disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message (or multiple chunks) to a Zulip chat."""
        if not content:
            return SendResult(success=True)

        outbound_chat_id = self._metadata_adjusted_chat_id(chat_id, metadata)

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, MAX_MESSAGE_LENGTH)

        last_id = None
        for chunk in chunks:
            result = await asyncio.to_thread(
                self._do_send_message,
                outbound_chat_id,
                chunk,
                reply_to,
            )
            if result.success:
                last_id = result.message_id
            else:
                return result

        return SendResult(success=True, message_id=last_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return chat name and type (dm/stream)."""
        # Try stream first.
        parsed = _parse_stream_chat_id(chat_id)
        if parsed:
            stream_id, topic = parsed
            stream_name = self._stream_name_cache.get(stream_id, chat_id)
            return {"name": f"#{stream_name} > {topic}", "type": "stream"}

        # Try DM.
        dm_email = _parse_dm_chat_id(chat_id)
        if dm_email:
            return {"name": dm_email, "type": "dm"}

        return {"name": chat_id, "type": "dm"}

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Send a typing indicator to Zulip.

        For streams, the modern Zulip typing API requires a numeric ``stream_id``
        + ``topic`` (see :func:`_build_stream_typing_request`). For DMs it
        requires integer user IDs (never bare emails) in the ``to`` array.
        Both are resolved on-demand via caches + resolvers below.
        """
        if not self._client:
            logger.debug("Zulip: send_typing called but no client yet for chat_id=%r", chat_id)
            return

        outbound_chat_id = chat_id
        thread_id = metadata.get("thread_id") if metadata else None
        if thread_id and not _parse_stream_chat_id(chat_id):
            if not is_dm_chat_id(chat_id) and not is_group_dm_chat_id(chat_id):
                outbound_chat_id = f"{chat_id}:{thread_id}"

        request = self._build_typing_request(outbound_chat_id, op="start")
        if not request:
            logger.warning("Zulip: send_typing failed — could not resolve chat_id %r (no request built)", outbound_chat_id)
            return

        # Success-path logging is debug only; INFO would spam gateway.log on
        # every assistant turn because _keep_typing refreshes every ~2s.
        logger.debug("Zulip: sending typing indicator → chat_id=%r payload=%s", outbound_chat_id, request)

        try:
            send_client = self._build_send_client()
            result = send_client.set_typing_status(request)
            if result.get("result") != "success":
                logger.debug("Zulip: send_typing API call failed — %s (payload was %s)", result.get("msg", "unknown error"), request)
            else:
                logger.debug("Zulip: send_typing SUCCESS for %r (payload=%s)", chat_id, request)
        except Exception as exc:
            logger.debug("Zulip: send_typing exception — %s (payload was %s)", exc, request)

    async def stop_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Clear the typing indicator in Zulip (send 'op': 'stop').

        Signature accepts optional *metadata* (for signature compatibility with
        send_typing and platforms like Slack that use it) and applies the same
        outbound_chat_id / thread_id adjustment logic so that stop matches the
        payload that would have been sent by the corresponding send_typing call.
        Note: metadata is accepted for sig-compat but current _keep_typing stop
        paths pass only chat_id (chat_id encoding for Zulip streams already
        carries the topic via _build_stream_chat_id).
        """
        if not self._client:
            logger.debug("Zulip: stop_typing called but no client yet for chat_id=%r", chat_id)
            return

        outbound_chat_id = chat_id
        thread_id = metadata.get("thread_id") if metadata else None
        if thread_id and not _parse_stream_chat_id(chat_id):
            if not is_dm_chat_id(chat_id) and not is_group_dm_chat_id(chat_id):
                outbound_chat_id = f"{chat_id}:{thread_id}"

        request = self._build_typing_request(outbound_chat_id, op="stop")
        if not request:
            return

        # Debug level only — stop is called on every turn completion and must
        # not pollute INFO logs.
        logger.debug("Zulip: sending STOP typing → chat_id=%r payload=%s", outbound_chat_id, request)

        try:
            send_client = self._build_send_client()
            result = send_client.set_typing_status(request)
            if result.get("result") != "success":
                logger.debug("Zulip: stop_typing failed — %s (payload=%s)", result.get("msg", "unknown error"), request)
            else:
                logger.debug("Zulip: stop_typing SUCCESS for %r", outbound_chat_id)
        except Exception as exc:
            logger.debug("Zulip: stop_typing exception — %s (payload=%s)", exc, request)

    async def edit_message(
        self, chat_id: str, message_id: str, content: str
    ) -> SendResult:
        """Edit an existing message."""
        if not self._client or not message_id:
            return SendResult(success=False, error="Not supported")

        formatted = self.format_message(content)
        send_client = self._build_send_client()
        try:
            result = send_client.update_message({
                "message_id": int(message_id),
                "content": formatted,
            })
            if result.get("result") == "success":
                return SendResult(success=True, message_id=message_id)
            else:
                return SendResult(
                    success=False,
                    error=result.get("msg", "update failed"),
                )
        except Exception as exc:
            return SendResult(success=False, error=str(exc))

    def format_message(self, content: str) -> str:
        """Zulip supports standard Markdown including code blocks, tables,
        LaTeX math, and image links.
        """
        return content

    def _metadata_adjusted_chat_id(
        self,
        chat_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Apply gateway thread metadata to a Zulip outbound chat ID.

        Intended use:
            Gateway callers sometimes pass a stream name in ``chat_id`` and a
            topic in ``metadata['thread_id']``.  Zulip stream sends need both
            values in one target string before :meth:`_do_send_message` can
            turn them into the API request fields.  DMs and already-canonical
            ``stream_id:topic`` chat IDs are left untouched.
        """
        thread_id = metadata.get("thread_id") if metadata else None
        if thread_id and not _parse_stream_chat_id(chat_id) and not _parse_stream_name_topic(chat_id):
            if not is_dm_chat_id(chat_id) and not is_group_dm_chat_id(chat_id):
                return f"{chat_id}:{thread_id}"
        return chat_id

    async def _send_zform_choices(
        self,
        chat_id: str,
        content: str,
        heading: str,
        choices: List[Dict[str, Any]],
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a Zulip message with an attached zform choices widget.

        Intended use:
            High-level Hermes prompts that are naturally a set of choices:
            dangerous-command approvals, slash-command confirmations, and
            multiple-choice clarify questions.

        How it works:
            The text body remains readable in clients that ignore widgets.  The
            attached ``widget_content`` renders buttons in compatible Zulip
            clients.  Button clicks emit normal visible Zulip replies, so the
            choice ``reply`` values deliberately reuse Hermes' existing text
            protocol instead of introducing a second callback channel.

        The zform widget is attached to one Zulip message only.  Callers should
        keep prompt bodies concise; this helper truncates overlong Markdown to
        Zulip's normal outbound chunk size so the buttons stay with the prompt.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")

        outbound_chat_id = self._metadata_adjusted_chat_id(chat_id, metadata)
        formatted = self.format_message(content or heading)
        chunks = self.truncate_message(formatted, MAX_MESSAGE_LENGTH)
        prompt_text = chunks[0] if chunks else str(heading)
        widget_content = _build_zform_widget_content(heading=heading, choices=choices)
        return await asyncio.to_thread(
            self._do_send_message,
            outbound_chat_id,
            prompt_text,
            reply_to=reply_to,
            widget_content=widget_content,
        )

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render a dangerous-command approval prompt as Zulip zform buttons.

        Intended use:
            Called by the gateway's approval notifier when a tool command is
            blocked waiting for user approval.

        How it works:
            Zulip zform buttons do not deliver hidden callback payloads.  They
            send visible replies, so the four zform choices are exactly the text
            commands the existing approval handler already accepts:
            ``/approve``, ``/approve session``, ``/approve always``, and
            ``/deny``.  ``session_key`` is accepted for interface parity with
            other rich-button adapters; the resolver remains session-scoped in
            ``tools.approval`` just like the plain-text fallback.
        """
        before_command = "⚠️ **Command Approval Required**\n\n```\n"
        after_command = (
            f"\n```\nReason: {description}\n\n"
            "Use the buttons below, or reply with `/approve`, `/approve session`, "
            "`/approve always`, or `/deny`."
        )
        # Keep the fallback instructions in the same Zulip message as the
        # widget.  If the command is very long, trim the preview instead of
        # letting generic message chunking detach the buttons from context.
        available_for_command = MAX_MESSAGE_LENGTH - len(before_command) - len(after_command)
        ellipsis = "..."
        if available_for_command <= len(ellipsis):
            cmd_preview = ellipsis[: max(0, available_for_command)]
        elif len(command) > available_for_command:
            cmd_preview = command[: available_for_command - len(ellipsis)] + ellipsis
        else:
            cmd_preview = command
        body = f"{before_command}{cmd_preview}{after_command}"
        choices = [
            {"short_name": "Once", "long_name": "Approve once", "reply": "/approve"},
            {"short_name": "Session", "long_name": "Approve this pattern for the session", "reply": "/approve session"},
            {"short_name": "Always", "long_name": "Approve this pattern permanently", "reply": "/approve always"},
            {"short_name": "Deny", "long_name": "Deny and cancel the command", "reply": "/deny"},
        ]
        return await self._send_zform_choices(
            chat_id=chat_id,
            content=body,
            heading="Command approval required",
            choices=choices,
            metadata=metadata,
        )

    async def send_slash_confirm(
        self,
        chat_id: str,
        title: str,
        message: str,
        session_key: str,
        confirm_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render a slash-command confirmation prompt as Zulip zform buttons.

        Intended use:
            Called by the gateway's generic slash-confirm primitive for commands
            that need an explicit acknowledgement, such as ``/reload-mcp``.

        How it works:
            The zform button replies are the same text commands intercepted by
            ``GatewayRunner`` for non-button platforms: ``/approve`` resolves
            the prompt once, ``/always`` persists the confirmation policy, and
            ``/cancel`` denies it.  ``confirm_id`` and ``session_key`` are kept
            in the signature for adapter parity; Zulip's visible text replies
            resolve via the existing session-scoped slash-confirm state.
        """
        body = (
            f"**{title}**\n\n"
            f"{message}\n\n"
            "Use the buttons below, or reply with `/approve`, `/always`, or `/cancel`."
        )
        choices = [
            {"short_name": "Approve", "long_name": "Approve once", "reply": "/approve"},
            {"short_name": "Always", "long_name": "Always approve this action", "reply": "/always"},
            {"short_name": "Cancel", "long_name": "Cancel", "reply": "/cancel"},
        ]
        return await self._send_zform_choices(
            chat_id=chat_id,
            content=body,
            heading=title,
            choices=choices,
            metadata=metadata,
        )

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[list],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render a clarify prompt using Zulip zform choices when possible.

        Intended use:
            Called by the gateway-side ``clarify`` tool callback while the agent
            thread is blocked waiting for the user's answer.

        How it works:
            For multiple-choice prompts, each zform button sends the literal
            choice text as a normal Zulip reply.  Before sending, the adapter
            marks the pending clarify entry as awaiting text so
            ``GatewayRunner._maybe_intercept_clarify_text`` captures either a
            button-emitted reply or a user-typed free-form answer.  For
            open-ended prompts, this method falls back to the base text path.
        """
        if not choices:
            return await super().send_clarify(
                chat_id=chat_id,
                question=question,
                choices=choices,
                clarify_id=clarify_id,
                session_key=session_key,
                metadata=metadata,
            )

        from tools.clarify_gateway import mark_awaiting_text

        mark_awaiting_text(clarify_id)
        choice_rows = [
            {
                "short_name": str(index),
                "long_name": str(choice),
                "reply": str(choice),
            }
            for index, choice in enumerate(choices, start=1)
        ]
        option_lines = "\n".join(
            f"  {index}. {choice}"
            for index, choice in enumerate(choices, start=1)
        )
        body = (
            f"❓ {question}\n\n"
            f"{option_lines}\n\n"
            "Use the buttons below, or type any other answer."
        )
        return await self._send_zform_choices(
            chat_id=chat_id,
            content=body,
            heading=str(question),
            choices=choice_rows,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Rich delivery: images, documents, video
    #
    # Zulip supports file uploads via ``POST /user_uploads`` which returns
    # a URI.  That URI is embedded in the message body using standard
    # Markdown image/link syntax:
    #
    #   * Images:  ``![alt](/user_uploads/...)``  →  rendered inline
    #   * Files:   ``[name](/user_uploads/...)``   →  rendered as link
    #
    # Voice messages have NO native representation in Zulip (no voice
    # bubbles).  ``send_voice`` intentionally falls back to the base
    # class, which sends the file path as text.
    # ------------------------------------------------------------------

    def _upload_file(
        self,
        file_bytes: bytes,
        filename: str,
    ) -> Optional[str]:
        """Upload *file_bytes* to Zulip and return the public URI.

        Returns the URI string on success (e.g. ``"/user_uploads/1/..."``),
        or ``None`` on failure.  Logs a warning but never raises.
        """
        if not self._client:
            logger.warning("Zulip: upload_file called while not connected")
            return None

        try:
            # The Zulip client expects a file-like object.  ``upload_file``
            # passes it to ``requests.post(files=[...])`` which reads the
            # content and uses the ``.name`` attribute (if present) as the
            # uploaded filename.  We wrap in ``BytesIO`` and set ``.name``
            # so the server gets a proper filename.
            buf = io.BytesIO(file_bytes)
            buf.name = filename
            send_client = self._build_send_client()
            result = send_client.upload_file(buf)
            if result.get("result") == "success":
                uri = result.get("uri", "")
                if uri:
                    return uri
            logger.warning(
                "Zulip: upload_file failed — %s",
                result.get("msg", "unknown error"),
            )
        except Exception as exc:
            logger.error("Zulip: upload_file exception — %s", exc)
        return None

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Download an image URL, upload to Zulip, and send inline.

        Falls back to sending the URL as plain text if the download or
        upload fails.
        """
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=30.0, follow_redirects=True,
            ) as client:
                resp = await client.get(
                    image_url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (compatible; HermesAgent/1.0)"
                        ),
                        "Accept": "image/*,*/*;q=0.8",
                    },
                )
                resp.raise_for_status()
                file_bytes = resp.content
        except Exception as exc:
            logger.warning(
                "Zulip: failed to download image %s: %s", image_url, exc,
            )
            text = f"{caption}\n{image_url}" if caption else image_url
            return await self.send(chat_id, content=text, reply_to=reply_to, metadata=metadata)

        # Derive filename from URL path.
        url_path = image_url.rsplit("/", 1)[-1].split("?")[0]
        ext = Path(url_path).suffix.lower() or ".png"
        filename = f"image{ext}"

        uri = await asyncio.to_thread(
            self._upload_file, file_bytes, filename,
        )
        if not uri:
            # Upload failed — fall back to URL in text.
            text = f"{caption}\n{image_url}" if caption else image_url
            return await self.send(chat_id, content=text, reply_to=reply_to, metadata=metadata)

        alt = caption or "image"
        content = f"![{alt}]({uri})"
        return await self.send(chat_id, content=content, reply_to=reply_to, metadata=metadata)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Upload a local image file and send it inline."""
        metadata = kwargs.get("metadata")
        p = Path(image_path)
        if not p.exists():
            text = f"{caption or ''}\n(file not found: {image_path})".strip()
            return await self.send(chat_id, content=text, reply_to=reply_to, metadata=metadata)

        file_bytes = p.read_bytes()
        filename = p.name

        uri = await asyncio.to_thread(
            self._upload_file, file_bytes, filename,
        )
        if not uri:
            return SendResult(
                success=False,
                error="File upload failed",
            )

        alt = caption or "image"
        content = f"![{alt}]({uri})"
        return await self.send(chat_id, content=content, reply_to=reply_to, metadata=metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Upload a local file and send it as a downloadable attachment.

        The file is presented as a Markdown link ``[filename](uri)`` in
        the message body, with *caption* as optional surrounding text.
        """
        metadata = kwargs.get("metadata")
        p = Path(file_path)
        if not p.exists():
            text = f"{caption or ''}\n(file not found: {file_path})".strip()
            return await self.send(chat_id, content=text, reply_to=reply_to, metadata=metadata)

        file_bytes = p.read_bytes()
        filename = file_name or p.name

        uri = await asyncio.to_thread(
            self._upload_file, file_bytes, filename,
        )
        if not uri:
            return SendResult(
                success=False,
                error="File upload failed",
            )

        # Format: optional caption + markdown link to uploaded file.
        link = f"[{filename}]({uri})"
        content = f"{caption}\n{link}" if caption else link
        return await self.send(chat_id, content=content, reply_to=reply_to, metadata=metadata)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Upload a video file and send it as a link.

        Zulip does not inline video playback.  The uploaded file is
        presented as a clickable Markdown link.  This is the best
        representation Zulip can provide for video content.
        """
        metadata = kwargs.get("metadata")
        p = Path(video_path)
        if not p.exists():
            text = f"{caption or ''}\n(file not found: {video_path})".strip()
            return await self.send(chat_id, content=text, reply_to=reply_to, metadata=metadata)

        file_bytes = p.read_bytes()
        filename = p.name

        uri = await asyncio.to_thread(
            self._upload_file, file_bytes, filename,
        )
        if not uri:
            return SendResult(
                success=False,
                error="Video upload failed",
            )

        link = f"[{filename}]({uri})"
        content = f"{caption}\n{link}" if caption else link
        return await self.send(chat_id, content=content, reply_to=reply_to, metadata=metadata)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ):
        """Send an audio file as a downloadable attachment.

        Zulip has no native voice message bubbles, so we upload the audio
        file and send it as a link (same as other file types).
        """
        return await self.send_document(
            chat_id=chat_id,
            file_path=audio_path,
            caption=caption,
            file_name=kwargs.get("file_name"),
            reply_to=reply_to,
            metadata=kwargs.get("metadata"),
        )

    # ------------------------------------------------------------------
    # Internal: sending
    # ------------------------------------------------------------------

    def _do_send_message(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        widget_content: Optional[str] = None,
    ) -> SendResult:
        """Build the correct request dict and call the Zulip API.

        This is synchronous because the Zulip client is not async.  Rich prompt
        helpers pass ``widget_content`` when they need Zulip to render a
        message widget such as zform; ordinary sends leave it unset so the
        request remains a normal text message.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")

        send_client = self._build_send_client()

        parsed = _parse_stream_chat_id(chat_id)
        if parsed:
            stream_id, topic = parsed
            request = {
                "type": "stream",
                "to": str(stream_id),
                "topic": topic,
                "content": content,
            }
        else:
            named_stream = _parse_stream_name_topic(chat_id)
            if named_stream:
                stream_name, topic = named_stream
                result = send_client.get_stream_id(stream_name)
                if result.get("result") != "success":
                    return SendResult(
                        success=False,
                        error=result.get("msg", f"Stream '{stream_name}' not found"),
                    )
                stream_id = result.get("stream_id")
                if stream_id is None:
                    return SendResult(success=False, error=f"Stream '{stream_name}' not found")
                request = {
                    "type": "stream",
                    "to": str(stream_id),
                    "topic": topic,
                    "content": content,
                }
            elif is_dm_chat_id(chat_id):
                email = _parse_dm_chat_id(chat_id)
                request = {
                    "type": "private",
                    "to": [email],
                    "content": content,
                }
            elif is_group_dm_chat_id(chat_id):
                emails = _parse_group_dm_chat_id(chat_id)
                if emails:
                    request = {
                        "type": "private",
                        "to": emails,
                        "content": content,
                    }
                else:
                    return SendResult(success=False, error="Invalid group DM chat ID")
            else:
                # Fallback: treat as DM to the email itself.
                request = {
                    "type": "private",
                    "to": [chat_id],
                    "content": content,
                }

        if widget_content is not None:
            request["widget_content"] = widget_content

        try:
            result = send_client.send_message(request)
            if result.get("result") == "success":
                msg_id = result.get("id")
                return SendResult(success=True, message_id=str(msg_id) if msg_id else None)
            else:
                return SendResult(
                    success=False,
                    error=result.get("msg", "send failed"),
                )
        except Exception as exc:
            logger.error("Zulip: send_message failed — %s", exc)
            return SendResult(success=False, error=str(exc))

    def _build_typing_request(
        self, chat_id: str, op: str = "start"
    ) -> Optional[Dict[str, Any]]:
        """Return a typing request dict for ``set_typing_status``.

        Zulip typing API quirks (both paths now robust):
        * Streams/channels: MUST use ``{"stream_id": N, "topic": "...", "type": "stream"}``.
          The legacy ``{"to": [stream_name]}`` form is no longer reliable
          (community patch + on-demand resolver fixed this).
        * DMs (1:1 or group): MUST use ``{"to": [integer_user_id, ...], "type": "direct"}``.
          Emails (even as strings) are rejected by the server. See the long
          comment on ``_user_id_cache`` and the population site in
          ``_dispatch_inbound``.
        On-demand resolvers + caches make this work even on first message to
        a stream/user in the process lifetime.
        """
        parsed = _parse_stream_chat_id(chat_id)
        if parsed:
            stream_id, topic = parsed
            return _build_stream_typing_request(stream_id, topic, op)

        named_stream = _parse_stream_name_topic(chat_id)
        if named_stream:
            stream_name, topic = named_stream
            key = stream_name.lower()
            stream_id = self._stream_id_cache.get(key)
            if stream_id is None:
                stream_id = self._resolve_stream_id(stream_name)
            if stream_id is not None:
                return _build_stream_typing_request(stream_id, topic, op)

            # Only as a last-ditch fallback (should rarely happen now)
            logger.debug("Zulip: falling back to legacy stream name for typing of %r", chat_id)
            return {"to": [stream_name], "type": "stream", "op": op}

        dm_email = _parse_dm_chat_id(chat_id)
        if dm_email:
            key = dm_email.lower()
            user_id = self._user_id_cache.get(key)
            if user_id is None:
                user_id = self._resolve_user_id(dm_email)
            if user_id is not None:
                return {"to": [user_id], "type": "direct", "op": op}
            logger.warning("Zulip: could not resolve DM email %r to user ID for typing (no cache hit, resolution failed)", dm_email)
            # Do not fall back to email — Zulip rejects non-integer user IDs for direct typing
            return None
        return None

    def _resolve_stream_id(self, stream_name: str) -> Optional[int]:
        """Best-effort live lookup of stream name → ID using the Zulip client.

        Populates both caches on success. Used by the typing path so we don't
        depend on the background stream list having run yet (e.g. first typing
        indicator before any messages arrived in a stream).

        Follows the same cache + on-demand pattern as the user_id resolver
        for DM typing.
        """
        if not self._client or not stream_name:
            return None
        key = stream_name.lower()
        try:
            result = self._client.get_streams()
            for s in result.get("streams", []):
                if s.get("name", "").lower() == key:
                    sid = s["stream_id"]
                    self._stream_id_cache[key] = sid
                    self._stream_name_cache[sid] = s["name"]
                    logger.debug("Zulip: on-demand resolved stream %r -> id=%s for typing", stream_name, sid)
                    return sid
        except Exception as exc:
            logger.warning("Zulip: on-demand stream resolution failed for %r: %s", stream_name, exc)
        return None

    def _resolve_user_id(self, email: str) -> Optional[int]:
        """Best-effort lookup of user email → user_id for DM typing.

        Zulip's set_typing_status for direct messages (type="direct") *requires*
        integer user IDs in the "to" field — emails are rejected at the API
        level (this was the root cause of DM typing being completely silent).

        We primarily populate the cache from inbound "sender_id" in
        _dispatch_inbound (Zulip always sends numeric IDs in message events).
        This on-demand path (via the zulip client's get_user helper, which
        does a /users/{email} lookup) is the fallback for the first outbound
        typing indicator to a user we have never received a message from yet.
        """
        if not email or not self._client:
            return None
        key = email.lower()
        if key in self._user_id_cache:
            return self._user_id_cache[key]
        try:
            # Standard zulip client helper
            result = self._client.get_user(email=email)
            if result.get("result") == "success":
                user = result.get("user") or {}
                uid = user.get("user_id")
                if uid:
                    self._user_id_cache[key] = uid
                    logger.debug("Zulip: on-demand resolved user %r -> id=%s for typing", email, uid)
                    return uid
        except Exception as exc:
            logger.warning("Zulip: on-demand user resolution failed for %r: %s", email, exc)
        return None

    # ------------------------------------------------------------------
    # Internal: event queue
    # ------------------------------------------------------------------

    def _run_event_queue(self) -> None:
        """Run the Zulip event queue in the current thread.

        Uses ``call_on_each_event`` which internally handles long-polling
        and basic reconnection.  Wraps with our own exponential backoff
        for the cases where the Zulip client's internal retry gives up.

        The backoff sleep uses :pymeth:`threading.Event.wait` so that
        :meth:`disconnect` can wake the thread immediately instead of
        waiting for the full delay to elapse.
        """
        delay = _RECONNECT_BASE_DELAY
        self._consecutive_failures = 0

        while not self._closing:
            try:
                self._client.call_on_each_event(
                    self._on_zulip_event,
                    event_types=["message"],
                    apply_markdown=False,
                )
                # ``call_on_each_event`` returned — server closed the
                # event queue stream or the client hit an internal limit.
                if self._closing:
                    return
                logger.info("Zulip: event queue stream ended — reconnecting")
                self._consecutive_failures = 0
                delay = _RECONNECT_BASE_DELAY
                continue
            except Exception as exc:
                if self._closing:
                    return

                self._consecutive_failures += 1
                retryable = _is_retryable_error(exc)

                if not retryable:
                    logger.error(
                        "Zulip: non-retryable error (attempt %d): %s — "
                        "stopping event queue",
                        self._consecutive_failures,
                        type(exc).__name__,
                    )
                    self._set_fatal_error(
                        "ZULIP_EVENT_QUEUE_FATAL",
                        f"Non-retryable error: {type(exc).__name__}: {exc}",
                        retryable=False,
                    )
                    return

                logger.warning(
                    "Zulip: event queue error (attempt %d): %s — "
                    "reconnecting in %.0fs",
                    self._consecutive_failures,
                    type(exc).__name__,
                    delay,
                )

            if self._closing:
                return

            # Exponential backoff with jitter.
            jitter = delay * _RECONNECT_JITTER * random.random()
            sleep_time = delay + jitter
            if self._consecutive_failures > 1:
                logger.info(
                    "Zulip: waiting %.1fs before reconnect attempt %d",
                    sleep_time,
                    self._consecutive_failures + 1,
                )
            if self._shutdown_event.wait(timeout=sleep_time):
                return  # Shutdown signal received during backoff.
            delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    def _on_zulip_event(self, event: Dict[str, Any]) -> None:
        """Callback invoked by ``call_on_each_event`` for each event.

        Runs in the event-queue thread.  Schedules the actual processing
        on the asyncio event loop via ``call_soon_threadsafe``.
        """
        if self._closing:
            return

        # Defense in depth: verify event shape.  The server-side filter
        # should only deliver "message" events, but validate anyway.
        event_type = event.get("type", "")
        if event_type != "message":
            logger.debug(
                "Zulip: ignoring non-message event (type=%s)",
                event_type,
            )
            return

        event_op = event.get("op", "add")
        if event_op != "add":
            # Edits/deletes come through as different event types or
            # ops — we only handle new-message creation.
            logger.debug(
                "Zulip: ignoring message event with op=%s",
                event_op,
            )
            return

        # Extract message payload.
        message = event.get("message")
        if not message or not isinstance(message, dict):
            return

        # Dedup by Zulip message ID.
        msg_id = str(message.get("id", ""))
        self._prune_seen()
        if msg_id and msg_id in self._seen_events:
            return
        if msg_id:
            self._seen_events[msg_id] = time.time()

        # Filter self-messages.
        sender_email = message.get("sender_email", "")
        sender_id = message.get("sender_id", -1)
        if sender_email == self._bot_email or sender_id == self._bot_user_id:
            return

        # Schedule async processing on the main event loop.
        msg_type_log = message.get("type", "unknown")
        logger.debug(
            "Zulip: inbound msg_id=%s sender=%s type=%s",
            msg_id,
            sender_email,
            msg_type_log,
        )
        if self._loop and not self._loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(
                self._dispatch_inbound(message, event), self._loop
            )
            self._event_futures.add(future)

            def _discard_done(done_future):
                self._event_futures.discard(done_future)
                if done_future.cancelled():
                    return
                try:
                    done_future.result()
                except Exception:
                    logger.exception("Zulip: inbound dispatch task failed")

            future.add_done_callback(_discard_done)

    async def _fetch_context(
        self, stream_name: str, topic: str
    ) -> list:
        """Fetch recent messages from a stream+topic via Zulip's /messages API.

        Returns a list of formatted context lines like ``["Alice: hello", ...]``,
        skipping the bot's own messages.  Runs the synchronous HTTP call in a
        thread executor so the event loop is never blocked.

        Returns an empty list on any failure — context is best-effort.
        """
        if self._context_depth <= 0:
            return []

        send_client = self._build_send_client()
        try:
            result = await asyncio.to_thread(
                send_client.get_messages,
                {
                    "anchor": "newest",
                    "num_before": self._context_depth,
                    "num_after": 0,
                    "narrow": [
                        ["stream", stream_name],
                        ["topic", topic],
                    ],
                    "apply_markdown": False,
                },
            )
        except Exception as exc:
            logger.warning("Zulip: context fetch failed — %s", exc)
            return []

        if result.get("result") != "success":
            logger.debug(
                "Zulip: context fetch error for #%s > %s — %s",
                stream_name, topic, result.get("msg", "unknown"),
            )
            return []

        messages = result.get("messages", [])
        context_lines = []
        for msg in reversed(messages):
            sender = msg.get("sender_full_name") or msg.get("sender_email", "?")
            content = (msg.get("content") or "").strip()
            # Skip the bot's own messages and empty content.
            if msg.get("sender_email") == self._bot_email:
                continue
            if not content:
                continue
            context_lines.append(f"{sender}: {content}")

        logger.debug(
            "Zulip: fetched %d context messages for #%s > %s (requested %d)",
            len(context_lines), stream_name, topic, self._context_depth,
        )
        return context_lines

    async def _dispatch_inbound(self, message: Dict[str, Any], raw_event: Dict[str, Any]) -> None:
        """Process an inbound message on the asyncio event loop.

        For stream @mentions, fetches recent context from Zulip's /messages API
        and injects it before the user's message so the agent has full awareness.
        """

        # Determine message type and chat context.
        msg_type_name = message.get("type", "")  # "stream" or "private"
        content = message.get("content", "")
        sender_email = message.get("sender_email", "")
        sender_full_name = message.get("sender_full_name", "") or sender_email
        sender_id = message.get("sender_id", -1)
        msg_id = str(message.get("id", ""))

        # Reject whitespace-only content early (before type-specific logic).
        if not content or not content.strip():
            return

        track_only = False  # May be set to True in stream block below

        if msg_type_name == "stream":
            stream_id = message.get("stream_id", -1)
            topic = message.get("subject") or "(no topic)"
            chat_id = _build_stream_chat_id(stream_id, topic)
            chat_type = "stream"
            chat_name = _resolve_stream_name(
                message, stream_id, self._stream_name_cache
            )
            chat_topic = topic
            user_id = sender_email
            user_name = sender_full_name

            # Check for @mention of the bot in stream messages.
            # DMs are always processed.
            mention_patterns = [
                f"@**{self._bot_full_name}**",
                f"@{self._bot_email}",
                # Zulip wildcard mentions that should wake the bot.
                "@**all**",
                "@**everyone**",
            ]

            # Determine if this stream requires a mention.
            require_mention = self._require_mention
            if require_mention and self._free_response_streams:
                # Check by stream name or stream ID.
                stream_name_lower = chat_name.lower()
                stream_id_str = str(stream_id)
                if (stream_name_lower in self._free_response_streams
                        or stream_id_str in self._free_response_streams):
                    require_mention = False

            if require_mention:
                has_mention = any(
                    pattern.lower() in content.lower()
                    for pattern in mention_patterns
                )
                if not has_mention:
                    logger.debug(
                        "Zulip: skipping stream message without @mention "
                        "(stream=%s, topic=%s)",
                        chat_name,
                        topic,
                    )
                    return

            # Fetch historical context from the same stream+topic via Zulip's
            # /messages API.  The bot gets full awareness of the conversation
            # even for messages that arrived while it was disconnected.
            if self._context_depth > 0:
                context = await self._fetch_context(chat_name, topic)
                if context:
                    content = _format_context_block(context) + content

            # Strip the bot mention from content so the agent sees
            # only the user's actual message (follows Slack/Discord pattern).
            bot_mention_only = [
                f"@**{self._bot_full_name}**",
                f"@{self._bot_email}",
            ]
            content = _strip_bot_mention(content, bot_mention_only)
        elif msg_type_name == "private":
            display_recipient = message.get("display_recipient")
            recipients = _extract_dm_recipients(
                display_recipient, self._bot_email, sender_email
            )

            if len(recipients) > 1:
                # Group DM (3+ original participants including bot).
                chat_id = _build_group_dm_chat_id(recipients)
                chat_type = "group"
                chat_name = ", ".join(recipients)
            else:
                # 1:1 DM.
                chat_id = _build_dm_chat_id(recipients[0] if recipients else sender_email)
                chat_type = "dm"
                chat_name = recipients[0] if recipients else sender_email

            chat_topic = None
            user_id = sender_email
            user_name = sender_full_name

            # Cache sender email -> user_id for DM typing.
            # Zulip typing for DMs requires the integer IDs (see _user_id_cache
            # declaration and _resolve_user_id for the full rationale).
            if sender_id > 0 and sender_email:
                self._user_id_cache[sender_email.lower()] = sender_id
        else:
            logger.debug("Zulip: ignoring message of type '%s'", msg_type_name)
            return

        # Determine message_type.
        mt = MessageType.TEXT
        if content.startswith("/") or content.startswith("!"):
            mt = MessageType.COMMAND

        # Reply-to detection (Zulip uses top-level reply metadata).
        reply_to_id = None
        # The Zulip event includes the message we're replying to in some
        # cases — but for now we handle outbound replies in send() via
        # the reply_to parameter.

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            chat_topic=chat_topic,
        )

        msg_event = MessageEvent(
            text=content,
            message_type=mt,
            source=source,
            raw_message=raw_event,
            message_id=msg_id,
            reply_to_message_id=reply_to_id,
        )

        # Schedule the handler coroutine on the event loop.
        asyncio.ensure_future(self.handle_message(msg_event))

    # ------------------------------------------------------------------
    # Internal: caches & helpers
    # ------------------------------------------------------------------

    def _refresh_stream_cache(self) -> None:
        """Fetch all streams and cache name ↔ ID mappings."""
        if not self._client:
            return
        try:
            result = self._client.get_streams()
            if result.get("result") == "success":
                for stream in result.get("streams", []):
                    sid = stream.get("stream_id")
                    name = stream.get("name", "")
                    if sid is not None and name:
                        self._stream_id_cache[name.lower()] = sid
                        self._stream_name_cache[sid] = name
                logger.info(
                    "Zulip: cached %d streams", len(self._stream_id_cache)
                )
        except Exception as exc:
            logger.warning("Zulip: failed to fetch streams — %s", exc)

    def _prune_seen(self) -> None:
        """Remove expired entries from the dedup cache."""
        if len(self._seen_events) < self._SEEN_MAX:
            return
        now = time.time()
        self._seen_events = {
            eid: ts
            for eid, ts in self._seen_events.items()
            if now - ts < self._SEEN_TTL
        }
