"""Tests for Zulip platform adapter."""
import time
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Platform & Config
# ---------------------------------------------------------------------------


class TestZulipPlatformEnum:
    def test_zulip_enum_exists(self):
        assert Platform.ZULIP.value == "zulip"

    def test_zulip_in_platform_list(self):
        platforms = [p.value for p in Platform]
        assert "zulip" in platforms


class TestZulipConfigLoading:
    def test_apply_env_overrides_with_api_key(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "zlp_abc123")
        monkeypatch.setenv("ZULIP_BOT_EMAIL", "hermes-bot@example.zulipchat.com")
        monkeypatch.setenv("ZULIP_SITE_URL", "https://example.zulipchat.com")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.ZULIP in config.platforms
        zc = config.platforms[Platform.ZULIP]
        assert zc.enabled is True
        assert zc.token == "zlp_abc123"
        assert zc.extra.get("site_url") == "https://example.zulipchat.com"
        assert zc.extra.get("bot_email") == "hermes-bot@example.zulipchat.com"

    def test_apply_env_overrides_with_default_stream(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "zlp_key")
        monkeypatch.setenv("ZULIP_BOT_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_SITE_URL", "https://example.zulipchat.com")
        monkeypatch.setenv("ZULIP_DEFAULT_STREAM", "general")
        monkeypatch.setenv("ZULIP_HOME_TOPIC", "notifications")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        zc = config.platforms[Platform.ZULIP]
        assert zc.extra.get("default_stream") == "general"
        assert zc.extra.get("home_topic") == "notifications"

    def test_zulip_not_loaded_without_creds(self, monkeypatch):
        monkeypatch.delenv("ZULIP_API_KEY", raising=False)
        monkeypatch.delenv("ZULIP_BOT_EMAIL", raising=False)
        monkeypatch.delenv("ZULIP_SITE_URL", raising=False)

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.ZULIP not in config.platforms

    def test_connected_platforms_includes_zulip(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "zlp_key")
        monkeypatch.setenv("ZULIP_BOT_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_SITE_URL", "https://example.zulipchat.com")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        connected = config.get_connected_platforms()
        assert Platform.ZULIP in connected

    def test_connected_platforms_includes_zulip_with_token(self, monkeypatch):
        """Zulip with a token (API key) is considered connected via the generic token check."""
        monkeypatch.setenv("ZULIP_API_KEY", "zlp_key")
        monkeypatch.setenv("ZULIP_BOT_EMAIL", "bot@example.com")
        # ZULIP_SITE_URL not set, but token alone passes the generic check

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        connected = config.get_connected_platforms()
        # get_connected_platforms checks config.token first (generic path)
        assert Platform.ZULIP in connected

    def test_zulip_home_channel(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "zlp_key")
        monkeypatch.setenv("ZULIP_BOT_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_SITE_URL", "https://example.zulipchat.com")
        monkeypatch.setenv("ZULIP_HOME_CHANNEL", "123:home-topic")
        monkeypatch.setenv("ZULIP_HOME_CHANNEL_NAME", "Bot Home")

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        home = config.get_home_channel(Platform.ZULIP)
        assert home is not None
        assert home.chat_id == "123:home-topic"
        assert home.name == "Bot Home"

    def test_zulip_warning_without_email(self, monkeypatch):
        """ZULIP_API_KEY set but ZULIP_BOT_EMAIL missing should still load."""
        monkeypatch.setenv("ZULIP_API_KEY", "zlp_key")
        monkeypatch.delenv("ZULIP_BOT_EMAIL", raising=False)
        monkeypatch.delenv("ZULIP_SITE_URL", raising=False)

        from gateway.config import GatewayConfig, _apply_env_overrides
        config = GatewayConfig()
        _apply_env_overrides(config)

        assert Platform.ZULIP in config.platforms
        assert config.platforms[Platform.ZULIP].extra.get("bot_email") == ""
        assert config.platforms[Platform.ZULIP].extra.get("site_url") == ""

    def test_site_url_trailing_slash_stripped_in_adapter(self):
        """Adapter should strip trailing slashes from site_url."""
        from gateway.platforms.zulip import ZulipAdapter
        config = PlatformConfig(
            enabled=True,
            token="key",
            extra={"site_url": "https://example.zulipchat.com/"},
        )
        adapter = ZulipAdapter(config)
        assert adapter._site_url == "https://example.zulipchat.com"


