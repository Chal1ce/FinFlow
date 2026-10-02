---
title: Discord
parent: 应用连接
nav_order: 2
---

# Discord 接入

Discord 入口通过 Gateway 接收消息，使用 Bot REST API 发送回复和证据文件。
它采用 `!status` 这样的普通消息命令，无需注册 Discord 原生 slash commands。
通用行为见[应用连接](app-integrations.md)。

## 配置应用

1. 在 Discord Developer Portal 创建应用和 Bot，把 token 填入 `.env` 的 `FINFLOW_DISCORD_BOT_TOKEN`。
2. 在 Bot 设置中启用 **Message Content Intent**；代码也会请求该 intent，以读取正文和附件。
3. 把机器人邀请到服务器，授予所需的 View Channel、Read Message History、Send Messages、Attach Files 权限。线程内使用时还需 Send Messages in Threads 和访问线程的权限。
4. 开启 Discord 客户端 Developer Mode，复制机器人 ID、自己的用户 ID 和允许使用的频道/线程 ID，全部以字符串填写。

在本机 `config/integrations.local.json` 的 `discord` 字段中填写：

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

`allowed_group_chats` 填频道 ID，在线程中操作时填**线程自身 ID**，不继承父频道白名单。
单聊仍检查用户白名单。自动日报填入 `notification_chats`；机器人必须能向目标发送消息。

参考：[discord.py intents](https://discordpy.readthedocs.io/en/stable/intents.html)、
[Discord 消息 API](https://docs.discord.com/developers/resources/message)。

## 启动与使用

首次复制通用配置模板后，在项目目录执行：

```sh
. .venv/bin/activate
python -m pip install -e '.[discord]'
python -m integrations.cli --config config/integrations.local.json preflight --channel discord
python -m integrations.cli --config config/integrations.local.json discord
```

启动时校验 bot token 与配置的 `bot_id`。一个进程同时运行接收器和后台 worker。

在单聊发送 `!help` 查看命令；频道/线程中需先 @ 机器人：

```text
@FinFlow !status
@FinFlow !report
@FinFlow !candidate 候选ID
@FinFlow !run --no-discover
@FinFlow !review TICKET accepted 已核对原图和来源
```

上面的 `@FinFlow` 必须在 Discord 中选择真正的机器人 mention，而不是手工填写同名纯文本。
同一条消息可附一份 PDF；频道内仍需 @。多个附件会提示拆开发送。
查询候选后会发送原图文件、完整 JSON 和带有效期的复核口令。

## 运行边界

- 只使用 Bot 账号；忽略机器人、webhook 发送的消息。
- 身份、用户角色和频道权限在入口校验，队列执行及发送时再次检查本机权限配置；修改配置后重启各进程。
- 文件下载前重新获取消息及附件信息，避免直接信任消息中的任意下载地址；附件仍受 FinFlow 和 Discord 双方大小限制。
- 输出禁用自动 mention，候选正文不会触发 `@everyone` 等批量提醒。
- Gateway 可自动重连，但冷启动不会主动补拉历史。停机期间或入库失败的命令需要重新发送；已进入本地队列的任务可恢复。
- 已发送阶段保存回执，近期重复发送使用 Discord nonce 辅助去重；仍可能有重复通知。
- `app-discord` 的训练用途默认未批准。当前无原生交互按钮，用 `!review` 提交人工决定。

目前完成静态检查；真实机器人和完整功能联调尚待配置后执行。
