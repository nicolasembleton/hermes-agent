"""Tests for tools/send_message_tool.py."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import Platform
from tools.send_message_tool import (
    _send_telegram,
    _send_to_platform,
    _send_zulip,
    _parse_target_ref,
    _parse_zulip_target_ref,
    send_message_tool,
)


def _run_async_immediately(coro):
    return asyncio.run(coro)


def _make_config():
    telegram_cfg = SimpleNamespace(enabled=True, token="***", extra={})
    return SimpleNamespace(
        platforms={Platform.TELEGRAM: telegram_cfg},
        get_home_channel=lambda _platform: None,
    ), telegram_cfg


def _install_telegram_mock(monkeypatch, bot):
    parse_mode = SimpleNamespace(MARKDOWN_V2="MarkdownV2", HTML="HTML")
    constants_mod = SimpleNamespace(ParseMode=parse_mode)
    telegram_mod = SimpleNamespace(Bot=lambda token: bot, constants=constants_mod)
    monkeypatch.setitem(sys.modules, "telegram", telegram_mod)
    monkeypatch.setitem(sys.modules, "telegram.constants", constants_mod)


class TestSendMessageTool:
    def test_cron_duplicate_target_is_skipped_and_explained(self):
        home = SimpleNamespace(chat_id="-1001")
        config, _telegram_cfg = _make_config()
        config.get_home_channel = lambda _platform: home

        with patch.dict(
            os.environ,
            {
                "HERMES_CRON_AUTO_DELIVER_PLATFORM": "telegram",
                "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "-1001",
            },
            clear=False,
        ), \
             patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "telegram",
                        "message": "hello",
                    }
                )
            )

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "cron_auto_delivery_duplicate_target"
        assert "final response" in result["note"]
        send_mock.assert_not_awaited()
        mirror_mock.assert_not_called()

    def test_cron_different_target_still_sends(self):
        config, telegram_cfg = _make_config()

        with patch.dict(
            os.environ,
            {
                "HERMES_CRON_AUTO_DELIVER_PLATFORM": "telegram",
                "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "-1001",
            },
            clear=False,
        ), \
             patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "telegram:-1002",
                        "message": "hello",
                    }
                )
            )

        assert result["success"] is True
        assert result.get("skipped") is not True
        send_mock.assert_awaited_once_with(
            Platform.TELEGRAM,
            telegram_cfg,
            "-1002",
            "hello",
            thread_id=None,
            media_files=[],
        )
        mirror_mock.assert_called_once_with("telegram", "-1002", "hello", source_label="cli", thread_id=None)

    def test_cron_same_chat_different_thread_still_sends(self):
        config, telegram_cfg = _make_config()

        with patch.dict(
            os.environ,
            {
                "HERMES_CRON_AUTO_DELIVER_PLATFORM": "telegram",
                "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "-1001",
                "HERMES_CRON_AUTO_DELIVER_THREAD_ID": "17585",
            },
            clear=False,
        ), \
             patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "telegram:-1001:99999",
                        "message": "hello",
                    }
                )
            )

        assert result["success"] is True
        assert result.get("skipped") is not True
        send_mock.assert_awaited_once_with(
            Platform.TELEGRAM,
            telegram_cfg,
            "-1001",
            "hello",
            thread_id="99999",
            media_files=[],
        )
        mirror_mock.assert_called_once_with("telegram", "-1001", "hello", source_label="cli", thread_id="99999")

    def test_sends_to_explicit_telegram_topic_target(self):
        config, telegram_cfg = _make_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "telegram:-1001:17585",
                        "message": "hello",
                    }
                )
            )

        assert result["success"] is True
        send_mock.assert_awaited_once_with(
            Platform.TELEGRAM,
            telegram_cfg,
            "-1001",
            "hello",
            thread_id="17585",
            media_files=[],
        )
        mirror_mock.assert_called_once_with("telegram", "-1001", "hello", source_label="cli", thread_id="17585")

    def test_resolved_telegram_topic_name_preserves_thread_id(self):
        config, telegram_cfg = _make_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("gateway.channel_directory.resolve_channel_name", return_value="-1001:17585"), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "telegram:Coaching Chat / topic 17585",
                        "message": "hello",
                    }
                )
            )

        assert result["success"] is True
        send_mock.assert_awaited_once_with(
            Platform.TELEGRAM,
            telegram_cfg,
            "-1001",
            "hello",
            thread_id="17585",
            media_files=[],
        )

    def test_media_only_message_uses_placeholder_for_mirroring(self):
        config, telegram_cfg = _make_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
            result = json.loads(
                send_message_tool(
                    {
                        "action": "send",
                        "target": "telegram:-1001",
                        "message": "MEDIA:/tmp/example.ogg",
                    }
                )
            )

        assert result["success"] is True
        send_mock.assert_awaited_once_with(
            Platform.TELEGRAM,
            telegram_cfg,
            "-1001",
            "",
            thread_id=None,
            media_files=[("/tmp/example.ogg", False)],
        )
        mirror_mock.assert_called_once_with(
            "telegram",
            "-1001",
            "[Sent audio attachment]",
            source_label="cli",
            thread_id=None,
        )


class TestSendTelegramMediaDelivery:
    def test_sends_text_then_photo_for_media_tag(self, tmp_path, monkeypatch):
        image_path = tmp_path / "photo.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
        bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=2))
        bot.send_video = AsyncMock()
        bot.send_voice = AsyncMock()
        bot.send_audio = AsyncMock()
        bot.send_document = AsyncMock()
        _install_telegram_mock(monkeypatch, bot)

        result = asyncio.run(
            _send_telegram(
                "token",
                "12345",
                "Hello there",
                media_files=[(str(image_path), False)],
            )
        )

        assert result["success"] is True
        assert result["message_id"] == "2"
        bot.send_message.assert_awaited_once()
        bot.send_photo.assert_awaited_once()
        sent_text = bot.send_message.await_args.kwargs["text"]
        assert "MEDIA:" not in sent_text
        assert sent_text == "Hello there"

    def test_sends_voice_for_ogg_with_voice_directive(self, tmp_path, monkeypatch):
        voice_path = tmp_path / "voice.ogg"
        voice_path.write_bytes(b"OggS" + b"\x00" * 32)

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_video = AsyncMock()
        bot.send_voice = AsyncMock(return_value=SimpleNamespace(message_id=7))
        bot.send_audio = AsyncMock()
        bot.send_document = AsyncMock()
        _install_telegram_mock(monkeypatch, bot)

        result = asyncio.run(
            _send_telegram(
                "token",
                "12345",
                "",
                media_files=[(str(voice_path), True)],
            )
        )

        assert result["success"] is True
        bot.send_voice.assert_awaited_once()
        bot.send_audio.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    def test_sends_audio_for_mp3(self, tmp_path, monkeypatch):
        audio_path = tmp_path / "clip.mp3"
        audio_path.write_bytes(b"ID3" + b"\x00" * 32)

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_video = AsyncMock()
        bot.send_voice = AsyncMock()
        bot.send_audio = AsyncMock(return_value=SimpleNamespace(message_id=8))
        bot.send_document = AsyncMock()
        _install_telegram_mock(monkeypatch, bot)

        result = asyncio.run(
            _send_telegram(
                "token",
                "12345",
                "",
                media_files=[(str(audio_path), False)],
            )
        )

        assert result["success"] is True
        bot.send_audio.assert_awaited_once()
        bot.send_voice.assert_not_awaited()

    def test_missing_media_returns_error_without_leaking_raw_tag(self, monkeypatch):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.send_photo = AsyncMock()
        bot.send_video = AsyncMock()
        bot.send_voice = AsyncMock()
        bot.send_audio = AsyncMock()
        bot.send_document = AsyncMock()
        _install_telegram_mock(monkeypatch, bot)

        result = asyncio.run(
            _send_telegram(
                "token",
                "12345",
                "",
                media_files=[("/tmp/does-not-exist.png", False)],
            )
        )

        assert "error" in result
        assert "No deliverable text or media remained" in result["error"]
        bot.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# Regression: long messages are chunked before platform dispatch
# ---------------------------------------------------------------------------


class TestSendToPlatformChunking:
    def test_long_message_is_chunked(self):
        """Messages exceeding the platform limit are split into multiple sends."""
        send = AsyncMock(return_value={"success": True, "message_id": "1"})
        long_msg = "word " * 1000  # ~5000 chars, well over Discord's 2000 limit
        with patch("tools.send_message_tool._send_discord", send):
            result = asyncio.run(
                _send_to_platform(
                    Platform.DISCORD,
                    SimpleNamespace(enabled=True, token="tok", extra={}),
                    "ch", long_msg,
                )
            )
        assert result["success"] is True
        assert send.await_count >= 3
        for call in send.await_args_list:
            assert len(call.args[2]) <= 2020  # each chunk fits the limit

    def test_telegram_media_attaches_to_last_chunk(self):
        """When chunked, media files are sent only with the last chunk."""
        sent_calls = []

        async def fake_send(token, chat_id, message, media_files=None, thread_id=None):
            sent_calls.append(media_files or [])
            return {"success": True, "platform": "telegram", "chat_id": chat_id, "message_id": str(len(sent_calls))}

        long_msg = "word " * 2000  # ~10000 chars, well over 4096
        media = [("/tmp/photo.png", False)]
        with patch("tools.send_message_tool._send_telegram", fake_send):
            asyncio.run(
                _send_to_platform(
                    Platform.TELEGRAM,
                    SimpleNamespace(enabled=True, token="tok", extra={}),
                    "123", long_msg, media_files=media,
                )
            )
        assert len(sent_calls) >= 3
        assert all(call == [] for call in sent_calls[:-1])
        assert sent_calls[-1] == media


# ---------------------------------------------------------------------------
# HTML auto-detection in Telegram send
# ---------------------------------------------------------------------------


class TestSendToPlatformWhatsapp:
    def test_whatsapp_routes_via_local_bridge_sender(self):
        chat_id = "test-user@lid"
        async_mock = AsyncMock(return_value={"success": True, "platform": "whatsapp", "chat_id": chat_id, "message_id": "abc123"})

        with patch("tools.send_message_tool._send_whatsapp", async_mock):
            result = asyncio.run(
                _send_to_platform(
                    Platform.WHATSAPP,
                    SimpleNamespace(enabled=True, token=None, extra={"bridge_port": 3000}),
                    chat_id,
                    "hello from hermes",
                )
            )

        assert result["success"] is True
        async_mock.assert_awaited_once_with({"bridge_port": 3000}, chat_id, "hello from hermes")


class TestSendTelegramHtmlDetection:
    """Verify that messages containing HTML tags are sent with parse_mode=HTML
    and that plain / markdown messages use MarkdownV2."""

    def _make_bot(self):
        bot = MagicMock()
        bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=1))
        bot.send_photo = AsyncMock()
        bot.send_video = AsyncMock()
        bot.send_voice = AsyncMock()
        bot.send_audio = AsyncMock()
        bot.send_document = AsyncMock()
        return bot

    def test_html_message_uses_html_parse_mode(self, monkeypatch):
        bot = self._make_bot()
        _install_telegram_mock(monkeypatch, bot)

        asyncio.run(
            _send_telegram("tok", "123", "<b>Hello</b> world")
        )

        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["parse_mode"] == "HTML"
        assert kwargs["text"] == "<b>Hello</b> world"

    def test_plain_text_uses_markdown_v2(self, monkeypatch):
        bot = self._make_bot()
        _install_telegram_mock(monkeypatch, bot)

        asyncio.run(
            _send_telegram("tok", "123", "Just plain text, no tags")
        )

        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["parse_mode"] == "MarkdownV2"

    def test_html_with_code_and_pre_tags(self, monkeypatch):
        bot = self._make_bot()
        _install_telegram_mock(monkeypatch, bot)

        html = "<pre>code block</pre> and <code>inline</code>"
        asyncio.run(_send_telegram("tok", "123", html))

        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["parse_mode"] == "HTML"

    def test_closing_tag_detected(self, monkeypatch):
        bot = self._make_bot()
        _install_telegram_mock(monkeypatch, bot)

        asyncio.run(_send_telegram("tok", "123", "text </div> more"))

        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["parse_mode"] == "HTML"

    def test_angle_brackets_in_math_not_detected(self, monkeypatch):
        """Expressions like 'x < 5' or '3 > 2' should not trigger HTML mode."""
        bot = self._make_bot()
        _install_telegram_mock(monkeypatch, bot)

        asyncio.run(_send_telegram("tok", "123", "if x < 5 then y > 2"))

        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["parse_mode"] == "MarkdownV2"

    def test_html_parse_failure_falls_back_to_plain(self, monkeypatch):
        """If Telegram rejects the HTML, fall back to plain text."""
        bot = self._make_bot()
        bot.send_message = AsyncMock(
            side_effect=[
                Exception("Bad Request: can't parse entities: unsupported html tag"),
                SimpleNamespace(message_id=2),  # plain fallback succeeds
            ]
        )
        _install_telegram_mock(monkeypatch, bot)

        result = asyncio.run(
            _send_telegram("tok", "123", "<invalid>broken html</invalid>")
        )

        assert result["success"] is True
        assert bot.send_message.await_count == 2
        second_call = bot.send_message.await_args_list[1].kwargs
        assert second_call["parse_mode"] is None


# ---------------------------------------------------------------------------
# Zulip target parsing
# ---------------------------------------------------------------------------


class TestParseZulipTargetRef:
    """Verify _parse_zulip_target_ref handles all canonical chat-ID formats."""

    def test_dm_explicit_prefix(self):
        chat_id, thread_id, is_explicit = _parse_zulip_target_ref("dm:user@example.com")
        assert chat_id == "dm:user@example.com"
        assert thread_id is None
        assert is_explicit is True

    def test_dm_explicit_prefix_with_subaddress(self):
        chat_id, _, is_explicit = _parse_zulip_target_ref("dm:user+tag@example.com")
        assert chat_id == "dm:user+tag@example.com"
        assert is_explicit is True

    def test_group_dm_explicit_prefix(self):
        chat_id, _, is_explicit = _parse_zulip_target_ref("group_dm:a@b.com,c@d.com")
        assert chat_id == "group_dm:a@b.com,c@d.com"
        assert is_explicit is True

    def test_group_dm_three_participants(self):
        chat_id, _, is_explicit = _parse_zulip_target_ref("group_dm:a@b.com,c@d.com,e@f.com")
        assert chat_id == "group_dm:a@b.com,c@d.com,e@f.com"
        assert is_explicit is True

    def test_stream_numeric_id_with_topic(self):
        chat_id, _, is_explicit = _parse_zulip_target_ref("123:General")
        assert chat_id == "123:General"
        assert is_explicit is True

    def test_stream_numeric_id_with_spaces_in_topic(self):
        chat_id, _, is_explicit = _parse_zulip_target_ref("42:Some Topic Here")
        assert chat_id == "42:Some Topic Here"
        assert is_explicit is True

    def test_implicit_dm_bare_email(self):
        chat_id, _, is_explicit = _parse_zulip_target_ref("user@example.com")
        assert chat_id == "dm:user@example.com"
        assert is_explicit is True

    def test_stream_name_is_not_explicit(self):
        chat_id, _, is_explicit = _parse_zulip_target_ref("#general")
        assert chat_id is None
        assert is_explicit is False

    def test_stream_name_with_topic_is_not_explicit(self):
        chat_id, _, is_explicit = _parse_zulip_target_ref("#general:My Topic")
        assert chat_id is None
        assert is_explicit is False

    def test_empty_string_not_explicit(self):
        chat_id, _, is_explicit = _parse_zulip_target_ref("")
        assert chat_id is None
        assert is_explicit is False


class TestParseTargetRefDispatchesZulip:
    """Verify that _parse_target_ref delegates to _parse_zulip_target_ref."""

    def test_zulip_dm_through_main_parser(self):
        chat_id, thread_id, is_explicit = _parse_target_ref("zulip", "dm:user@example.com")
        assert chat_id == "dm:user@example.com"
        assert is_explicit is True

    def test_zulip_stream_through_main_parser(self):
        chat_id, _, is_explicit = _parse_target_ref("zulip", "99:Announcements")
        assert chat_id == "99:Announcements"
        assert is_explicit is True

    def test_zulip_stream_name_not_explicit_through_main_parser(self):
        chat_id, _, is_explicit = _parse_target_ref("zulip", "#general")
        assert chat_id is None
        assert is_explicit is False

    def test_non_zulip_unaffected(self):
        # Telegram parsing still works as before
        chat_id, thread_id, is_explicit = _parse_target_ref("telegram", "-1001:17585")
        assert chat_id == "-1001"
        assert thread_id == "17585"
        assert is_explicit is True


# ---------------------------------------------------------------------------
# Zulip _send_to_platform routing
# ---------------------------------------------------------------------------


class TestSendToPlatformZulip:
    """Verify _send_to_platform routes Zulip correctly."""

    def _make_zulip_pconfig(self):
        return SimpleNamespace(
            enabled=True,
            token="zulip-api-key",
            extra={
                "site_url": "https://example.zulipchat.com",
                "bot_email": "hermes-bot@example.com",
            },
        )

    def test_zulip_routes_to_send_zulip(self):
        pconfig = self._make_zulip_pconfig()
        async_mock = AsyncMock(
            return_value={"success": True, "platform": "zulip", "chat_id": "123:General", "message_id": "42"}
        )

        with patch("tools.send_message_tool._send_zulip", async_mock) as mock:
            result = asyncio.run(
                _send_to_platform(Platform.ZULIP, pconfig, "123:General", "hello zulip")
            )

        assert result["success"] is True
        mock.assert_awaited_once_with(pconfig, "123:General", "hello zulip")

    def test_zulip_long_message_is_chunked(self):
        pconfig = self._make_zulip_pconfig()
        # Zulip MAX_MESSAGE_LENGTH = 4000
        long_msg = "word " * 1500  # ~7500 chars, well over 4000
        call_count = 0

        async def fake_send(pc, chat_id, message):
            nonlocal call_count
            call_count += 1
            assert len(message) <= 4000
            return {"success": True, "platform": "zulip", "chat_id": chat_id, "message_id": str(call_count)}

        with patch("tools.send_message_tool._send_zulip", fake_send):
            result = asyncio.run(
                _send_to_platform(Platform.ZULIP, pconfig, "123:General", long_msg)
            )

        assert result["success"] is True
        assert call_count >= 2


# ---------------------------------------------------------------------------
# Zulip _send_zulip standalone helper
# ---------------------------------------------------------------------------


class TestSendZulipStandalone:
    """Verify _send_zulip builds correct Zulip API requests."""

    def test_sends_stream_message(self, monkeypatch):
        client = MagicMock()
        client.send_message = MagicMock(return_value={"result": "success", "id": 42})

        zulip_mod = SimpleNamespace(Client=lambda **kw: client)
        monkeypatch.setitem(sys.modules, "zulip", zulip_mod)

        pconfig = SimpleNamespace(
            token="key123",
            extra={"site_url": "https://chat.example.com", "bot_email": "bot@example.com"},
        )

        result = asyncio.run(_send_zulip(pconfig, "99:General", "Hello stream"))

        assert result["success"] is True
        assert result["platform"] == "zulip"
        assert result["message_id"] == "42"
        client.send_message.assert_called_once_with({
            "type": "stream",
            "to": "99",
            "topic": "General",
            "content": "Hello stream",
        })

    def test_sends_dm(self, monkeypatch):
        client = MagicMock()
        client.send_message = MagicMock(return_value={"result": "success", "id": 55})

        zulip_mod = SimpleNamespace(Client=lambda **kw: client)
        monkeypatch.setitem(sys.modules, "zulip", zulip_mod)

        pconfig = SimpleNamespace(
            token="key123",
            extra={"site_url": "https://chat.example.com", "bot_email": "bot@example.com"},
        )

        result = asyncio.run(_send_zulip(pconfig, "dm:user@example.com", "Hello DM"))

        assert result["success"] is True
        assert result["message_id"] == "55"
        client.send_message.assert_called_once_with({
            "type": "private",
            "to": ["user@example.com"],
            "content": "Hello DM",
        })

    def test_sends_group_dm(self, monkeypatch):
        client = MagicMock()
        client.send_message = MagicMock(return_value={"result": "success", "id": 60})

        zulip_mod = SimpleNamespace(Client=lambda **kw: client)
        monkeypatch.setitem(sys.modules, "zulip", zulip_mod)

        pconfig = SimpleNamespace(
            token="key123",
            extra={"site_url": "https://chat.example.com", "bot_email": "bot@example.com"},
        )

        result = asyncio.run(
            _send_zulip(pconfig, "group_dm:a@b.com,c@d.com", "Hello group")
        )

        assert result["success"] is True
        client.send_message.assert_called_once_with({
            "type": "private",
            "to": ["a@b.com", "c@d.com"],
            "content": "Hello group",
        })

    def test_returns_error_on_api_failure(self, monkeypatch):
        client = MagicMock()
        client.send_message = MagicMock(return_value={"result": "error", "msg": "Stream not found"})

        zulip_mod = SimpleNamespace(Client=lambda **kw: client)
        monkeypatch.setitem(sys.modules, "zulip", zulip_mod)

        pconfig = SimpleNamespace(
            token="key123",
            extra={"site_url": "https://chat.example.com", "bot_email": "bot@example.com"},
        )

        result = asyncio.run(_send_zulip(pconfig, "999:Missing", "test"))

        assert result.get("error") is not None
        assert "Stream not found" in result["error"]

    def test_returns_error_when_missing_config(self, monkeypatch):
        zulip_mod = SimpleNamespace(Client=lambda **kw: MagicMock())
        monkeypatch.setitem(sys.modules, "zulip", zulip_mod)

        # Missing site_url
        pconfig = SimpleNamespace(token="key", extra={"site_url": "", "bot_email": "bot@x.com"})
        result = asyncio.run(_send_zulip(pconfig, "dm:user@x.com", "test"))
        assert "not fully configured" in result["error"]

    def test_fallback_treats_unknown_chat_id_as_email(self, monkeypatch):
        """When chat_id doesn't match any known format, treat as a bare email."""
        client = MagicMock()
        client.send_message = MagicMock(return_value={"result": "success", "id": 70})

        zulip_mod = SimpleNamespace(Client=lambda **kw: client)
        monkeypatch.setitem(sys.modules, "zulip", zulip_mod)

        pconfig = SimpleNamespace(
            token="key123",
            extra={"site_url": "https://chat.example.com", "bot_email": "bot@example.com"},
        )

        # chat_id that is neither stream nor dm nor group_dm format
        result = asyncio.run(_send_zulip(pconfig, "unknown-user@example.com", "fallback test"))

        assert result["success"] is True
        client.send_message.assert_called_once_with({
            "type": "private",
            "to": ["unknown-user@example.com"],
            "content": "fallback test",
        })


