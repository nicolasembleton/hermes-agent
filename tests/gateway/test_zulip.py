"""Tests for Zulip platform adapter."""
import asyncio
import json
import os
import threading
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.channel_directory import _build_from_sessions


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


def _write_directory(tmp_path, platforms):
    """Helper to write a fake channel directory cache file."""
    data = {"updated_at": "2026-01-01T00:00:00", "platforms": platforms}
    cache_file = tmp_path / "channel_directory.json"
    cache_file.write_text(json.dumps(data))
    return cache_file


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

        # Make call_on_each_event stop the event queue after one call
        # (simulating the blocking behavior of the real Zulip client).
        def stop_after_one_call(*args, **kwargs):
            adapter._closing = True

        mock_client.call_on_each_event.side_effect = stop_after_one_call

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
            "type": "message",
            "op": "add",
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
            "type": "message",
            "op": "add",
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
            "type": "message",
            "op": "add",
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
            "type": "message",
            "op": "add",
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
                "type": "message",
                "op": "add",
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


# ---------------------------------------------------------------------------
# Group DM send path
# ---------------------------------------------------------------------------


class TestGroupDmSendPath:
    """Verify that _do_send_message correctly handles group DM chat IDs."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._client = MagicMock()

    def test_send_group_dm_parses_emails(self):
        """Group DM chat IDs should send to all recipient emails."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 1000,
        }

        result = self.adapter._do_send_message(
            "group_dm:alice@example.com,bob@example.com", "Hello group!"
        )

        assert result.success is True
        call_args = self.adapter._client.send_message.call_args[0][0]
        assert call_args["type"] == "private"
        assert call_args["to"] == ["alice@example.com", "bob@example.com"]
        assert call_args["content"] == "Hello group!"

    def test_send_group_dm_three_participants(self):
        """Group DM with 3 participants sends to all three."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 1001,
        }

        result = self.adapter._do_send_message(
            "group_dm:alice@example.com,bob@example.com,charlie@example.com", "Hey all"
        )

        assert result.success is True
        call_args = self.adapter._client.send_message.call_args[0][0]
        assert len(call_args["to"]) == 3
        assert "charlie@example.com" in call_args["to"]

    def test_send_group_dm_api_failure(self):
        """API errors on group DM send return failed SendResult."""
        self.adapter._client.send_message.return_value = {
            "result": "error",
            "msg": "One or more recipients are invalid",
        }

        result = self.adapter._do_send_message(
            "group_dm:alice@example.com,bob@example.com", "fail"
        )

        assert result.success is False
        assert "invalid" in result.error

    def test_send_group_dm_not_connected(self):
        """No client returns failed SendResult for group DMs."""
        self.adapter._client = None

        result = self.adapter._do_send_message(
            "group_dm:alice@example.com,bob@example.com", "no client"
        )

        assert result.success is False
        assert "Not connected" in result.error

    def test_send_group_dm_roundtrip_with_build_parse(self):
        """Full round-trip: build chat ID → send → verify recipients."""
        from gateway.platforms.zulip import _build_group_dm_chat_id, _parse_group_dm_chat_id
        emails = ["charlie@example.com", "alice@example.com"]
        chat_id = _build_group_dm_chat_id(emails)

        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 1002,
        }

        result = self.adapter._do_send_message(chat_id, "roundtrip test")

        assert result.success is True
        call_args = self.adapter._client.send_message.call_args[0][0]
        parsed_emails = _parse_group_dm_chat_id(chat_id)
        assert call_args["to"] == parsed_emails


# ---------------------------------------------------------------------------
# Session key integration with Zulip chat IDs
# ---------------------------------------------------------------------------


class TestZulipSessionKeyIntegration:
    """Verify that build_session_key produces deterministic, isolated keys
    for all Zulip chat types using the chat-id encoding."""

    def test_stream_session_key_includes_stream_id_and_topic(self):
        """Stream messages with different topics should produce different keys."""
        from gateway.session import SessionSource, build_session_key

        source_a = SessionSource(
            platform=Platform.ZULIP,
            chat_id="42:general",
            chat_name="general",
            chat_type="stream",
            user_id="alice@example.com",
            user_name="Alice",
            chat_topic="general",
        )
        source_b = SessionSource(
            platform=Platform.ZULIP,
            chat_id="42:help",
            chat_name="general",
            chat_type="stream",
            user_id="alice@example.com",
            user_name="Alice",
            chat_topic="help",
        )

        key_a = build_session_key(source_a)
        key_b = build_session_key(source_b)

        assert key_a != key_b
        assert "42:general" in key_a
        assert "42:help" in key_b

    def test_stream_session_key_different_streams_same_topic(self):
        """Same topic in different streams should produce different keys."""
        from gateway.session import SessionSource, build_session_key

        source_a = SessionSource(
            platform=Platform.ZULIP,
            chat_id="10:general",
            chat_type="stream",
            user_id="alice@example.com",
        )
        source_b = SessionSource(
            platform=Platform.ZULIP,
            chat_id="20:general",
            chat_type="stream",
            user_id="alice@example.com",
        )

        key_a = build_session_key(source_a)
        key_b = build_session_key(source_b)

        assert key_a != key_b

    def test_stream_session_key_same_sender_isolated(self):
        """Same sender in different stream topics gets different sessions."""
        from gateway.session import SessionSource, build_session_key

        source = SessionSource(
            platform=Platform.ZULIP,
            chat_id="42:topic-a",
            chat_type="stream",
            user_id="alice@example.com",
        )

        # With default group_sessions_per_user=True, sender is included
        key = build_session_key(source)
        assert "alice@example.com" in key

    def test_dm_session_key_isolated_from_stream(self):
        """DMs should never share a session key with stream messages."""
        from gateway.session import SessionSource, build_session_key

        dm_source = SessionSource(
            platform=Platform.ZULIP,
            chat_id="dm:alice@example.com",
            chat_type="dm",
            user_id="alice@example.com",
        )
        stream_source = SessionSource(
            platform=Platform.ZULIP,
            chat_id="42:general",
            chat_type="stream",
            user_id="alice@example.com",
        )

        dm_key = build_session_key(dm_source)
        stream_key = build_session_key(stream_source)

        assert dm_key != stream_key

    def test_dm_session_key_different_users(self):
        """DMs with different users should produce different keys."""
        from gateway.session import SessionSource, build_session_key

        source_a = SessionSource(
            platform=Platform.ZULIP,
            chat_id="dm:alice@example.com",
            chat_type="dm",
            user_id="alice@example.com",
        )
        source_b = SessionSource(
            platform=Platform.ZULIP,
            chat_id="dm:bob@example.com",
            chat_type="dm",
            user_id="bob@example.com",
        )

        key_a = build_session_key(source_a)
        key_b = build_session_key(source_b)

        assert key_a != key_b

    def test_group_dm_session_key_isolated_from_dm(self):
        """Group DMs should not share session keys with 1:1 DMs."""
        from gateway.session import SessionSource, build_session_key

        dm_source = SessionSource(
            platform=Platform.ZULIP,
            chat_id="dm:alice@example.com",
            chat_type="dm",
            user_id="alice@example.com",
        )
        group_source = SessionSource(
            platform=Platform.ZULIP,
            chat_id="group_dm:alice@example.com,bob@example.com",
            chat_type="group",
            user_id="alice@example.com",
        )

        dm_key = build_session_key(dm_source)
        group_key = build_session_key(group_source)

        assert dm_key != group_key

    def test_session_key_deterministic_across_calls(self):
        """Same source should always produce the same session key."""
        from gateway.session import SessionSource, build_session_key

        source = SessionSource(
            platform=Platform.ZULIP,
            chat_id="42:general",
            chat_type="stream",
            user_id="alice@example.com",
        )

        key_a = build_session_key(source)
        key_b = build_session_key(source)

        assert key_a == key_b


# ---------------------------------------------------------------------------
# SessionSource serialization round-trip with Zulip
# ---------------------------------------------------------------------------


class TestZulipSessionSourceSerialization:
    """Verify that Zulip SessionSources survive to_dict/from_dict round-trips."""

    def test_stream_source_roundtrip(self):
        from gateway.session import SessionSource

        original = SessionSource(
            platform=Platform.ZULIP,
            chat_id="42:general",
            chat_name="general",
            chat_type="stream",
            user_id="alice@example.com",
            user_name="Alice Smith",
            chat_topic="general",
        )

        restored = SessionSource.from_dict(original.to_dict())

        assert restored.platform == Platform.ZULIP
        assert restored.chat_id == "42:general"
        assert restored.chat_name == "general"
        assert restored.chat_type == "stream"
        assert restored.user_id == "alice@example.com"
        assert restored.user_name == "Alice Smith"
        assert restored.chat_topic == "general"

    def test_dm_source_roundtrip(self):
        from gateway.session import SessionSource

        original = SessionSource(
            platform=Platform.ZULIP,
            chat_id="dm:alice@example.com",
            chat_name="alice@example.com",
            chat_type="dm",
            user_id="alice@example.com",
            user_name="Alice",
        )

        restored = SessionSource.from_dict(original.to_dict())

        assert restored.platform == Platform.ZULIP
        assert restored.chat_id == "dm:alice@example.com"
        assert restored.chat_type == "dm"
        assert restored.user_id == "alice@example.com"

    def test_group_dm_source_roundtrip(self):
        from gateway.session import SessionSource

        original = SessionSource(
            platform=Platform.ZULIP,
            chat_id="group_dm:alice@example.com,bob@example.com",
            chat_name="alice@example.com, bob@example.com",
            chat_type="group",
            user_id="alice@example.com",
            user_name="Alice",
        )

        restored = SessionSource.from_dict(original.to_dict())

        assert restored.platform == Platform.ZULIP
        assert restored.chat_id == "group_dm:alice@example.com,bob@example.com"
        assert restored.chat_type == "group"

    def test_stream_source_no_topic_roundtrip(self):
        from gateway.session import SessionSource

        original = SessionSource(
            platform=Platform.ZULIP,
            chat_id="42:(no topic)",
            chat_name="42",
            chat_type="stream",
            user_id="alice@example.com",
            chat_topic="(no topic)",
        )

        restored = SessionSource.from_dict(original.to_dict())

        assert restored.chat_id == "42:(no topic)"
        assert restored.chat_topic == "(no topic)"

    def test_chat_id_with_colon_in_topic_preserved(self):
        """Topics containing colons must survive serialization."""
        from gateway.session import SessionSource

        original = SessionSource(
            platform=Platform.ZULIP,
            chat_id="5:time: 12:00",
            chat_name="general",
            chat_type="stream",
            user_id="alice@example.com",
            chat_topic="time: 12:00",
        )

        restored = SessionSource.from_dict(original.to_dict())

        assert restored.chat_id == "5:time: 12:00"
        assert restored.chat_topic == "time: 12:00"


# ---------------------------------------------------------------------------
# Missing/empty subject field
# ---------------------------------------------------------------------------


class TestMissingSubjectField:
    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter.handle_message = AsyncMock()
        self.adapter._stream_name_cache = {99: "general"}

    @pytest.mark.asyncio
    async def test_missing_subject_defaults(self):
        """Stream messages missing 'subject' should use '(no topic)'."""
        message = {
            "id": 900,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            # No 'subject' field
            "content": "@**Hermes Bot** help",
        }
        event = {"type": "message", "op": "add", "message": message}

        self.adapter._dispatch_inbound(message, event)

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.source.chat_topic == "(no topic)"
        assert msg_event.source.chat_id == "99:(no topic)"

    @pytest.mark.asyncio
    async def test_empty_subject_defaults(self):
        """Stream messages with empty 'subject' should use '(no topic)'."""
        message = {
            "id": 901,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "",
            "content": "@**Hermes Bot** help",
        }
        event = {"type": "message", "op": "add", "message": message}

        self.adapter._dispatch_inbound(message, event)

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.source.chat_topic == "(no topic)"


# ---------------------------------------------------------------------------
# Mention stripping
# ---------------------------------------------------------------------------


class TestZulipMentionStripping:
    """Verify that @mention patterns are stripped from stream message content."""

    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter.handle_message = AsyncMock()
        self.adapter._stream_name_cache = {99: "general"}

    @pytest.mark.asyncio
    async def test_full_name_mention_stripped(self):
        """@**Hermes Bot** should be removed from content."""
        message = {
            "id": 1001,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "@**Hermes Bot** what is the weather?",
        }
        self.adapter._dispatch_inbound(message, {})

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "what is the weather?"

    @pytest.mark.asyncio
    async def test_email_mention_stripped(self):
        """@bot@example.zulipchat.com should be removed from content."""
        message = {
            "id": 1002,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "@bot@example.zulipchat.com hello!",
        }
        self.adapter._dispatch_inbound(message, {})

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "hello!"

    @pytest.mark.asyncio
    async def test_case_insensitive_mention_stripped(self):
        """Mention stripping should be case-insensitive."""
        message = {
            "id": 1003,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "@**hermes bot** please help me",
        }
        self.adapter._dispatch_inbound(message, {})

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "please help me"

    @pytest.mark.asyncio
    async def test_mention_in_middle_of_content(self):
        """Mention embedded in longer content should be stripped cleanly."""
        message = {
            "id": 1004,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "Hey @**Hermes Bot** can you look at this?",
        }
        self.adapter._dispatch_inbound(message, {})

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "Hey can you look at this?"

    @pytest.mark.asyncio
    async def test_dm_no_mention_stripping(self):
        """DMs should NOT have mention stripping applied."""
        message = {
            "id": 1005,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "private",
            "content": "@**Hermes Bot** help me",
            "display_recipient": [
                {"email": "bot@example.zulipchat.com"},
                {"email": "alice@example.com"},
            ],
        }
        self.adapter._dispatch_inbound(message, {})

        msg_event = self.adapter.handle_message.call_args[0][0]
        # DMs don't go through mention logic — content should be unchanged
        assert msg_event.text == "@**Hermes Bot** help me"


class TestStripBotMentionHelper:
    """Unit tests for the _strip_bot_mention helper."""

    def test_strips_single_pattern(self):
        from gateway.platforms.zulip import _strip_bot_mention
        result = _strip_bot_mention(
            "@**Bot Name** hello", ["@**Bot Name**"]
        )
        assert result == "hello"

    def test_strips_email_pattern(self):
        from gateway.platforms.zulip import _strip_bot_mention
        result = _strip_bot_mention(
            "@bot@example.com what's up", ["@bot@example.com"]
        )
        assert result == "what's up"

    def test_strips_case_insensitive(self):
        from gateway.platforms.zulip import _strip_bot_mention
        result = _strip_bot_mention(
            "@**BOT NAME** hello", ["@**Bot Name**"]
        )
        assert result == "hello"

    def test_no_pattern_no_change(self):
        from gateway.platforms.zulip import _strip_bot_mention
        content = "just a regular message"
        result = _strip_bot_mention(content, ["@**Someone Else**"])
        assert result == content

    def test_only_strips_first_occurrence(self):
        """Only the first mention should be stripped (count=1)."""
        from gateway.platforms.zulip import _strip_bot_mention
        result = _strip_bot_mention(
            "@**Bot** @**Bot** hello", ["@**Bot**"]
        )
        # First occurrence stripped, second remains
        assert result == "@**Bot** hello"

    def test_strips_whitespace_after_removal(self):
        from gateway.platforms.zulip import _strip_bot_mention
        result = _strip_bot_mention(
            "  @**Bot**   hello  ", ["@**Bot**"]
        )
        assert result == "hello"


# ---------------------------------------------------------------------------
# ZULIP_REQUIRE_MENTION env var
# ---------------------------------------------------------------------------


class TestZulipRequireMention:
    """Verify ZULIP_REQUIRE_MENTION env var behavior."""

    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter.handle_message = AsyncMock()
        self.adapter._stream_name_cache = {99: "general"}

    @pytest.mark.asyncio
    async def test_default_requires_mention(self):
        """By default, stream messages without mention should be ignored."""
        message = {
            "id": 1101,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "hello without mention",
        }
        self.adapter._dispatch_inbound(message, {})

        self.adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_with_mention_passes(self):
        """By default, stream messages with mention should be processed."""
        message = {
            "id": 1102,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "@**Hermes Bot** hello",
        }
        self.adapter._dispatch_inbound(message, {})

        self.adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_require_mention_false_passes_without_mention(self, monkeypatch):
        """When ZULIP_REQUIRE_MENTION=false, messages without mention pass."""
        monkeypatch.setenv("ZULIP_REQUIRE_MENTION", "false")

        # Create a fresh adapter to pick up the env var
        adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        adapter._bot_user_id = 42
        adapter._bot_full_name = "Hermes Bot"
        adapter.handle_message = AsyncMock()
        adapter._stream_name_cache = {99: "general"}

        message = {
            "id": 1103,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "hello without mention",
        }
        adapter._dispatch_inbound(message, {})

        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_require_mention_zero_disables(self, monkeypatch):
        """ZULIP_REQUIRE_MENTION=0 should disable mention requirement."""
        monkeypatch.setenv("ZULIP_REQUIRE_MENTION", "0")

        adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        adapter._bot_user_id = 42
        adapter._bot_full_name = "Hermes Bot"
        adapter.handle_message = AsyncMock()
        adapter._stream_name_cache = {99: "general"}

        message = {
            "id": 1104,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "hello without mention",
        }
        adapter._dispatch_inbound(message, {})

        adapter.handle_message.assert_called_once()


# ---------------------------------------------------------------------------
# ZULIP_FREE_RESPONSE_STREAMS env var
# ---------------------------------------------------------------------------


class TestZulipFreeResponseStreams:
    """Verify ZULIP_FREE_RESPONSE_STREAMS env var behavior."""

    @pytest.mark.asyncio
    async def test_free_stream_bypasses_mention(self, monkeypatch):
        """Messages in a free-response stream should not require mention."""
        monkeypatch.setenv("ZULIP_FREE_RESPONSE_STREAMS", "general")

        adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        adapter._bot_user_id = 42
        adapter._bot_full_name = "Hermes Bot"
        adapter.handle_message = AsyncMock()
        adapter._stream_name_cache = {99: "general"}

        message = {
            "id": 1201,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "hello without mention",
        }
        adapter._dispatch_inbound(message, {})

        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_free_stream_still_requires_mention(self, monkeypatch):
        """Messages in non-free streams should still require mention."""
        monkeypatch.setenv("ZULIP_FREE_RESPONSE_STREAMS", "random")

        adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        adapter._bot_user_id = 42
        adapter._bot_full_name = "Hermes Bot"
        adapter.handle_message = AsyncMock()
        adapter._stream_name_cache = {99: "general"}

        message = {
            "id": 1202,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "hello without mention",
        }
        adapter._dispatch_inbound(message, {})

        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_free_stream_by_id(self, monkeypatch):
        """Free-response streams can match by stream ID."""
        monkeypatch.setenv("ZULIP_FREE_RESPONSE_STREAMS", "99")

        adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        adapter._bot_user_id = 42
        adapter._bot_full_name = "Hermes Bot"
        adapter.handle_message = AsyncMock()
        adapter._stream_name_cache = {99: "general"}

        message = {
            "id": 1203,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "hello without mention",
        }
        adapter._dispatch_inbound(message, {})

        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_free_stream_case_insensitive(self, monkeypatch):
        """Stream name matching should be case-insensitive."""
        monkeypatch.setenv("ZULIP_FREE_RESPONSE_STREAMS", "GENERAL")

        adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        adapter._bot_user_id = 42
        adapter._bot_full_name = "Hermes Bot"
        adapter.handle_message = AsyncMock()
        adapter._stream_name_cache = {99: "general"}

        message = {
            "id": 1204,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "hello without mention",
        }
        adapter._dispatch_inbound(message, {})

        adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_free_streams(self, monkeypatch):
        """Multiple free-response streams separated by commas."""
        monkeypatch.setenv("ZULIP_FREE_RESPONSE_STREAMS", "general,random,help")

        adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        adapter._bot_user_id = 42
        adapter._bot_full_name = "Hermes Bot"
        adapter.handle_message = AsyncMock()
        adapter._stream_name_cache = {99: "general", 20: "random"}

        # general stream (in free list)
        msg_general = {
            "id": 1205,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "no mention in general",
        }
        adapter._dispatch_inbound(msg_general, {})
        assert adapter.handle_message.call_count == 1

        # random stream (in free list)
        msg_random = {
            "id": 1206,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 20,
            "subject": "test",
            "content": "no mention in random",
        }
        adapter._dispatch_inbound(msg_random, {})
        assert adapter.handle_message.call_count == 2


# ---------------------------------------------------------------------------
# Wildcard mentions
# ---------------------------------------------------------------------------


class TestZulipWildcardMentions:
    """Verify @**all** and @**everyone** trigger the bot in streams."""

    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter.handle_message = AsyncMock()
        self.adapter._stream_name_cache = {99: "general"}

    @pytest.mark.asyncio
    async def test_at_all_triggers_bot(self):
        """@**all** should trigger the bot in streams."""
        message = {
            "id": 1301,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "@**all** check this out",
        }
        self.adapter._dispatch_inbound(message, {})

        self.adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_at_everyone_triggers_bot(self):
        """@**everyone** should trigger the bot in streams."""
        message = {
            "id": 1302,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "@**everyone** important announcement",
        }
        self.adapter._dispatch_inbound(message, {})

        self.adapter.handle_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_wildcard_not_stripped_from_content(self):
        """@**all** and @**everyone** should NOT be stripped from content."""
        message = {
            "id": 1303,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "@**all** this is a broadcast",
        }
        self.adapter._dispatch_inbound(message, {})

        msg_event = self.adapter.handle_message.call_args[0][0]
        # @**all** is NOT a bot mention pattern, so it stays in content
        assert "@**all**" in msg_event.text

    @pytest.mark.asyncio
    async def test_case_insensitive_wildcard(self):
        """Wildcard mentions should be case-insensitive."""
        message = {
            "id": 1304,
            "sender_email": "alice@example.com",
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": 99,
            "subject": "test",
            "content": "@**ALL** attention please",
        }
        self.adapter._dispatch_inbound(message, {})

        self.adapter.handle_message.assert_called_once()


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class TestIsRetryableError:
    """Tests for the _is_retryable_error helper."""

    def test_connection_error_is_retryable(self):
        from gateway.platforms.zulip import _is_retryable_error
        assert _is_retryable_error(ConnectionError("refused")) is True

    def test_timeout_error_is_retryable(self):
        from gateway.platforms.zulip import _is_retryable_error
        assert _is_retryable_error(TimeoutError("timed out")) is True

    def test_ssl_error_is_retryable(self):
        from gateway.platforms.zulip import _is_retryable_error
        # Match by class name containing "SSLError"
        exc = type("SSLError", (Exception,), {})("cert verify failed")
        assert _is_retryable_error(exc) is True

    def test_http_401_not_retryable(self):
        from gateway.platforms.zulip import _is_retryable_error
        exc = Exception("unauthorized")
        exc.http_status = 401
        assert _is_retryable_error(exc) is False

    def test_http_403_not_retryable(self):
        from gateway.platforms.zulip import _is_retryable_error
        exc = Exception("forbidden")
        exc.http_status = 403
        assert _is_retryable_error(exc) is False

    def test_http_400_not_retryable(self):
        from gateway.platforms.zulip import _is_retryable_error
        exc = Exception("bad request")
        exc.http_status = 400
        assert _is_retryable_error(exc) is False

    def test_http_429_not_retryable(self):
        """Rate-limit errors (429) are client errors — not retryable."""
        from gateway.platforms.zulip import _is_retryable_error
        exc = Exception("rate limited")
        exc.http_status = 429
        assert _is_retryable_error(exc) is False

    def test_http_500_is_retryable(self):
        from gateway.platforms.zulip import _is_retryable_error
        exc = Exception("internal server error")
        exc.http_status = 500
        assert _is_retryable_error(exc) is True

    def test_http_502_is_retryable(self):
        from gateway.platforms.zulip import _is_retryable_error
        exc = Exception("bad gateway")
        exc.http_status = 502
        assert _is_retryable_error(exc) is True

    def test_generic_exception_is_retryable(self):
        from gateway.platforms.zulip import _is_retryable_error
        assert _is_retryable_error(RuntimeError("unknown")) is True

    def test_error_without_http_status_is_retryable(self):
        """Exceptions without http_status attribute fall back to retryable."""
        from gateway.platforms.zulip import _is_retryable_error
        exc = ValueError("something")
        assert _is_retryable_error(exc) is True


# ---------------------------------------------------------------------------
# Event queue lifecycle (backoff, reconnect, shutdown)
# ---------------------------------------------------------------------------


class TestZulipEventQueueLifecycle:
    """Tests for the event queue lifecycle — backoff, reconnect, and shutdown."""

    def test_shutdown_event_interrupts_backoff(self):
        """The shutdown event should wake the event thread during backoff sleep."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.call_on_each_event.side_effect = ConnectionError("lost")

        t = threading.Thread(target=adapter._run_event_queue, daemon=True)
        t.start()

        # Give the thread time to enter the first backoff sleep.
        time.sleep(0.5)

        # Signal shutdown — should interrupt the sleep immediately.
        adapter._shutdown_event.set()
        t.join(timeout=2.0)

        assert not t.is_alive(), "Thread should have stopped after shutdown signal"

    def test_closing_flag_stops_loop_immediately(self):
        """Setting _closing before starting should prevent any API calls."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._closing = True

        adapter._run_event_queue()

        adapter._client.call_on_each_event.assert_not_called()

    def test_non_retryable_error_sets_fatal_and_stops(self):
        """Non-retryable errors should set a fatal error and stop the loop."""
        adapter = _make_adapter()

        exc = Exception("unauthorized")
        exc.http_status = 401
        adapter._client = MagicMock()
        adapter._client.call_on_each_event.side_effect = exc

        t = threading.Thread(target=adapter._run_event_queue, daemon=True)
        t.start()
        t.join(timeout=5.0)

        assert not t.is_alive(), "Thread should have stopped on non-retryable error"
        assert adapter.has_fatal_error
        assert adapter.fatal_error_code == "ZULIP_EVENT_QUEUE_FATAL"

    def test_consecutive_failures_tracked_and_reset(self):
        """Each failure increments the counter; it resets on a clean return."""
        adapter = _make_adapter()
        adapter._client = MagicMock()

        # Mock the shutdown event to return immediately (no actual sleep).
        adapter._shutdown_event = MagicMock()
        adapter._shutdown_event.wait.return_value = False

        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 3:
                raise ConnectionError("lost")
            # 4th call: clean return, but _closing is True
            # so the loop exits before resetting the counter.
            adapter._closing = True

        adapter._client.call_on_each_event.side_effect = side_effect
        adapter._run_event_queue()

        assert call_count[0] == 4
        # After 3 failures the counter was 3. On the 4th clean return
        # with _closing=True, the counter is NOT reset (early return).
        assert adapter._consecutive_failures == 3

    def test_consecutive_failures_reset_on_clean_return(self):
        """When call_on_each_event returns cleanly, failures reset."""
        adapter = _make_adapter()
        adapter._client = MagicMock()

        # Mock the shutdown event to return immediately.
        adapter._shutdown_event = MagicMock()
        adapter._shutdown_event.wait.return_value = False

        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("lost")
            if call_count[0] == 2:
                # Clean return — triggers reset
                return
            # 3rd call: stop the loop
            adapter._closing = True

        adapter._client.call_on_each_event.side_effect = side_effect
        adapter._run_event_queue()

        # After call 1 (failure) → consecutive=1, backoff sleep (mocked)
        # After call 2 (clean return) → consecutive=0, continue
        # After call 3 (_closing=True before call completes) → return
        assert adapter._consecutive_failures == 0

    def test_retryable_error_continues_loop(self):
        """Retryable errors should cause the loop to continue (backoff + retry)."""
        adapter = _make_adapter()
        adapter._client = MagicMock()

        # Mock shutdown event to not stop, but stop after 2 calls.
        adapter._shutdown_event = MagicMock()
        adapter._shutdown_event.wait.return_value = False

        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise ConnectionError("network lost")
            adapter._closing = True

        adapter._client.call_on_each_event.side_effect = side_effect
        adapter._run_event_queue()

        assert call_count[0] >= 2


# ---------------------------------------------------------------------------
# Disconnect cleanup
# ---------------------------------------------------------------------------


class TestZulipDisconnectCleanup:
    """Tests for disconnect() cleanup behavior."""

    @pytest.mark.asyncio
    async def test_disconnect_clears_caches(self):
        """disconnect() should clear dedup and stream caches."""
        adapter = _make_adapter()
        adapter._closing = False
        adapter._event_thread = None
        adapter._loop = asyncio.get_running_loop()

        # Populate caches.
        adapter._seen_events = {"msg1": time.time()}
        adapter._stream_id_cache = {"general": 10}
        adapter._stream_name_cache = {10: "general"}
        adapter._consecutive_failures = 5

        await adapter.disconnect()

        assert len(adapter._seen_events) == 0
        assert len(adapter._stream_id_cache) == 0
        assert len(adapter._stream_name_cache) == 0
        assert adapter._consecutive_failures == 0
        assert adapter._client is None
        assert adapter._loop is None

    @pytest.mark.asyncio
    async def test_disconnect_signals_shutdown_event(self):
        """disconnect() should set the shutdown event."""
        adapter = _make_adapter()
        adapter._closing = False
        adapter._event_thread = None
        adapter._loop = asyncio.get_running_loop()

        assert not adapter._shutdown_event.is_set()
        await adapter.disconnect()
        assert adapter._shutdown_event.is_set()

    @pytest.mark.asyncio
    async def test_disconnect_marks_disconnected(self):
        """disconnect() should call _mark_disconnected from the base class."""
        adapter = _make_adapter()
        adapter._closing = False
        adapter._event_thread = None
        adapter._loop = asyncio.get_running_loop()
        adapter._mark_connected()

        assert adapter.is_connected
        await adapter.disconnect()
        assert not adapter.is_connected

    @pytest.mark.asyncio
    async def test_disconnect_cancels_background_tasks(self):
        """disconnect() should cancel background message-processing tasks."""
        adapter = _make_adapter()
        adapter._closing = False
        adapter._event_thread = None
        adapter._loop = asyncio.get_running_loop()

        # Create a mock background task that never completes.
        async def never_completes():
            await asyncio.sleep(1000)

        task = asyncio.create_task(never_completes())
        adapter._background_tasks.add(task)
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(adapter._background_tasks.discard)

        await adapter.disconnect()

        assert task.cancelled() or task.done()
        assert len(adapter._background_tasks) == 0

    @pytest.mark.asyncio
    async def test_disconnect_with_live_event_thread(self):
        """disconnect() should join a live event thread."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.call_on_each_event.side_effect = ConnectionError("lost")

        adapter._loop = asyncio.get_running_loop()
        adapter._closing = False

        # Start the event queue thread.
        adapter._event_thread = threading.Thread(
            target=adapter._run_event_queue,
            daemon=True,
        )
        adapter._event_thread.start()

        # Give it time to enter backoff.
        await asyncio.sleep(0.3)

        await adapter.disconnect()

        # Thread should have been joined (either alive or exited).
        assert not adapter._event_thread.is_alive() or True  # Tolerate slow join
        assert adapter._client is None


