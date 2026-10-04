---
title: 项目介绍
nav_order: 1
permalink: /
---

# 金融文档治理与训练数据


把金融文档转成可追溯的预训练与 SFT 数据：从来源采集、OCR 和图表治理，到模型处理、独立审核与语料发布。

![处理流程概览](assets/images/pipeline-overview.svg)

首次使用从[快速开始](quick-start.md)进入；左侧导航按数据处理、训练数据、运维和参考分组。

网站提供中文与英文版本，可在顶部切换。尚未翻译的页面会切换到英文首页；
英文版目前覆盖主要使用流程，完整参考教程仍可查阅中文版。

[返回项目首页](https://github.com/Chal1ce/FinFlow/blob/main/README.md)

本文档随仓库代码维护。所有命令均在项目根目录执行；真实连接与凭据填写在本机 `.env`，
来源目标位于 `config/collection.json`，飞轮配方与预算位于 `config/flywheel.json`。

## 从哪里开始

| 使用场景 | 推荐入口 | 接着阅读 |
| --- | --- | --- |
| 每日自动抓取、处理图表并生成预训练数据 | `workflow.flywheel_cli` | [每日数据飞轮](data-flywheel.md) |
| 飞书 / Telegram / Discord / Slack、PDF 上传、复核，或连接外部助手 | `integrations.cli` | [应用连接与 MCP](app-integrations.md) |
| 手工提供一份财报 PDF | `workflow.cli` 的 `local_financial` | [运维手册](local_operations.md#21-从本地-pdf-完整处理) |
| 已经采集好财报 | `workflow.cli` 的 `collected_financial` | [运维手册](local_operations.md#22-采集后接管财报) |
| 已经完成 OCR | `workflow.cli` 的 `govern_and_publish` | [运维手册](local_operations.md#23-治理已有-ocr-输出) |
| 只想发现来源或下载 PDF | 财报 / 学术采集器、下载器 | [数据来源与采集](acquisition.md) |
| 从 v7 证据生成文本与多模态指令数据 | `training.sft_cli` | [文本 SFT](sft.md) · [图表多模态 SFT](sft-vision.md) |
| 扩展生成方法、控制比例和学习配比 | `training.mixture_cli` / `training.mixture_experiments` | [方法库](training-methods.md) · [数据配比](data-mixtures.md) · [DoReMi / RegMix](mixture-experiments.md) |
| 从已有 v5/v6 发布包生成文本语料 | `training.cli` | [已有发布包生成预训练语料](pretraining.md) |

## 推荐阅读顺序

首次使用每日飞轮：

1. [安装与模型配置](data-flywheel.md#1-环境和模型配置)：Python、OCR、视觉/审核模型与 tokenizer。
2. [来源与准入策略](data-flywheel.md#2-来源和准入策略)：采集目标、生成方法、训练使用范围及预算。
3. [日常操作](data-flywheel.md#3-日常操作)：执行一次、查看状态、复核、重试和血缘查询。
4. [发布与训练数据](data-flywheel.md#5-发布与训练数据)：数据增量、来源增量与累计快照。
5. [每日定时](data-flywheel.md#6-每日定时)：先完成少量真实运行和抽查，再安装系统调度。

## 按主题查阅

### 使用与配置

- [每日数据飞轮](data-flywheel.md)：完整自动流程的主要手册。
- [应用连接与 MCP](app-integrations.md)：四个消息入口、后台 worker 和 OpenClaw / Hermes 配置，左侧子页面按软件分开说明。
- [数据来源与采集](acquisition.md)：财报、论文与手工下载清单。
- [PaddleOCR 部署与连接](paddleocr_remote.md)：自建服务、隧道与云端客户端。
- [命令与参数参考](cli-reference.md)：独立入口的详细参数。
- [环境模板](https://github.com/Chal1ce/FinFlow/blob/main/.env.example) · [来源配置](https://github.com/Chal1ce/FinFlow/blob/main/config/collection.json) · [飞轮配方](https://github.com/Chal1ce/FinFlow/blob/main/config/flywheel.json)。


- [可选小模型精炼插件](text-refiner.md)：开关、模型选择、操作协议、审核与恢复。

### 处理与数据格式

- [财报处理与交付](financial-workflow.md)：传统流水线、运行目录、财报元数据和发布内容。
- [清洗、切片与 LLM 治理](governance.md)：规则、chunk 身份、表格增强与幂等处理。
- [已有发布包生成预训练语料](pretraining.md)：v5/v6 文本构建入口。
- [飞轮数据与数据库](data-flywheel.md#4-本地数据和数据库)：图表资产、任务台账和多父级血缘。

### 运维与设计

- [本地运行与运维](local_operations.md)：观察状态、校验交付、人工复核、备份与恢复。
- [运行日志与排错](logging.md)：日志格式、级别与轮转配置。
- [本机环境检查记录](environment-check.md)：实际配置检查截图、启动结果与缺项处理。
- [数据飞轮计划](data-flywheel-plan.md)：设计背景、实施范围与阶段划分。
- [编写文档与添加图片](docs-authoring.md)：图片路径、导航和页面写法。
- [GitHub Pages 发布说明](github-pages.md)：首次启用、日常更新和部署排错。

## 两类发布入口

传统财报流程输出 v5/v6 发布包，由 `training.cli` 生成模型无关的正文语料。
每日飞轮输出 v7 证据包，由飞轮构建器执行来源准入、候选审核、目标 tokenizer 分段和训练/验证划分。
处理 v7 数据时使用飞轮入口，具体文件格式见[发布与训练数据](data-flywheel.md#5-发布与训练数据)。

SFT 使用 `training.sft_cli` 从本机 v7 证据生成指令数据，也可作为每日可选阶段，见 [SFT 数据生成](sft.md)。