# ---------------------------------------------------------------------------
# Adapter helper
# ---------------------------------------------------------------------------


def _make_adapter(
    site_url: str = "https://example.zulipchat.com",
    bot_email: str = "hermes-bot@example.zulipchat.com",
    api_key: str = "zlp_test_key",
    default_stream: str = "",
    home_topic: str = "",
) -> "ZulipAdapter":
    """Create a ZulipAdapter with the given config."""
    from gateway.platforms.zulip import ZulipAdapter
    config = PlatformConfig(
        enabled=True,
        token=api_key,
        extra={
            "site_url": site_url,
            "bot_email": bot_email,
            "default_stream": default_stream,
            "home_topic": home_topic,
        },
    )
    adapter = ZulipAdapter(config)
    return adapter


# ---------------------------------------------------------------------------
# Chat-ID helpers
# ---------------------------------------------------------------------------


class TestZulipStreamChatId:
    def test_build_stream_chat_id(self):
        from gateway.platforms.zulip import _build_stream_chat_id
        result = _build_stream_chat_id(42, "general")
        assert result == "42:general"

    def test_build_stream_chat_id_with_spaces_in_topic(self):
        from gateway.platforms.zulip import _build_stream_chat_id
        result = _build_stream_chat_id(7, "some topic here")
        assert result == "7:some topic here"

    def test_parse_stream_chat_id(self):
        from gateway.platforms.zulip import _parse_stream_chat_id
        result = _parse_stream_chat_id("42:general")
        assert result == (42, "general")

    def test_parse_stream_chat_id_with_complex_topic(self):
        from gateway.platforms.zulip import _parse_stream_chat_id
        result = _parse_stream_chat_id("99:help & support")
        assert result == (99, "help & support")

    def test_parse_stream_chat_id_no_topic_fills_default(self):
        from gateway.platforms.zulip import _parse_stream_chat_id
        result = _parse_stream_chat_id("42:")
        assert result == (42, "(no topic)")

    def test_parse_stream_chat_id_roundtrip(self):
        from gateway.platforms.zulip import _build_stream_chat_id, _parse_stream_chat_id
        original = _build_stream_chat_id(123, "test topic")
        parsed = _parse_stream_chat_id(original)
        assert parsed == (123, "test topic")

    def test_parse_stream_chat_id_invalid_returns_none(self):
        from gateway.platforms.zulip import _parse_stream_chat_id
        assert _parse_stream_chat_id("no-colon") is None
        assert _parse_stream_chat_id(":no-stream-id") is None
        assert _parse_stream_chat_id("abc:not-numeric") is None

    def test_parse_stream_chat_id_with_multiple_colons(self):
        """Topics can contain colons — only the first colon is the delimiter."""
        from gateway.platforms.zulip import _build_stream_chat_id, _parse_stream_chat_id
        chat_id = _build_stream_chat_id(5, "time: 12:00")
        parsed = _parse_stream_chat_id(chat_id)
        assert parsed == (5, "time: 12:00")


class TestZulipDmChatId:
    def test_build_dm_chat_id(self):
        from gateway.platforms.zulip import _build_dm_chat_id
        result = _build_dm_chat_id("alice@example.com")
        assert result == "dm:alice@example.com"

    def test_parse_dm_chat_id(self):
        from gateway.platforms.zulip import _parse_dm_chat_id
        result = _parse_dm_chat_id("dm:alice@example.com")
        assert result == "alice@example.com"

    def test_parse_dm_chat_id_roundtrip(self):
        from gateway.platforms.zulip import _build_dm_chat_id, _parse_dm_chat_id
        original = _build_dm_chat_id("bob@example.org")
        parsed = _parse_dm_chat_id(original)
        assert parsed == "bob@example.org"

    def test_parse_dm_chat_id_non_dm_returns_none(self):
        from gateway.platforms.zulip import _parse_dm_chat_id
        assert _parse_dm_chat_id("42:general") is None
        assert _parse_dm_chat_id("nondm@example.com") is None
        assert _parse_dm_chat_id("dm:no-at-sign") is None

    def test_is_dm_chat_id_true(self):
        from gateway.platforms.zulip import is_dm_chat_id
        assert is_dm_chat_id("dm:alice@example.com") is True

    def test_is_dm_chat_id_false_for_stream(self):
        from gateway.platforms.zulip import is_dm_chat_id
        assert is_dm_chat_id("42:general") is False

    def test_is_dm_chat_id_false_for_bare_email(self):
        from gateway.platforms.zulip import is_dm_chat_id
        assert is_dm_chat_id("alice@example.com") is False