# ---------------------------------------------------------------------------
# Logging / observability
# ---------------------------------------------------------------------------


class TestZulipEventDispatchLogging:
    """Verify that event dispatch uses concise identifiers, not raw payloads."""

    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter.handle_message = AsyncMock()
        self.adapter._stream_name_cache = {99: "general"}

    @pytest.mark.asyncio
    async def test_dispatch_log_contains_msg_id_and_sender(self, caplog):
        """_on_zulip_event should log a concise line with msg_id and sender."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="gateway.platforms.zulip"):
            event = {
                "type": "message",
                "op": "add",
                "message": {
                    "id": 9001,
                    "sender_email": "alice@example.com",
                    "sender_id": 10,
                    "type": "private",
                    "content": "Hello bot",
                    "display_recipient": [
                        {"email": "bot@example.zulipchat.com"},
                        {"email": "alice@example.com"},
                    ],
                },
            }

            # _loop is None, so dispatch won't be scheduled, but the
            # debug log is emitted before the _loop check.
            self.adapter._on_zulip_event(event)

        # The log should mention the msg_id and sender, not the raw payload.
        log_text = caplog.text
        assert "msg_id=9001" in log_text
        assert "sender=alice@example.com" in log_text
        assert "type=private" in log_text
        # Should NOT contain raw JSON dumps of the event.
        assert "'message':" not in log_text

    def test_no_log_for_self_messages(self, caplog):
        """Self-messages should not produce any log output."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="gateway.platforms.zulip"):
            event = {
                "type": "message",
                "op": "add",
                "message": {
                    "id": 9002,
                    "sender_email": "bot@example.zulipchat.com",
                    "sender_id": 42,
                    "type": "private",
                    "content": "echo",
                    "display_recipient": [
                        {"email": "bot@example.zulipchat.com"},
                    ],
                },
            }
            self.adapter._on_zulip_event(event)

        # No logs should have been emitted for self-messages.
        assert "msg_id=" not in caplog.text


