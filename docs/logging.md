---
title: 日志说明
nav_order: 1
parent: 运维与排错
---

# 运行日志与排错

[文档首页](index.md) · [项目首页](https://github.com/Chal1ce/FinFlow/blob/main/README.md)

在项目根目录执行以下命令。每日飞轮启动脚本的重定向日志及任务状态查询见[飞轮运行说明](data-flywheel.md#3-日常操作)。

## 运行日志

所有命令行入口都会把运行事件写到终端的 `stderr`，并同时追加到
`logs/fin-doc-governance.log`。原有命令结果 JSON 保持在 `stdout`，所以日志不会破坏
管道、重定向或后续 JSON 解析。日志覆盖命令启动、工作流阶段、实体处理步骤、下载、交付校验、
发布、备份和异常；每条记录包含可用于筛选的 `batch_id`、`run_id`、步骤名或发布 ID。

默认单个日志文件最大为 5 MiB，达到上限后自动滚动为
`fin-doc-governance.log.1`、`.2` 等文件，保留最近 5 份历史。可在 `.env` 或命令行调整：

```powershell
# 观察完整的工作流与每个步骤；彩色仅影响终端，日志文件始终是无颜色文本
python -m workflow.cli --log-level DEBUG --log-color always run `
  --pipeline collected_financial --batch-id fin-2026-08-11 --ocr-backend local

# 需要被其他工具采集时，终端和日志文件都可以输出逐行 JSON
python -m workflow.cli --log-format json overview

# 为一次排障任务指定独立日志及较小的滚动上限
python -m workflow.cli --log-file logs/debug-session.log --log-max-bytes 1048576 `
  --log-backup-count 3 overview
```

可配置项为 `FIN_DOC_LOG_LEVEL`（`DEBUG`、`INFO`、`WARNING`、`ERROR`）、
`FIN_DOC_LOG_FORMAT`（`pretty` 或 `json`）、`FIN_DOC_LOG_COLOR`（`auto`、`always`、`never`）、
`FIN_DOC_LOG_FILE`、`FIN_DOC_LOG_MAX_BYTES` 与 `FIN_DOC_LOG_BACKUP_COUNT`；完整模板见
[.env.example](https://github.com/Chal1ce/FinFlow/blob/main/.env.example)。