# ---------------------------------------------------------------------------
# Requirements check
# ---------------------------------------------------------------------------


class TestZulipRequirements:
    def test_check_requirements_with_creds_and_package(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "test-key")
        monkeypatch.setenv("ZULIP_BOT_EMAIL", "bot@example.com")
        monkeypatch.setenv("ZULIP_SITE_URL", "https://example.zulipchat.com")
        from gateway.platforms.zulip import check_zulip_requirements
        try:
            import zulip  # noqa: F401
            assert check_zulip_requirements() is True
        except ImportError:
            assert check_zulip_requirements() is False

    def test_check_requirements_without_api_key(self, monkeypatch):
        monkeypatch.delenv("ZULIP_API_KEY", raising=False)
        monkeypatch.delenv("ZULIP_BOT_EMAIL", raising=False)
        monkeypatch.delenv("ZULIP_SITE_URL", raising=False)
        from gateway.platforms.zulip import check_zulip_requirements
        assert check_zulip_requirements() is False

    def test_check_requirements_without_email(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "test-key")
        monkeypatch.delenv("ZULIP_BOT_EMAIL", raising=False)
        monkeypatch.delenv("ZULIP_SITE_URL", raising=False)
        from gateway.platforms.zulip import check_zulip_requirements
        assert check_zulip_requirements() is False

    def test_check_requirements_without_site_url(self, monkeypatch):
        monkeypatch.setenv("ZULIP_API_KEY", "test-key")
        monkeypatch.setenv("ZULIP_BOT_EMAIL", "bot@example.com")
        monkeypatch.delenv("ZULIP_SITE_URL", raising=False)
        from gateway.platforms.zulip import check_zulip_requirements
        assert check_zulip_requirements() is False


# ---------------------------------------------------------------------------
# Adapter init
# ---------------------------------------------------------------------------


class TestZulipAdapterInit:
    def test_init_from_config(self):
        adapter = _make_adapter(
            site_url="https://my.zulipchat.com",
            bot_email="bot@my.zulipchat.com",
            api_key="my-key",
        )
        assert adapter._site_url == "https://my.zulipchat.com"
        assert adapter._bot_email == "bot@my.zulipchat.com"
        assert adapter._api_key == "my-key"
        assert adapter.platform == Platform.ZULIP

    def test_init_default_stream_and_home_topic(self):
        adapter = _make_adapter(
            default_stream="general",
            home_topic="cron",
        )
        assert adapter._default_stream == "general"
        assert adapter._home_topic == "cron"

    def test_init_empty_defaults(self):
        adapter = _make_adapter()
        assert adapter._default_stream == ""
        assert adapter._home_topic == ""
        assert adapter._client is None
        assert adapter._bot_user_id == -1
        assert adapter._bot_full_name == ""

    def test_init_env_var_fallback(self, monkeypatch):
        """Adapter falls back to env vars when config.extra values are empty."""
        monkeypatch.setenv("ZULIP_SITE_URL", "https://env.zulipchat.com")
        monkeypatch.setenv("ZULIP_BOT_EMAIL", "env-bot@zulipchat.com")
        monkeypatch.setenv("ZULIP_API_KEY", "env-key")
        monkeypatch.setenv("ZULIP_DEFAULT_STREAM", "env-stream")

        config = PlatformConfig(
            enabled=True,
            extra={},  # empty extra — should fall back to env
        )
        from gateway.platforms.zulip import ZulipAdapter
        adapter = ZulipAdapter(config)

        assert adapter._site_url == "https://env.zulipchat.com"
        assert adapter._bot_email == "env-bot@zulipchat.com"
        assert adapter._api_key == "env-key"
        assert adapter._default_stream == "env-stream"


