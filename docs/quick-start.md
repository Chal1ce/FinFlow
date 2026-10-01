---
title: 快速开始
nav_order: 2
---

# 快速开始

需要 Python 3.10 或以上。以下命令在项目根目录执行。

## 1. 安装

macOS / Linux：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
cp -n .env.example .env
```

已有 `.env` 时只补充字段。Windows 安装方式见[运行前检查](local_operations.md#1-运行前检查)。

## 2. 选择入口

| 输入或目标 | 入口 |
| --- | --- |
| 每日采集、处理图表并生成预训练数据 | [每日数据飞轮](data-flywheel.md) |
| 手工提供财报 PDF / 已有财报 OCR | [财报处理与交付](financial-workflow.md) |
| 单独发现来源或下载 PDF | [数据来源与采集](acquisition.md) |

## 3. 配置并运行飞轮

按[模型配置说明](data-flywheel.md#1-环境和模型配置)填写 OCR、视觉模型、独立评审和目标 tokenizer。
再在 `config/flywheel.json` 中确认[来源训练准入](data-flywheel.md#2-来源和准入策略)。

```sh
# 本地配置预检，不请求网络服务
python -m workflow.flywheel_cli preflight

# 配置通过后执行一次
python -m workflow.flywheel_cli run

python -m workflow.flywheel_cli status
```

## 4. 自动运行

先完成少量真实运行，抽查图表与候选质量，再按[每日定时](data-flywheel.md#6-每日定时)安装 cron / launchd。

## 阅读文档

电脑端使用左侧分组导航，手机端使用顶部菜单。搜索框用于查找页面内容和命令；中文连续短语的命中效果受主题分词方式影响，也可用 `OCR`、`tokenizer` 或完整术语搜索。
