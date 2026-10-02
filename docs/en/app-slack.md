---
title: Slack
parent: App integrations
nav_order: 3
---

# Slack

Slack uses the official Python SDK's Socket Mode without a public webhook endpoint.
Commands are ordinary messages such as `!status`. See [App integrations](app-integrations.md)
for queues, PDF provenance, review, and recovery.

## App and permissions

1. Create a Slack App, enable Socket Mode, and generate an App-Level Token with `connections:write`.
2. Install it to your workspace and obtain a Bot User OAuth Token. Enable the App Home Messages Tab and allow messages.
3. Subscribe to `message.im` for DMs, `message.channels` for public channels, and/or `message.groups` for private channels. FinFlow requires allowlisted channels and a bot mention. It uses full message events rather than `app_mention`.
4. Configure bot scopes `chat:write`, `im:history`, `files:read`, and `files:write`. Public/private channels additionally need `channels:history` / `groups:history`. Reinstall after scope changes and invite the bot to target channels.

References: [Socket Mode](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/),
[SDK file uploads](https://docs.slack.dev/tools/python-slack-sdk/web/#uploading-files).

Local `.env`:

```dotenv
FINFLOW_SLACK_BOT_TOKEN=xoxb-your-bot-token
FINFLOW_SLACK_APP_TOKEN=xapp-your-app-level-token
```

Set the local configuration's `slack` section:

```json
{
  "enabled": true,
  "team_id": "T_WORKSPACE_ID",
  "app_id": "A_APPLICATION_ID",
  "bot_user_id": "U_BOT_USER_ID",
  "users": {
    "U_YOUR_USER_ID": ["viewer", "operator", "reviewer"]
  },
  "allowed_group_chats": ["C_ALLOWED_CHANNEL_ID"],
  "notification_chats": []
}
```

Find the app ID under Basic Information and verify workspace/bot user IDs with `auth.test`.
Copy your member ID from your profile and channel IDs from channel details; display names are not IDs.
`bot_user_id` is the `U...` user identity, not a `B...` bot ID. Inbound events verify workspace,
application, and user identity; delivery verifies the token's bot identity.

## Start

Copy the shared template on first setup, then run from the repository root:

```sh
. .venv/bin/activate
python -m pip install -e '.[slack]'
python -m integrations.cli --config config/integrations.local.json preflight --channel slack
python -m integrations.cli --config config/integrations.local.json slack
```

This starts Socket Mode and a worker. Restart after configuration/environment changes.
All concurrent channel processes must share the same complete settings and data directory.

## Commands and files

In a DM:

```text
!status
!report
!audit
!candidate CANDIDATE_ID
!run --no-discover
!review TICKET rejected The currency disagrees with the image
```

Use an actual Slack bot mention in channels, such as `@FinFlow !status`. Attach one PDF per
message, still mentioning the bot in channels. Replies preserve existing Slack threads;
scheduled reports go to the configured channel's main conversation.
Original images and complete candidate JSON are uploaded as files. Human review uses ticket-bound
`!review` commands; Slack interactive buttons are not implemented.

## Runtime limits

- This is an installed bot for one workspace, without a multi-tenant OAuth installation flow.
- New messages/file shares are handled; edits, deletions, and other bots are ignored. Persisted events are acknowledged afterward. Duplicate events share a channel/message-timestamp key.
- Files must be Slack-hosted and readable by the bot. External files, inaccessible cross-workspace files, and downloads redirecting outside allowed hosts are not imported automatically.
- Local and workspace file limits both apply; uploads use `files_upload_v2`.
- Restart all relevant processes after changing credentials, users, or channels. Workers recheck local roles and conversation rules.
- Receipts protect completed stages, but an interrupted upload before checkpointing can still duplicate attachments.
- `app-slack` is unapproved for training by default and follows the shared review/publication policy.

Static checks are complete; live workspace operation and functional validation remain pending.
