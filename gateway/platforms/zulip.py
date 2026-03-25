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
    ZULIP_REQUIRE_MENTION    Require @mention in streams (default: "true")
    ZULIP_FREE_RESPONSE_STREAMS  Comma-separated stream names or IDs that
                             don't require @mention
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
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


def _build_stream_chat_id(stream_id: int, topic: str) -> str:
    """Encode a stream message's origin as a stable chat ID.

    Format: ``"{stream_id}:{topic}"``
    """
    return f"{stream_id}:{topic}"


def _parse_stream_chat_id(chat_id: str) -> Optional[Tuple[int, str]]:
    """Parse a stream chat ID back into ``(stream_id, topic)``.

    Returns ``None`` if *chat_id* does not look like a stream chat ID.
    """
    # Stream chat IDs look like "123:some topic" — the part before the
    # first colon must be a plain integer.
    colon = chat_id.find(":")
    if colon < 1:
        return None
    stream_part = chat_id[:colon]
    if not stream_part.isdigit():
        return None
    topic = chat_id[colon + 1:] or "(no topic)"
    return (int(stream_part), topic)


def _build_dm_chat_id(sender_email: str) -> str:
    """Encode a DM origin as a stable chat ID.

    Format: ``"dm:{sender_email}"``
    """
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


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------


def check_zulip_requirements() -> bool:
    """Return True if the Zulip adapter can be used."""
    api_key = os.getenv("ZULIP_API_KEY", "")
    email = os.getenv("ZULIP_BOT_EMAIL", "")
    site = os.getenv("ZULIP_SITE_URL", "")

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
        logger.warning(
            "Zulip: zulip package not installed. "
            "Run: pip install zulip"
        )
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

        # Zulip client (created in connect())
        self._client: Any = None

        # Background thread running the event queue.
        self._event_thread: Optional[threading.Thread] = None
        self._closing = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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

        # Graceful shutdown: event that wakes the event-queue thread
        # immediately when disconnect() is called, instead of waiting
        # for the full backoff sleep to elapse.
        self._shutdown_event = threading.Event()
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Required overrides
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to Zulip, verify auth, and start the event queue."""
        import zulip

        if not self._site_url or not self._api_key or not self._bot_email:
            logger.error(
                "Zulip: missing configuration (site_url, api_key, or bot_email)"
            )
            return False

        # Create the synchronous Zulip client.
        self._client = zulip.Client(
            site=self._site_url,
            email=self._bot_email,
            api_key=self._api_key,
        )

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

        profile = result.get("profile", {})
        self._bot_user_id = profile.get("user_id", -1)
        self._bot_full_name = profile.get("full_name", "")
        logger.info(
            "Zulip: authenticated as %s (user_id=%d) on %s",
            self._bot_email,
            self._bot_user_id,
            self._site_url,
        )

        # Populate stream-id cache.
        self._refresh_stream_cache()

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

        self._client = None
        self._loop = None

        # Clear caches to free memory and avoid stale data on reconnect.
        self._seen_events.clear()
        self._stream_id_cache.clear()
        self._stream_name_cache.clear()
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

        formatted = self.format_message(content)
        chunks = self.truncate_message(formatted, MAX_MESSAGE_LENGTH)

        last_id = None
        for chunk in chunks:
            result = self._do_send_message(chat_id, chunk, reply_to)
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
        """Send a typing indicator to Zulip."""
        if not self._client:
            return

        to, op = self._resolve_typing_target(chat_id)
        if not to:
            return

        try:
            self._client.set_typing_status({"to": to, "op": op})
        except Exception:
            pass  # Non-critical — don't spam logs.

    async def edit_message(
        self, chat_id: str, message_id: str, content: str
    ) -> SendResult:
        """Edit an existing message."""
        if not self._client or not message_id:
            return SendResult(success=False, error="Not supported")

        formatted = self.format_message(content)
        try:
            result = self._client.update_message({
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
        LaTeX math, and image links.  Strip image markdown into plain
        URLs so the base class can extract and send them as attachments.
        """
        content = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\2", content)
        return content

    # ------------------------------------------------------------------
    # Internal: sending
    # ------------------------------------------------------------------

    def _do_send_message(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        """Build the correct request dict and call the Zulip API.

        This is synchronous because the Zulip client is not async.
        """
        if not self._client:
            return SendResult(success=False, error="Not connected")

        # Determine message type and recipient from chat_id.
        parsed = _parse_stream_chat_id(chat_id)
        if parsed:
            stream_id, topic = parsed
            request: Dict[str, Any] = {
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

        try:
            result = self._client.send_message(request)
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

    def _resolve_typing_target(
        self, chat_id: str
    ) -> Tuple[Optional[List[str]], str]:
        """Return ``(to, op)`` for a typing notification.

        *op* is ``"start"`` (the caller can ignore stop — Zulip auto-expires).
        """
        parsed = _parse_stream_chat_id(chat_id)
        if parsed:
            stream_id, topic = parsed
            stream_name = self._stream_name_cache.get(stream_id)
            if stream_name:
                # For stream messages, typing goes to the stream (no topic needed).
                return [stream_name], "start"
        dm_email = _parse_dm_chat_id(chat_id)
        if dm_email:
            return [dm_email], "start"
        return None, "start"

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
            self._loop.call_soon_threadsafe(
                self._dispatch_inbound, message, event
            )

    def _dispatch_inbound(self, message: Dict[str, Any], raw_event: Dict[str, Any]) -> None:
        """Process an inbound message on the asyncio event loop.

        Creates a :class:`MessageEvent` and dispatches it via
        ``self.handle_message()``.
        """
        import asyncio

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