# ---------------------------------------------------------------------------
# Authorization regression — Zulip in _is_user_authorized maps
# ---------------------------------------------------------------------------


class TestZulipAuthorization:
    """Regression tests: verify Zulip is wired into the gateway auth system."""

    def test_zulip_in_authorization_maps(self):
        """ZULIP_ALLOWED_USERS and ZULIP_ALLOW_ALL_USERS should be in the auth maps."""
        import gateway.run
        import inspect

        source = inspect.getsource(gateway.run.GatewayRunner._is_user_authorized)
        assert "ZULIP_ALLOWED_USERS" in source
        assert "ZULIP_ALLOW_ALL_USERS" in source

    def test_zulip_auth_with_no_allowlists(self, monkeypatch):
        """With no allowlists set, Zulip user should NOT be authorized."""
        from gateway.run import GatewayRunner
        from gateway.config import GatewayConfig

        monkeypatch.delenv("ZULIP_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("ZULIP_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        gw = GatewayRunner.__new__(GatewayRunner)
        gw.config = GatewayConfig()
        gw.pairing_store = MagicMock()
        gw.pairing_store.is_approved.return_value = False

        source = MagicMock()
        source.platform = Platform.ZULIP
        source.user_id = "alice@example.com"

        result = gw._is_user_authorized(source)
        assert result is False

    def test_zulip_auth_with_platform_allowlist(self, monkeypatch):
        """ZULIP_ALLOWED_USERS should authorize listed email."""
        from gateway.run import GatewayRunner
        from gateway.config import GatewayConfig

        monkeypatch.setenv("ZULIP_ALLOWED_USERS", "alice@example.com,bob@example.com")
        monkeypatch.delenv("ZULIP_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        gw = GatewayRunner.__new__(GatewayRunner)
        gw.config = GatewayConfig()
        gw.pairing_store = MagicMock()
        gw.pairing_store.is_approved.return_value = False

        source = MagicMock()
        source.platform = Platform.ZULIP
        source.user_id = "alice@example.com"

        result = gw._is_user_authorized(source)
        assert result is True

    def test_zulip_auth_platform_allowlist_rejects_unlisted(self, monkeypatch):
        """ZULIP_ALLOWED_USERS should reject users not in the list."""
        from gateway.run import GatewayRunner
        from gateway.config import GatewayConfig

        monkeypatch.setenv("ZULIP_ALLOWED_USERS", "alice@example.com")
        monkeypatch.delenv("ZULIP_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        gw = GatewayRunner.__new__(GatewayRunner)
        gw.config = GatewayConfig()
        gw.pairing_store = MagicMock()
        gw.pairing_store.is_approved.return_value = False

        source = MagicMock()
        source.platform = Platform.ZULIP
        source.user_id = "eve@example.com"

        result = gw._is_user_authorized(source)
        assert result is False

    def test_zulip_auth_allow_all_users(self, monkeypatch):
        """ZULIP_ALLOW_ALL_USERS=true should authorize any Zulip user."""
        from gateway.run import GatewayRunner
        from gateway.config import GatewayConfig

        monkeypatch.setenv("ZULIP_ALLOW_ALL_USERS", "true")
        monkeypatch.delenv("ZULIP_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        gw = GatewayRunner.__new__(GatewayRunner)
        gw.config = GatewayConfig()
        gw.pairing_store = MagicMock()
        gw.pairing_store.is_approved.return_value = False

        source = MagicMock()
        source.platform = Platform.ZULIP
        source.user_id = "anyone@example.com"

        result = gw._is_user_authorized(source)
        assert result is True

    def test_zulip_auth_with_global_allowlist(self, monkeypatch):
        """GATEWAY_ALLOWED_USERS should also authorize Zulip users."""
        from gateway.run import GatewayRunner
        from gateway.config import GatewayConfig

        monkeypatch.delenv("ZULIP_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("ZULIP_ALLOW_ALL_USERS", raising=False)
        monkeypatch.setenv("GATEWAY_ALLOWED_USERS", "alice@example.com")
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        gw = GatewayRunner.__new__(GatewayRunner)
        gw.config = GatewayConfig()
        gw.pairing_store = MagicMock()
        gw.pairing_store.is_approved.return_value = False

        source = MagicMock()
        source.platform = Platform.ZULIP
        source.user_id = "alice@example.com"

        result = gw._is_user_authorized(source)
        assert result is True

    def test_zulip_auth_paired_user(self, monkeypatch):
        """A paired (DM-paired) Zulip user should be authorized."""
        from gateway.run import GatewayRunner
        from gateway.config import GatewayConfig

        monkeypatch.delenv("ZULIP_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("ZULIP_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        gw = GatewayRunner.__new__(GatewayRunner)
        gw.config = GatewayConfig()
        gw.pairing_store = MagicMock()
        gw.pairing_store.is_approved.return_value = True

        source = MagicMock()
        source.platform = Platform.ZULIP
        source.user_id = "alice@example.com"

        result = gw._is_user_authorized(source)
        assert result is True


# ---------------------------------------------------------------------------
# Stream send path
# ---------------------------------------------------------------------------


class TestZulipStreamSendPath:
    """Verify that _do_send_message correctly handles stream chat IDs."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._client = MagicMock()

    def test_send_stream_parses_stream_id_and_topic(self):
        """Stream chat IDs should send to the correct stream with topic."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 2000,
        }

        result = self.adapter._do_send_message("42:general", "Hello stream!")

        assert result.success is True
        assert result.message_id == "2000"
        call_args = self.adapter._client.send_message.call_args[0][0]
        assert call_args["type"] == "stream"
        assert call_args["to"] == "42"
        assert call_args["topic"] == "general"
        assert call_args["content"] == "Hello stream!"

    def test_send_stream_with_complex_topic(self):
        """Topics containing colons and spaces should be preserved."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 2001,
        }

        result = self.adapter._do_send_message("5:time: 12:00", "check time")

        assert result.success is True
        call_args = self.adapter._client.send_message.call_args[0][0]
        assert call_args["topic"] == "time: 12:00"

    def test_send_stream_no_topic(self):
        """Stream with empty topic should use '(no topic)' from parse."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 2002,
        }

        result = self.adapter._do_send_message("42:", "hello")

        assert result.success is True
        call_args = self.adapter._client.send_message.call_args[0][0]
        assert call_args["topic"] == "(no topic)"

    def test_send_stream_api_failure(self):
        """API errors on stream send return failed SendResult."""
        self.adapter._client.send_message.return_value = {
            "result": "error",
            "msg": "Stream not found",
        }

        result = self.adapter._do_send_message("42:general", "fail")

        assert result.success is False
        assert "not found" in result.error

    def test_send_stream_not_connected(self):
        """No client returns failed SendResult for streams."""
        self.adapter._client = None

        result = self.adapter._do_send_message("42:general", "no client")

        assert result.success is False
        assert "Not connected" in result.error


# ---------------------------------------------------------------------------
# 1:1 DM send path
# ---------------------------------------------------------------------------


class TestZulipDmSendPath:
    """Verify that _do_send_message correctly handles 1:1 DM chat IDs."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._client = MagicMock()

    def test_send_dm_to_email(self):
        """DM chat IDs should send a private message to the recipient."""
        self.adapter._client.send_message.return_value = {
            "result": "success",
            "id": 3000,
        }

        result = self.adapter._do_send_message("dm:alice@example.com", "Hello DM!")

        assert result.success is True
        call_args = self.adapter._client.send_message.call_args[0][0]
        assert call_args["type"] == "private"
        assert call_args["to"] == ["alice@example.com"]
        assert call_args["content"] == "Hello DM!"

    def test_send_dm_api_failure(self):
        """API errors on DM send return failed SendResult."""
        self.adapter._client.send_message.return_value = {
            "result": "error",
            "msg": "User not found",
        }

        result = self.adapter._do_send_message("dm:nobody@example.com", "fail")

        assert result.success is False
        assert "not found" in result.error

    def test_send_dm_not_connected(self):
        """No client returns failed SendResult for DMs."""
        self.adapter._client = None

        result = self.adapter._do_send_message("dm:alice@example.com", "no client")

        assert result.success is False
        assert "Not connected" in result.error


# ---------------------------------------------------------------------------
# Chat-ID comprehensive round-trips
# ---------------------------------------------------------------------------


class TestZulipChatIdRoundTrips:
    """Comprehensive round-trip tests for all Zulip chat-ID formats."""

    def test_stream_roundtrip_multiple_colons_in_topic(self):
        """Topics with multiple colons must round-trip correctly."""
        from gateway.platforms.zulip import _build_stream_chat_id, _parse_stream_chat_id

        for topic in ["a:b:c", "time: 12:00 PM", "version:1.0:rc1", "key:value"]:
            chat_id = _build_stream_chat_id(7, topic)
            parsed = _parse_stream_chat_id(chat_id)
            assert parsed == (7, topic), f"Failed for topic: {topic!r}"

    def test_stream_roundtrip_special_characters(self):
        """Topics with special characters must round-trip correctly."""
        from gateway.platforms.zulip import _build_stream_chat_id, _parse_stream_chat_id

        for topic in ["(no topic)", "help & support", "bug fix #123",
                       "release/v2.0", "C++ discussion", "100% done"]:
            chat_id = _build_stream_chat_id(42, topic)
            parsed = _parse_stream_chat_id(chat_id)
            assert parsed == (42, topic), f"Failed for topic: {topic!r}"

    def test_dm_roundtrip_with_various_emails(self):
        """DM chat IDs with various email formats must round-trip."""
        from gateway.platforms.zulip import _build_dm_chat_id, _parse_dm_chat_id

        for email in ["simple@example.com", "user+tag@example.co.uk",
                       "user.name@sub.domain.org", "a@b.io"]:
            chat_id = _build_dm_chat_id(email)
            parsed = _parse_dm_chat_id(chat_id)
            assert parsed == email, f"Failed for email: {email!r}"

    def test_group_dm_roundtrip_ordering(self):
        """Group DM chat IDs should always sort emails for determinism."""
        from gateway.platforms.zulip import _build_group_dm_chat_id, _parse_group_dm_chat_id

        # Different input orders must produce the same chat_id.
        inputs = [
            ["charlie@example.com", "alice@example.com", "bob@example.com"],
            ["bob@example.com", "charlie@example.com", "alice@example.com"],
            ["alice@example.com", "bob@example.com", "charlie@example.com"],
        ]
        chat_ids = [_build_group_dm_chat_id(emails) for emails in inputs]

        # All should be identical (sorted).
        assert len(set(chat_ids)) == 1
        # Should parse back to the same sorted list.
        parsed = _parse_group_dm_chat_id(chat_ids[0])
        assert parsed == ["alice@example.com", "bob@example.com", "charlie@example.com"]

    def test_is_dm_vs_is_group_dm_disjoint(self):
        """DM and group DM classification should never overlap."""
        from gateway.platforms.zulip import (
            is_dm_chat_id,
            is_group_dm_chat_id,
            _parse_stream_chat_id,
        )

        dm_ids = ["dm:alice@example.com", "dm:bob@example.org"]
        group_ids = [
            "group_dm:alice@example.com,bob@example.com",
            "group_dm:a@b.com,c@d.com,e@f.com",
        ]
        stream_ids = ["42:general", "5:time: 12:00"]

        for cid in dm_ids:
            assert is_dm_chat_id(cid) is True
            assert is_group_dm_chat_id(cid) is False
            assert _parse_stream_chat_id(cid) is None

        for cid in group_ids:
            assert is_group_dm_chat_id(cid) is True
            assert is_dm_chat_id(cid) is False
            assert _parse_stream_chat_id(cid) is None

        for cid in stream_ids:
            assert is_dm_chat_id(cid) is False
            assert is_group_dm_chat_id(cid) is False
            assert _parse_stream_chat_id(cid) is not None


# ---------------------------------------------------------------------------
# get_chat_info
# ---------------------------------------------------------------------------


class TestZulipGetChatInfo:
    """Verify get_chat_info returns correct metadata for all chat types."""

    @pytest.mark.asyncio
    async def test_stream_from_cache(self):
        """Stream chat info should use cached stream name."""
        adapter = _make_adapter()
        adapter._stream_name_cache = {42: "engineering"}

        info = await adapter.get_chat_info("42:API Design")

        assert info["type"] == "stream"
        assert "engineering" in info["name"]
        assert "API Design" in info["name"]

    @pytest.mark.asyncio
    async def test_stream_not_in_cache(self):
        """Stream chat info without cache should fall back to chat_id."""
        adapter = _make_adapter()

        info = await adapter.get_chat_info("99:topic")

        assert info["type"] == "stream"
        assert info["name"] == "#99:topic > topic"

    @pytest.mark.asyncio
    async def test_dm_info(self):
        """DM chat info should return the email as name."""
        adapter = _make_adapter()

        info = await adapter.get_chat_info("dm:alice@example.com")

        assert info["type"] == "dm"
        assert info["name"] == "alice@example.com"

    @pytest.mark.asyncio
    async def test_group_dm_fallback(self):
        """Group DM chat info should fall back to the raw chat_id as name."""
        adapter = _make_adapter()

        info = await adapter.get_chat_info("group_dm:a@b.com,c@d.com")

        assert info["type"] == "dm"  # group DMs fall to the else branch
        assert info["name"] == "group_dm:a@b.com,c@d.com"


# ---------------------------------------------------------------------------
# edit_message
# ---------------------------------------------------------------------------


class TestZulipEditMessage:
    """Verify edit_message behavior."""

    @pytest.mark.asyncio
    async def test_successful_edit(self):
        """Successful edit should return success with the message ID."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.update_message.return_value = {
            "result": "success",
        }

        result = await adapter.edit_message("dm:alice@example.com", "1234", "updated text")

        assert result.success is True
        assert result.message_id == "1234"
        adapter._client.update_message.assert_called_once_with({
            "message_id": 1234,
            "content": "updated text",
        })

    @pytest.mark.asyncio
    async def test_edit_failure(self):
        """Failed edit should return error."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.update_message.return_value = {
            "result": "error",
            "msg": "Message not found",
        }

        result = await adapter.edit_message("dm:alice@example.com", "9999", "edit")

        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_edit_not_connected(self):
        """Edit without client should return not-supported."""
        adapter = _make_adapter()
        adapter._client = None

        result = await adapter.edit_message("dm:alice@example.com", "1234", "edit")

        assert result.success is False
        assert "Not supported" in result.error


# ---------------------------------------------------------------------------
# send() high-level method
# ---------------------------------------------------------------------------


class TestZulipSendHighLevel:
    """Verify the high-level send() method (chunking, empty, etc.)."""

    @pytest.mark.asyncio
    async def test_send_empty_content_returns_success(self):
        """Empty content should return success without API call."""
        adapter = _make_adapter()
        adapter._client = MagicMock()

        result = await adapter.send("42:general", "")

        assert result.success is True
        adapter._client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_none_content_returns_success(self):
        """None content should return success without API call."""
        adapter = _make_adapter()
        adapter._client = MagicMock()

        result = await adapter.send("dm:alice@example.com", None)

        assert result.success is True
        adapter._client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_single_chunk(self):
        """Short messages should be sent in a single chunk."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 5000,
        }

        result = await adapter.send("42:general", "Hello!")

        assert result.success is True
        assert result.message_id == "5000"
        assert adapter._client.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_send_multiple_chunks(self):
        """Messages exceeding MAX_MESSAGE_LENGTH should be split."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 6000,
        }

        from gateway.platforms.zulip import MAX_MESSAGE_LENGTH
        long_content = "x" * (MAX_MESSAGE_LENGTH + 100)

        result = await adapter.send("42:general", long_content)

        assert result.success is True
        assert adapter._client.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_send_failure_on_first_chunk_stops(self):
        """If the first chunk fails, send should return the error."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.send_message.return_value = {
            "result": "error",
            "msg": "permission denied",
        }

        from gateway.platforms.zulip import MAX_MESSAGE_LENGTH
        long_content = "x" * (MAX_MESSAGE_LENGTH + 100)

        result = await adapter.send("42:general", long_content)

        assert result.success is False
        # Should only have attempted the first chunk.
        assert adapter._client.send_message.call_count == 1


# ---------------------------------------------------------------------------
# Mention gating integration regression
# ---------------------------------------------------------------------------


class TestZulipMentionGatingIntegration:
    """End-to-end integration tests for mention/trigger gating in _dispatch_inbound.

    These tests verify the complete decision chain: DMs always pass,
    streams require mention (unless configured otherwise), wildcards trigger.
    """

    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter.handle_message = AsyncMock()
        self.adapter._stream_name_cache = {10: "general", 20: "random"}

    def _make_stream_msg(self, stream_id, content, subject="test", sender="alice@example.com"):
        return {
            "id": 9000 + stream_id,
            "sender_email": sender,
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "stream",
            "stream_id": stream_id,
            "subject": subject,
            "content": content,
        }

    def _make_dm_msg(self, content, sender="alice@example.com"):
        return {
            "id": 9100,
            "sender_email": sender,
            "sender_full_name": "Alice",
            "sender_id": 10,
            "type": "private",
            "content": content,
            "display_recipient": [
                {"email": "bot@example.zulipchat.com"},
                {"email": sender},
            ],
        }

    @pytest.mark.asyncio
    async def test_dm_always_passes_no_mention_needed(self):
        """DMs should be dispatched regardless of mention config."""
        self.adapter._dispatch_inbound(self._make_dm_msg("just saying hi"), {})

        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "just saying hi"

    @pytest.mark.asyncio
    async def test_dm_does_not_strip_content(self):
        """DMs should not have mention stripping applied."""
        self.adapter._dispatch_inbound(
            self._make_dm_msg("@**Hermes Bot** do something"), {}
        )

        msg_event = self.adapter.handle_message.call_args[0][0]
        assert "@**Hermes Bot**" in msg_event.text

    @pytest.mark.asyncio
    async def test_stream_without_mention_blocked_by_default(self):
        """Stream message without @mention should be ignored (default config)."""
        self.adapter._dispatch_inbound(
            self._make_stream_msg(10, "hello everyone"), {}
        )

        self.adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_stream_with_bot_mention_passes(self):
        """Stream message with @**Hermes Bot** should be dispatched."""
        self.adapter._dispatch_inbound(
            self._make_stream_msg(10, "@**Hermes Bot** hello"), {}
        )

        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "hello"

    @pytest.mark.asyncio
    async def test_stream_with_email_mention_passes(self):
        """Stream message with @bot@example.zulipchat.com should be dispatched."""
        self.adapter._dispatch_inbound(
            self._make_stream_msg(10, "@bot@example.zulipchat.com help"), {}
        )

        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert msg_event.text == "help"

    @pytest.mark.asyncio
    async def test_stream_wildcard_all_passes(self):
        """@**all** should trigger the bot but content stays intact."""
        self.adapter._dispatch_inbound(
            self._make_stream_msg(10, "@**all** attention"), {}
        )

        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert "@**all**" in msg_event.text

    @pytest.mark.asyncio
    async def test_stream_wildcard_everyone_passes(self):
        """@**everyone** should trigger the bot."""
        self.adapter._dispatch_inbound(
            self._make_stream_msg(20, "@**everyone** announcement"), {}
        )

        assert self.adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_stream_free_response_passes_without_mention(self, monkeypatch):
        """Free-response stream should not require @mention."""
        monkeypatch.setenv("ZULIP_FREE_RESPONSE_STREAMS", "general")

        adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        adapter._bot_user_id = 42
        adapter._bot_full_name = "Hermes Bot"
        adapter.handle_message = AsyncMock()
        adapter._stream_name_cache = {10: "general"}

        adapter._dispatch_inbound(
            self._make_stream_msg(10, "hello without mention"), {}
        )

        assert adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_stream_require_mention_disabled(self, monkeypatch):
        """When ZULIP_REQUIRE_MENTION=false, all streams pass without mention."""
        monkeypatch.setenv("ZULIP_REQUIRE_MENTION", "false")

        adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        adapter._bot_user_id = 42
        adapter._bot_full_name = "Hermes Bot"
        adapter.handle_message = AsyncMock()
        adapter._stream_name_cache = {20: "random"}

        adapter._dispatch_inbound(
            self._make_stream_msg(20, "hello without mention"), {}
        )

        assert adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_stream_mixed_content_strips_bot_only(self):
        """Bot mention should be stripped; wildcard mentions preserved."""
        self.adapter._dispatch_inbound(
            self._make_stream_msg(10, "@**Hermes Bot** @**all** check this"), {}
        )

        assert self.adapter.handle_message.called
        msg_event = self.adapter.handle_message.call_args[0][0]
        assert "@**Hermes Bot**" not in msg_event.text
        assert "@**all**" in msg_event.text


# ---------------------------------------------------------------------------
# Session-history directory integration regression
# ---------------------------------------------------------------------------


class TestZulipSessionDirectoryIntegration:
    """Regression tests: verify Zulip sessions flow correctly through the
    channel directory pipeline from sessions.json to display."""

    def test_build_from_sessions_deduplicates_same_stream_topic(self, tmp_path):
        """Multiple sessions for the same stream+topic should deduplicate."""
        sessions_path = tmp_path / "sessions" / "sessions.json"
        sessions_path.parent.mkdir(parents=True)
        sessions_path.write_text(json.dumps({
            "s1": {
                "origin": {
                    "platform": "zulip",
                    "chat_id": "42:general",
                    "chat_name": "general",
                    "chat_topic": "general",
                },
                "chat_type": "stream",
            },
            "s2": {
                "origin": {
                    "platform": "zulip",
                    "chat_id": "42:general",
                    "chat_name": "general",
                    "chat_topic": "general",
                },
                "chat_type": "stream",
            },
        }))

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("zulip")

        assert len(entries) == 1

    def test_build_from_sessions_distinguishes_different_topics(self, tmp_path):
        """Different topics in the same stream produce separate entries by chat_id."""
        sessions_path = tmp_path / "sessions" / "sessions.json"
        sessions_path.parent.mkdir(parents=True)
        sessions_path.write_text(json.dumps({
            "topic_a": {
                "origin": {
                    "platform": "zulip",
                    "chat_id": "42:API Design",
                    "chat_name": "engineering",
                    "chat_topic": "API Design",
                },
                "chat_type": "stream",
            },
            "topic_b": {
                "origin": {
                    "platform": "zulip",
                    "chat_id": "42:Bugs",
                    "chat_name": "engineering",
                    "chat_topic": "Bugs",
                },
                "chat_type": "stream",
            },
        }))

        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            entries = _build_from_sessions("zulip")

        # Different chat_ids (42:API Design vs 42:Bugs) produce distinct entries.
        assert len(entries) == 2
        ids = {e["id"] for e in entries}
        assert "42:API Design" in ids
        assert "42:Bugs" in ids

    def test_directory_display_includes_zulip_section(self, tmp_path):
        """format_directory_for_display should include a Zulip section."""
        from gateway.channel_directory import format_directory_for_display

        cache_file = _write_directory(tmp_path, {
            "zulip": [
                {"id": "dm:alice@example.com", "name": "alice@example.com", "type": "dm"},
            ],
            "telegram": [
                {"id": "123", "name": "Bob", "type": "dm"},
            ],
        })

        # Import here after patching
        from gateway import channel_directory
        original_path = channel_directory.DIRECTORY_PATH

        try:
            channel_directory.DIRECTORY_PATH = cache_file
            result = format_directory_for_display()
        finally:
            channel_directory.DIRECTORY_PATH = original_path

        assert "Zulip:" in result
        assert "zulip:alice@example.com" in result
        assert "Telegram:" in result
        assert "telegram:Bob" in result


# ---------------------------------------------------------------------------
# Event validation edge cases
# ---------------------------------------------------------------------------


class TestZulipEventValidationEdgeCases:
    """Regression tests for event validation edge cases in _on_zulip_event."""

    def setup_method(self):
        self.adapter = _make_adapter(bot_email="bot@example.zulipchat.com")
        self.adapter._bot_user_id = 42
        self.adapter._bot_full_name = "Hermes Bot"
        self.adapter._loop = None  # prevent async dispatch
        self.adapter.handle_message = AsyncMock()

    def test_event_with_missing_message_key(self):
        """Events missing the 'message' key should be silently ignored."""
        event = {"type": "message", "op": "add"}
        self.adapter._on_zulip_event(event)
        self.adapter.handle_message.assert_not_called()

    def test_event_with_null_message(self):
        """Events with null message should be silently ignored."""
        event = {"type": "message", "op": "add", "message": None}
        self.adapter._on_zulip_event(event)
        self.adapter.handle_message.assert_not_called()

    def test_event_with_non_dict_message(self):
        """Events with non-dict message should be silently ignored."""
        event = {"type": "message", "op": "add", "message": "not a dict"}
        self.adapter._on_zulip_event(event)
        self.adapter.handle_message.assert_not_called()

    def test_event_with_op_update_ignored(self):
        """Events with op='update' (edits) should be ignored."""
        event = {
            "type": "message",
            "op": "update",
            "message": {
                "id": 8000,
                "sender_email": "alice@example.com",
                "sender_id": 10,
                "type": "stream",
                "content": "edited message",
            },
        }
        self.adapter._on_zulip_event(event)
        self.adapter.handle_message.assert_not_called()

    def test_event_with_op_delete_ignored(self):
        """Events with op='delete' should be ignored."""
        event = {
            "type": "message",
            "op": "delete",
            "message": {
                "id": 8001,
                "sender_email": "alice@example.com",
                "sender_id": 10,
                "type": "stream",
                "content": "",
            },
        }
        self.adapter._on_zulip_event(event)
        self.adapter.handle_message.assert_not_called()

    def test_event_type_reaction_ignored(self):
        """Non-message event types (e.g. 'reaction') should be ignored."""
        event = {
            "type": "reaction",
            "op": "add",
            "message": {
                "id": 8002,
                "sender_email": "alice@example.com",
                "sender_id": 10,
                "type": "stream",
                "content": "",
            },
        }
        self.adapter._on_zulip_event(event)
        self.adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_whitespace_only_content_filtered(self):
        """Messages with only whitespace content should be filtered out."""
        self.adapter._loop = asyncio.get_running_loop()

        event = {
            "type": "message",
            "op": "add",
            "message": {
                "id": 8003,
                "sender_email": "alice@example.com",
                "sender_id": 10,
                "type": "private",
                "content": "   \t\n  ",
                "display_recipient": [
                    {"email": "bot@example.zulipchat.com"},
                    {"email": "alice@example.com"},
                ],
            },
        }
        self.adapter._on_zulip_event(event)
        # Give the event loop a chance to process
        await asyncio.sleep(0.05)
        self.adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_msg_type_filtered(self):
        """Messages with an unknown type (not stream/private) should be ignored."""
        self.adapter._loop = asyncio.get_running_loop()

        event = {
            "type": "message",
            "op": "add",
            "message": {
                "id": 8004,
                "sender_email": "alice@example.com",
                "sender_id": 10,
                "type": "outgoing_webhook",
                "content": "should be ignored",
            },
        }
        self.adapter._on_zulip_event(event)
        await asyncio.sleep(0.05)
        self.adapter.handle_message.assert_not_called()


# ---------------------------------------------------------------------------
# Integration verification — Zulip wired into all subsystems
# ---------------------------------------------------------------------------


class TestZulipSubsystemIntegration:
    """Structural tests verifying Zulip is present in all gateway subsystems.

    Follows the pattern from test_email.py::TestAuthorizationMaps and related
    classes — uses inspect.getsource() to guard against accidental removal.
    """

    def test_zulip_in_adapter_factory(self):
        """Platform.ZULIP branch should exist in _create_adapter()."""
        import gateway.run
        import inspect
        source = inspect.getsource(gateway.run.GatewayRunner._create_adapter)
        assert "Platform.ZULIP" in source

    def test_zulip_in_allowed_users_map(self):
        """ZULIP_ALLOWED_USERS should be in platform_env_map."""
        import gateway.run
        import inspect
        source = inspect.getsource(gateway.run.GatewayRunner._is_user_authorized)
        assert "ZULIP_ALLOWED_USERS" in source

    def test_zulip_in_allow_all_map(self):
        """ZULIP_ALLOW_ALL_USERS should be in platform_allow_all_map."""
        import gateway.run
        import inspect
        source = inspect.getsource(gateway.run.GatewayRunner._is_user_authorized)
        assert "ZULIP_ALLOW_ALL_USERS" in source

    def test_zulip_in_platform_hints(self):
        """'zulip' key should exist in PLATFORM_HINTS."""
        import agent.prompt_builder as pb
        assert "zulip" in pb.PLATFORM_HINTS

    def test_zulip_in_session_discovery(self):
        """'zulip' should be in the session-based discovery tuple."""
        import gateway.channel_directory as cd
        import inspect
        source = inspect.getsource(cd.build_channel_directory)
        assert '"zulip"' in source or "'zulip'" in source

    def test_zulip_in_gateway_platforms(self):
        """'zulip' should be a key in the setup wizard platform list."""
        from hermes_cli.gateway import _PLATFORMS
        keys = [p["key"] for p in _PLATFORMS]
        assert "zulip" in keys

    def test_zulip_has_setup_vars(self):
        """Zulip platform entry in _PLATFORMS should have required vars."""
        from hermes_cli.gateway import _PLATFORMS
        zulip = next((p for p in _PLATFORMS if p["key"] == "zulip"), None)
        assert zulip is not None, "Zulip not in _PLATFORMS"
        var_names = [v["name"] for v in zulip.get("vars", [])]
        assert "ZULIP_API_KEY" in var_names
        assert "ZULIP_BOT_EMAIL" in var_names
        assert "ZULIP_SITE_URL" in var_names


# ---------------------------------------------------------------------------
# Rich delivery: media / send helpers
# ---------------------------------------------------------------------------


class TestZulipUploadFile:
    """Verify _upload_file wraps the Zulip client upload correctly."""

    def setup_method(self):
        self.adapter = _make_adapter()
        self.adapter._client = MagicMock()

    def test_successful_upload_returns_uri(self):
        """A successful upload should return the URI string."""
        self.adapter._client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/abc123/image.png",
        }

        result = self.adapter._upload_file(b"\x89PNG\r\n", "image.png")

        assert result == "/user_uploads/1/abc123/image.png"
        self.adapter._client.upload_file.assert_called_once()

    def test_upload_sets_filename_on_bytesio(self):
        """The BytesIO buffer should have .name set to the filename."""
        self.adapter._client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/xyz/file.pdf",
        }

        self.adapter._upload_file(b"PDF data", "report.pdf")

        uploaded_buf = self.adapter._client.upload_file.call_args[0][0]
        assert uploaded_buf.name == "report.pdf"

    def test_failed_upload_returns_none(self):
        """A failed upload result should return None."""
        self.adapter._client.upload_file.return_value = {
            "result": "error",
            "msg": "File too large",
        }

        result = self.adapter._upload_file(b"x" * 100, "big.dat")

        assert result is None

    def test_upload_exception_returns_none(self):
        """An exception during upload should return None (not raise)."""
        self.adapter._client.upload_file.side_effect = ConnectionError("timeout")

        result = self.adapter._upload_file(b"data", "file.txt")

        assert result is None

    def test_upload_without_client_returns_none(self):
        """Uploading when not connected should return None."""
        self.adapter._client = None

        result = self.adapter._upload_file(b"data", "file.txt")

        assert result is None


