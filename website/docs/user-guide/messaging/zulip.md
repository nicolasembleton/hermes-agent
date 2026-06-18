---
sidebar_position: 12
title: "Zulip"
description: "Set up Hermes Agent as a Zulip bot"
---

# Zulip Setup

Hermes Agent integrates with Zulip as a bot, letting you chat with your AI assistant through direct messages or stream topics. Zulip is an open-source team chat platform — you can use Zulip Cloud (hosted at zulipchat.com) or run it on your own infrastructure. The bot connects via the official `zulip` Python package using Zulip's REST API and long-polling event queue, processes messages through the Hermes Agent pipeline (including tool use, memory, and reasoning), and responds in real time. It supports text, images, documents, video uploads, and typing indicators.

The `zulip` Python package is optional. Hermes lazy-installs it on first
gateway start when Zulip is configured. If your environment does not allow
runtime installs, preinstall the Zulip extra instead:

```bash
pip install 'hermes-agent[zulip]'
```

:::info
`[all]` intentionally excludes optional messaging SDKs such as Zulip so one
upstream package issue does not break fresh installs for users who never enable
that platform.
:::

Before setup, here's the part most people want to know: how Hermes behaves once it's in your Zulip organization.

## How Hermes Behaves

| Context | Behavior |
|---------|----------|
| **DMs** | Hermes responds to every message. No `@mention` needed. Each DM has its own session. |
| **Stream messages** | Hermes responds when you `@mention` it. Without a mention, Hermes ignores the message. |
| **Topics** | Each stream+topic combination gets its own session. Changing the topic starts a fresh conversation. |
| **Group DMs** | Hermes responds to every message in group DMs. Each group DM has its own session. |
| **Shared streams with multiple users** | By default, Hermes isolates session history per user inside the stream. Two people talking in the same stream do not share one transcript unless you explicitly disable that. |

:::tip
If you want Hermes to respond in certain streams without an @mention, use `ZULIP_FREE_RESPONSE_STREAMS` to list stream names or IDs. This is useful for bot-dedicated channels.
:::

### Session Model in Zulip

By default:

- each DM gets its own session
- each stream+topic gets its own session
- each user in a shared stream gets their own session inside that stream+topic

This is controlled by `config.yaml`:

```yaml
group_sessions_per_user: true
```

Set it to `false` only if you explicitly want one shared conversation for the entire stream:

```yaml
group_sessions_per_user: false
```

Shared sessions can be useful for a collaborative stream, but they also mean:

- users share context growth and token costs
- one person's long tool-heavy task can bloat everyone else's context
- one person's in-flight run can interrupt another person's follow-up in the same stream

This guide walks you through the full setup process — from creating your bot on Zulip to sending your first message.

## Step 1: Create a Bot Account

1. Log in to your Zulip organization (cloud or self-hosted).
2. Go to **Settings** → **Your bots**.
3. Click **Add a new bot**.
4. Fill in the details:
   - **Bot type**: choose **Generic bot**.
   - **Bot email**: e.g., `hermes-bot@your-org.zulipchat.com`
   - **Full name**: e.g., `Hermes Agent`
   - **Role**: can be a normal user or admin, depending on your needs
5. Click **Create bot**.
6. Zulip will display the **bot's API key**. **Copy it immediately.**

:::warning[API key shown only once]
The bot's API key is only displayed once when you create the bot. If you lose it, you'll need to regenerate it from the bot's settings page. Never share your API key publicly or commit it to Git — anyone with this key has full control of the bot.
:::

:::info
For self-hosted Zulip, make sure the bot is enabled after creation. Navigate to the bot in **Settings** → **Your bots** and verify its status.
:::

Store the API key somewhere safe (a password manager, for example). You'll need it in Step 3.

## Step 2: Subscribe the Bot to Streams

The bot needs to be subscribed to any stream where you want it to respond:

1. Open the stream where you want the bot.
2. Click the **stream name** → **Stream settings**.
3. Go to the **Subscribers** tab.
4. Search for the bot's email address and add it.

For DMs, simply open a direct message with the bot — it will be able to respond immediately without subscribing to any streams.

## Step 3: Configure Hermes Agent

### Option A: Interactive Setup (Recommended)

Run the guided setup command:

