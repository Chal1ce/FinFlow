---
title: Telegram
parent: App integrations
nav_order: 1
---

# Telegram

The Telegram entry uses Bot API long polling on your computer or server, without a public webhook.
It supports queries, reports, PDFs, original evidence files, human review commands, runs, and retries.
See [App integrations](app-integrations.md) for roles, admission, storage, and recovery.

## Configure

1. Create a bot with Telegram's official BotFather. Set `FINFLOW_TELEGRAM_BOT_TOKEN` in your local `.env`.
2. Use the official `getMe` API to obtain the bot's numeric ID. Send it a message and inspect `getUpdates` locally to obtain your user ID and desired chat IDs. Keep tokens out of Git.
3. On first setup, copy `config/integrations.json` to `config/integrations.local.json`. Set its `telegram` section:

```json
{
  "enabled": true,
  "bot_id": "123456789",
  "users": {
    "987654321": ["viewer", "operator", "reviewer"]
  },
  "allowed_group_chats": [],
  "notification_chats": []
}
```

Keep the surrounding configuration. **Write all IDs as strings**, preserving negative group IDs.
Add group IDs to `allowed_group_chats`; configure report recipients separately in `notification_chats`.
Start a conversation with the bot first, or add it to a group with the required permissions.
Reference: [Telegram Bot API](https://core.telegram.org/bots/api).

## Start

Telegram uses core project dependencies:

```sh
. .venv/bin/activate
python -m pip install -e .
python -m integrations.cli --config config/integrations.local.json preflight --channel telegram
python -m integrations.cli --config config/integrations.local.json telegram
```

Startup verifies the token against the configured bot identity. An active webhook blocks polling;
remove it explicitly when migrating. Run only one poller per bot. The command includes a worker.

## Commands

In a direct conversation:

```text
/status
/report
/audit
/candidate CANDIDATE_ID
/run --no-discover
/review TICKET rejected The table unit is incorrect
```

The complete command set matches Feishu. In groups, use `/status@YourBotUsername`, or mention the bot
before a command. A group PDF needs a bot mention in its caption. Replies preserve Telegram topic IDs.

Original images are sent as **documents**, preserving bytes, followed by the full candidate JSON and
a review command. There are no review buttons in this entry. Review ownership, conversation,
evidence version, and expiry are enforced by the shared service.

## Limits and recovery

- FinFlow defaults to 32 MiB; Telegram's cloud download limit is 20 MB. [Official limit](https://core.telegram.org/bots/api#file)
- This version uses the official cloud API, without a self-hosted Bot API Server option. Import larger PDFs with the local CLI.
- Only new ordinary messages are processed. Bots, anonymous group senders, edits, and channel broadcasts are ignored.
- Events and polling position commit together. Restart resumes from that position, subject to Telegram's retention period; extended downtime can lose messages.
- Interrupted sends may duplicate notifications; confirmed stages keep their receipts.
- `app-telegram` is not approved for training by default. Review its source policy before publishing candidates.

Static checks are complete; live messaging and functional validation remain pending.
