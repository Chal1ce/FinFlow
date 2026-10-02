---
title: Telegram
parent: 应用连接
nav_order: 1
---

# Telegram 接入

Telegram 入口使用 Bot API 长轮询，在本机或服务器常驻运行，无需公网回调地址。
支持状态/日报、PDF 导入、原始图像与候选文件、人工复核口令、任务运行与重试。
通用角色、来源准入、数据目录和恢复方式见[应用连接](app-integrations.md)。

## 配置

1. 通过 Telegram 官方 BotFather 创建机器人，把 token 填入本机 `.env` 的 `FINFLOW_TELEGRAM_BOT_TOKEN`。
2. 使用官方 Bot API 的 `getMe` 获取机器人数字 ID；向机器人发送消息后，通过 `getUpdates` 获取自己的用户 ID，以及需要启用的会话 ID。接口调试在本机进行，token 不写入 Git。
3. 首次复制 `config/integrations.json` 为 `config/integrations.local.json`，在其中补充或修改 `telegram` 字段。

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

这是 `telegram` 字段的内容，保留外层其他设置。**所有 ID 都写成字符串**，群 ID 的负号也要保留。
群聊 ID 必须加入 `allowed_group_chats`；自动日报目标另填 `notification_chats`。
用户须先与机器人开始会话，或将机器人加入目标群并授予相应权限。

官方 API 说明：[Telegram Bot API](https://core.telegram.org/bots/api)。

## 启动

Telegram 使用项目基础依赖，无需额外 SDK：

```sh
. .venv/bin/activate
python -m pip install -e .
python -m integrations.cli --config config/integrations.local.json preflight --channel telegram
python -m integrations.cli --config config/integrations.local.json telegram
```

接收器启动时核对 token 对应的机器人身份。如果该机器人已有 webhook，会拒绝启动；确认迁移后自行删除 webhook。
一个机器人只运行一个轮询接收器。命令同时启动 worker，无需为这个入口再开一份 worker。

## 使用

单聊示例：

```text
/status
/report
/audit
/candidate 候选ID
/run --no-discover
/review TICKET rejected 表格金额单位不一致
```

完整命令与飞书相同。群内使用 `/status@你的机器人用户名`，或在命令前 @ 机器人。
群内发送 PDF 时，在文件说明中 @ 机器人；不提供 mention 信息的群消息不执行。
支持保留 Telegram 话题 ID，将回复送回原话题。

候选原图以**文件**发送，保留原始字节；随后发送完整 JSON 和复核口令。
本入口不提供飞书式复核按钮。复核人身份、会话、候选版本和有效期仍由统一服务校验。

## 限制与恢复

- 本机附件默认上限 32 MiB；Telegram 云端 Bot API 的下载上限为 20 MB，实际还受平台限制。[官方文件限制](https://core.telegram.org/bots/api#file)
- 本版本固定使用 Telegram 官方云端地址，未接入自建 Bot API Server。较大 PDF 使用本机 `import-pdf` 导入。
- 仅处理新的普通消息，忽略机器人消息、匿名群发送者、编辑消息和频道广播。
- 消息和轮询位置在同一数据库事务中保存，重启后从已持久化位置继续；平台仍有消息保留期限，长期离线不能保证补全。
- 发送没有严格一次保证，网络中断可能出现重复通知；已确认的发送阶段保留回执。
- `app-telegram` 默认未批准训练用途，按通用来源策略确认后再启用生成与发布。

目前完成静态检查；真实账号收发与完整功能联调尚待配置后执行。