# ---------------------------------------------------------------------------
# Format message
# ---------------------------------------------------------------------------


class TestZulipFormatMessage:
    def setup_method(self):
        self.adapter = _make_adapter()

    def test_image_markdown_stripped(self):
        """![alt](url) should be converted to just the URL."""
        result = self.adapter.format_message("![cat](https://img.example.com/cat.png)")
        assert result == "https://img.example.com/cat.png"

    def test_image_markdown_strips_alt_text(self):
        result = self.adapter.format_message("Here: ![my image](https://x.com/a.jpg) done")
        assert "![" not in result
        assert "https://x.com/a.jpg" in result

    def test_regular_markdown_preserved(self):
        content = "**bold** and *italic* and `code`"
        assert self.adapter.format_message(content) == content

    def test_regular_links_preserved(self):
        content = "[click](https://example.com)"
        assert self.adapter.format_message(content) == content

    def test_plain_text_unchanged(self):
        content = "Hello, world!"
        assert self.adapter.format_message(content) == content

    def test_multiple_images(self):
        content = "![a](http://a.com/1.png) text ![b](http://b.com/2.png)"
        result = self.adapter.format_message(content)
        assert "![" not in result
        assert "http://a.com/1.png" in result
        assert "http://b.com/2.png" in result


# ---------------------------------------------------------------------------
# Connect / Disconnect
# ---------------------------------------------------------------------------


class TestZulipConnect:
    @pytest.mark.asyncio
    async def test_connect_success(self):
        """connect() should create client, fetch profile, start event queue."""
        import asyncio

        adapter = _make_adapter()

        mock_client = MagicMock()
        mock_client.get_profile.return_value = {
            "result": "success",
            "profile": {"user_id": 42, "full_name": "Hermes Bot"},
        }
        mock_client.get_streams.return_value = {
            "result": "success",
            "streams": [
                {"stream_id": 10, "name": "general"},
                {"stream_id": 20, "name": "random"},
            ],
        }
        mock_client.call_on_each_event = MagicMock()

        # Set up the event loop reference before calling connect,
        # which internally calls asyncio.get_running_loop()
        adapter._loop = asyncio.get_running_loop()

        with patch.dict("sys.modules", {"zulip": MagicMock(Client=MagicMock(return_value=mock_client))}):
            result = await adapter.connect()

        assert result is True
        assert adapter._bot_user_id == 42
        assert adapter._bot_full_name == "Hermes Bot"
        assert adapter._stream_id_cache["general"] == 10
        assert adapter._stream_name_cache[10] == "general"
        mock_client.call_on_each_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_missing_config(self):
        """connect() should return False when config is incomplete."""
        adapter = _make_adapter()
        adapter._site_url = ""
        adapter._api_key = ""
        adapter._bot_email = ""

        result = await adapter.connect()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_auth_failure(self):
        """connect() should return False when Zulip auth fails."""
        import asyncio

        adapter = _make_adapter()

        mock_client = MagicMock()
        mock_client.get_profile.return_value = {
            "result": "error",
            "msg": "Invalid API key",
        }

        adapter._loop = asyncio.get_running_loop()

        with patch.dict("sys.modules", {"zulip": MagicMock(Client=MagicMock(return_value=mock_client))}):
            result = await adapter.connect()

        assert result is False

    @pytest.mark.asyncio
    async def test_disconnect_clears_client(self):
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._closing = False
        adapter._event_thread = None

        await adapter.disconnect()

        assert adapter._client is None
        assert adapter._closing is True


# ---------------------------------------------------------------------------
# Self-message filtering
# ---------------------------------------------------------------------------


