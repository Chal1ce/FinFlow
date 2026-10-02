---
title: Slack
parent: 应用连接
nav_order: 3
---

# Slack 接入

Slack 入口使用官方 Python SDK 的 Socket Mode，无需公网请求地址。命令采用 `!status` 等普通消息形式。
共享队列、PDF 来源血缘、候选复核和恢复逻辑见[应用连接](app-integrations.md)。

## 应用与权限

1. 创建 Slack App，启用 Socket Mode，生成具备 `connections:write` 的 App-Level Token。
2. 安装应用到目标工作区，取得 Bot User OAuth Token。在 App Home 开放机器人 Messages Tab，允许用户发送消息。
3. 订阅 Bot Events：单聊用 `message.im`，公开频道用 `message.channels`，私有频道用 `message.groups`，按需要选择。代码只接受白名单频道中 @ 机器人的消息和附件；本入口使用完整消息事件，不依赖 `app_mention`。
4. Bot scopes 配置 `chat:write`、`im:history`、`files:read`、`files:write`；使用公开/私有频道时补充 `channels:history` / `groups:history`。修改权限后重新安装应用，把机器人加入目标频道。

参考：[Slack Socket Mode](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/)、
[文件上传 SDK](https://docs.slack.dev/tools/python-slack-sdk/web/#uploading-files)。

本机 `.env`：

```dotenv
FINFLOW_SLACK_BOT_TOKEN=xoxb-your-bot-token
FINFLOW_SLACK_APP_TOKEN=xapp-your-app-level-token
```

在 `config/integrations.local.json` 的 `slack` 字段中填写：

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

应用 ID 来自 Basic Information；工作区和机器人用户 ID 可用官方 `auth.test` 确认。
用户 ID 从个人资料的 Copy member ID 获取，频道 ID 从频道详情获取。不要用显示名代替 ID。
这里的 `bot_user_id` 为 `U...` 用户身份，并非 `B...` 的 bot ID。
接收器同时验证工作区、应用与用户，发送前核对 token 的机器人身份。

## 启动

首次先复制通用配置模板，在项目目录执行：

```sh
. .venv/bin/activate
python -m pip install -e '.[slack]'
python -m integrations.cli --config config/integrations.local.json preflight --channel slack
python -m integrations.cli --config config/integrations.local.json slack
```

该进程同时启动 Socket Mode 和 worker。修改配置或环境变量后重启。
同时运行其他渠道时，所有进程使用同一份完整配置与数据目录。

## 命令与附件

单聊：

```text
!status
!report
!audit
!candidate 候选ID
!run --no-discover
!review TICKET rejected 图中币种与描述不符
```

频道中使用 `@FinFlow !status`，并在 Slack 中选择真正的机器人 mention。
可发送一份 PDF 文件；频道附件消息也需 @。回复保留原 Slack 线程，自动日报默认发送到配置的频道主会话。
候选原图、完整 JSON 通过文件发送，人工决定使用带 ticket 的 `!review`，目前无 Slack 交互按钮。

## 运行边界

- 本版本面向一个工作区中的已安装机器人，不提供多租户 OAuth 安装入口。
- 接收新消息和文件分享，忽略编辑、删除、其他 bot 消息。事件先入库再确认；同一条消息的多个事件投递按频道和消息时间戳去重。
- 文件需机器人具备读取权限且由 Slack 托管。外链文件、尚未可访问的跨工作区文件，以及重定向至未允许域名的下载，不会自动导入。
- 文件大小受 FinFlow 设置和工作区限制共同约束；发送使用 `files_upload_v2`。
- 凭证、用户或频道权限变化后，重启全部相关进程；后台再次检查角色和会话授权。
- 阶段回执用于避免重复发送，上传成功但本地未保存回执的中断仍可能产生重复附件。
- `app-slack` 默认未批准训练用途，审核与发布继续执行通用来源策略。

目前完成静态检查；真实工作区收发和完整功能联调尚待配置后执行。