```bash
hermes gateway setup
```

Select **Zulip** when prompted, then enter your server URL, bot email, API key, and allowed user emails when asked.

### Option B: Manual Configuration

Add the following to your `~/.hermes/.env` file:

```bash
# Required
ZULIP_SITE_URL=https://your-org.zulipchat.com
ZULIP_BOT_EMAIL=hermes-bot@your-org.zulipchat.com
ZULIP_API_KEY=***

# Required unless ZULIP_ALLOW_ALL_USERS=true
ZULIP_ALLOWED_USERS=you@example.com

# Multiple allowed users (comma-separated)
# ZULIP_ALLOWED_USERS=you@example.com,colleague@example.com
```

Optional settings in `~/.hermes/.env`:

```bash
# Allow all users without an allowlist (NOT recommended for bots with terminal access)
# ZULIP_ALLOW_ALL_USERS=true

# Default stream for outbound messages
ZULIP_DEFAULT_STREAM=general

# Home topic for cron/reminder delivery when ZULIP_HOME_CHANNEL is not set
# ZULIP_HOME_TOPIC=notifications

# Mention gating (default: true)
# ZULIP_REQUIRE_MENTION=false

# TLS for self-hosted/local Zulip with private CA or self-signed certs
# Preferred: trust your local CA explicitly
# ZULIP_CERT_BUNDLE=/path/to/ca.pem
# Temporary local-dev fallback only:
# ZULIP_ALLOW_INSECURE=true   # disables TLS verification

# Streams where @mention is not required (comma-separated names or IDs)
# ZULIP_FREE_RESPONSE_STREAMS=bot-commands,42
```

Optional behavior settings in `~/.hermes/config.yaml`:

```yaml
group_sessions_per_user: true
```

- `group_sessions_per_user: true` keeps each participant's context isolated inside shared streams and group DMs

### Start the Gateway

Once configured, start the gateway in the foreground:

```bash
hermes gateway run
```

The bot should connect to your Zulip server within a few seconds. You'll see a log message like:

```
Zulip: authenticated as hermes-bot@your-org.zulipchat.com (user_id=123) on https://your-org.zulipchat.com
```

Send it a DM or @mention it in a stream to test.

:::tip
Use `hermes gateway run` for a foreground test run. Once that works, you can install the systemd/launchd service for persistent operation.
:::

## Home Channel

You can designate a "home stream+topic" where the bot sends proactive messages (such as cron job output, reminders, and notifications). There are two ways to set it.

### Using the Slash Command

Type `/sethome` in any Zulip stream or DM where the bot is present. That stream+topic becomes the home channel.

### Manual Configuration

Add either a combined stream+topic target:

```bash
ZULIP_HOME_CHANNEL=general:notifications
```

The format is `stream_name:topic`. Hermes resolves the stream name to the correct Zulip stream ID before sending, so manual config stays human-readable.

Or use separate variables:

```bash
ZULIP_DEFAULT_STREAM=general
ZULIP_HOME_TOPIC=notifications
```

`ZULIP_HOME_CHANNEL` takes precedence when both forms are set.

## Mention Gating

By default, Hermes only responds in streams when it is @mentioned. This prevents the bot from processing every message in a busy stream.

### Disabling Mention Requirement

Set `ZULIP_REQUIRE_MENTION=false` in your `~/.hermes/.env` to make the bot respond to all messages in every stream:

```bash
ZULIP_REQUIRE_MENTION=false
```

### Per-Stream Exemptions

Use `ZULIP_FREE_RESPONSE_STREAMS` to exempt specific streams from the mention requirement while keeping it active elsewhere:

```bash
ZULIP_FREE_RESPONSE_STREAMS=bot-commands,ai-assistant
```

You can use stream names or stream IDs (comma-separated). This is useful for dedicated bot channels where you want a conversational experience without the @mention overhead.

### Historical Context on @mention

By default, the bot only sees the message where it was @mentioned — it has no idea what was discussed beforehand. It's like walking into a room mid-conversation.

Set `ZULIP_CONTEXT_DEPTH` in your `~/.hermes/.env` to instruct the bot to fetch recent messages from the same stream+topic when summoned:

```bash
ZULIP_CONTEXT_DEPTH=20
```