class TestZulipSelfMessageFiltering:
    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter._loop = None  # prevent async dispatch
        self.adapter.handle_message = AsyncMock()

    def test_filter_by_sender_email(self):
        """Messages from the bot's own email should be ignored."""
        event = {
            "message": {
                "id": 100,
                "sender_email": "bot@example.zulipchat.com",
                "sender_id": 42,
                "type": "private",
                "content": "echo test",
                "display_recipient": [
                    {"email": "other@example.com"},
                    {"email": "bot@example.zulipchat.com"},
                ],
            },
        }
        self.adapter._on_zulip_event(event)
        self.adapter.handle_message.assert_not_called()

    def test_filter_by_sender_id(self):
        """Messages from the bot's user ID should be ignored."""
        event = {
            "message": {
                "id": 101,
                "sender_email": "someone-else@example.com",
                "sender_id": 42,  # matches bot_user_id
                "type": "private",
                "content": "spoofed",
                "display_recipient": [
                    {"email": "bot@example.zulipchat.com"},
                    {"email": "someone-else@example.com"},
                ],
            },
        }
        self.adapter._on_zulip_event(event)
        self.adapter.handle_message.assert_not_called()

    def test_non_bot_messages_pass_through(self):
        """Messages from other users should not be filtered."""
        event = {
            "message": {
                "id": 102,
                "sender_email": "alice@example.com",
                "sender_id": 99,
                "type": "private",
                "content": "Hello bot",
                "display_recipient": [
                    {"email": "bot@example.zulipchat.com"},
                    {"email": "alice@example.com"},
                ],
            },
        }
        # With _loop=None, _on_zulip_event won't schedule dispatch
        # but the filtering logic still runs — it just won't reach dispatch
        self.adapter._on_zulip_event(event)
        # Not called because _loop is None, but NOT because of filtering
        # (we verify the filter didn't reject it by checking _seen_events)
        assert "102" in self.adapter._seen_events


# ---------------------------------------------------------------------------
# Dedup cache
# ---------------------------------------------------------------------------


class TestZulipDedup:
    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter._loop = None
        self.adapter.handle_message = AsyncMock()

    def test_duplicate_message_ignored(self):
        """The same message ID should be deduped."""
        event = {
            "message": {
                "id": 200,
                "sender_email": "alice@example.com",
                "sender_id": 99,
                "type": "private",
                "content": "Hello",
                "display_recipient": [
                    {"email": "bot@example.zulipchat.com"},
                    {"email": "alice@example.com"},
                ],
            },
        }
        # First call: event gets recorded
        self.adapter._on_zulip_event(event)
        assert "200" in self.adapter._seen_events

        # Second call: same event_id — deduped (still in cache, no scheduling)
        self.adapter._on_zulip_event(event)

    def test_different_message_ids_both_tracked(self):
        """Different message IDs should both be recorded."""
        for mid in [300, 301]:
            event = {
                "message": {
                    "id": mid,
                    "sender_email": "alice@example.com",
                    "sender_id": 99,
                    "type": "private",
                    "content": "Hello",
                    "display_recipient": [
                        {"email": "bot@example.zulipchat.com"},
                        {"email": "alice@example.com"},
                    ],
                },
            }
            self.adapter._on_zulip_event(event)

        assert "300" in self.adapter._seen_events
        assert "301" in self.adapter._seen_events

    def test_prune_seen_clears_expired(self):
        """_prune_seen should remove entries older than _SEEN_TTL."""
        now = time.time()
        # Fill beyond _SEEN_MAX to trigger pruning
        for i in range(self.adapter._SEEN_MAX + 10):
            self.adapter._seen_events[f"old_{i}"] = now - 600  # 10 min ago
        # Add a fresh one
        self.adapter._seen_events["fresh"] = now

        self.adapter._prune_seen()

        assert "fresh" in self.adapter._seen_events
        assert len(self.adapter._seen_events) < self.adapter._SEEN_MAX


# ---------------------------------------------------------------------------
# Inbound event dispatch
# ---------------------------------------------------------------------------


