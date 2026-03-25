# Phase 03: Delivery Tools, Media, and User-Facing Docs

Turn the Zulip adapter into a full Hermes platform by completing outbound delivery outside the live gateway process, adding the best-supported media flows, and updating every user-facing surface that claims which platforms Hermes supports. This phase matters because Zulip should now work not just for live chat replies, but also for `send_message`, cron delivery, setup, and documentation.

## Tasks

- [x] Complete standalone outbound delivery support for Zulip:
  - Update `tools/send_message_tool.py` so Zulip is a first-class `target` value, following the same routing structure as Mattermost, Matrix, and Telegram topic support
  - Implement a standalone `_send_zulip(...)` helper that can send to a home destination, a DM target, or a `stream:topic` destination without requiring the live gateway adapter instance
  - Make sure target parsing is deterministic and reuse the adapter’s chat-id parsing helpers where practical instead of duplicating string logic

- [x] Add Zulip to scheduled delivery and tool schemas:
  - Update `cron/scheduler.py` platform delivery mapping so cron jobs can deliver to Zulip
  - Update `tools/cronjob_tools.py` descriptions/examples to mention Zulip wherever delivery options are enumerated
  - Verify home-channel fallback behavior for Zulip using `ZULIP_DEFAULT_STREAM` and `ZULIP_HOME_TOPIC`, and keep the behavior explicit in code comments/tests

- [x] Implement the supported rich-delivery methods in `gateway/platforms/zulip.py`:
  - Add `send_typing`, `send_image`, and any feasible document/voice helpers using the Zulip client upload/send APIs
  - Reuse `cache_image_from_bytes`, `cache_document_from_bytes`, and related base-adapter helpers where inbound attachments are available
  - Keep scope tight: implement what Zulip natively supports well, and document graceful fallbacks for anything Hermes supports elsewhere but Zulip cannot represent cleanly
  - **Notes:**
    - `send_typing` was already implemented (uses `client.set_typing_status`)
    - Added `_upload_file()` internal helper that wraps `client.upload_file()` with `BytesIO` for clean file uploads
    - Added `send_image()` — downloads URL, uploads to Zulip, sends as `![alt](/user_uploads/...)` for inline rendering; falls back to URL-as-text on failure
    - Added `send_image_file()` — uploads local image file, sends inline via same markdown-image pattern
    - Added `send_document()` — uploads file, sends as `[filename](/user_uploads/...)` markdown link with optional caption
    - Added `send_video()` — uploads video file as downloadable link (Zulip does not inline video playback)
    - `send_voice` intentionally NOT overridden — Zulip has no native voice bubble support; base class fallback sends file path as text
    - Added imports for `io`, `mimetypes`, `Path`, `cache_image_from_bytes`, `cache_document_from_bytes` for future inbound-media caching use
    - All 178 existing Zulip tests pass with no regressions

- [x] Update setup, status, and environment-reference surfaces to match the completed feature set:
  - Extend `hermes_cli/gateway.py` prompts/help text only where the finished Zulip feature set requires clearer setup guidance
  - Update `website/docs/reference/environment-variables.md` with all Zulip env vars
  - Update `README.md`, `AGENTS.md`, and `website/docs/user-guide/messaging/index.md` anywhere platform lists, docs tables, diagrams, or security examples should now include Zulip
  - **Notes:**
    - `hermes_cli/gateway.py`: Enhanced Zulip setup instructions to mention `pip install zulip`, mention-gating configuration vars, media delivery support (images/documents/video), and cron delivery via `ZULIP_HOME_CHANNEL` or `deliver='zulip:stream_id:topic'`
    - `environment-variables.md`: Added all 10 Zulip env vars (ZULIP_SITE_URL, ZULIP_BOT_EMAIL, ZULIP_API_KEY, ZULIP_ALLOWED_USERS, ZULIP_ALLOW_ALL_USERS, ZULIP_DEFAULT_STREAM, ZULIP_HOME_TOPIC, ZULIP_HOME_CHANNEL, ZULIP_REQUIRE_MENTION, ZULIP_FREE_RESPONSE_STREAMS) in the Messaging section between Matrix and Home Assistant entries
    - `messaging/index.md`: Added Zulip to frontmatter description, intro paragraph, Mermaid architecture diagram (node + edge), Security allowlist examples, Platform-Specific Toolsets table (with image/document/video delivery note), and Next Steps links (pointing to future zulip.md guide)
    - `README.md`: Added Zulip to "Lives where you do" feature row and Messaging Gateway docs table
    - `AGENTS.md`: Added `zulip` to the platforms/ directory comment in the project structure

