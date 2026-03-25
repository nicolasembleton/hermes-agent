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

- [ ] Update setup, status, and environment-reference surfaces to match the completed feature set:
  - Extend `hermes_cli/gateway.py` prompts/help text only where the finished Zulip feature set requires clearer setup guidance
  - Update `website/docs/reference/environment-variables.md` with all Zulip env vars
  - Update `README.md`, `AGENTS.md`, and `website/docs/user-guide/messaging/index.md` anywhere platform lists, docs tables, diagrams, or security examples should now include Zulip

- [ ] Create the dedicated Zulip setup guide at `website/docs/user-guide/messaging/zulip.md`:
  - Use `website/docs/user-guide/messaging/mattermost.md` as the starting pattern, but rewrite for Zulip’s actual bot creation, stream/topic behavior, auth model, and home-destination concepts
  - Include working examples for DM use, stream mention use, allowlists, cron delivery, and home stream/topic configuration
  - Keep the guide practical and minimal; avoid promising features the adapter does not implement yet

- [ ] Add tests for outbound delivery and platform-wide integration points:
  - Extend `tests/tools/test_send_message_tool.py` with Zulip routing, target parsing, and home-channel fallback coverage
  - Extend `tests/gateway/test_zulip.py` with media/send helper coverage that can be verified with mocked Zulip client methods
  - Update any cron-delivery or shared-tool tests only where Zulip now belongs in canonical platform maps

- [ ] Run verification for tooling + docs-sensitive changes:
  - Run `source venv/bin/activate && python -m pytest tests/gateway/test_zulip.py tests/tools/test_send_message_tool.py tests/gateway/test_delivery.py -q`
  - Run `source venv/bin/activate && python -m compileall gateway tools hermes_cli tests`
  - Manually open the changed docs files and confirm every Zulip env var/example matches the implemented names exactly