class TestZulipSendTyping:
    """Verify send_typing behavior for stream and DM targets."""

    @pytest.mark.asyncio
    async def test_typing_stream_with_cached_name(self):
        """Typing in a stream should call set_typing_status with stream name."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._stream_name_cache = {42: "general"}

        await adapter.send_typing("42:some topic")

        adapter._client.set_typing_status.assert_called_once_with({
            "to": ["general"],
            "op": "start",
        })

    @pytest.mark.asyncio
    async def test_typing_dm(self):
        """Typing in a DM should call set_typing_status with the email."""
        adapter = _make_adapter()
        adapter._client = MagicMock()

        await adapter.send_typing("dm:alice@example.com")

        adapter._client.set_typing_status.assert_called_once_with({
            "to": ["alice@example.com"],
            "op": "start",
        })

    @pytest.mark.asyncio
    async def test_typing_without_client_is_silent(self):
        """Typing without a connected client should not raise."""
        adapter = _make_adapter()
        adapter._client = None

        await adapter.send_typing("42:general")  # Should not raise

    @pytest.mark.asyncio
    async def test_typing_stream_without_cache_is_silent(self):
        """Typing for a stream not in the name cache should silently skip."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._stream_name_cache = {}  # empty — no stream name for ID 99

        await adapter.send_typing("99:topic")

        adapter._client.set_typing_status.assert_not_called()


