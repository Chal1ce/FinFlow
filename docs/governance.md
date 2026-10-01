---
title: 清洗与治理
nav_order: 3
parent: 数据处理
---

# 清洗、切片与 LLM 治理
{: .no_toc }

## 本页目录
{: .no_toc }

1. TOC
{:toc}


[文档首页](index.md) · [项目首页](https://github.com/Chal1ce/FinFlow/blob/main/README.md)

本页对应独立 `processing.governance_runner`。其中 `mock` 可供本地练习；正式[每日飞轮](data-flywheel.md)不会回退到 mock。

OCR 产物进入治理层后，原始 PaddleOCR `result.json` 和 `output.md` 不会修改；派生产物统一放在 `data/processed`：

```text
data/
├── processed/
│   ├── governed/scholarly/{work_uid}/{asset_uid}/{backend}/
│   │   ├── governed.md
│   │   ├── governed.json
│   │   └── llm.json
│   ├── chunks/scholarly/{work_uid}/{asset_uid}.jsonl
│   └── chunks.jsonl
├── runs/*-governance.json
└── state/pipeline.db
```

执行一次治理（默认无 LLM Key 时使用确定性 mock，不影响结果可复现）：

```bash
python -m processing.governance_runner --config config/collection.json
```

只预览会处理哪些文档、不写产物：

```bash
python -m processing.governance_runner --config config/collection.json --dry-run
```

限制本轮最多处理 2 份文档：

```bash
python -m processing.governance_runner \
  --config config/collection.json \
  --max-docs 2
```

清洗规则按 `result.json` 的 block 标签或云端 Markdown 的标题/HTML 表格确定性执行：去掉 `header/footer/number/image/chart` 噪声，HTML 表格转 Markdown，Unicode 归一化、OCR 断词修复、重复块去重，并识别 `参考文献`、`参考资料`、`References`、`Bibliography`（含常见编号和 Markdown 标题）后删除该尾部章节。原始 PDF、OCR 分段结果和 OCR `result.json` 不会修改；`governed.json.stats` 会记录是否识别到参考文献、删除块数和字符数。默认规则版本为 `ocr-cleaning-v3`；规则版本 `FIN_DOC_RULE_VERSION` 会参与 `governed_uid` 计算，完成标记也会校验该身份，因此旧规则结果不会被误跳过，原始 OCR 层可随时重跑。

切片不是按字数硬切：标题作为上下文，表格独立成块，公式尽量留在所属段落，超长段落按句子边界拆分。每个 chunk 记录 `chunk_id`、`chunk_version_uid`、兼容字段 `chunk_uid`、`document_uid`、页码、`bbox`、`content_type`、`title_context`、字符偏移和 `text_hash`。同一逻辑文档和相同规范化内容可以跨治理版本复用 `chunk_id`，具体产物使用 `chunk_version_uid` 区分。

LLM 治理层可插拔：

```bash
# 显式使用 mock，不联网
python -m processing.governance_runner --config config/collection.json --llm-backend mock

# 配置好 LLM_API_URL/LLM_API_KEY/LLM_MODEL 后使用 OpenAI Python SDK
python -m processing.governance_runner --config config/collection.json --llm-backend openai

# 完全跳过 LLM
python -m processing.governance_runner --config config/collection.json --llm-backend none
```

LLM 当前负责含表格文档的元数据补全/摘要/主题/质量评分，以及对表格和 chunk 上下文做语义增强；清洗、切片和基础 QC 都不依赖 LLM。治理器会把本地 `result.json` 的表格块，以及官方云端 `pages/page_*.md` / `output.md` 内的 HTML 或 Markdown 表格，统一识别为独立 table chunk。一次治理运行内，表格摘要先生成，再提供给文档级总结和 chunk 上下文补全使用。没有表格的文档会完全跳过 LLM，不发送任何模型请求，只保留规则治理结果和确定性的标题上下文。`.env` 中的 `FIN_DOC_DROP_BLOCK_LABELS`、`FIN_DOC_CHUNK_MAX_CHARS`、`FIN_DOC_CHUNK_MIN_CHARS` 可以调整规则。

LLM 增强不会覆盖 chunk 的原始规范化 `text`。表格 chunk 会新增 `table` 字段保存表格自然语言摘要；`table_source` 和 `table_status` 记录摘要来源。显式选择 `openai`（或 `auto` 且配置了 Key）时，任一 LLM 请求、响应格式或内容校验失败都会使当前文档和本次治理运行标记为失败，绝不降级为本地兜底结果。错误会写入终端日志、`data/runs/*-governance.json`、SQLite 的 `pipeline_step.error_msg`，并使命令返回非零退出码；失败文档不会写入新的 `.complete` 标记。`mock` 和 `none` 仍是显式的本地确定性模式。

每个 LLM 派生 chunk 还记录 `llm_enrichment_uid`、`llm_model_version` 和 `llm_prompt_version`。`chunk_id`、`chunk_version_uid` 仍由原始治理内容决定，不会因摘要文本变化而改变；LLM 结果独立登记在 `llm_governance` 和 `artifact` 中。

治理结果写入 SQLite 的 `document_registry`、`governed_document`、`document_chunk`、`artifact`、`llm_governance` 表，QC 结果继续写入 `qc_result`。重复执行是幂等的：已写入完成标记 `.complete` 的文档会跳过，`--force` 可强制重建；中途失败不会留下完成标记，下一次运行会继续处理；同一次运行内先写临时文件再原子替换，避免留下半个 JSONL。artifact 记录本地文件的类型、路径、Hash、父 artifact、批次和运行，便于从 chunk 逐级追溯到 OCR 和原始 PDF。