- [x] Create the dedicated Zulip setup guide at `website/docs/user-guide/messaging/zulip.md`:
   - Use `website/docs/user-guide/messaging/mattermost.md` as the starting pattern, but rewrite for Zulip's actual bot creation, stream/topic behavior, auth model, and home-destination concepts
   - Include working examples for DM use, stream mention use, allowlists, cron delivery, and home stream/topic configuration
   - Keep the guide practical and minimal; avoid promising features the adapter does not implement yet
   - **Notes:**
     - Followed the Mattermost guide structure: frontmatter, intro, "How Hermes Behaviors" table, session model, step-by-step setup (bot creation, stream subscription, configuration), home channel, mention gating, cross-platform sending, cron delivery, media delivery, troubleshooting, security, and notes sections
     - Auth section covers Zulip-specific bot creation via Settings → Your bots → Generic bot, with API key handling
     - Stream subscription step added (Zulip-specific requirement unlike Mattermost's channel membership)
     - Mention gating section covers `ZULIP_REQUIRE_MENTION` and `ZULIP_FREE_RESPONSE_STREAMS` with practical examples
     - All 10 Zulip env vars documented: `ZULIP_SITE_URL`, `ZULIP_BOT_EMAIL`, `ZULIP_API_KEY`, `ZULIP_ALLOWED_USERS`, `ZULIP_ALLOW_ALL_USERS`, `ZULIP_DEFAULT_STREAM`, `ZULIP_HOME_TOPIC`, `ZULIP_HOME_CHANNEL`, `ZULIP_REQUIRE_MENTION`, `ZULIP_FREE_RESPONSE_STREAMS`
     - Media delivery table documents what works (images inline, documents as links, video as links) and what doesn't (voice messages)
     - Cron delivery section explains `ZULIP_HOME_CHANNEL` and `deliver='zulip:stream_id:topic'` syntax
     - Troubleshooting covers: not responding, 401 errors, stream mention gating, missing zulip package, event queue reconnects, offline bot, and user not allowed
     - Voice messages explicitly documented as not supported with explanation

- [x] Add tests for outbound delivery and platform-wide integration points:
  - Extend `tests/tools/test_send_message_tool.py` with Zulip routing, target parsing, and home-channel fallback coverage
  - Extend `tests/gateway/test_zulip.py` with media/send helper coverage that can be verified with mocked Zulip client methods
  - Update any cron-delivery or shared-tool tests only where Zulip now belongs in canonical platform maps
  - **Notes:**
    - `test_send_message_tool.py`: Added 6 new tests to `TestSendMessageToolZulip` — group DM routing (`test_sends_to_explicit_group_dm_target`), home-channel error when not set (`test_no_home_channel_returns_error`), cron duplicate skip for Zulip (`test_cron_duplicate_zulip_target_is_skipped`), media warning for non-Telegram platforms (`test_zulip_media_files_produce_warning`), unknown platform rejection with Zulip in available list (`test_zulip_unknown_platform_rejected`)
    - `test_zulip.py`: Added 6 new test classes with 23 total tests — `TestZulipUploadFile` (5 tests: success, filename on BytesIO, failure, exception, no client), `TestZulipSendTyping` (4 tests: stream with cache, DM, no client, no cache), `TestZulipSendImage` (3 tests: success with download+upload, download failure fallback, upload failure fallback), `TestZulipSendImageFile` (3 tests: success, missing file, upload failure), `TestZulipSendDocument` (5 tests: success with caption, no caption, missing file, upload failure, custom filename), `TestZulipSendVideo` (3 tests: success, missing file, upload failure)
    - `test_delivery.py`: Added 5 new tests to `TestParseTargetPlatformChat` — Zulip platform-only parse, stream target parse, DM target parse, stream roundtrip (lowercase-aware); verified `DeliveryTarget.parse` correctly routes `zulip:*` targets
    - Cron-delivery tool descriptions (`cronjob_tools.py`) and scheduler (`scheduler.py`) already included Zulip from Phase 02 — no changes needed
    - All 271 tests pass across the three test files (`test_zulip.py`: 201 passed, `test_send_message_tool.py`: 50 passed, `test_delivery.py`: 20 passed)

- [ ] Run verification for tooling + docs-sensitive changes:
  - Run `source venv/bin/activate && python -m pytest tests/gateway/test_zulip.py tests/tools/test_send_message_tool.py tests/gateway/test_delivery.py -q`
  - Run `source venv/bin/activate && python -m compileall gateway tools hermes_cli tests`
  - Manually open the changed docs files and confirm every Zulip env var/example matches the implemented names exactly
