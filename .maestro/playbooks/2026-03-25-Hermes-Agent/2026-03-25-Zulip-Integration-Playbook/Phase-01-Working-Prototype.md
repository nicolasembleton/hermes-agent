# Phase 01: Zulip Gateway Working Prototype

Build the smallest end-to-end Zulip integration that genuinely works inside Hermes: dependency wiring, config loading, a text-only Zulip adapter, gateway registration, and targeted tests proving inbound event normalization plus outbound reply delivery. This phase matters because it creates a runnable Hermes↔Zulip prototype without waiting on media, cron, or documentation polish, giving fast visible progress and a safe foundation for later phases.

## Tasks

- [x] Map the prototype scope against the real repo before editing anything:
  - Re-read `gateway/platforms/ADDING_A_PLATFORM.md` and copy its required integration points into your working notes in `.maestro/playbooks/2026-03-25-Hermes-Agent/Working/` if helpful
  - Inspect `gateway/platforms/mattermost.py`, `gateway/platforms/matrix.py`, `gateway/run.py`, and `gateway/config.py` to reuse existing adapter/config patterns instead of inventing new ones
  - Confirm where the Zulip dependency belongs by checking `pyproject.toml` optional extras before changing package metadata
  - Notes: See `Working/zulip-scope-mapping.md` for detailed mapping of all 16 integration points from ADDING_A_PLATFORM.md, verified file paths, code patterns from Mattermost/Matrix/DingTalk adapters, pyproject.toml extra structure, env var inventory, and per-task file change list. Key findings: (1) Zulip uses email+API_key auth (not token), needs custom `get_connected_platforms()` check; (2) dedicated `zulip` extra follows pattern of `matrix`, `dingtalk`, `homeassistant`; (3) several existing platforms (Mattermost, Matrix, DingTalk) are missing from toolsets.py and status.py — Zulip should match their level of wiring for Phase 1 consistency.

- [x] Add Zulip dependency and base configuration plumbing:
  - Update `pyproject.toml` so the Zulip client installs with the messaging extras used by gateway platforms
  - Extend `gateway/config.py` with `Platform.ZULIP = "zulip"`
  - Load `ZULIP_SITE_URL`, `ZULIP_BOT_EMAIL`, `ZULIP_API_KEY`, `ZULIP_DEFAULT_STREAM`, `ZULIP_HOME_TOPIC`, and optional home-channel naming into the Zulip `PlatformConfig.extra` / `home_channel` fields using the same style as Mattermost and Matrix
  - Ensure `GatewayConfig.get_connected_platforms()` recognizes Zulip’s credential shape without breaking existing platforms

- [ ] Create the initial Zulip adapter in `gateway/platforms/zulip.py`:
  - Reuse `BasePlatformAdapter`, `MessageEvent`, `MessageType`, and `SendResult` conventions from existing adapters
  - Implement `check_zulip_requirements()` with dependency verification and minimal config validation
  - Implement a text-first `ZulipAdapter` that can connect, register for message events, normalize DM vs stream/topic chat IDs, filter self-messages, dispatch inbound text via `self.handle_message(event)`, send outbound text replies, and return `get_chat_info()` metadata
  - Keep reconnection/backoff logic minimal but real, with readable helpers for chat-id parsing/formatting so later phases can extend media and typing cleanly

- [ ] Wire the prototype through the gateway runtime and agent surfaces:
  - Update `gateway/run.py` to create the Zulip adapter and include Zulip in per-platform allowlist / allow-all authorization maps
  - Add a Zulip platform hint to `agent/prompt_builder.py` so the agent uses Zulip-friendly Markdown and understands streams + topics
  - Add a Zulip platform toolset in `toolsets.py` and include it in `hermes-gateway`

- [ ] Expose the prototype in CLI configuration flows so it is visibly usable:
  - Add Zulip to the interactive setup inventory in `hermes_cli/gateway.py`, reusing the Mattermost-style setup structure but with Zulip-specific bot creation instructions and env vars
  - Add Zulip to the messaging platform section in `hermes_cli/status.py`
  - Follow existing naming patterns exactly; do not introduce new config UX unless the repo already needs it for other platforms

- [ ] Write prototype-focused automated tests before claiming the phase works:
  - Create `tests/gateway/test_zulip.py` covering enum/config loading, adapter init, self-message filtering, stream/topic chat-id generation, DM chat-id generation, inbound `MessageEvent` dispatch, and outbound send request construction with mocked Zulip client calls
  - Update any existing shared tests only where the new platform must appear (for example config or channel-directory expectations) rather than cloning broad test logic unnecessarily

- [ ] Run the prototype verification loop and fix issues until green:
  - Run targeted tests with `source venv/bin/activate && python -m pytest tests/gateway/test_zulip.py tests/gateway/test_config.py -q`
  - Run a lightweight syntax/import smoke check on touched Python modules with `source venv/bin/activate && python -m compileall gateway hermes_cli tests`
  - If the repo exposes a more specific formatting command for touched files while you work, run it; otherwise keep imports/order/docstrings clean manually and verify the files still pass the commands above
