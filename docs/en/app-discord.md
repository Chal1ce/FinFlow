---
title: Discord
parent: App integrations
nav_order: 2
---

# Discord

Discord receives messages through the Gateway and sends replies/files through the Bot REST API.
Use ordinary messages such as `!status`; native slash-command registration is unnecessary.
Shared behavior is described in [App integrations](app-integrations.md).

## Configure

1. Create an application and Bot in the Discord Developer Portal. Set `FINFLOW_DISCORD_BOT_TOKEN` in `.env`.
2. Enable **Message Content Intent**. The client also requests it to read message text and attachments.
3. Invite the bot with View Channel, Read Message History, Send Messages, and Attach Files permissions. Threads also require thread access and Send Messages in Threads.
4. Enable Developer Mode in Discord and copy the bot, user, and channel/thread IDs. Store them as strings.

Set `discord` in your local integration configuration:

```json
{
  "enabled": true,
  "bot_id": "123456789012345678",
  "users": {
    "987654321098765432": ["viewer", "operator", "reviewer"]
  },
  "allowed_group_chats": ["123456789012345679"],
  "notification_chats": []
}
```

Allowlist channel IDs. For threads, add the **thread's own ID**; parent-channel access does not
implicitly allow threads. Direct messages still require an authorized user. Report recipients belong
in `notification_chats`, and the bot must be able to send to each destination.
References: [discord.py intents](https://discordpy.readthedocs.io/en/stable/intents.html),
[Discord messages](https://docs.discord.com/developers/resources/message).

## Start and use

After copying the shared configuration template on first setup:

```sh
. .venv/bin/activate
python -m pip install -e '.[discord]'
python -m integrations.cli --config config/integrations.local.json preflight --channel discord
python -m integrations.cli --config config/integrations.local.json discord
```

Startup checks the token against `bot_id`. The command runs both receiver and worker.
Send `!help` in a DM. Mention the bot in channels/threads:

```text
@FinFlow !status
@FinFlow !report
@FinFlow !candidate CANDIDATE_ID
@FinFlow !run --no-discover
@FinFlow !review TICKET accepted Checked the original image and source
```

Select Discord's actual mention of the bot; a plain-text display name is insufficient.
Attach one PDF per message, still mentioning the bot in channels. Multiple attachments prompt
separate messages. Candidate replies contain the original image file, full JSON, and an expiring review command.

## Runtime limits

- Only bot accounts are supported; messages from bots/webhooks are ignored.
- Local identity, role, and chat rules are enforced at ingestion, queued execution, and delivery. Restart all processes after configuration changes.
- Downloads refresh the message/attachment metadata and use platform file hosts. Both local and Discord file-size limits apply.
- Outgoing messages disable automatic mentions, including `@everyone`.
- Gateway reconnects, but cold startup does not fetch message history. Resend commands missed during downtime or before durable inbox storage. Persisted jobs remain recoverable.
- Receipts persist successful stages; Discord nonces help deduplicate recent sends, without guaranteeing exactly-once delivery.
- `app-discord` defaults to unapproved training use. There are no native review buttons; use `!review`.

Static checks are complete; real bot operation and functional validation remain pending.