When someone types `@bot what do you think about the proposal above?`, the bot fetches the last 20 messages from Zulip's `/messages` REST API and injects them as context before the current message. The agent sees something like:

```
Recent messages in this topic:
Alice: I think we should use PostgreSQL
Bob: Agreed, but what about migrations?
Charlie: We can use Alembic for that
---
@**Hermes Bot** what do you think about the proposal above?
```

**Key properties:**

- **Survives disconnects** — uses Zulip as the source of truth via REST API, not the event queue. If the bot was offline for an hour, it still sees what happened.
- **On-demand** — only fetches when @mentioned (or in free-response streams). Zero overhead on unmentioned messages.
- **No local storage** — context is fetched fresh each time, never persisted between turns.
- **Privacy-preserving** — the bot only reads the stream+topic when explicitly summoned. It never silently observes or stores messages.
- **Bot's own messages skipped** — the bot filters itself out of the context so it doesn't see its own previous responses.

Set it to `0` (the default) to disable context fetching:

:::info
DMs and group DMs always bypass mention gating — the bot responds to every message in private conversations.
:::

### Message History Search Tool

The bot has access to a `zulip_search_messages` tool that lets it search and paginate through Zulip message history. This wraps Zulip's `/messages` API — the bot can use it to:

- **Fetch context** around a specific message ID (e.g., a message someone replied to)
- **Paginate** further back when the initial auto-fetched context isn't enough
- **Search** for specific content (e.g., "find where we discussed PostgreSQL")
- **Filter by sender** using Zulip's search operators

**Tool parameters:**

| Parameter | Description | Example |
|-----------|-------------|---------|
| `stream` | Stream name | `"general"` |
| `topic` | Topic name | `"database"` |
| `query` | Full-text search (Zulip operators) | `"postgresql"`, `"sender:alice@example.com"` |
| `anchor` | Message ID or `"newest"`/`"oldest"` | `"newest"`, `"42"` |
| `num_before` | Messages to fetch before anchor | `20` |
| `num_after` | Messages to fetch after anchor | `5` |

**Common patterns the bot uses:**

```python
# Recent conversation context
zulip_search_messages(stream="general", topic="database", anchor="newest", num_before=20)

# Context around a specific message (e.g., a reply target)
zulip_search_messages(stream="general", anchor="42", num_before=5, num_after=5)

# Find where PostgreSQL was discussed
zulip_search_messages(stream="general", query="postgresql")

# Show older page of history
zulip_search_messages(stream="general", topic="database", anchor="<oldest_message_id>", num_before=20)
```

:::warning[Search scope is restricted to the current conversation]
When the bot is talking to you **through Zulip** (DM, group DM, or stream), the `zulip_search_messages` tool is automatically restricted to the **current conversation only**. A user in a private DM cannot ask the bot to search messages from streams or other DMs the bot is subscribed to. This prevents the bot from being used as a proxy to exfiltrate content from conversations you don't have access to.

