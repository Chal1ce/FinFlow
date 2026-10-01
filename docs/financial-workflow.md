---
title: 财报处理与交付
nav_order: 4
parent: 数据处理
---

# 财报处理与交付
{: .no_toc }

## 本页目录
{: .no_toc }

1. TOC
{:toc}


[文档首页](index.md) · [项目首页](https://github.com/Chal1ce/FinFlow/blob/main/README.md)

本页说明 `workflow.cli` 的手工财报路径及 v5/v6 交付格式。这些路径的 OCR 告警不阻断本地交付；[每日飞轮](data-flywheel.md)的训练准入和审核规则另行执行。

## 本地工作流与交付包

默认本地工作流支持从用户提供的金融 PDF 开始，依次导入、OCR、财报元数据规范化、治理、发布前校验和本地交付。默认不需要 PostgreSQL、MinIO、Docker 或远程知识库：状态保存在 SQLite，发布包保存在本地磁盘。OCR 使用现有 PaddleOCR 本地 Serving 或官方云端 API。

```powershell
# 查看可用流水线
python -m workflow.cli list

# 从本地 PDF 开始执行完整流程。local 后端需配置 PADDLEOCR_LOCAL_API_URL。
python -m workflow.cli run --pipeline local_financial `
  --input .\samples\annual-report.pdf `
  --ocr-backend local `
  --batch-id fin-2026-08-11 `
  --stock-code 600519 --report-year 2023 --report-type annual `
  --company-name "贵州茅台" --announcement-date 2024-04-03 `
  --title "贵州茅台2023年年度报告" `
  --llm-backend mock

# 不写入文件、不请求 OCR 服务，只检查完整流程和参数是否可执行
python -m workflow.cli run --pipeline local_financial `
  --input .\samples\annual-report.pdf --ocr-backend local --dry-run

# 已有 OCR 产物时，跳过导入和 OCR，直接治理并发布
python -m workflow.cli run --pipeline govern_and_publish --batch-id fin-2026-08-11 --llm-backend mock

# 查看工作流状态；失败后从成功阶段之后继续
python -m workflow.cli status <workflow_run_id>
python -m workflow.cli resume <workflow_run_id>
```

### 运行产物总览

以下按 `local_financial` / `collected_financial` 的流水线阶段说明“跑完后写到了哪里”。
除特别说明外，`<work_uid>` 是逻辑文档 ID，`<asset_uid>` 是 PDF 内容 SHA-256 派生
资产 ID，`<backend>` 是 `local` 或 `cloud`。

| 阶段 | 输出目录 | 主要文件 |
| --- | --- | --- |
| 采集 `spiders.collector` | `data/discovery/`、`data/manifests/`、`data/raw_pdfs/{年份}/{股票代码}/`、`data/quarantine/` | `candidates.jsonl`；`reports.jsonl` 记录 URL、Hash、批次和来源；`<股票代码>_<年份>_<类型>_<hash>.pdf`；无效文件隔离 |
| 学术采集 `spiders.scholarly_collector` | `data/discovery/`、`data/manifests/`、`data/raw_pdfs/scholarly/`、`data/named_pdfs/scholarly/`、`data/parsed_md/scholarly/` | `scholarly_candidates.jsonl`；`scholarly_documents.jsonl`；原始 PDF、编号副本、可选 OCR 结果 |
| 导入/接管 `ingest` / `adopt_collected` | `data/manifests/`，手工导入另写 `data/raw_pdfs/imported/` | `local_documents.jsonl`；采集路径只引用不复制，手工 PDF 按 `<asset_uid>.pdf` 落盘；状态写入 SQLite |
| OCR `ocr` | `data/parsed_md/financial/{work_uid}/{asset_uid}/{backend}/` | `input.json`、`output.md`、`attempt.json`；自建后端另有紧凑合并的 `result.json`、`ocr_segments.json`、`segments/segment-*/result.json` 和 `images/`；云端另有 `job.json`、`result.jsonl`、`pages/page_*.md`、`images/markdown/`、`images/output/`；强制重跑时旧结果保留在 `{backend}_history/{attempt_uid}/` |
| OCR 质量 `ocr_quality` | `data/manifests/` | `ocr_quality.jsonl`；告警同时写入 SQLite `ocr_quality`，不阻断发布 |
| 元数据 `metadata` | `data/manifests/` | `financial_metadata.jsonl`、`financial_metadata_overrides.jsonl`、`financial_metadata_override_audit.jsonl`、`financial_report_versions.jsonl`、`financial_report_version_selection_audit.jsonl` |
| 治理 `govern` | `data/processed/governed/financial/{work_uid}/{asset_uid}/{backend}/`、`data/processed/chunks/` | `governed.md`、`governed.json`、`llm.json`、`.complete`；chunk 文件 `data/processed/chunks/financial/{work_uid}/{asset_uid}.jsonl` 和汇总 `data/processed/chunks.jsonl`；运行摘要 `data/runs/*-governance.json` |
| 校验 `validate` | 不生成新业务文件 | 检查治理产物、`.complete` 标记和 SQLite QC 记录，失败通过命令结果和日志给出原因 |
| 发布 `publish` | `data/published/{batch_id}/{workflow_run_id}/` | `manifest.json`、`checksums.sha256`、`metadata.jsonl`、`governed-documents.jsonl`、`financial_metadata.jsonl`、`ocr-quality.jsonl`、`metadata-qc-summary.json`、`ocr-quality-summary.json`、`metadata-override-audit.jsonl`、`financial-report-versions.jsonl`、`version-selection-audit.jsonl`、`chunks.jsonl`、`artifacts.jsonl`；同批次 `latest.json` 指向最新发布 |

所有阶段共用的运行状态和日志位于 `data/state/pipeline.db`、`data/runs/run-*.json` 和
`logs/fin-doc-governance.log`。采集和学术来源目录见[数据来源与采集](acquisition.md)，治理目录见[清洗与治理](governance.md)。

也可以单独调试前几个阶段：

```powershell
python -m workflow.cli ingest --input .\samples\annual-report.pdf --batch-id fin-2026-08-11
python -m workflow.cli ocr --batch-id fin-2026-08-11 --ocr-backend local
python -m workflow.cli ocr-quality --batch-id fin-2026-08-11
python -m workflow.cli metadata --batch-id fin-2026-08-11

# 列出 OCR 内容质量告警；该检查不阻断发布
python -m workflow.cli ocr-review --batch-id fin-2026-08-11

# 保留当前 OCR 输出后，仅重跑一个已复核的资产，并刷新质量检查
python -m workflow.cli ocr-retry --batch-id fin-2026-08-11 `
  --asset-id <asset_uid> `
  --ocr-backend local `
  --reason "OCR 页面不完整，人工复核后重跑"

# 列出需要人工复核的资产；输出中包含 asset_uid、缺失字段和告警
python -m workflow.cli metadata-review --batch-id fin-2026-08-11

# 使用复核过的值修正元数据；不重新 OCR，也不重新治理正文
python -m workflow.cli metadata-override --batch-id fin-2026-08-11 `
  --asset-id <asset_uid> `
  --set company_name="贵州茅台" `
  --set announcement_date=2024-04-03 `
  --reason "根据已签发年报封面复核"

# 查看同一逻辑报告的 PDF 版本；多个版本存在且仍为自动选择时会标记待复核
python -m workflow.cli version-review --batch-id fin-2026-08-11

# 选择本地有效版本；候选版本会保留，不会删除
python -m workflow.cli version-select --batch-id fin-2026-08-11 `
  --report-group-id <report_group_uid> `
  --asset-id <asset_uid> `
  --reason "人工比对后确认该文件为修订后的完整年报"
```

每次成功发布会生成 `data/published/<batch_id>/<workflow_run_id>/`，包含：

- `metadata.jsonl`：治理文档及其元数据；
- `governed-documents.jsonl`：完整、确定性的治理 Markdown 正文快照；这是继续预训练语料的主文本来源；
- `financial_metadata.jsonl`：按不可变 PDF 资产保存的股票代码、公司、报告期、报告类型、来源和版本等标准财报元数据；
- `ocr-quality.jsonl`：每个有效 PDF 资产的 OCR 文本、页数覆盖和异常字符质量检查结果；
- `metadata-qc-summary.json`：元数据缺失、格式或版本冲突的汇总提示；该文件明确标记为 `blocking: false`，不会阻止本地发布；
- `ocr-quality-summary.json`：OCR 质量告警汇总，同样标记为 `blocking: false`，不会阻止本地发布；
- `metadata-override-audit.jsonl`：本次交付资产的人工元数据更正记录，包括修改前后值、原因和时间；
- `financial-report-versions.jsonl`：本批次逻辑报告的全部 PDF 版本及当前有效版本状态；
- `version-selection-audit.jsonl`：人工选择有效 PDF 版本的前后状态、理由和时间；
- `chunks.jsonl`：可供检索或下游入库的内容块；
- `artifacts.jsonl`：OCR、治理和切块产物的血缘与哈希；
- `manifest.json` 与 `checksums.sha256`：交付说明与文件完整性校验。

同批次的 `latest.json` 原子更新为最新发布版本；旧版本不会被覆盖。发布前可单独校验，或对已治理批次手动发布：

```powershell
python -m workflow.cli validate --batch-id fin-2026-08-11
python -m workflow.cli publish --batch-id fin-2026-08-11 --release-id manual-review-1
```

## 元数据与重跑规则

`local_financial` 的导入记录保存在 `data/manifests/local_documents.jsonl`，规范化财报元数据保存在 `data/manifests/financial_metadata.jsonl`，OCR 质量记录保存在 `data/manifests/ocr_quality.jsonl`，人工覆盖的当前值与完整审计轨迹分别保存在 `data/manifests/financial_metadata_overrides.jsonl` 和 `data/manifests/financial_metadata_override_audit.jsonl`，版本状态与选择审计分别保存在 `data/manifests/financial_report_versions.jsonl` 和 `data/manifests/financial_report_version_selection_audit.jsonl`，原始文件按 hash 保存到 `data/raw_pdfs/imported/`，OCR 输出位于 `data/parsed_md/financial/`。元数据优先采用人工复核值，再使用导入时填写的字段、来源清单、文件名、标题和 OCR 首页标题补全；支持 `--company-name`、`--report-period`、`--announcement-date`、`--language` 和 `--document-variant` 等可选字段。OCR 质量检查会标记文本过短、页数覆盖不足、缺少结果文件和异常字符等告警，但不会阻断发布；使用 `ocr-retry` 重跑时，旧输出会保存到同级 `local_history/<attempt_uid>/`，并在导入清单中保留完整尝试记录。版本组按股票代码、报告年度、报告类型和语言生成；新组默认选择最早发现的文件，出现候选版本后可用 `version-select` 切换，交付包只包含当前有效版本，其他版本仍完整保留在本地。重复执行会复用相同内容的原始 PDF、OCR 输出和未变更的元数据；传入 `--force` 才会重新生成相应阶段。网页采集仍使用 `spiders.collector` / `spiders.scholarly_collector`；现有 OCR 产物可通过 `govern_and_publish` 流水线交付。