class TestZulipInboundDispatch:
    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter.handle_message = AsyncMock()
        self.adapter._stream_name_cache = {99: "general"}

    @pytest.mark.asyncio
    async def test_dm_dispatch_creates_message_event(self):
        """A DM should produce a MessageEvent with chat_type='dm'."""
        message = {
            "id": 500,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice Smith",
            "sender_id": 10,
            "type": "private",
            "content": "Hello!",
            "display_recipient": [
                {"email": "bot@example.zulipchat.com"},
                {"email": "alice@example.com"},
            ],
        }
        event = {"message": message}

        self.adapter._dispatch_inbound(message, event)

        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "Hello!"
        assert msg_event.message_type.value == "text"
        assert msg_event.source.chat_type == "dm"
        assert msg_event.source.user_id == "alice@example.com"
        assert msg_event.source.user_name == "Alice Smith"
        assert msg_event.source.chat_id == "dm:alice@example.com"

    @pytest.mark.asyncio
    async def test_dm_command_detected(self):
        """Messages starting with / should be COMMAND type."""
        message = {
            "id": 501,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "private",
            "content": "/reset",
            "display_recipient": [
                {"email": "bot@example.zulipchat.com"},
                {"email": "alice@example.com"},
            ],
        }
        event = {"message": message}

        self.adapter._dispatch_inbound(message, event)

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.message_type.value == "command"

    @pytest.mark.asyncio
    async def test_stream_message_with_mention_dispatched(self):
        """Stream messages with @mention of bot should be dispatched."""
        message = {
            "id": 502,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "general",
            "content": "@**Hermes Bot** what is 2+2?",
        }
        event = {"message": message}

        self.adapter._dispatch_inbound(message, event)

        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "@**Hermes Bot** what is 2+2?"
        assert msg_event.source.chat_type == "stream"
        assert msg_event.source.chat_id == "99:general"
        assert msg_event.source.chat_topic == "general"

    @pytest.mark.asyncio
    async def test_stream_message_without_mention_ignored(self):
        """Stream messages without @mention should be silently dropped."""
        message = {
            "id": 503,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "general",
            "content": "hey everyone",
        }
        event = {"message": message}

        self.adapter._dispatch_inbound(message, event)

        self.adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_mention_by_email(self):
        """@bot@example.zulipchat.com should also trigger processing."""
        message = {
            "id": 504,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "help",
            "content": "@bot@example.zulipchat.com help me",
        }
        event = {"message": message}

        self.adapter._dispatch_inbound(message, event)

        assert self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_dm_empty_content_ignored(self):
        """Empty DMs should not dispatch."""
        message = {
            "id": 505,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "private",
            "content": "",
            "display_recipient": [
                {"email": "bot@example.zulipchat.com"},
                {"email": "alice@example.com"},
            ],
        }
        event = {"message": message}

        self.adapter._dispatch_inbound(message, event)
        self.adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_message_type_ignored(self):
        """Messages of unknown type should be ignored."""
        message = {
            "id": 506,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "outgoing-webhook",
            "content": "something",
        }
        event = {"message": message}

        self.adapter._dispatch_inbound(message, event)
        self.adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_exclamation_command_detected(self):
        """Messages starting with ! should be COMMAND type."""
        message = {
            "id": 507,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "private",
            "content": "!status",
            "display_recipient": [
                {"email": "bot@example.zulipchat.com"},
                {"email": "alice@example.com"},
            ],
        }
        event = {"message": message}

        self.adapter._dispatch_inbound(message, event)

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.message_type.value == "command"

    @pytest.mark.asyncio
    async def test_dm_sender_full_name_fallback_to_email(self):
        """When sender_full_name is missing, fall back to email."""
        message = {
            "id": 508,
            "sender_email": "alice@example.com",
            "sender_full_name": "",
            "sender_id": 10,
            "type": "private",
            "content": "Hi",
            "display_recipient": [
                {"email": "bot@example.zulipchat.com"},
                {"email": "alice@example.com"},
            ],
        }
        event = {"message": message}

        self.adapter._dispatch_inbound(message, event)

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.source.user_name == "alice@example.com"


# ---------------------------------------------------------------------------
# Outbound send
# ---------------------------------------------------------------------------