When the bot is invoked from the **CLI** or other platforms, the full search scope is available (subject to the bot's own Zulip permissions).
:::

The response includes pagination hints (`oldest_message_id`, `newest_message_id`) so the bot can continue browsing without guesswork.

:::tip
If the bot says "I don't have enough context," tell it: "use zulip_search_messages to look further back in this topic." It will paginate until it finds what it needs.
:::

## Sending Messages Cross-Platform

You can send messages to Zulip from other platforms using the `send_message` tool. The target format for Zulip is:

| Target | Description |
|--------|-------------|
| `zulip` | Sends to the home stream+topic |
| `zulip:123:General` | Sends to stream ID 123, topic "General" |
| `zulip:general:notifications` | Sends to stream "general", topic "notifications" |
| `zulip:dm:user@example.com` | Sends a DM to the specified user |
| `zulip:group_dm:a@b.com,c@d.com` | Sends to a group DM |

## Cron Delivery

Cron jobs can deliver results to Zulip. Use `deliver="zulip"` to send to the
configured home stream+topic, or specify the target directly in the cron job:

```
deliver="zulip"
deliver="zulip:stream_id:topic"
deliver="zulip:general:notifications"
```

## Media Delivery

The Zulip adapter supports uploading and sending media files:

| Type | Behavior |
|------|----------|
| **Images** | Uploaded to Zulip and rendered inline using `![alt](/user_uploads/...)` |
| **Documents** | Uploaded and sent as a clickable Markdown link `[filename](/user_uploads/...)` |
| **Video** | Uploaded and sent as a downloadable link (Zulip does not inline video playback) |
| **Voice/audio** | Uploaded and sent as a downloadable Markdown link. Zulip has no native voice bubble support. |

Media delivery works in both streams and DMs.

## Troubleshooting

### Bot is not responding to messages

**Cause**: The bot is not subscribed to the stream, or `ZULIP_ALLOWED_USERS` doesn't include your email.

**Fix**: Subscribe the bot to the stream (stream settings → Subscribers → add the bot's email). Verify your email is in `ZULIP_ALLOWED_USERS`. Restart the gateway.

### 401 Unauthorized errors

**Cause**: The API key, bot email, or server URL is incorrect.

**Fix**: Verify all three values in your `.env` file. Check that `ZULIP_SITE_URL` includes `https://` and has no trailing slash.

Run `hermes gateway run` in the foreground and check for the authenticated log
line. If the optional package is missing in a locked-down environment, install
`hermes-agent[zulip]` and restart the gateway.

### Bot ignores stream messages

**Cause**: `ZULIP_REQUIRE_MENTION` is `true` (the default) and the bot isn't @mentioned.

**Fix**: Either @mention the bot (e.g., `@**Hermes Agent** hello`), or add the stream to `ZULIP_FREE_RESPONSE_STREAMS`, or set `ZULIP_REQUIRE_MENTION=false`.

### "zulip package not installed" on startup

**Cause**: The `zulip` Python package is not installed.

**Fix**: Let Hermes lazy-install it by restarting the gateway, or preinstall the
Zulip extra in locked-down environments:

```bash
pip install 'hermes-agent[zulip]'
hermes gateway run
```

:::info
`[all]` intentionally excludes optional messaging SDKs such as Zulip. Use the
`[zulip]` extra for direct installs, or rebuild custom containers with that
extra enabled.
:::

### Event queue disconnects / reconnection loops

**Cause**: Network instability, Zulip server restarts, or firewall issues with long-polling connections.

**Fix**: The adapter automatically reconnects with exponential backoff (2s → 60s). Check your network connectivity. If you're behind a proxy, ensure it supports long-lived HTTP connections.

### Bot is offline

**Cause**: The Hermes gateway isn't running, or it failed to connect.

**Fix**: Check that `hermes gateway run` is running. Look at the terminal output or `~/.hermes/logs/gateway.log` / `~/.hermes/logs/gateway.error.log` for error messages. Common issues: wrong server URL, stale API key, Zulip server unreachable, or self-signed TLS without `ZULIP_CERT_BUNDLE` / `ZULIP_ALLOW_INSECURE`.

### "User not allowed" / Bot ignores you

**Cause**: Your email isn't in `ZULIP_ALLOWED_USERS`.

**Fix**: Add your email to `ZULIP_ALLOWED_USERS` in `~/.hermes/.env` and restart the gateway. Remember: this is your **email address**, not your Zulip username.

## Security

:::warning
Always set `ZULIP_ALLOWED_USERS` to restrict who can interact with the bot. Without it, the gateway denies all users by default as a safety measure. Only add emails of people you trust — authorized users have full access to the agent's capabilities, including tool use and system access.
:::

If you want to allow all users in your Zulip organization, set `ZULIP_ALLOW_ALL_USERS=true`. This is only appropriate for private organizations where all members are trusted.

For more information on securing your Hermes Agent deployment, see the [Security Guide](../security.md).

## Notes

- **Zulip Cloud and self-hosted**: Works with both zulipchat.com cloud organizations and self-hosted Zulip servers.
- **Official client**: Uses the `zulip` Python package for reliable API access.
- **Long-polling**: The event queue uses Zulip's long-polling mechanism — no WebSocket or incoming webhook needed.
- **Stream topics**: Each topic in a stream gets its own session, which maps naturally to Zulip's topic-based conversation model.
- **DM pairing**: Unknown users who DM the bot receive a one-time pairing code (see the [Messaging Gateway](index.md) docs for details on the pairing flow).