# ---------------------------------------------------------------------------
# Zulip end-to-end routing via send_message_tool
# ---------------------------------------------------------------------------


class TestSendMessageToolZulip:
    """Verify send_message_tool routes Zulip targets end-to-end."""

    def _make_zulip_config(self, home_channel=None):
        zulip_cfg = SimpleNamespace(
            enabled=True,
            token="zulip-key",
            extra={
                "site_url": "https://chat.example.com",
                "bot_email": "bot@example.com",
            },
        )
        config = SimpleNamespace(
            platforms={Platform.ZULIP: zulip_cfg},
            get_home_channel=lambda _platform: home_channel,
        )
        return config, zulip_cfg

    def test_sends_to_explicit_zulip_stream_target(self):
        config, zulip_cfg = self._make_zulip_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "zulip:123:General",
                    "message": "hello zulip",
                })
            )

        assert result["success"] is True
        send_mock.assert_awaited_once_with(
            Platform.ZULIP,
            zulip_cfg,
            "123:General",
            "hello zulip",
            thread_id=None,
            media_files=[],
        )
        mirror_mock.assert_called_once_with(
            "zulip", "123:General", "hello zulip",
            source_label="cli", thread_id=None,
        )

    def test_sends_to_explicit_zulip_dm_target(self):
        config, zulip_cfg = self._make_zulip_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "zulip:dm:user@example.com",
                    "message": "hello DM",
                })
            )

        assert result["success"] is True
        send_mock.assert_awaited_once_with(
            Platform.ZULIP,
            zulip_cfg,
            "dm:user@example.com",
            "hello DM",
            thread_id=None,
            media_files=[],
        )

    def test_sends_to_home_channel_when_no_target_specified(self):
        home = SimpleNamespace(chat_id="42:Home")
        config, zulip_cfg = self._make_zulip_config(home_channel=home)

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock:
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "zulip",
                    "message": "hello home",
                })
            )

        assert result["success"] is True
        assert "home channel" in result.get("note", "")
        send_mock.assert_awaited_once_with(
            Platform.ZULIP,
            zulip_cfg,
            "42:Home",
            "hello home",
            thread_id=None,
            media_files=[],
        )

    def test_implicit_dm_bare_email(self):
        """A bare email as Zulip target is treated as an implicit DM."""
        config, zulip_cfg = self._make_zulip_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock:
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "zulip:person@example.com",
                    "message": "quick DM",
                })
            )

        assert result["success"] is True
        # The implicit DM should produce chat_id "dm:person@example.com"
        send_mock.assert_awaited_once_with(
            Platform.ZULIP,
            zulip_cfg,
            "dm:person@example.com",
            "quick DM",
            thread_id=None,
            media_files=[],
        )

    def test_sends_to_explicit_group_dm_target(self):
        """zulip:group_dm:a@b.com,c@d.com should route to the group DM sender."""
        config, zulip_cfg = self._make_zulip_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock:
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "zulip:group_dm:a@b.com,c@d.com",
                    "message": "group hello",
                })
            )

        assert result["success"] is True
        send_mock.assert_awaited_once_with(
            Platform.ZULIP,
            zulip_cfg,
            "group_dm:a@b.com,c@d.com",
            "group hello",
            thread_id=None,
            media_files=[],
        )

    def test_no_home_channel_returns_error(self):
        """Sending to bare 'zulip' without a home channel should return an error."""
        config, zulip_cfg = self._make_zulip_config(home_channel=None)

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately):
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "zulip",
                    "message": "hello",
                })
            )

        assert result.get("success") is not True
        assert "No home channel" in result.get("error", "")

    def test_cron_duplicate_zulip_target_is_skipped(self):
        """Cron duplicate skip should work for Zulip targets."""
        home = SimpleNamespace(chat_id="42:Home")
        config, zulip_cfg = self._make_zulip_config(home_channel=home)

        with patch.dict(
            os.environ,
            {
                "HERMES_CRON_AUTO_DELIVER_PLATFORM": "zulip",
                "HERMES_CRON_AUTO_DELIVER_CHAT_ID": "42:Home",
            },
            clear=False,
        ), \
             patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})) as send_mock, \
             patch("gateway.mirror.mirror_to_session", return_value=True) as mirror_mock:
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "zulip",
                    "message": "cron duplicate check",
                })
            )

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reason"] == "cron_auto_delivery_duplicate_target"
        send_mock.assert_not_awaited()
        mirror_mock.assert_not_called()

    def test_zulip_media_files_produce_warning(self):
        """Non-Telegram media attachments should produce a warning, not error."""
        config, zulip_cfg = self._make_zulip_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("gateway.mirror.mirror_to_session", return_value=True), \
             patch("tools.send_message_tool._send_zulip", new=AsyncMock(return_value={"success": True})) as send_zulip_mock:
            # _send_to_platform routes to _send_zulip; media files are passed but
            # Zulip doesn't support native media delivery — a warning should appear.
            result = asyncio.run(
                _send_to_platform(
                    Platform.ZULIP,
                    zulip_cfg,
                    "123:General",
                    "text with media",
                    media_files=[("/tmp/photo.png", False)],
                )
            )

        assert result["success"] is True
        assert any("MEDIA" in w for w in result.get("warnings", []))

    def test_zulip_unknown_platform_rejected(self):
        """A gibberish platform name should be rejected with available list."""
        config, _ = self._make_zulip_config()

        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately):
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "notaplatform:123",
                    "message": "hello",
                })
            )

        assert result.get("success") is not True
        assert "Unknown platform" in result.get("error", "")
        assert "zulip" in result.get("error", "")