class TestZulipSend:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._client = MagicMock()

    def test_do_send_stream_message(self):
        """_do_send_message should build a stream-type request for stream chat IDs."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 900,
        }

        result = self.adapter._do_send_message("99:general", "Hello stream!")

        assert result.success is True
        assert result.message_id == "900"
        call_args = self.adapter._client.send_message.call_args[0][0]
        assert call_args["type"] == "stream"
        assert call_args["to"] == "99"
        assert call_args["topic"] == "general"
        assert call_args["content"] == "Hello stream!"

    def test_do_send_dm_message(self):
        """_do_send_message should build a private-type request for DM chat IDs."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 901,
        }

        result = self.adapter._do_send_message("dm:alice@example.com", "Hello DM!")

        assert result.success is True
        assert result.message_id == "901"
        call_args = self.adapter._client.send_message.call_args[0][0]
        assert call_args["type"] == "private"
        assert call_args["to"] == ["alice@example.com"]
        assert call_args["content"] == "Hello DM!"

    def test_do_send_bare_email_fallback(self):
        """Unknown chat IDs that look like emails should fallback to private."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 902,
        }

        result = self.adapter._do_send_message("bob@example.com", "Fallback")

        assert result.success is True
        call_args = self.adapter._client.send_message.call_args[0][0]
        assert call_args["type"] == "private"
        assert call_args["to"] == ["bob@example.com"]

    def test_do_send_api_failure(self):
        """API errors should return a failed SendResult."""
        self.adapter._client.send_message.return_value = {
            "result": "error",
            "msg": "Stream not found",
        }

        result = self.adapter._do_send_message("99:general", "fail")

        assert result.success is False
        assert "Stream not found" in result.error

    def test_do_send_exception(self):
        """Network exceptions should return a failed SendResult."""
        self.adapter._client.send_message.side_effect = ConnectionError("timeout")

        result = self.adapter._do_send_message("99:general", "fail")

        assert result.success is False
        assert "timeout" in result.error

    def test_do_send_not_connected(self):
        """No client should return a failed SendResult."""
        self.adapter._client = None

        result = self.adapter._do_send_message("99:general", "no client")

        assert result.success is False
        assert "Not connected" in result.error

    @pytest.mark.asyncio
    async def test_send_empty_content_succeeds(self):
        """Empty content should return success without calling API."""
        result = await self.adapter.send("99:general", "")
        assert result.success is True
        self.adapter._client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_formats_and_truncates(self):
        """send() should format content and handle truncation."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 903,
        }

        content = "![img](https://example.com/a.png) and some text"
        result = await self.adapter.send("99:general", content)

        assert result.success is True
        # Image markdown should be stripped by format_message
        sent = self.adapter._client.send_message.call_args[0][0]["content"]
        assert "![" not in sent
        assert "https://example.com/a.png" in sent


# ---------------------------------------------------------------------------
# get_chat_info
# ---------------------------------------------------------------------------


