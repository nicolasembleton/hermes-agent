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

- [ ] Perform a missed-integration sweep across the repo before declaring success:
  - Search all platform switchboards and platform lists in `gateway/`, `tools/`, `agent/`, `cron/`, `hermes_cli/`, `README.md`, and `website/docs/` for places that enumerate platforms and confirm Zulip is included where appropriate
  - Recheck `gateway/platforms/ADDING_A_PLATFORM.md` item by item against the actual diff so nothing from the checklist is left half-done
  - Keep the sweep surgical: add Zulip only where the code path genuinely supports messaging platforms, not to unrelated lists

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