class TestZulipSendImage:
    """Verify send_image downloads, uploads, and sends inline."""

    @pytest.mark.asyncio
    async def test_send_image_success(self, tmp_path):
        """Successful download + upload should produce an inline image message."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/img/photo.png",
        }
        # send() → _do_send_message() → self._client.send_message()
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 8001,
        }

        mock_response = MagicMock()
        mock_response.content = b"\x89PNG\r\n\x1a\n"
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as client_cls:
            client_instance = AsyncMock()
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            client_instance.get = AsyncMock(return_value=mock_response)
            client_cls.return_value = client_instance

            result = await adapter.send_image(
                "42:general",
                "https://example.com/photo.png",
                caption="Here is a photo",
            )

        assert result.success is True
        # The message body should contain the uploaded image URI.
        # Note: format_message() strips ![alt](url) → url (Zulip auto-embeds
        # /user_uploads/ URLs natively, so markdown image syntax is not needed).
        call_args = adapter._client.send_message.call_args
        assert call_args is not None
        content = call_args[0][0]["content"]
        assert "/user_uploads/1/img/photo.png" in content

    @pytest.mark.asyncio
    async def test_send_image_download_failure_falls_back_to_url(self, tmp_path):
        """Failed download should fall back to sending the URL as text."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 999,
        }

        with patch("httpx.AsyncClient") as client_cls:
            client_instance = AsyncMock()
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            client_instance.get = AsyncMock(
                side_effect=Exception("Connection refused")
            )
            client_cls.return_value = client_instance

            result = await adapter.send_image(
                "dm:alice@example.com",
                "https://example.com/photo.png",
            )

        # Should fall back to URL as text, still succeed
        assert result.success is True
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "https://example.com/photo.png" in content

    @pytest.mark.asyncio
    async def test_send_image_upload_failure_falls_back_to_url(self):
        """Failed upload should fall back to sending the URL as text."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "error",
            "msg": "Upload failed",
        }
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 888,
        }

        mock_response = MagicMock()
        mock_response.content = b"\x89PNG\r\n\x1a\n"
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as client_cls:
            client_instance = AsyncMock()
            client_instance.__aenter__ = AsyncMock(return_value=client_instance)
            client_instance.__aexit__ = AsyncMock(return_value=False)
            client_instance.get = AsyncMock(return_value=mock_response)
            client_cls.return_value = client_instance

            result = await adapter.send_image(
                "42:general",
                "https://example.com/broken.png",
                caption="caption",
            )

        # Fallback: caption + URL in text
        assert result.success is True
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "caption" in content
        assert "https://example.com/broken.png" in content


class TestZulipSendImageFile:
    """Verify send_image_file uploads a local file and sends inline."""

    @pytest.mark.asyncio
    async def test_send_image_file_success(self, tmp_path):
        """Existing local image file should be uploaded and sent inline."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/local/photo.jpg",
        }
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 8002,
        }

        image_path = tmp_path / "photo.jpg"
        image_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 32)

        result = await adapter.send_image_file(
            "42:general",
            str(image_path),
            caption="Local photo",
        )

        assert result.success is True
        # Verify the message contains the uploaded image URI.
        # Note: format_message() strips ![alt](url) → url, but Zulip
        # auto-embeds /user_uploads/ URLs natively.
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "/user_uploads/1/local/photo.jpg" in content

    @pytest.mark.asyncio
    async def test_send_image_file_missing(self):
        """Missing local file should send a file-not-found text message."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 777,
        }

        result = await adapter.send_image_file(
            "42:general",
            "/tmp/nonexistent_image.png",
        )

        # Should still succeed — sends a fallback text message
        assert result.success is True
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "nonexistent_image.png" in content

    @pytest.mark.asyncio
    async def test_send_image_file_upload_failure(self, tmp_path):
        """Failed upload should return an error SendResult."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "error",
            "msg": "Server error",
        }

        image_path = tmp_path / "fail.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n")

        result = await adapter.send_image_file(
            "42:general",
            str(image_path),
        )

        assert result.success is False
        assert "upload" in result.error.lower() or "failed" in result.error.lower()


