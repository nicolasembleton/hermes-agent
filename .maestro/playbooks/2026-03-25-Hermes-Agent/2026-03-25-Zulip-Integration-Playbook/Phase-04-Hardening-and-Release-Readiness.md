# Phase 04: Hardening and Release Readiness

Finish the Zulip integration to a production-ready standard: security redaction, missed-touchpoint audit, broader regression coverage, and final verification that the new platform behaves like the rest of the Hermes gateway. This phase matters because it turns a feature-complete integration into something maintainable, safe, and upstream-ready.

## Tasks

- [x] Audit security and log-redaction needs for Zulip identifiers: <!-- MAESTRO: completed 2026-03-25 -->
  - Inspect `agent/redact.py` and existing gateway logs to decide whether Zulip-specific email/site-url patterns need masking beyond the generic secret redaction already present
  - Add only the minimum necessary Zulip redaction rules, with comments explaining what sensitive value is being protected and why generic masking was not enough
  - Verify adapter logs do not print raw API keys, queue IDs, or full payloads during failures

  <!-- Audit notes:
  - Zulip bot emails are public (visible in Zulip UI); site URLs are not secrets → NO redaction needed for these
  - Zulip API keys are caught by the existing ENV assignment pattern (ZULIP_API_KEY=value) and JSON field pattern
  - Zulip queue IDs are managed internally by call_on_each_event and never logged → NO redaction needed
  - GAPS FOUND: The existing _DB_CONNSTR_RE only handles database protocol URLs (postgres://, mysql://, etc.).
    Zulip's Python client uses HTTP basic auth (email:api_key) via the requests library. If an exception
    message includes the full HTTPS URL with credentials, the password portion was NOT being redacted.
  - FIX: Added _HTTPS_CREDENTIALS_RE pattern to redact passwords in https://user:password@host URLs.
    This is a general-purpose pattern (not Zulip-specific) that benefits any HTTP service using basic auth.
  - VERIFIED: All 16 logger.* calls in gateway/platforms/zulip.py were audited:
    - Raw API key: never logged directly ✅
    - Queue IDs: never logged ✅
    - Full payloads: never logged ✅
    - Bot email/site URL: logged at INFO level (not secrets) ✅
    - Exception strings: potentially include credential URLs → now covered by new redactor ✅
  -->

- [x] Perform a missed-integration sweep across the repo before declaring success: <!-- MAESTRO: completed 2026-03-25 -->
  - Search all platform switchboards and platform lists in `gateway/`, `tools/`, `agent/`, `cron/`, `hermes_cli/`, `README.md`, and `website/docs/` for places that enumerate platforms and confirm Zulip is included where appropriate
  - Recheck `gateway/platforms/ADDING_A_PLATFORM.md` item by item against the actual diff so nothing from the checklist is left half-done
  - Keep the sweep surgical: add Zulip only where the code path genuinely supports messaging platforms, not to unrelated lists

  <!-- Sweep notes:
  - CHECKED 16+ files across gateway/, tools/, agent/, cron/, hermes_cli/, website/docs/
  - CONFIRMED PRESENT: gateway/config.py (enum, env overrides, connected check),
    gateway/run.py (adapter factory, auth maps), agent/prompt_builder.py (hints),
    toolsets.py (toolset + composite), cron/scheduler.py (delivery map),
    tools/send_message_tool.py (routing + schema), tools/cronjob_tools.py (deliver desc),
    gateway/channel_directory.py (discovery list), hermes_cli/status.py,
    hermes_cli/gateway.py (_PLATFORMS + status), hermes_cli/config.py (env vars),
    agent/redact.py (HTTPS creds), README.md, AGENTS.md, website docs (messaging index,
    env vars, zulip.md)
  - GAPS FOUND AND FIXED:
    1. gateway/run.py: Added Platform.ZULIP to 4 dicts (default_toolset_map x2,
       platform_config_key x2) in _run_agent() and _run_background_agent().
       Without this, Zulip users fell through to "telegram" toolset/config key.
    2. hermes_cli/tools_config.py: Added "zulip" entry to PLATFORMS dict and
       Zulip detection in _get_enabled_platforms() (checks ZULIP_API_KEY +
       ZULIP_SITE_URL).
    3. website/docs/reference/toolsets-reference.md: Added hermes-zulip row
       to platform toolset table.
    4. website/docs/user-guide/features/vision.md: Added Zulip to image
       platform list.
    5. hermes_cli/main.py: Added zulip to --deliver help text and insights
       stats source loop.
    6. website/docs/reference/faq.md: Added [zulip] to install example.
  - NOT CHANGED (surgical scope): Documentation listing only a subset of
    platforms as examples (e.g. cron.md delivery table, tools-reference.md TTS
    description) left as-is since those gaps affect ALL newer platforms equally
    and fixing only Zulip there would be inconsistent.
  -->

- [ ] Expand final regression coverage to the shared edges Zulip can break:
  - Update or add tests covering authorization map inclusion, channel-directory visibility, cron delivery mappings, send-message platform maps, and any redaction helpers introduced in this phase
  - Prefer extending existing shared test files when the behavior is cross-platform; keep Zulip-specific adapter behavior in `tests/gateway/test_zulip.py`

- [ ] Run the final verification matrix and fix failures before moving on:
  - Run `source venv/bin/activate && python -m pytest tests/gateway/test_zulip.py tests/gateway/ tests/tools/test_send_message_tool.py -q`
  - Run `source venv/bin/activate && python -m compileall agent gateway tools hermes_cli tests`
  - If any repo-owned docs validation command is available for the touched messaging docs, run that too; otherwise manually check formatting/front matter consistency against adjacent platform docs

- [ ] Do a final readability and maintenance pass on all Zulip changes:
  - Remove dead code, duplicated parsing helpers, stray debug output, and unused imports
  - Make sure helper names are explicit (`parse_zulip_chat_id`, `build_stream_topic_chat_id`, etc.) and comments explain Zulip-specific constraints rather than restating the code
  - Confirm all newly public helpers/types/docstrings are clear enough for the next contributor to extend safely

- [ ] Prepare the implementation for handoff without creating extra project files:
  - Review the changed file list and confirm it matches the intended scope from the discovery spec
  - Capture any manual validation notes needed by the human reviewer inside the PR description or commit message later, not as new repo files during this playbook run
  - Leave the branch with passing tests and updated docs, ready for normal review workflow
