---
title: 环境配置排查
parent: 运维与排错
nav_order: 3
---

# 环境配置排查

本页说明飞轮启动前的配置检查与常见缺项处理。安装依赖和基础配置见[快速开始](quick-start.md)。

## 运行预检

在项目根目录激活 Python 环境后执行：

```sh
python -m workflow.flywheel_cli preflight
```

使用自定义配方时，添加与运行命令一致的全局 `--config` 参数：

```sh
python -m workflow.flywheel_cli --config config/flywheel.local.json preflight
```

预检检查本地依赖、启用角色的配置和 tokenizer 文件，不请求远端服务。通过预检后，服务鉴权、图像支持和输出质量仍需在运行中确认。

## 常见缺项

| 检查项 | 处理方法 |
| --- | --- |
| `.env` 不存在 | 从 `.env.example` 建立本机配置；已有文件时只补缺失字段 |
| Python 依赖缺失 | 激活项目虚拟环境并安装项目依赖 |
| OCR 配置缺失 | 按所选本地或云端后端配置 OCR 连接 |
| 文本治理模型配置缺失 | 补齐对应角色的 URL、密钥和模型名 |
| 视觉模型配置缺失 | 补齐视觉角色配置，确认服务支持图像输入 |
| 审核模型配置缺失 | 补齐独立审核角色配置；视觉候选审核需要图像支持 |
| tokenizer 文件不存在 | 指向目标模型对应的 tokenizer 文件 |
| API URL 格式无效 | 检查地址是否为空、协议与端点是否符合服务要求 |

空地址可能同时触发缺失和 URL 格式检查。配置字段及角色说明见[每日数据飞轮](data-flywheel.md)。

## 补齐配置

已有 `.env` 时不要覆盖。尚未建立时可执行：

```sh
cp -n .env.example .env
```

视觉、审核和 tokenizer 的配置字段包括：

```dotenv
FIN_DOC_VISION_API_URL=
FIN_DOC_VISION_API_KEY=
FIN_DOC_VISION_MODEL=
FIN_DOC_PRETRAIN_REVIEW_API_URL=
FIN_DOC_PRETRAIN_REVIEW_API_KEY=
FIN_DOC_PRETRAIN_REVIEW_MODEL=
FIN_DOC_PRETRAIN_TOKENIZER=/absolute/path/to/target-model/tokenizer.json
```

密钥仅保存在本机配置，不提交到仓库。tokenizer 应与目标模型对应。

补齐后重新执行预检，再按[每日数据飞轮](data-flywheel.md)运行。通过配方中的 `source_policy` 设置各来源的训练准入。