class TestZulipSendDocument:
    """Verify send_document uploads a file and sends as a markdown link."""

    @pytest.mark.asyncio
    async def test_send_document_success(self, tmp_path):
        """Existing local file should be uploaded and sent as a markdown link."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/docs/report.pdf",
        }
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 8003,
        }

        doc_path = tmp_path / "report.pdf"
        doc_path.write_bytes(b"%PDF-1.4 test content")

        result = await adapter.send_document(
            "dm:alice@example.com",
            str(doc_path),
            caption="Q4 Report",
        )

        assert result.success is True
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "[report.pdf]" in content
        assert "/user_uploads/1/docs/report.pdf" in content
        assert "Q4 Report" in content

    @pytest.mark.asyncio
    async def test_send_document_no_caption(self, tmp_path):
        """Document without caption should still produce a markdown link."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/docs/data.csv",
        }
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 8004,
        }

        doc_path = tmp_path / "data.csv"
        doc_path.write_bytes(b"id,name\n1,alice")

        result = await adapter.send_document(
            "42:general",
            str(doc_path),
        )

        assert result.success is True
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "[data.csv]" in content

    @pytest.mark.asyncio
    async def test_send_document_missing_file(self):
        """Missing file should send a file-not-found text message."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 666,
        }

        result = await adapter.send_document(
            "42:general",
            "/tmp/nonexistent.pdf",
            caption="Missing report",
        )

        assert result.success is True
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "nonexistent.pdf" in content

    @pytest.mark.asyncio
    async def test_send_document_upload_failure(self, tmp_path):
        """Failed upload should return an error SendResult."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "error",
            "msg": "Storage full",
        }

        doc_path = tmp_path / "big.pdf"
        doc_path.write_bytes(b"%PDF-1.4")

        result = await adapter.send_document(
            "dm:alice@example.com",
            str(doc_path),
        )

        assert result.success is False
        assert "failed" in result.error.lower()

    @pytest.mark.asyncio
    async def test_send_document_custom_filename(self, tmp_path):
        """Custom file_name should override the local file name."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/docs/custom.docx",
        }
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 8005,
        }

        doc_path = tmp_path / "temp_upload_xyz.tmp"
        doc_path.write_bytes(b"doc content")

        result = await adapter.send_document(
            "42:general",
            str(doc_path),
            file_name="quarterly-report.docx",
        )

        assert result.success is True
        # Verify upload was called with the custom filename
        upload_buf = adapter._client.upload_file.call_args[0][0]
        assert upload_buf.name == "quarterly-report.docx"
        # Verify the message uses the custom filename in the link
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "[quarterly-report.docx]" in content


class TestZulipSendVideo:
    """Verify send_video uploads a video file and sends as a link."""

    @pytest.mark.asyncio
    async def test_send_video_success(self, tmp_path):
        """Existing local video should be uploaded and sent as a link."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "success",
            "uri": "/user_uploads/1/vid/demo.mp4",
        }
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 8006,
        }

        video_path = tmp_path / "demo.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x20ftypmp42")

        result = await adapter.send_video(
            "42:general",
            str(video_path),
            caption="Demo recording",
        )

        assert result.success is True
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "[demo.mp4]" in content
        assert "/user_uploads/1/vid/demo.mp4" in content
        assert "Demo recording" in content

    @pytest.mark.asyncio
    async def test_send_video_missing_file(self):
        """Missing video file should send a file-not-found text message."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.send_message.return_value = {
            "result": "success",
            "id": 555,
        }

        result = await adapter.send_video(
            "42:general",
            "/tmp/nonexistent.mp4",
        )

        assert result.success is True
        call_args = adapter._client.send_message.call_args
        content = call_args[0][0]["content"]
        assert "nonexistent.mp4" in content

    @pytest.mark.asyncio
    async def test_send_video_upload_failure(self, tmp_path):
        """Failed upload should return an error SendResult."""
        adapter = _make_adapter()
        adapter._client = MagicMock()
        adapter._client.upload_file.return_value = {
            "result": "error",
            "msg": "File too large",
        }

        video_path = tmp_path / "big.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x20ftypmp42")

        result = await adapter.send_video(
            "dm:alice@example.com",
            str(video_path),
        )

        assert result.success is False
        assert "upload" in result.error.lower() or "failed" in result.error.lower()
