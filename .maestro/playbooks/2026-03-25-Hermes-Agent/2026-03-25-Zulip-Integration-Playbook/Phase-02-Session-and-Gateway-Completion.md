# Phase 02: Session Semantics and Gateway Completion

Expand the prototype into a first-class Zulip gateway adapter by finishing the platform-specific behavior Hermes depends on day to day: mention handling, stream/topic session identity, authorization, channel discovery, and resilient event processing. This phase matters because Hermes should now behave like a real Zulip bot in shared conversations, not just a mocked transport layer.

## Tasks

- [x] Harden inbound message handling around Zulip's real event shapes:
  - Inspect the official Zulip event payloads and compare them with the assumptions made in `gateway/platforms/zulip.py`
  - Support direct messages, stream messages, and topic-based sessions consistently, including helpers that convert between Zulip payloads and Hermes `SessionSource` / `chat_id` expectations
  - Filter self-messages, bot echoes, empty-content messages, and any unsupported event types without spamming logs
  - Notes: Compared the official Zulip event API (GET /api/v1/events) against adapter assumptions. Key findings: (1) `call_on_each_event` with `event_types=["message"]` only delivers message events server-side, but added defense-in-depth `type` and `op` validation in `_on_zulip_event`; (2) Zulip's `display_recipient` for streams is a string (modern) or dict with `name` key (legacy) — added `_resolve_stream_name()` helper that checks cache first, then falls back to `display_recipient`; (3) private messages with 3+ participants (group DMs) now use `_build_group_dm_chat_id()` with sorted emails for deterministic chat-ids; (4) `_extract_dm_recipients()` helper centralizes DM recipient parsing with graceful fallbacks for malformed payloads; (5) whitespace-only content is now filtered early before type-specific logic; (6) all unsupported event types and non-"add" ops are logged at DEBUG level only. Added 36 new tests covering: event validation (7), whitespace content filtering (3), group DM helpers (7), DM recipient extraction (6), stream name resolution (6), group DM dispatch (3), display_recipient fallback (3), and missing/empty subject handling (2). All 122 tests pass.

- [x] Implement the final session model for Zulip stream + topic conversations:
  - Keep Hermes sessions isolated per `stream + topic`, with a deterministic chat-id encoding that survives restarts and round-trips through config, send paths, and stored sessions
  - Reuse existing gateway session conventions from `gateway/session.py` and existing topic-aware tests before introducing any Zulip-specific metadata changes
  - Only extend `SessionSource` if verified code paths truly need more than `chat_id`, `chat_name`, `chat_topic`, and `user_id`
  - Notes: Verified that the existing chat-ID encoding from Phase 1 (`stream_id:topic`, `dm:email`, `group_dm:sorted_emails`) provides full session isolation through `build_session_key()` in `gateway/session.py`. Stream sessions produce keys like `agent:main:zulip:stream:42:general:alice@example.com` (isolated per stream+topic+user), DM sessions like `agent:main:zulip:dm:dm:alice@example.com`, and group DM sessions like `agent:main:zulip:group:group_dm:alice@example.com,bob@example.com:alice@example.com`. No `SessionSource` extension was needed — the existing fields (`chat_id`, `chat_name`, `chat_topic`, `user_id`, `chat_type`) are sufficient because: (1) stream_id:topic encoding carries all identity in `chat_id`; (2) topic is duplicated in `chat_topic` for the system prompt; (3) `chat_type` distinguishes stream/dm/group for session key construction; (4) serialization round-trips (`to_dict`/`from_dict`) preserve all chat-id formats including colons in topics. Fixed a bug where `_do_send_message` didn't handle group DM chat IDs (fell through to wrong fallback sending to the raw chat-id string). Added 3 new test classes: `TestGroupDmSendPath` (5 tests — send path for group DMs), `TestZulipSessionKeyIntegration` (7 tests — session key isolation/determinism for all Zulip chat types), `TestZulipSessionSourceSerialization` (5 tests — serialization round-trips). All 70 Zulip tests pass, all session tests pass (48), all channel_directory tests pass (27).

- [ ] Finish authorization and conversation-entry behavior for Zulip:
  - Make sure `gateway/run.py` authorization covers `ZULIP_ALLOWED_USERS` and `ZULIP_ALLOW_ALL_USERS`
  - Implement the expected inbound trigger rules in the adapter: DMs should work directly; shared streams should respect mentions or any explicitly configured stream/topic behavior you can verify from existing platform patterns and the discovery spec
  - Keep DM pairing / unauthorized behavior aligned with how other gateway platforms behave; reuse existing gateway helpers instead of inventing Zulip-only flows

- [ ] Add Zulip to session-based discovery and platform visibility surfaces:
  - Update `gateway/channel_directory.py` so Zulip chats can be rediscovered from session history when direct platform enumeration is not available or not worth the extra API cost
  - Verify whether the directory should show `stream / topic` display names similar to Telegram topic naming, and implement the same display shape everywhere it appears
  - Update any shared gateway/platform listings that still omit Zulip after the prototype phase

- [ ] Strengthen connection lifecycle and observability in `gateway/platforms/zulip.py`:
  - Add exponential backoff + jitter for event queue failures and reconnects, following the style used by long-lived platform adapters like Mattermost
  - Clean up background polling tasks on disconnect and avoid orphaned tasks during shutdown
  - Keep logging useful but redactable, with concise identifiers instead of dumping raw payloads

- [ ] Add focused regression tests for the completed gateway behavior:
  - Extend `tests/gateway/test_zulip.py` with authorization, mention/trigger gating, chat-id round-trips, reconnect/backoff behavior, and any session-history directory cases introduced for Zulip
  - Update `tests/gateway/test_channel_directory.py` or other shared gateway tests only where Zulip now belongs in common platform lists

- [ ] Verify the completed gateway behavior before moving on:
  - Run `source venv/bin/activate && python -m pytest tests/gateway/test_zulip.py tests/gateway/test_channel_directory.py tests/gateway/test_status.py -q`
  - Re-run `source venv/bin/activate && python -m compileall gateway hermes_cli tests`
  - Manually review the adapter for dead helpers, duplicate parsing code, and any unhandled shutdown paths before starting outbound-media/tool work