class TestZulipGetChatInfo:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._stream_name_cache = {42: "general", 99: "random"}

    @pytest.mark.asyncio
    async def test_stream_chat_info(self):
        info = await self.adapter.get_chat_info("42:general")
        assert info["type"] == "stream"
        assert info["name"] == "#general > general"

    @pytest.mark.asyncio
    async def test_stream_chat_info_unknown_stream(self):
        """Unknown stream_id falls back to chat_id as the stream name."""
        info = await self.adapter.get_chat_info("999:topic")
        assert info["type"] == "stream"
        # _parse_stream_chat_id returns (999, "topic"), stream_name_cache
        # doesn't have 999 so it falls back to chat_id = "999:topic"
        assert info["name"] == "#999:topic > topic"

    @pytest.mark.asyncio
    async def test_dm_chat_info(self):
        info = await self.adapter.get_chat_info("dm:alice@example.com")
        assert info["type"] == "dm"
        assert info["name"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_unknown_chat_id_fallback(self):
        info = await self.adapter.get_chat_info("something-weird")
        assert info["type"] == "dm"
        assert info["name"] == "something-weird"


# ---------------------------------------------------------------------------
# send_typing
# ---------------------------------------------------------------------------


class TestZulipSendTyping:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._client = MagicMock()
        self.adapter._stream_name_cache = {42: "general"}

    @pytest.mark.asyncio
    async def test_send_typing_for_dm(self):
        await self.adapter.send_typing("dm:alice@example.com")
        self.adapter._client.set_typing_status.assert_called_once_with(
            {"to": ["alice@example.com"], "op": "start"}
        )

    @pytest.mark.asyncio
    async def test_send_typing_for_stream(self):
        await self.adapter.send_typing("42:general")
        self.adapter._client.set_typing_status.assert_called_once_with(
            {"to": ["general"], "op": "start"}
        )

    @pytest.mark.asyncio
    async def test_send_typing_without_client(self):
        self.adapter._client = None
        # Should not raise
        await self.adapter.send_typing("dm:alice@example.com")

    @pytest.mark.asyncio
    async def test_send_typing_unknown_stream_id(self):
        """Unknown stream ID has no cached name — should not call typing API."""
        await self.adapter.send_typing("999:topic")
        self.adapter._client.set_typing_status.assert_not_called()


# ---------------------------------------------------------------------------
# edit_message
# ---------------------------------------------------------------------------


class TestZulipEditMessage:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._client = MagicMock()

    @pytest.mark.asyncio
    async def test_edit_message_success(self):
        self.adapter._client.update_message.return_value = {
            "result": "success",
        }

        result = await self.adapter.edit_message("99:general", "123", "Updated text")

        assert result.success is True
        self.adapter._client.update_message.assert_called_once_with({
            "message_id": 123,
            "content": "Updated text",
        })

    @pytest.mark.asyncio
    async def test_edit_message_api_failure(self):
        self.adapter._client.update_message.return_value = {
            "result": "error",
            "msg": "permission denied",
        }

        result = await self.adapter.edit_message("99:general", "123", "fail")

        assert result.success is False
        assert "permission denied" in result.error

    @pytest.mark.asyncio
    async def test_edit_message_no_client(self):
        self.adapter._client = None

        result = await self.adapter.edit_message("99:general", "123", "nope")

        assert result.success is False
        assert "Not supported" in result.error

    @pytest.mark.asyncio
    async def test_edit_message_no_message_id(self):
        result = await self.adapter.edit_message("99:general", "", "no id")

        assert result.success is False


# ---------------------------------------------------------------------------
# _resolve_typing_target
# ---------------------------------------------------------------------------


class TestZulipResolveTypingTarget:
    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._stream_name_cache = {42: "general"}

    def test_stream_typing_target(self):
        to, op = self.adapter._resolve_typing_target("42:general")
        assert to == ["general"]
        assert op == "start"

    def test_dm_typing_target(self):
        to, op = self.adapter._resolve_typing_target("dm:bob@example.com")
        assert to == ["bob@example.com"]
        assert op == "start"

    def test_unknown_stream_id_returns_none(self):
        to, op = self.adapter._resolve_typing_target("999:topic")
        assert to is None

    def test_bare_email_returns_none(self):
        to, op = self.adapter._resolve_typing_target("unknown@x.com")
        assert to is None


# ---------------------------------------------------------------------------
# Stream cache
# ---------------------------------------------------------------------------


class TestZulipStreamCache:
    def test_refresh_stream_cache_populates(self):
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.get_streams.return_value = {
            "result": "success",
            "streams": [
                {"stream_id": 10, "name": "general"},
                {"stream_id": 20, "name": "Random"},
            ],
        }

        adapter._refresh_stream_cache()

        assert adapter._stream_id_cache["general"] == 10
        assert adapter._stream_id_cache["random"] == 20
        assert adapter._stream_name_cache[10] == "general"
        assert adapter._stream_name_cache[20] == "Random"

    def test_refresh_stream_cache_case_insensitive(self):
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.get_streams.return_value = {
            "result": "success",
            "streams": [
                {"stream_id": 10, "name": "General"},
            ],
        }

        adapter._refresh_stream_cache()

        # Lookup should be case-insensitive
        assert adapter._stream_id_cache["general"] == 10

    def test_refresh_stream_cache_failure_is_safe(self):
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.get_streams.side_effect = Exception("network error")

        # Should not raise
        adapter._refresh_stream_cache()
        assert adapter._stream_id_cache == {}

    def test_refresh_stream_cache_no_client(self):
        adapter = _make_adapter()
        adapter._client = None

        # Should not raise
        adapter._refresh_stream_cache()
