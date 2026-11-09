# Zulip platform plugin for Hermes Agent

Gateway adapter that connects Hermes to [Zulip](https://zulip.com) (Cloud or
self-hosted): DMs, streams/topics with @mention gating, group DMs,
attachments, reactions, message editing, home-channel cron/notification
delivery, plus agent tools for searching messages, downloading attachments,
and posting to arbitrary stream/topic targets.

This is a **standalone plugin** — it touches no Hermes core files and registers
through the public `ctx.register_platform()` / `PlatformEntry` seams.

## Install

From this repository (branch `zulip-plugin`), into your Hermes home:

```bash
hermes plugins install nicolasembleton/hermes-agent/plugins/platforms/zulip --ref zulip-plugin --yes-deps
```

or with an explicit URL / subdir fragment:

```bash
hermes plugins install https://github.com/nicolasembleton/hermes-agent.git#plugins/platforms/zulip --ref zulip-plugin --yes-deps
```

`--yes-deps` consents to installing the plugin's declared `python_dependencies`
(`zulip==0.9.1`) into the Hermes venv via PM, under Hermes's core dependency
constraints. Without it, installs prompt interactively; `--no-deps` skips them
and the gateway will lazy-install on first use via the registry's
`ensure_deps_fn`.

Validate without installing:

```bash
hermes plugins validate plugins/platforms/zulip        # from a repo checkout
hermes plugins validate zulip                           # once catalog-listed
```

## Configure

Create a bot in Zulip (Settings → Bots), then set:

| Env var | Required | Purpose |
|---|---|---|
| `ZULIP_SITE_URL` | yes | e.g. `https://example.zulipchat.com` |
| `ZULIP_BOT_EMAIL` | yes | Bot email |
| `ZULIP_API_KEY` | yes | Bot API key (stored via Hermes secrets) |
| `ZULIP_ALLOWED_USERS` | — | Comma-separated user emails allowed to DM the bot |
| `ZULIP_ALLOW_ALL_USERS` | — | `true` to allow anyone (dev only) |
| `ZULIP_HOME_CHANNEL` | — | `<stream>[:<topic>]` for cron/notification delivery |
| `ZULIP_HOME_CHANNEL_NAME` | — | Display name for the home channel |
| `ZULIP_REQUIRE_MENTION` | — | Require @mention in streams (default `true`) |
| `ZULIP_FREE_RESPONSE_STREAMS` | — | Streams exempt from the mention requirement |
| `ZULIP_CONTEXT_DEPTH` | — | Prior topic messages injected as context on @mention |
| `ZULIP_CATCHUP` / `ZULIP_CATCHUP_MAX_MESSAGES` | — | Replay missed stream messages after reconnect |
| `ZULIP_CONVERT_MATH` | — | Rewrite LaTeX delimiters to Zulip KaTeX (default `true`) |
| `ZULIP_CERT_BUNDLE` / `ZULIP_ALLOW_INSECURE` | — | TLS for self-hosted servers |

or run the interactive setup: `hermes gateway setup` → Zulip (the plugin's
`setup_fn` writes the same env vars through Hermes's secret storage).

Then start the gateway: `hermes gateway run` — Zulip appears under
`hermes status` once `ZULIP_*` is set (env enablement is declared via the
plugin manifest's `requires_env`/`optional_env`).

## Agent tools

- `zulip_search_messages` — full-text search across messages
- `zulip_download_attachment` — download an upload to disk
- `zulip_send_topic_message` — post to any stream/topic (or DM) from the agent

## Outbound / cron delivery

`send_message` targets like `zulip:<stream>:<topic>` and `zulip:dm:<email>`,
`zulip:group_dm:<a@b.com,c@d.com>` work in `send_message` and cron
`deliver:` targets, including the standalone sender (cron without a
co-resident gateway).

## Development

Run the plugin's test suite from a repo checkout:

```bash
scripts/run_tests.sh tests/plugins/platforms/zulip
```

The `zulip` SDK is not installed in the core test environment;
`tests/plugins/platforms/zulip/conftest.py` installs a `sys.modules` mock, so
the suite runs hermetically.

## Security / disclosure notes

- Talks only to the configured `ZULIP_SITE_URL`; credentials come from Hermes
  env/secret storage (`ZULIP_*`), never read from files by the plugin.
- `ZULIP_ALLOW_INSECURE=true` disables TLS verification — development only.
- No telemetry; no self-updating code; updates arrive by re-installing at a
  new `--ref`/catalog SHA.
