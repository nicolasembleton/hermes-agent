---
sidebar_position: 12
title: "Zulip"
description: "Set up Hermes Agent as a Zulip bot"
---

# Zulip Setup

Hermes Agent integrates with Zulip as a bot, letting you chat with your AI assistant through direct messages or stream topics. Zulip is an open-source team chat platform — you can use Zulip Cloud (hosted at zulipchat.com) or run it on your own infrastructure. The bot connects via the official `zulip` Python package using Zulip's REST API and long-polling event queue, processes messages through the Hermes Agent pipeline (including tool use, memory, and reasoning), and responds in real time. It supports text, images, documents, video uploads, and typing indicators.

The `zulip` Python package is required — install it before setup:

```bash
pip install zulip
```

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

# Mention gating (default: true)
# ZULIP_REQUIRE_MENTION=false

# Streams where @mention is not required (comma-separated names or IDs)
# ZULIP_FREE_RESPONSE_STREAMS=bot-commands,42
```

Optional behavior settings in `~/.hermes/config.yaml`:

```yaml
group_sessions_per_user: true
```

- `group_sessions_per_user: true` keeps each participant's context isolated inside shared streams and group DMs

### Start the Gateway

Once configured, start the gateway:

```bash
hermes gateway
```

The bot should connect to your Zulip server within a few seconds. You'll see a log message like:

```
Zulip: authenticated as hermes-bot@your-org.zulipchat.com (user_id=123) on https://your-org.zulipchat.com
```

Send it a DM or @mention it in a stream to test.

:::tip
You can run `hermes gateway` in the background or as a systemd/launchd service for persistent operation. See the deployment docs for details.
:::

## Home Channel

You can designate a "home stream+topic" where the bot sends proactive messages (such as cron job output, reminders, and notifications). There are two ways to set it.

### Using the Slash Command

Type `/sethome` in any Zulip stream or DM where the bot is present. That stream+topic becomes the home channel.

### Manual Configuration

Add this to your `~/.hermes/.env`:

```bash
ZULIP_HOME_CHANNEL=general:notifications
```

The format is `stream_name:topic`. The bot will send cron job results, background task completions, and other proactive notifications to this location.

You can also use `ZULIP_DEFAULT_STREAM` and `ZULIP_HOME_TOPIC` as separate variables, but `ZULIP_HOME_CHANNEL` takes precedence when set.

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

:::info
DMs and group DMs always bypass mention gating — the bot responds to every message in private conversations.
:::

## Sending Messages Cross-Platform

You can send messages to Zulip from other platforms using the `send_message` tool. The target format for Zulip is:

| Target | Description |
|--------|-------------|
| `zulip` | Sends to the home channel |
| `zulip:123:General` | Sends to stream ID 123, topic "General" |
| `zulip:dm:user@example.com` | Sends a DM to the specified user |
| `zulip:group_dm:a@b.com,c@d.com` | Sends to a group DM |

## Cron Delivery

Cron jobs can deliver results to Zulip. Set `ZULIP_HOME_CHANNEL` as described above, or specify the target directly in the cron job:

```
deliver='zulip:stream_id:topic'
```

## Media Delivery

The Zulip adapter supports uploading and sending media files:

| Type | Behavior |
|------|----------|
| **Images** | Uploaded to Zulip and rendered inline using `![alt](/user_uploads/...)` |
| **Documents** | Uploaded and sent as a clickable Markdown link `[filename](/user_uploads/...)` |
| **Video** | Uploaded and sent as a downloadable link (Zulip does not inline video playback) |
| **Voice messages** | Not supported — Zulip has no native voice bubble support. The file path is sent as text instead. |

Media delivery works in both streams and DMs.

## Troubleshooting

### Bot is not responding to messages

**Cause**: The bot is not subscribed to the stream, or `ZULIP_ALLOWED_USERS` doesn't include your email.

**Fix**: Subscribe the bot to the stream (stream settings → Subscribers → add the bot's email). Verify your email is in `ZULIP_ALLOWED_USERS`. Restart the gateway.

### 401 Unauthorized errors

**Cause**: The API key, bot email, or server URL is incorrect.

**Fix**: Verify all three values in your `.env` file. Check that `ZULIP_SITE_URL` includes `https://` and has no trailing slash. Try the credentials manually:

```bash
pip install zulip
python -c "
import zulip
c = zulip.Client(site='https://your-org.zulipchat.com', email='bot@example.com', api_key='YOUR_KEY')
print(c.get_profile())
"
```

If this prints the bot's profile, the credentials are valid.

### Bot ignores stream messages

**Cause**: `ZULIP_REQUIRE_MENTION` is `true` (the default) and the bot isn't @mentioned.

**Fix**: Either @mention the bot (e.g., `@**Hermes Agent** hello`), or add the stream to `ZULIP_FREE_RESPONSE_STREAMS`, or set `ZULIP_REQUIRE_MENTION=false`.

### "zulip package not installed" on startup

**Cause**: The `zulip` Python package is not installed.

**Fix**: Install it and restart the gateway:

```bash
pip install zulip
hermes gateway
```

### Event queue disconnects / reconnection loops

**Cause**: Network instability, Zulip server restarts, or firewall issues with long-polling connections.

**Fix**: The adapter automatically reconnects with exponential backoff (2s → 60s). Check your network connectivity. If you're behind a proxy, ensure it supports long-lived HTTP connections.

### Bot is offline

**Cause**: The Hermes gateway isn't running, or it failed to connect.

**Fix**: Check that `hermes gateway` is running. Look at the terminal output for error messages. Common issues: wrong server URL, expired API key, Zulip server unreachable.

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
